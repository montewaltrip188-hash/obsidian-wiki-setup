from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "tools" / "verify_keyword_runtime.py"


class VerifyKeywordRuntimeTests(unittest.TestCase):
    def test_probe_executes_dependency_and_keyword_query_without_mutating_fixture(self):
        with tempfile.TemporaryDirectory(prefix="runtime-probe-") as temporary:
            root = Path(temporary)
            query_script = root / "query.py"
            runtime_root = root / "runtime"
            runtime_root.mkdir()
            (runtime_root / "immutable.txt").write_text("immutable\n", encoding="utf-8")
            query_script.write_text(
                """import json, os, sys
from pathlib import Path
root = Path(os.environ['KB_ROOT'])
print('RECEIPT_JSON:' + json.dumps({
    'action': 'search', 'status': 'degraded', 'degraded': True,
    'result_paths': ['wiki/concepts/offline-runtime-probe.md'],
    'answerability': 'candidate_supported',
}))
""",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "--runtime-python",
                    sys.executable,
                    "--query-script",
                    query_script,
                    "--runtime-root",
                    runtime_root,
                    "--expected-python",
                    f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                    "--skip-locked-package-versions",
                ],
                cwd=ROOT,
                text=True,
                encoding="utf-8",
                capture_output=True,
                env={**os.environ, "PYTHONUTF8": "1"},
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            receipt = json.loads(completed.stdout)
            self.assertEqual("completed", receipt["status"])
            self.assertEqual("keyword_query", receipt["probe"])
            self.assertEqual(
                "wiki/concepts/offline-runtime-probe.md", receipt["result_path"]
            )
            self.assertTrue(receipt["runtime_tree_unchanged"])


if __name__ == "__main__":
    unittest.main()

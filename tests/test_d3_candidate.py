from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "release" / "d3_candidate.py"
sys.path.insert(0, str(ROOT / "release"))
from d3_candidate import acceptance_temp_parent  # noqa: E402


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def run_cli(*args: object, expected: int = 0) -> dict:
    completed = subprocess.run(
        [sys.executable, str(CLI), *map(str, args)],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        env={**os.environ, "PYTHONUTF8": "1"},
        check=False,
    )
    if completed.returncode != expected:
        raise AssertionError(completed.stderr or completed.stdout)
    return json.loads(completed.stdout if expected == 0 else completed.stderr)


def write_fixture(
    root: Path, target: str = "macos-arm64", release_state: str = "unreleased_candidate"
) -> tuple[Path, Path]:
    platform_name = "windows" if target == "windows-x64" else "macos"
    interpreter = (
        "python/python.exe" if target == "windows-x64" else "python/bin/python3"
    )
    candidate_id = "a" * 64
    bundle = {
        "bundle_version": "2.1.0",
        "candidate_id": candidate_id,
        "components": {
            "installer": {"commit": "1" * 40},
            "product": {"commit": "2" * 40},
            "wiki_skills": {"commit": "3" * 40},
        },
        "manifest_format": 1,
        "release_state": release_state,
    }
    descriptor = {
        "interpreter": interpreter,
        "runtime_id": "cpython-3.12.14+20260825",
        "target": target,
    }
    manifest = {
        "candidate_id": candidate_id,
        "platform": platform_name,
        "runtime": {
            "runtime_id": descriptor["runtime_id"],
            "targets": {
                target: {
                    "interpreter": descriptor["interpreter"],
                    "target_root": f"runtime/targets/{target}",
                }
            },
        },
    }
    candidate = root / f"candidate-{platform_name}.zip"
    with zipfile.ZipFile(candidate, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("bundle-manifest.json", json.dumps(bundle))
        archive.writestr(
            f"runtime/targets/{target}/.runtime-target.json",
            json.dumps(descriptor),
        )
        archive.writestr(
            f"runtime/targets/{target}/{interpreter}", b"fixture"
        )
    candidate_sha256 = hashlib.sha256(candidate.read_bytes()).hexdigest()
    other_platform = "macos" if platform_name == "windows" else "windows"
    payload = {
        "bundle_version": "2.1.0",
        "candidates": {
            platform_name: {
                "candidate_id": candidate_id,
                "candidate_zip_sha256": candidate_sha256,
                "candidate_zip_size": candidate.stat().st_size,
                "platform": platform_name,
                "reproducible": True,
            },
            other_platform: {
                "candidate_id": "b" * 64,
                "candidate_zip_sha256": "c" * 64,
                "candidate_zip_size": 1,
                "platform": other_platform,
                "reproducible": True,
            },
        },
        "next_action": "run_approval_required",
        "orchestrator_format": 1,
        "release_gates": {
            "keyword_runtime": {
                "automatic_network_install": False,
                "offline_baseline": "keyword",
                "runtime_id": descriptor["runtime_id"],
                "status": "ready",
                "targets": ["windows-x64", "macos-x64", "macos-arm64"],
            }
        },
        "release_state": release_state,
        "sources": {
            "installer": {"commit": "1" * 40},
            "product": {"commit": "2" * 40},
            "skill": {"commit": "3" * 40},
        },
        "status": "planned",
    }
    payload["plan_id"] = digest(payload)
    payload["receipt_sha256"] = digest(payload)
    release_plan = root / "release-plan.json"
    release_plan.write_text(json.dumps(payload), encoding="utf-8")
    return release_plan, candidate


class D3CandidateTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows 短路径临时根合同")
    def test_windows_acceptance_uses_drive_root_for_path_budget(self):
        parent = acceptance_temp_parent()
        self.assertIsNotNone(parent)
        self.assertEqual(parent, Path(parent.drive + "\\"))

    def test_macos_ci_uses_two_architectures_pinned_actions_and_attested_receipts(self):
        workflow = (
            ROOT / ".github" / "workflows" / "d3-macos-candidate.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("macos-15-intel", workflow)
        self.assertIn("macos-15", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("attestations: write", workflow)
        self.assertIn("release/d3_candidate.py run", workflow)
        self.assertIn("actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6", workflow)
        self.assertIn("bundle-path", workflow)
        self.assertIn("attestation.sigstore.json", workflow)
        self.assertIn("control/release-plan.json", workflow)
        self.assertIn("control/candidates/macos/first/candidate.zip", workflow)
        self.assertNotIn("control/d3-workspace/", workflow)
        self.assertIn("actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("private-key", workflow.casefold())

    def test_macos_preflight_binds_plan_candidate_target_and_bundle_version(self):
        with tempfile.TemporaryDirectory(prefix="d3-preflight-") as temporary:
            release_plan, candidate = write_fixture(Path(temporary))

            ready = run_cli(
                "preflight",
                "--release-plan", release_plan,
                "--candidate", candidate,
                "--target", "macos-arm64",
            )

            self.assertEqual("ready", ready["status"])
            self.assertEqual("2.1.0", ready["bundle_version"])
            self.assertEqual("macos-arm64", ready["target"])
            self.assertEqual("a" * 64, ready["candidate_id"])
            self.assertEqual(
                "cpython-3.12.14+20260825", ready["runtime_id"]
            )

    def test_windows_preflight_binds_windows_candidate_and_interpreter(self):
        with tempfile.TemporaryDirectory(prefix="d3-windows-preflight-") as temporary:
            release_plan, candidate = write_fixture(
                Path(temporary), target="windows-x64"
            )

            ready = run_cli(
                "preflight",
                "--release-plan", release_plan,
                "--candidate", candidate,
                "--target", "windows-x64",
            )

            self.assertEqual("ready", ready["status"])
            self.assertEqual("windows-x64", ready["target"])

    def test_stable_preflight_requires_plan_and_candidate_state_to_match(self):
        with tempfile.TemporaryDirectory(prefix="d3-stable-preflight-") as temporary:
            release_plan, candidate = write_fixture(
                Path(temporary), release_state="stable"
            )
            ready = run_cli(
                "preflight",
                "--release-plan", release_plan,
                "--candidate", candidate,
                "--target", "macos-arm64",
            )
            self.assertEqual("ready", ready["status"])

    @unittest.skipIf(sys.platform == "darwin", "仅验证非 macOS 主机拒绝路径")
    def test_macos_run_rejects_non_macos_host_before_extraction(self):
        with tempfile.TemporaryDirectory(prefix="d3-host-gate-") as temporary:
            root = Path(temporary)
            release_plan, candidate = write_fixture(root)
            output = root / "receipt.json"

            blocked = run_cli(
                "run",
                "--release-plan", release_plan,
                "--candidate", candidate,
                "--target", "macos-arm64",
                "--output", output,
                expected=2,
            )

            self.assertEqual("blocked", blocked["status"])
            self.assertEqual("MACOS_HOST_REQUIRED", blocked["error"])
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()

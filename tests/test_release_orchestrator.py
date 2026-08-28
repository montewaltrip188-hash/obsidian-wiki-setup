from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_install_candidate import installer_contract_files, make_repo
from tests.test_vault_update import inventory


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "release" / "orchestrator.py"


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


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_three_repositories(root: Path, *, runtime_ready: bool = False):
    skill_files = {
        "VERSION": "2.1.0\n",
        "core/design-juan-wiki/SKILL.md": "design\n",
        "core/wiki-hybrid-search/SKILL.md": "query\n",
        "core/ocr-and-documents/SKILL.md": "ocr\n",
        "COMPATIBILITY.json": json.dumps(
            {
                "contract_format": 1,
                "runtime_id": "claudecode-wiki-skills",
                "runtime_version": "2.1.0",
                "release_state": "unreleased_candidate",
                "supports": [
                    {
                        "product_id": "obsidian-llm-wiki-template",
                        "schema_range": ">=1.0.0 <2.0.0",
                    }
                ],
            }
        )
        + "\n",
    }
    skill, skill_commit, skill_tree = make_repo(root, "skill", skill_files)
    product_files = {
        "AGENTS.md": "agents\n",
        "CLAUDE.md": "product\n",
        "schema/daily-review-rules.md": "daily\n",
        "schema/domain-rules.md": "domain\n",
        "schema/lint-rules.md": "lint\n",
        "schema/runtime-contract.json": json.dumps(
            {
                "contract_format": 1,
                "product_id": "obsidian-llm-wiki-template",
                "schema_version": "1.0.0",
                "runtime": {
                    "id": "claudecode-wiki-skills",
                    "required_range": ">=2.1.0 <3.0.0",
                    "tested": {"version": "2.1.0", "commit": skill_commit},
                    "required_entries": [
                        "design-juan-wiki",
                        "wiki-hybrid-search",
                        "ocr-and-documents",
                    ],
                },
            }
        )
        + "\n",
        "schema/templates.md": "templates\n",
        "schema/update-policy.json": "{}\n",
    }
    product, product_commit, product_tree = make_repo(root, "product", product_files)
    installer_files = installer_contract_files()
    installer_files["tools/install_candidate.py"] = (
        ROOT / "tools" / "install_candidate.py"
    ).read_text(encoding="utf-8")
    release = json.loads(installer_files["release/bundle-release.json"])
    release["wiki_skills_version"] = "2.1.0"
    installer_files["release/bundle-release.json"] = json.dumps(release) + "\n"
    runtime_bom = json.loads(installer_files["contracts/offline-keyword-runtime-bom.json"])
    lifecycle_defaults = {
        "offline_baseline": "keyword",
        "keyword_runtime_ready": runtime_ready,
        "keyword_runtime_status": "ready" if runtime_ready else "blocked_missing_interpreter_and_locked_dependencies",
        "keyword_runtime_error": None if runtime_ready else "KEYWORD_RUNTIME_UNPROVISIONED",
    }
    if runtime_ready:
        lifecycle_defaults.update(
            {
                "keyword_runtime_id": runtime_bom["runtime_id"],
                "keyword_runtime_targets": list(runtime_bom["targets"]),
            }
        )
    installer_files["contracts/wiki-skill-lifecycle.json"] = json.dumps(
        {
            "schema_version": 2,
            "component": "claudecode-wiki-skills",
            "defaults": lifecycle_defaults,
            "dependency_policy": {
                "automatic_network_install": False,
                "client_package_install": "forbidden",
                "system_python_modification": "forbidden",
                "bom": "contracts/offline-keyword-runtime-bom.json",
                "ready_requires": [
                    "isolated_interpreter",
                    "locked_dependency_bundle",
                    "keyword_query_runtime_test",
                ],
            },
        }
    ) + "\n"
    installer, installer_commit, installer_tree = make_repo(
        root, "installer", installer_files
    )
    return {
        "product": (product, product_commit, product_tree),
        "skill": (skill, skill_commit, skill_tree),
        "installer": (installer, installer_commit, installer_tree),
    }


class ReleaseOrchestratorTests(unittest.TestCase):
    def test_ready_runtime_gate_advances_only_to_version_approval(self):
        with tempfile.TemporaryDirectory(prefix="d2-orchestrator-ready-") as temporary:
            root = Path(temporary)
            repositories = make_three_repositories(root, runtime_ready=True)
            planned = run_cli(
                "plan",
                "--product-repo", repositories["product"][0],
                "--product-ref", repositories["product"][1],
                "--skill-repo", repositories["skill"][0],
                "--skill-ref", repositories["skill"][1],
                "--installer-repo", repositories["installer"][0],
                "--installer-ref", repositories["installer"][1],
                "--workspace", root / "workspace",
            )
            gate = planned["release_gates"]["keyword_runtime"]
            self.assertEqual("version_approval_required", planned["next_action"])
            self.assertEqual("ready", gate["status"])
            self.assertEqual("fixture-python-3.12", gate["runtime_id"])
            self.assertEqual(
                ["windows-x64", "macos-x64", "macos-arm64"], gate["targets"]
            )

    def test_plan_freezes_fresh_clones_and_reproducible_two_platform_candidates(self):
        with tempfile.TemporaryDirectory(prefix="d2-orchestrator-") as temporary:
            root = Path(temporary)
            repositories = make_three_repositories(root)
            workspace = root / "workspace"
            source_before = {
                name: inventory(repository)
                for name, (repository, _commit, _tree) in repositories.items()
            }

            planned = run_cli(
                "plan",
                "--product-repo", repositories["product"][0],
                "--product-ref", repositories["product"][1],
                "--skill-repo", repositories["skill"][0],
                "--skill-ref", repositories["skill"][1],
                "--installer-repo", repositories["installer"][0],
                "--installer-ref", repositories["installer"][1],
                "--workspace", workspace,
            )

            self.assertEqual("planned", planned["status"])
            self.assertEqual("runtime_provisioning_required", planned["next_action"])
            self.assertEqual("blocked", planned["release_gates"]["keyword_runtime"]["status"])
            self.assertEqual(
                "KEYWORD_RUNTIME_UNPROVISIONED",
                planned["release_gates"]["keyword_runtime"]["error"],
            )
            self.assertRegex(planned["plan_id"], r"^[0-9a-f]{64}$")
            self.assertRegex(planned["receipt_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual({"windows", "macos"}, set(planned["candidates"]))
            for name, (_repository, commit, tree) in repositories.items():
                self.assertEqual(commit, planned["sources"][name]["commit"])
                self.assertEqual(tree, planned["sources"][name]["tree"])
            for platform, candidate in planned["candidates"].items():
                self.assertTrue(candidate["reproducible"])
                first = workspace / candidate["first_candidate_zip"]
                second = workspace / candidate["second_candidate_zip"]
                self.assertEqual(sha256(first), sha256(second), platform)
            for name, (repository, _commit, _tree) in repositories.items():
                self.assertEqual(source_before[name], inventory(repository))

            workspace_before = inventory(workspace)
            status = run_cli("status", "--workspace", workspace)
            self.assertEqual("planned", status["status"])
            self.assertEqual("runtime_provisioning_required", status["next_action"])
            self.assertEqual(workspace_before, inventory(workspace))

    def test_status_rejects_candidate_drift_without_rewriting_state(self):
        with tempfile.TemporaryDirectory(prefix="d2-orchestrator-drift-") as temporary:
            root = Path(temporary)
            repositories = make_three_repositories(root)
            workspace = root / "workspace"
            planned = run_cli(
                "plan",
                "--product-repo", repositories["product"][0],
                "--product-ref", repositories["product"][1],
                "--skill-repo", repositories["skill"][0],
                "--skill-ref", repositories["skill"][1],
                "--installer-repo", repositories["installer"][0],
                "--installer-ref", repositories["installer"][1],
                "--workspace", workspace,
            )
            candidate = workspace / planned["candidates"]["windows"]["first_candidate_zip"]
            candidate.write_bytes(candidate.read_bytes() + b"tamper")
            before = inventory(workspace)

            blocked = run_cli("status", "--workspace", workspace, expected=2)

            self.assertEqual("blocked", blocked["status"])
            self.assertEqual("CANDIDATE_ARTIFACT_DRIFT", blocked["error"])
            self.assertEqual(before, inventory(workspace))


if __name__ == "__main__":
    unittest.main()

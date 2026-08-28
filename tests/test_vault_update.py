from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "tools" / "vault_update.py"


def inventory(root: Path) -> dict[str, tuple[int, str]]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            data = path.read_bytes()
            result[relative] = (len(data), hashlib.sha256(data).hexdigest())
    return result


def write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def contract_files(
    root: Path,
    *,
    bundle_version: str | None = "2.1.0",
    compatibility_release_state: str | None = None,
) -> tuple[Path, Path, Path]:
    product_commit = "1" * 40
    skill_commit = "2" * 40
    product = write_json(
        root / "runtime-contract.json",
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
        },
    )
    compatibility = write_json(
        root / "COMPATIBILITY.json",
        {
            "contract_format": 1,
            "runtime_id": "claudecode-wiki-skills",
            "runtime_version": "2.1.0",
            **(
                {"release_state": compatibility_release_state}
                if compatibility_release_state is not None
                else {}
            ),
            "supports": [
                {
                    "product_id": "obsidian-llm-wiki-template",
                    "schema_range": ">=1.0.0 <2.0.0",
                }
            ],
        },
    )
    bundle = write_json(
        root / "bundle-manifest.json",
        {
            "manifest_format": 1,
            "release_state": "stable" if bundle_version else "unreleased_candidate",
            "bundle_version": bundle_version,
            "candidate_id": "3" * 64,
            "components": {
                "product": {
                    "repository": "montewaltrip188-hash/obsidian-llm-wiki-template",
                    "schema_version": "1.0.0",
                    "commit": product_commit,
                    "tree": "4" * 40,
                },
                "wiki_skills": {
                    "repository": "montewaltrip188-hash/claudecode-wiki-skills",
                    "version": "2.1.0",
                    "commit": skill_commit,
                    "tree": "5" * 40,
                },
                "installer": {"commit": "6" * 40, "tree": "7" * 40},
            },
        },
    )
    return product, compatibility, bundle


def write_state(vault: Path, *, bundle_version: str = "2.0.0") -> Path:
    return write_json(
        vault / ".juanyong-ai" / "product-state.json",
        {
            "schema_version": 1,
            "vault_id": "synthetic-vault-001",
            "product": {
                "repository": "montewaltrip188-hash/obsidian-llm-wiki-template",
                "base_commit": "8" * 40,
                "base_tree": "9" * 40,
                "baseline_sha256": "a" * 64,
            },
            "bundle": {"version": bundle_version, "candidate_id": "b" * 64},
            "skills": {"version": "2.1.0", "commit": "2" * 40},
            "managed_inventory_sha256": "c" * 64,
            "applied_migrations": [],
            "last_transaction": None,
        },
    )


def write_policy(root: Path) -> Path:
    return write_json(
        root / "update-policy.json",
        {
            "policy_format": 1,
            "default": {"ownership": "unmanaged", "action": "preserve"},
            "excluded_roots": ["raw/", "wiki/", "inbox/", "log/", "output/"],
            "rules": [
                {
                    "id": "product-rules",
                    "paths": ["CLAUDE.md", "AGENTS.md"],
                    "ownership": "product_merge",
                    "action": "three_way",
                }
            ],
        },
    )


def run_cli(*args: object, expected: int = 0) -> dict:
    completed = subprocess.run(
        [sys.executable, str(CLI), *map(str, args)],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    if completed.returncode != expected:
        raise AssertionError(completed.stderr or completed.stdout)
    stream = completed.stdout if expected == 0 else completed.stderr
    return json.loads(stream)


class VaultUpdateStatusTests(unittest.TestCase):
    def test_status_requires_legacy_adoption_without_writing_vault(self) -> None:
        with tempfile.TemporaryDirectory(prefix="u1-status-") as temporary:
            vault = Path(temporary) / "客户知识库"
            vault.mkdir()
            (vault / "CLAUDE.md").write_text("local rules\n", encoding="utf-8")
            before = inventory(vault)

            receipt = run_cli("status", "--vault", vault)

            self.assertEqual("legacy_adoption_required", receipt["status"])
            self.assertEqual("product_state_missing", receipt["reason"])
            self.assertEqual(before, inventory(vault))
            self.assertFalse((vault / ".juanyong-ai").exists())

    def test_status_blocks_an_incomplete_product_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="u1-state-") as temporary:
            vault = Path(temporary) / "vault"
            state_dir = vault / ".juanyong-ai"
            state_dir.mkdir(parents=True)
            (state_dir / "product-state.json").write_text(
                json.dumps({"schema_version": 1}), encoding="utf-8"
            )
            before = inventory(vault)

            receipt = run_cli("status", "--vault", vault, expected=2)

            self.assertEqual("blocked", receipt["status"])
            self.assertEqual("PRODUCT_STATE_INVALID", receipt["error"])
            self.assertEqual(before, inventory(vault))

    def test_status_reports_a_valid_managed_vault_without_writing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="u1-managed-") as temporary:
            vault = Path(temporary) / "vault"
            vault.mkdir()
            write_state(vault)
            before = inventory(vault)

            receipt = run_cli("status", "--vault", vault)

            self.assertEqual("managed", receipt["status"])
            self.assertEqual(
                "synthetic-vault-001", receipt["product_state"]["vault_id"]
            )
            self.assertEqual(before, inventory(vault))


class VaultUpdateCheckTests(unittest.TestCase):
    def test_check_requires_legacy_adoption_without_writing_vault(self) -> None:
        with tempfile.TemporaryDirectory(prefix="u1-check-") as temporary:
            root = Path(temporary)
            vault = root / "客户知识库"
            vault.mkdir()
            (vault / "raw").mkdir()
            (vault / "raw" / "客户资料.md").write_text(
                "private content\n", encoding="utf-8"
            )
            product, compatibility, bundle = contract_files(root / "contracts")
            before = inventory(vault)

            receipt = run_cli(
                "check",
                "--vault",
                vault,
                "--product-contract",
                product,
                "--skill-compatibility",
                compatibility,
                "--bundle-manifest",
                bundle,
            )

            self.assertEqual("legacy_adoption_required", receipt["status"])
            self.assertEqual("product_state_missing", receipt["reason"])
            self.assertEqual(before, inventory(vault))

    def test_check_distinguishes_current_and_upgrade_states(self) -> None:
        with tempfile.TemporaryDirectory(prefix="u1-check-managed-") as temporary:
            root = Path(temporary)
            vault = root / "vault"
            vault.mkdir()
            state_path = write_state(vault, bundle_version="2.1.0")
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["product"]["base_commit"] = "1" * 40
            state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            product, compatibility, bundle = contract_files(root / "contracts")
            before = inventory(vault)

            current = run_cli(
                "check",
                "--vault",
                vault,
                "--product-contract",
                product,
                "--skill-compatibility",
                compatibility,
                "--bundle-manifest",
                bundle,
            )
            self.assertEqual("up_to_date", current["status"])

            state["bundle"]["version"] = "2.0.0"
            state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            changed_before = inventory(vault)
            upgrade = run_cli(
                "check",
                "--vault",
                vault,
                "--product-contract",
                product,
                "--skill-compatibility",
                compatibility,
                "--bundle-manifest",
                bundle,
            )
            self.assertEqual("upgrade_available", upgrade["status"])
            self.assertEqual(changed_before, inventory(vault))
            self.assertNotEqual(before, changed_before)

    def test_check_blocks_an_unassigned_release_bundle_without_writing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="u1-unreleased-") as temporary:
            root = Path(temporary)
            vault = root / "vault"
            vault.mkdir()
            write_state(vault)
            product, compatibility, bundle = contract_files(
                root / "contracts", bundle_version=None
            )
            before = inventory(vault)

            receipt = run_cli(
                "check",
                "--vault",
                vault,
                "--product-contract",
                product,
                "--skill-compatibility",
                compatibility,
                "--bundle-manifest",
                bundle,
                expected=2,
            )

            self.assertEqual("blocked", receipt["status"])
            self.assertEqual("BUNDLE_VERSION_UNASSIGNED", receipt["error"])
            self.assertEqual(before, inventory(vault))

    def test_check_reports_an_installed_skill_outside_the_supported_range(self) -> None:
        with tempfile.TemporaryDirectory(prefix="u1-unsupported-") as temporary:
            root = Path(temporary)
            vault = root / "vault"
            vault.mkdir()
            state_path = write_state(vault)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["skills"]["version"] = "1.9.0"
            state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            product, compatibility, bundle = contract_files(root / "contracts")
            before = inventory(vault)

            receipt = run_cli(
                "check",
                "--vault",
                vault,
                "--product-contract",
                product,
                "--skill-compatibility",
                compatibility,
                "--bundle-manifest",
                bundle,
            )

            self.assertEqual("unsupported_old", receipt["status"])
            self.assertEqual("installed_skill_outside_product_range", receipt["reason"])
            self.assertEqual(before, inventory(vault))

    def test_check_blocks_a_stable_bundle_bound_to_an_unreleased_skill(self) -> None:
        with tempfile.TemporaryDirectory(prefix="u1-skill-state-") as temporary:
            root = Path(temporary)
            vault = root / "vault"
            vault.mkdir()
            write_state(vault)
            product, compatibility, bundle = contract_files(
                root / "contracts",
                compatibility_release_state="unreleased_candidate",
            )
            before = inventory(vault)

            receipt = run_cli(
                "check",
                "--vault",
                vault,
                "--product-contract",
                product,
                "--skill-compatibility",
                compatibility,
                "--bundle-manifest",
                bundle,
                expected=2,
            )

            self.assertEqual("blocked", receipt["status"])
            self.assertEqual("SKILL_RELEASE_UNSTABLE", receipt["error"])
            self.assertEqual(before, inventory(vault))


class VaultUpdatePlanTests(unittest.TestCase):
    def test_plan_proposes_product_update_without_writing_vault(self) -> None:
        with tempfile.TemporaryDirectory(prefix="u1-plan-") as temporary:
            root = Path(temporary)
            base = root / "base"
            local = root / "客户知识库"
            target = root / "target"
            for directory in (base, local, target):
                directory.mkdir()
            (base / "CLAUDE.md").write_text("old rule\n", encoding="utf-8")
            (local / "CLAUDE.md").write_text("old rule\n", encoding="utf-8")
            (target / "CLAUDE.md").write_text("new rule\n", encoding="utf-8")
            write_state(local)
            product, compatibility, bundle = contract_files(root / "contracts")
            policy = write_policy(root / "contracts")
            before = inventory(local)

            receipt = run_cli(
                "plan",
                "--vault",
                local,
                "--base-root",
                base,
                "--target-root",
                target,
                "--path-policy",
                policy,
                "--product-contract",
                product,
                "--skill-compatibility",
                compatibility,
                "--bundle-manifest",
                bundle,
            )

            self.assertEqual("approval_required", receipt["status"])
            self.assertRegex(receipt["plan_id"], r"^[0-9a-f]{64}$")
            self.assertEqual("3" * 64, receipt["bundle_candidate_id"])
            self.assertEqual(["CLAUDE.md"], receipt["scanned_paths"])
            self.assertEqual("update_candidate", receipt["changes"][0]["decision"])
            self.assertIn("-old rule", receipt["changes"][0]["target_diff"])
            self.assertIn("+new rule", receipt["changes"][0]["target_diff"])
            self.assertEqual(before, inventory(local))

    def test_plan_covers_the_approved_three_way_matrix(self) -> None:
        with tempfile.TemporaryDirectory(prefix="u1-matrix-") as temporary:
            root = Path(temporary)
            base = root / "base"
            local = root / "客户知识库"
            target = root / "target"
            for directory in (base, local, target):
                (directory / "schema").mkdir(parents=True)
            cases = {
                "01-update.md": ("base", "base", "target", "update_candidate"),
                "02-local.md": ("base", "local", "base", "preserve_local"),
                "03-converged.md": ("base", "same", "same", "no_op_converged"),
                "04-conflict.md": ("base", "local", "target", "conflict"),
                "05-add.md": (None, None, "target", "add_candidate"),
                "06-local-only.md": (None, "local", None, "preserve_local"),
                "07-delete.md": ("base", "base", None, "delete_candidate"),
                "08-delete-conflict.md": ("base", "local", None, "conflict_delete"),
            }
            for name, (base_value, local_value, target_value, _) in cases.items():
                for directory, value in (
                    (base, base_value),
                    (local, local_value),
                    (target, target_value),
                ):
                    if value is not None:
                        (directory / "schema" / name).write_text(
                            value + "\n", encoding="utf-8"
                        )
            (local / "raw").mkdir()
            (local / "raw" / "private.md").write_text(
                "must not be scanned\n", encoding="utf-8"
            )
            write_state(local)
            product, compatibility, bundle = contract_files(root / "contracts")
            policy = write_json(
                root / "contracts" / "update-policy.json",
                {
                    "policy_format": 1,
                    "default": {"ownership": "unmanaged", "action": "preserve"},
                    "excluded_roots": ["raw/", "wiki/", "inbox/", "log/", "output/"],
                    "rules": [
                        {
                            "id": "matrix",
                            "paths": [f"schema/{name}" for name in cases],
                            "ownership": "product_merge",
                            "action": "three_way",
                        }
                    ],
                },
            )
            before = inventory(local)

            receipt = run_cli(
                "plan",
                "--vault",
                local,
                "--base-root",
                base,
                "--target-root",
                target,
                "--path-policy",
                policy,
                "--product-contract",
                product,
                "--skill-compatibility",
                compatibility,
                "--bundle-manifest",
                bundle,
            )

            actual = {item["path"]: item["decision"] for item in receipt["changes"]}
            expected = {f"schema/{name}": values[3] for name, values in cases.items()}
            self.assertEqual(expected, actual)
            self.assertNotIn("raw/private.md", receipt["scanned_paths"])
            self.assertEqual(before, inventory(local))


if __name__ == "__main__":
    unittest.main()

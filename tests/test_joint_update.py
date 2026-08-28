from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_vault_transaction import base_bundle_from, run_cli as run_vault_cli
from tests.test_vault_update import contract_files, inventory, write_json, write_policy


ROOT = Path(__file__).resolve().parents[1]
JOINT_CLI = ROOT / "tools" / "joint_update.py"
SKILL_CLI = ROOT / "tools" / "manage_wiki_skills.py"
CORE = ("design-juan-wiki", "wiki-hybrid-search", "ocr-and-documents")


def run_json(script: Path, *args: object, expected: int = 0) -> dict:
    completed = subprocess.run(
        [sys.executable, str(script), *map(str, args)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1", "WIKI_PYTHON": sys.executable},
        check=False,
    )
    if completed.returncode != expected:
        raise AssertionError(completed.stderr or completed.stdout)
    return json.loads(completed.stdout if expected == 0 else completed.stderr)


def make_skill_source(root: Path, version: str, fingerprint: str) -> Path:
    root.mkdir(parents=True)
    (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    for name in CORE:
        skill = root / "core" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\n---\n", encoding="utf-8"
        )
    scripts = root / "core" / "wiki-hybrid-search" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "wiki_search.py").write_text(
        """import argparse, json, os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('command', choices=('index-status', 'index', 'search'))
parser.add_argument('query', nargs='?')
args = parser.parse_args()
root = Path(os.environ['KB_ROOT'])
fingerprint = %r
sentinel = root / '.state.db'
if args.command == 'index-status':
    current = sentinel.read_text(encoding='utf-8').strip() if sentinel.exists() else None
    receipt = {
        'action': 'index-status', 'status': 'completed', 'kb_root': str(root),
        'index_action': 'none' if current == fingerprint else 'full_rebuild',
        'reason': 'signature_current' if current == fingerprint else 'signature_mismatch',
        'stored_fingerprint': current, 'target_fingerprint': fingerprint,
        'stored_signatures': None, 'target_signatures': {'fixture': fingerprint},
    }
elif args.command == 'index':
    sentinel.write_text(fingerprint + '\\n', encoding='utf-8')
    receipt = {
        'action': 'index', 'status': 'completed', 'kb_root': str(root),
        'files_updated': 1, 'chunks': 1, 'skipped': 0, 'errors': 0,
        'cleaned': 0, 'no_op': False, 'faiss_repaired': False,
        'index_fingerprint': fingerprint,
    }
else:
    expected = 'wiki/concepts/联合事务.md'
    receipt = {
        'action': 'search', 'status': 'completed', 'kb_root': str(root),
        'query': args.query, 'vector_backend': 'fixture', 'returned_results': 1,
        'result_paths': [expected], 'degraded': False,
        'answerability': 'candidate_supported', 'query_term_coverage': 1.0,
    }
print('RECEIPT_JSON: ' + json.dumps(receipt, ensure_ascii=False, sort_keys=True))
"""
        % fingerprint,
        encoding="utf-8",
    )
    (scripts / "run-wiki-search.ps1").write_text(
        "$ErrorActionPreference='Stop'\n"
        "$python=$env:WIKI_PYTHON\n"
        "& $python (Join-Path $PSScriptRoot 'wiki_search.py') @args\n"
        "exit $LASTEXITCODE\n",
        encoding="utf-8",
    )
    return root


def make_runtime_source(root: Path, runtime_id: str) -> Path:
    files = {
        "python/python.exe": b"fixture isolated python\n",
        "python/Lib/site-packages/jieba/__init__.py": b"__version__ = '0.42.1'\n",
        "python/Lib/site-packages/numpy/__init__.py": b"__version__ = '2.5.2'\n",
        "python/Lib/site-packages/requests/__init__.py": b"__version__ = '2.34.2'\n",
    }
    records = []
    for relative, content in sorted(files.items()):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        records.append(
            {
                "mode": "100644",
                "path": relative,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        )
    material = b"".join(
        record["path"].encode("utf-8")
        + b"\0"
        + str(record["size"]).encode("ascii")
        + b"\0"
        + record["sha256"].encode("ascii")
        + b"\n"
        for record in records
    )
    descriptor = {
        "files": records,
        "interpreter": "python/python.exe",
        "runtime_id": runtime_id,
        "schema_version": 1,
        "site_packages": "python/Lib/site-packages",
        "target": "windows-x64",
        "tree_sha256": hashlib.sha256(material).hexdigest(),
    }
    (root / ".runtime-target.json").write_text(
        json.dumps(descriptor, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return root


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class JointUpdatePlanTests(unittest.TestCase):
    def test_joint_plan_binds_vault_skill_index_and_strict_query_without_writes(self):
        with tempfile.TemporaryDirectory(prefix="u3-joint-plan-") as temporary:
            root = Path(temporary)
            base = root / "base"
            target = root / "target"
            vault = root / "vault"
            cache = root / "cache"
            home = root / "home"
            for directory in (base, target, vault):
                directory.mkdir()
            for directory in (base, vault):
                (directory / "AGENTS.md").write_text("old rules\n", encoding="utf-8")
            (target / "AGENTS.md").write_text("new rules\n", encoding="utf-8")
            customer = vault / "wiki" / "concepts" / "联合事务.md"
            customer.parent.mkdir(parents=True)
            customer.write_text("# 联合事务\n\nU3-STRICT-QUERY。\n", encoding="utf-8")

            contracts = root / "contracts"
            product_contract, compatibility, target_bundle = contract_files(contracts)
            base_bundle = base_bundle_from(target_bundle, contracts / "base-bundle.json")
            policy = write_policy(contracts)
            run_vault_cli(
                "fresh-install",
                "--vault", vault,
                "--product-root", base,
                "--cache-root", cache,
                "--path-policy", policy,
                "--product-contract", product_contract,
                "--skill-compatibility", compatibility,
                "--bundle-manifest", base_bundle,
            )

            old_source = make_skill_source(root / "old-skill", "2.0.1", "1" * 64)
            target_source = make_skill_source(root / "target-skill", "2.1.0", "2" * 64)
            old_runtime = make_runtime_source(root / "old-runtime", "fixture-old")
            target_runtime = make_runtime_source(root / "target-runtime", "fixture-target")
            run_json(
                SKILL_CLI,
                "install", "--source", old_source, "--home", home,
                "--runtime-source", old_runtime,
                "--link-mode", "copy", "--allow-copy-fallback",
            )

            vault_before = inventory(vault)
            cache_before = inventory(cache)
            home_before = inventory(home)
            result = run_json(
                JOINT_CLI,
                "plan",
                "--vault", vault,
                "--cache-root", cache,
                "--target-root", target,
                "--path-policy", policy,
                "--product-contract", product_contract,
                "--skill-compatibility", compatibility,
                "--bundle-manifest", target_bundle,
                "--skill-source", target_source,
                "--runtime-source", target_runtime,
                "--home", home,
                "--query", "联合事务",
                "--expect-path", "wiki/concepts/联合事务.md",
                "--expect-content-sha256", sha256(customer),
            )

            self.assertEqual("approval_required", result["status"])
            self.assertEqual("upgrade", result["skill_plan"]["action"])
            self.assertEqual("approval_required", result["vault_plan"]["status"])
            self.assertEqual("full_rebuild", result["index_plan"]["index_action"])
            self.assertEqual("2" * 64, result["index_plan"]["target_fingerprint"])
            self.assertEqual("联合事务", result["strict_query"]["query"])
            self.assertEqual("wiki/concepts/联合事务.md", result["strict_query"]["expect_path"])
            self.assertRegex(result["plan_id"], r"^[0-9a-f]{64}$")
            self.assertEqual(vault_before, inventory(vault))
            self.assertEqual(cache_before, inventory(cache))
            self.assertEqual(home_before, inventory(home))

            plan_path = write_json(root / "joint-plan.json", result)
            approval_path = write_json(
                root / "joint-approval.json",
                {
                    "approval_format": 1,
                    "allow_deletes": False,
                    "allow_index_rebuild": False,
                    "approve_skill_change": False,
                    "approved_at": "2026-08-28T00:00:00Z",
                    "approved_changes": [
                        {"change_sha256": item["change_sha256"], "path": item["path"]}
                        for item in result["vault_plan"]["changes"]
                        if item["requires_approval"]
                    ],
                    "plan_id": result["plan_id"],
                    "subject": "synthetic-unapproved-plan",
                },
            )
            blocked = run_json(
                JOINT_CLI,
                "apply",
                "--vault", vault,
                "--cache-root", cache,
                "--target-root", target,
                "--path-policy", policy,
                "--product-contract", product_contract,
                "--skill-compatibility", compatibility,
                "--bundle-manifest", target_bundle,
                "--skill-source", target_source,
                "--runtime-source", target_runtime,
                "--home", home,
                "--query", "联合事务",
                "--expect-path", "wiki/concepts/联合事务.md",
                "--expect-content-sha256", sha256(customer),
                "--plan", plan_path,
                "--approval", approval_path,
                expected=2,
            )
            self.assertEqual("INDEX_REBUILD_NOT_APPROVED", blocked["error"])
            self.assertEqual(vault_before, inventory(vault))
            self.assertEqual(cache_before, inventory(cache))
            self.assertEqual(home_before, inventory(home))

    def test_joint_apply_verify_and_rollback_restore_vault_skill_and_index(self):
        with tempfile.TemporaryDirectory(prefix="u3-joint-transaction-") as temporary:
            root = Path(temporary)
            base = root / "base"
            target = root / "target"
            vault = root / "vault"
            cache = root / "cache"
            home = root / "home"
            artifacts = root / "artifacts"
            for directory in (base, target, vault, artifacts):
                directory.mkdir()
            for directory in (base, vault):
                (directory / "AGENTS.md").write_text("old rules\n", encoding="utf-8")
            (target / "AGENTS.md").write_text("new rules\n", encoding="utf-8")
            customer = vault / "wiki" / "concepts" / "联合事务.md"
            customer.parent.mkdir(parents=True)
            customer.write_text("# 联合事务\n\nU3-STRICT-QUERY。\n", encoding="utf-8")
            customer_before = sha256(customer)

            contracts = root / "contracts"
            product_contract, compatibility, target_bundle = contract_files(contracts)
            base_bundle = base_bundle_from(target_bundle, contracts / "base-bundle.json")
            policy = write_policy(contracts)
            run_vault_cli(
                "fresh-install",
                "--vault", vault,
                "--product-root", base,
                "--cache-root", cache,
                "--path-policy", policy,
                "--product-contract", product_contract,
                "--skill-compatibility", compatibility,
                "--bundle-manifest", base_bundle,
            )

            old_source = make_skill_source(root / "old-skill", "2.0.1", "1" * 64)
            target_source = make_skill_source(root / "target-skill", "2.1.0", "2" * 64)
            old_runtime = make_runtime_source(root / "old-runtime", "fixture-old")
            target_runtime = make_runtime_source(root / "target-runtime", "fixture-target")
            run_json(
                SKILL_CLI,
                "install", "--source", old_source, "--home", home,
                "--runtime-source", old_runtime,
                "--link-mode", "copy", "--allow-copy-fallback",
            )

            common = (
                "--vault", vault,
                "--cache-root", cache,
                "--target-root", target,
                "--path-policy", policy,
                "--product-contract", product_contract,
                "--skill-compatibility", compatibility,
                "--bundle-manifest", target_bundle,
                "--skill-source", target_source,
                "--runtime-source", target_runtime,
                "--home", home,
                "--query", "联合事务",
                "--expect-path", "wiki/concepts/联合事务.md",
                "--expect-content-sha256", customer_before,
            )
            plan = run_json(JOINT_CLI, "plan", *common)
            plan_path = write_json(artifacts / "joint-plan.json", plan)
            approval = {
                "approval_format": 1,
                "allow_deletes": False,
                "allow_index_rebuild": True,
                "approve_skill_change": True,
                "approved_at": "2026-08-28T00:00:00Z",
                "approved_changes": [
                    {"change_sha256": item["change_sha256"], "path": item["path"]}
                    for item in plan["vault_plan"]["changes"]
                    if item["requires_approval"]
                ],
                "plan_id": plan["plan_id"],
                "subject": "synthetic-joint-approval",
            }
            approval_path = write_json(artifacts / "joint-approval.json", approval)

            applied = run_json(
                JOINT_CLI,
                "apply",
                *common,
                "--plan", plan_path,
                "--approval", approval_path,
                "--skill-link-mode", "copy",
                "--allow-skill-copy-fallback",
            )

            self.assertEqual("completed", applied["status"])
            joint_receipt = Path(applied["receipt"])
            self.assertTrue(joint_receipt.is_file())
            self.assertEqual((target / "AGENTS.md").read_bytes(), (vault / "AGENTS.md").read_bytes())
            self.assertEqual("2" * 64, (vault / ".state.db").read_text(encoding="utf-8").strip())
            self.assertEqual(customer_before, sha256(customer))
            skill_state = json.loads(
                (home / ".agents" / "packages" / "claudecode-wiki-skills" / "state.json").read_text(encoding="utf-8")
            )
            self.assertEqual("2.1.0", skill_state["active_version"])

            tampered_payload = json.loads(joint_receipt.read_text(encoding="utf-8"))
            tampered_payload["plan_id"] = "0" * 64
            tampered_receipt = write_json(artifacts / "tampered-joint-receipt.json", tampered_payload)
            tampered = run_json(
                JOINT_CLI,
                "verify",
                "--vault", vault,
                "--home", home,
                "--receipt", tampered_receipt,
                expected=2,
            )
            self.assertEqual("JOINT_RECEIPT_TAMPERED", tampered["error"])

            verified = run_json(
                JOINT_CLI,
                "verify",
                "--vault", vault,
                "--home", home,
                "--receipt", joint_receipt,
            )
            self.assertEqual("verified", verified["status"])

            rolled_back = run_json(
                JOINT_CLI,
                "rollback",
                "--vault", vault,
                "--home", home,
                "--cache-root", cache,
                "--receipt", joint_receipt,
            )
            self.assertEqual("completed", rolled_back["status"])
            self.assertEqual((base / "AGENTS.md").read_bytes(), (vault / "AGENTS.md").read_bytes())
            self.assertFalse((vault / ".state.db").exists())
            self.assertEqual(customer_before, sha256(customer))
            restored_state = json.loads(
                (home / ".agents" / "packages" / "claudecode-wiki-skills" / "state.json").read_text(encoding="utf-8")
            )
            self.assertEqual("2.0.1", restored_state["active_version"])

    def test_strict_query_failure_rolls_back_all_components_and_emits_recovery_receipt(self):
        with tempfile.TemporaryDirectory(prefix="u3-joint-recovery-") as temporary:
            root = Path(temporary)
            base = root / "base"
            target = root / "target"
            vault = root / "vault"
            cache = root / "cache"
            home = root / "home"
            artifacts = root / "artifacts"
            for directory in (base, target, vault, artifacts):
                directory.mkdir()
            for directory in (base, vault):
                (directory / "AGENTS.md").write_text("old rules\n", encoding="utf-8")
            (target / "AGENTS.md").write_text("new rules\n", encoding="utf-8")
            expected_page = vault / "wiki" / "concepts" / "不会命中.md"
            expected_page.parent.mkdir(parents=True)
            expected_page.write_text("# 不会命中\n\nQUERY-ROLLBACK。\n", encoding="utf-8")
            customer_before = sha256(expected_page)

            contracts = root / "contracts"
            product_contract, compatibility, target_bundle = contract_files(contracts)
            base_bundle = base_bundle_from(target_bundle, contracts / "base-bundle.json")
            policy = write_policy(contracts)
            run_vault_cli(
                "fresh-install",
                "--vault", vault,
                "--product-root", base,
                "--cache-root", cache,
                "--path-policy", policy,
                "--product-contract", product_contract,
                "--skill-compatibility", compatibility,
                "--bundle-manifest", base_bundle,
            )
            old_source = make_skill_source(root / "old-skill", "2.0.1", "1" * 64)
            target_source = make_skill_source(root / "target-skill", "2.1.0", "2" * 64)
            old_runtime = make_runtime_source(root / "old-runtime", "fixture-old")
            target_runtime = make_runtime_source(root / "target-runtime", "fixture-target")
            run_json(
                SKILL_CLI,
                "install", "--source", old_source, "--home", home,
                "--runtime-source", old_runtime,
                "--link-mode", "copy", "--allow-copy-fallback",
            )
            common = (
                "--vault", vault,
                "--cache-root", cache,
                "--target-root", target,
                "--path-policy", policy,
                "--product-contract", product_contract,
                "--skill-compatibility", compatibility,
                "--bundle-manifest", target_bundle,
                "--skill-source", target_source,
                "--runtime-source", target_runtime,
                "--home", home,
                "--query", "不会命中",
                "--expect-path", "wiki/concepts/不会命中.md",
                "--expect-content-sha256", customer_before,
            )
            plan = run_json(JOINT_CLI, "plan", *common)
            plan_path = write_json(artifacts / "joint-plan.json", plan)
            approval_path = write_json(
                artifacts / "joint-approval.json",
                {
                    "approval_format": 1,
                    "allow_deletes": False,
                    "allow_index_rebuild": True,
                    "approve_skill_change": True,
                    "approved_at": "2026-08-28T00:00:00Z",
                    "approved_changes": [
                        {"change_sha256": item["change_sha256"], "path": item["path"]}
                        for item in plan["vault_plan"]["changes"]
                        if item["requires_approval"]
                    ],
                    "plan_id": plan["plan_id"],
                    "subject": "synthetic-recovery-approval",
                },
            )

            blocked = run_json(
                JOINT_CLI,
                "apply",
                *common,
                "--plan", plan_path,
                "--approval", approval_path,
                "--skill-link-mode", "copy",
                "--allow-skill-copy-fallback",
                expected=2,
            )

            self.assertTrue(blocked["error"].startswith("JOINT_APPLY_FAILED_ROLLED_BACK:"))
            self.assertEqual("rolled_back", blocked["recovery_status"])
            recovery_receipt = Path(blocked["recovery_receipt"])
            self.assertTrue(recovery_receipt.is_file())
            recovery = json.loads(recovery_receipt.read_text(encoding="utf-8"))
            self.assertEqual("apply_failure", recovery["operation"])
            self.assertEqual("rolled_back", recovery["recovery_status"])
            self.assertRegex(recovery["receipt_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual((base / "AGENTS.md").read_bytes(), (vault / "AGENTS.md").read_bytes())
            self.assertFalse((vault / ".state.db").exists())
            self.assertEqual(customer_before, sha256(expected_page))
            restored_state = json.loads(
                (home / ".agents" / "packages" / "claudecode-wiki-skills" / "state.json").read_text(encoding="utf-8")
            )
            self.assertEqual("2.0.1", restored_state["active_version"])


if __name__ == "__main__":
    unittest.main()

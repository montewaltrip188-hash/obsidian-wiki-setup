from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_vault_update import contract_files, inventory, write_json, write_policy


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "tools" / "vault_update.py"


def run_cli(
    *args: object, expected: int = 0, extra_env: dict[str, str] | None = None
) -> dict:
    completed = subprocess.run(
        [sys.executable, str(CLI), *map(str, args)],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        env={**os.environ, "PYTHONUTF8": "1", **(extra_env or {})},
    )
    if completed.returncode != expected:
        raise AssertionError(completed.stderr or completed.stdout)
    stream = completed.stdout if expected == 0 else completed.stderr
    return json.loads(stream)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def approval_for(plan: dict, *, allow_deletes: bool = False) -> dict:
    return {
        "approval_format": 1,
        "allow_deletes": allow_deletes,
        "approved_at": "2026-08-28T00:00:00Z",
        "approved_changes": [
            {"change_sha256": item["change_sha256"], "path": item["path"]}
            for item in plan["changes"]
            if item["requires_approval"]
        ],
        "plan_id": plan["plan_id"],
        "subject": "synthetic-test",
    }


def base_bundle_from(target_bundle: Path, output: Path) -> Path:
    value = json.loads(target_bundle.read_text(encoding="utf-8"))
    value["bundle_version"] = "2.0.0"
    value["candidate_id"] = "d" * 64
    value["components"]["product"]["commit"] = "8" * 40
    value["components"]["product"]["tree"] = "9" * 40
    return write_json(output, value)


class FreshInstallTransactionTests(unittest.TestCase):
    def test_fresh_install_blocks_product_drift_and_cache_inside_vault_before_state_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="u2-fresh-block-") as temporary:
            root = Path(temporary)
            product = root / "product"
            vault = root / "vault"
            product.mkdir()
            vault.mkdir()
            (product / "CLAUDE.md").write_text("product\n", encoding="utf-8")
            (vault / "CLAUDE.md").write_text("drift\n", encoding="utf-8")
            product_contract, compatibility, bundle = contract_files(root / "contracts")
            policy = write_policy(root / "contracts")
            common = (
                "--vault",
                vault,
                "--product-root",
                product,
                "--path-policy",
                policy,
                "--product-contract",
                product_contract,
                "--skill-compatibility",
                compatibility,
                "--bundle-manifest",
                bundle,
            )
            before = inventory(vault)
            blocked = run_cli(
                "fresh-install",
                *common,
                "--cache-root",
                root / "cache",
                expected=2,
            )
            self.assertEqual("FRESH_INSTALL_PRODUCT_DRIFT", blocked["error"])
            self.assertEqual(before, inventory(vault))
            self.assertFalse((vault / ".juanyong-ai").exists())

            (vault / "CLAUDE.md").write_text("product\n", encoding="utf-8")
            before = inventory(vault)
            blocked = run_cli(
                "fresh-install",
                *common,
                "--cache-root",
                vault / ".update-cache",
                expected=2,
            )
            self.assertEqual("CACHE_ROOT_INSIDE_VAULT", blocked["error"])
            self.assertEqual(before, inventory(vault))
            self.assertFalse((vault / ".juanyong-ai").exists())

    def test_fresh_install_writes_bound_state_and_external_base_without_touching_customer_content(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="u2-fresh-") as temporary:
            root = Path(temporary)
            product = root / "product"
            vault = root / "客户知识库"
            cache = root / "cache"
            for directory in (product, vault):
                directory.mkdir()
            (product / "CLAUDE.md").write_text("base rules\n", encoding="utf-8")
            (product / "AGENTS.md").write_text("base agents\n", encoding="utf-8")
            (vault / "CLAUDE.md").write_text("base rules\n", encoding="utf-8")
            (vault / "AGENTS.md").write_text("base agents\n", encoding="utf-8")
            (vault / "raw").mkdir()
            (vault / "raw" / "客户资料.md").write_text(
                "private customer content\n", encoding="utf-8"
            )
            product_contract, compatibility, bundle = contract_files(root / "contracts")
            policy = write_policy(root / "contracts")
            customer_before = sha256(vault / "raw" / "客户资料.md")

            receipt = run_cli(
                "fresh-install",
                "--vault",
                vault,
                "--product-root",
                product,
                "--cache-root",
                cache,
                "--path-policy",
                policy,
                "--product-contract",
                product_contract,
                "--skill-compatibility",
                compatibility,
                "--bundle-manifest",
                bundle,
            )

            self.assertEqual("completed", receipt["status"])
            state_path = vault / ".juanyong-ai" / "product-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertRegex(state["vault_id"], r"^[0-9a-f]{32}$")
            self.assertEqual("1" * 40, state["product"]["base_commit"])
            self.assertEqual("4" * 40, state["product"]["base_tree"])
            self.assertEqual("2.1.0", state["bundle"]["version"])
            self.assertEqual("2.1.0", state["skills"]["version"])
            baseline = cache / "baselines" / (("4" * 40) + ".zip")
            self.assertTrue(baseline.is_file())
            self.assertEqual(sha256(baseline), state["product"]["baseline_sha256"])
            self.assertEqual(customer_before, sha256(vault / "raw" / "客户资料.md"))
            self.assertFalse((vault / "raw" / ".juanyong-ai").exists())

            status = run_cli("status", "--vault", vault)
            self.assertEqual("managed", status["status"])
            blocked = run_cli(
                "fresh-install",
                "--vault",
                vault,
                "--product-root",
                product,
                "--cache-root",
                cache,
                "--path-policy",
                policy,
                "--product-contract",
                product_contract,
                "--skill-compatibility",
                compatibility,
                "--bundle-manifest",
                bundle,
                expected=2,
            )
            self.assertEqual("PRODUCT_STATE_ALREADY_EXISTS", blocked["error"])

    def test_plan_restores_the_verified_base_from_external_cache_read_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="u2-cache-plan-") as temporary:
            root = Path(temporary)
            product = root / "product"
            target = root / "target"
            vault = root / "客户知识库"
            cache = root / "cache"
            for directory in (product, target, vault):
                directory.mkdir()
            for directory, value in (
                (product, "base rules\n"),
                (vault, "base rules\n"),
                (target, "target rules\n"),
            ):
                (directory / "CLAUDE.md").write_text(value, encoding="utf-8")
                (directory / "AGENTS.md").write_text("agents\n", encoding="utf-8")
            product_contract, compatibility, bundle = contract_files(root / "contracts")
            policy = write_policy(root / "contracts")
            run_cli(
                "fresh-install",
                "--vault",
                vault,
                "--product-root",
                product,
                "--cache-root",
                cache,
                "--path-policy",
                policy,
                "--product-contract",
                product_contract,
                "--skill-compatibility",
                compatibility,
                "--bundle-manifest",
                bundle,
            )
            before = inventory(vault)

            plan = run_cli(
                "plan",
                "--vault",
                vault,
                "--cache-root",
                cache,
                "--target-root",
                target,
                "--path-policy",
                policy,
                "--product-contract",
                product_contract,
                "--skill-compatibility",
                compatibility,
                "--bundle-manifest",
                bundle,
            )

            decisions = {item["path"]: item["decision"] for item in plan["changes"]}
            self.assertEqual("update_candidate", decisions["CLAUDE.md"])
            self.assertEqual(before, inventory(vault))


class ApplyVerifyRollbackTests(unittest.TestCase):
    def test_approved_update_can_apply_verify_and_rollback_without_touching_customer_content(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="u2-lifecycle-") as temporary:
            root = Path(temporary)
            product = root / "base-product"
            target = root / "target-product"
            vault = root / "客户知识库"
            cache = root / "cache"
            for directory in (product, target, vault):
                directory.mkdir()
            for directory, value in (
                (product, "base rules\n"),
                (vault, "base rules\n"),
                (target, "target rules\n"),
            ):
                (directory / "CLAUDE.md").write_text(value, encoding="utf-8")
                (directory / "AGENTS.md").write_text("agents\n", encoding="utf-8")
            (vault / "raw").mkdir()
            customer = vault / "raw" / "客户资料.md"
            customer.write_text("private customer content\n", encoding="utf-8")
            customer_before = sha256(customer)
            product_contract, compatibility, target_bundle = contract_files(
                root / "contracts"
            )
            base_bundle = base_bundle_from(
                target_bundle, root / "contracts" / "base-bundle.json"
            )
            policy = write_policy(root / "contracts")
            run_cli(
                "fresh-install",
                "--vault",
                vault,
                "--product-root",
                product,
                "--cache-root",
                cache,
                "--path-policy",
                policy,
                "--product-contract",
                product_contract,
                "--skill-compatibility",
                compatibility,
                "--bundle-manifest",
                base_bundle,
            )
            plan = run_cli(
                "plan",
                "--vault",
                vault,
                "--cache-root",
                cache,
                "--target-root",
                target,
                "--path-policy",
                policy,
                "--product-contract",
                product_contract,
                "--skill-compatibility",
                compatibility,
                "--bundle-manifest",
                target_bundle,
            )
            plan_path = write_json(root / "plan.json", plan)
            approval_path = write_json(root / "approval.json", approval_for(plan))

            applied = run_cli(
                "apply",
                "--vault",
                vault,
                "--cache-root",
                cache,
                "--target-root",
                target,
                "--path-policy",
                policy,
                "--product-contract",
                product_contract,
                "--skill-compatibility",
                compatibility,
                "--bundle-manifest",
                target_bundle,
                "--plan",
                plan_path,
                "--approval",
                approval_path,
            )

            self.assertEqual("completed", applied["status"])
            self.assertEqual(
                "target rules\n", (vault / "CLAUDE.md").read_text(encoding="utf-8")
            )
            self.assertEqual(customer_before, sha256(customer))
            state = json.loads(
                (vault / ".juanyong-ai" / "product-state.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual("1" * 40, state["product"]["base_commit"])
            self.assertEqual("4" * 40, state["product"]["base_tree"])
            self.assertEqual(applied["transaction_id"], state["last_transaction"])

            verified = run_cli(
                "verify", "--vault", vault, "--receipt", applied["receipt"]
            )
            self.assertEqual("verified", verified["status"])

            rolled_back = run_cli(
                "rollback",
                "--vault",
                vault,
                "--cache-root",
                cache,
                "--receipt",
                applied["receipt"],
            )
            self.assertEqual("completed", rolled_back["status"])
            self.assertEqual(
                "base rules\n", (vault / "CLAUDE.md").read_text(encoding="utf-8")
            )
            self.assertEqual(customer_before, sha256(customer))
            state = json.loads(
                (vault / ".juanyong-ai" / "product-state.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual("8" * 40, state["product"]["base_commit"])
            self.assertEqual("9" * 40, state["product"]["base_tree"])

            verified_rollback = run_cli(
                "verify",
                "--vault",
                vault,
                "--receipt",
                rolled_back["receipt"],
            )
            self.assertEqual("verified", verified_rollback["status"])


class TransactionFaultMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="u2-fault-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.product = self.root / "base-product"
        self.target = self.root / "target-product"
        self.vault = self.root / "客户知识库"
        self.cache = self.root / "cache"
        for directory in (self.product, self.target, self.vault):
            directory.mkdir()
        for directory, value in (
            (self.product, "base rules\n"),
            (self.vault, "base rules\n"),
            (self.target, "target rules\n"),
        ):
            (directory / "CLAUDE.md").write_text(value, encoding="utf-8")
            (directory / "AGENTS.md").write_text("agents\n", encoding="utf-8")
        (self.vault / "raw").mkdir()
        (self.vault / "raw" / "客户资料.md").write_text(
            "private customer content\n", encoding="utf-8"
        )
        (
            self.product_contract,
            self.compatibility,
            self.target_bundle,
        ) = contract_files(self.root / "contracts")
        self.base_bundle = base_bundle_from(
            self.target_bundle, self.root / "contracts" / "base-bundle.json"
        )
        self.policy = write_policy(self.root / "contracts")
        run_cli(
            "fresh-install",
            "--vault",
            self.vault,
            "--product-root",
            self.product,
            "--cache-root",
            self.cache,
            "--path-policy",
            self.policy,
            "--product-contract",
            self.product_contract,
            "--skill-compatibility",
            self.compatibility,
            "--bundle-manifest",
            self.base_bundle,
        )
        self.refresh_plan()

    def refresh_plan(self, *, allow_deletes: bool = False) -> None:
        self.plan = run_cli(
            "plan",
            "--vault",
            self.vault,
            "--cache-root",
            self.cache,
            "--target-root",
            self.target,
            "--path-policy",
            self.policy,
            "--product-contract",
            self.product_contract,
            "--skill-compatibility",
            self.compatibility,
            "--bundle-manifest",
            self.target_bundle,
        )
        self.plan_path = write_json(self.root / "plan.json", self.plan)
        self.approval_path = write_json(
            self.root / "approval.json",
            approval_for(self.plan, allow_deletes=allow_deletes),
        )

    def apply(
        self, *, expected: int = 0, extra_env: dict[str, str] | None = None
    ) -> dict:
        return run_cli(
            "apply",
            "--vault",
            self.vault,
            "--cache-root",
            self.cache,
            "--target-root",
            self.target,
            "--path-policy",
            self.policy,
            "--product-contract",
            self.product_contract,
            "--skill-compatibility",
            self.compatibility,
            "--bundle-manifest",
            self.target_bundle,
            "--plan",
            self.plan_path,
            "--approval",
            self.approval_path,
            expected=expected,
            extra_env=extra_env,
        )

    def test_stale_plan_and_wrong_approval_fail_before_vault_writes(self) -> None:
        (self.vault / "CLAUDE.md").write_text("customer drift\n", encoding="utf-8")
        before = inventory(self.vault)
        blocked = self.apply(expected=2)
        self.assertEqual("PLAN_STALE", blocked["error"])
        self.assertEqual(before, inventory(self.vault))

        (self.vault / "CLAUDE.md").write_text("base rules\n", encoding="utf-8")
        self.refresh_plan()
        approval = approval_for(self.plan)
        approval["approved_changes"][0]["change_sha256"] = "0" * 64
        write_json(self.approval_path, approval)
        before = inventory(self.vault)
        blocked = self.apply(expected=2)
        self.assertEqual("APPROVAL_MISMATCH", blocked["error"])
        self.assertEqual(before, inventory(self.vault))

    def test_conflict_and_unapproved_delete_are_blocked(self) -> None:
        (self.vault / "CLAUDE.md").write_text("customer rules\n", encoding="utf-8")
        self.refresh_plan()
        before = inventory(self.vault)
        blocked = self.apply(expected=2)
        self.assertEqual("PLAN_HAS_CONFLICTS", blocked["error"])
        self.assertEqual(before, inventory(self.vault))

        (self.vault / "CLAUDE.md").write_text("base rules\n", encoding="utf-8")
        (self.target / "AGENTS.md").unlink()
        self.refresh_plan(allow_deletes=False)
        blocked = self.apply(expected=2)
        self.assertEqual("DELETE_NOT_APPROVED", blocked["error"])
        self.assertTrue((self.vault / "AGENTS.md").is_file())

    def test_approved_delete_can_be_rolled_back(self) -> None:
        (self.target / "AGENTS.md").unlink()
        self.refresh_plan(allow_deletes=True)
        applied = self.apply()
        self.assertFalse((self.vault / "AGENTS.md").exists())
        rolled_back = run_cli(
            "rollback",
            "--vault",
            self.vault,
            "--cache-root",
            self.cache,
            "--receipt",
            applied["receipt"],
        )
        self.assertEqual("completed", rolled_back["status"])
        self.assertEqual(
            "agents\n", (self.vault / "AGENTS.md").read_text(encoding="utf-8")
        )

    def test_injected_partial_write_is_automatically_rolled_back(self) -> None:
        (self.target / "AGENTS.md").write_text("target agents\n", encoding="utf-8")
        self.refresh_plan()
        before = inventory(self.vault)
        blocked = self.apply(
            expected=2,
            extra_env={
                "JUNYONG_AI_TEST_MODE": "1",
                "JUNYONG_AI_TEST_FAIL_AFTER_WRITE": "1",
            },
        )
        self.assertTrue(blocked["error"].startswith("APPLY_FAILED_ROLLED_BACK:"))
        self.assertEqual(before, inventory(self.vault))

    def test_failure_after_state_write_is_automatically_rolled_back(self) -> None:
        before = inventory(self.vault)
        blocked = self.apply(
            expected=2,
            extra_env={
                "JUNYONG_AI_TEST_MODE": "1",
                "JUNYONG_AI_TEST_FAIL_STAGE": "after_state",
            },
        )
        self.assertEqual(
            "APPLY_FAILED_ROLLED_BACK:TEST_INJECTED_STATE_FAILURE",
            blocked["error"],
        )
        self.assertEqual(before, inventory(self.vault))

    def test_lock_contention_blocks_without_vault_writes(self) -> None:
        state = json.loads(
            (self.vault / ".juanyong-ai" / "product-state.json").read_text(
                encoding="utf-8"
            )
        )
        lock = self.cache / "locks" / f"{state['vault_id']}.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("other-transaction", encoding="ascii")
        before = inventory(self.vault)
        blocked = self.apply(expected=2)
        self.assertIn("UPDATE_LOCK_BUSY", blocked["error"])
        self.assertEqual(before, inventory(self.vault))
        self.assertEqual("other-transaction", lock.read_text(encoding="ascii"))

    def test_legacy_vault_id_is_encoded_only_for_external_cache_paths(self) -> None:
        state_path = self.vault / ".juanyong-ai" / "product-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["vault_id"] = "../../legacy customer id"
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.refresh_plan()
        applied = self.apply()
        cache_key = (
            "sha256-" + hashlib.sha256(state["vault_id"].encode("utf-8")).hexdigest()
        )
        receipt = Path(applied["receipt"])
        self.assertTrue(
            (self.cache / "backups" / cache_key).samefile(receipt.parents[1])
        )
        self.assertFalse((self.root / "outside").exists())
        verified = run_cli(
            "verify", "--vault", self.vault, "--receipt", applied["receipt"]
        )
        self.assertEqual("verified", verified["status"])

    def test_rollback_blocks_drift_and_replay(self) -> None:
        applied = self.apply()
        (self.vault / "CLAUDE.md").write_text(
            "post-apply customer drift\n", encoding="utf-8"
        )
        blocked = run_cli(
            "rollback",
            "--vault",
            self.vault,
            "--cache-root",
            self.cache,
            "--receipt",
            applied["receipt"],
            expected=2,
        )
        self.assertEqual("ROLLBACK_TARGET_DRIFT", blocked["error"])
        self.assertEqual(
            "post-apply customer drift\n",
            (self.vault / "CLAUDE.md").read_text(encoding="utf-8"),
        )
        (self.vault / "CLAUDE.md").write_text("target rules\n", encoding="utf-8")
        run_cli(
            "rollback",
            "--vault",
            self.vault,
            "--cache-root",
            self.cache,
            "--receipt",
            applied["receipt"],
        )
        replay = run_cli(
            "rollback",
            "--vault",
            self.vault,
            "--cache-root",
            self.cache,
            "--receipt",
            applied["receipt"],
            expected=2,
        )
        self.assertEqual("ROLLBACK_TARGET_DRIFT", replay["error"])

    def test_rollback_failure_recovers_the_exact_post_apply_state(self) -> None:
        (self.target / "AGENTS.md").write_text("target agents\n", encoding="utf-8")
        self.refresh_plan()
        applied = self.apply()
        post_apply = inventory(self.vault)
        blocked = run_cli(
            "rollback",
            "--vault",
            self.vault,
            "--cache-root",
            self.cache,
            "--receipt",
            applied["receipt"],
            expected=2,
            extra_env={
                "JUNYONG_AI_TEST_MODE": "1",
                "JUNYONG_AI_TEST_ROLLBACK_FAIL_AFTER_WRITE": "1",
            },
        )
        self.assertEqual(
            "ROLLBACK_FAILED_RESTORED:TEST_INJECTED_ROLLBACK_FAILURE",
            blocked["error"],
        )
        self.assertEqual(post_apply, inventory(self.vault))
        verified = run_cli(
            "verify", "--vault", self.vault, "--receipt", applied["receipt"]
        )
        self.assertEqual("verified", verified["status"])

    def test_corrupt_backup_blocks_rollback_before_vault_writes(self) -> None:
        applied = self.apply()
        receipt_path = Path(applied["receipt"])
        (receipt_path.parent / "before" / "CLAUDE.md").write_text(
            "corrupt backup\n", encoding="utf-8"
        )
        post_apply = inventory(self.vault)
        blocked = run_cli(
            "rollback",
            "--vault",
            self.vault,
            "--cache-root",
            self.cache,
            "--receipt",
            receipt_path,
            expected=2,
        )
        self.assertEqual("BACKUP_DIGEST_MISMATCH", blocked["error"])
        self.assertEqual(post_apply, inventory(self.vault))

    def test_target_drift_invalidates_the_bound_plan(self) -> None:
        (self.target / "CLAUDE.md").write_text("newer target\n", encoding="utf-8")
        before = inventory(self.vault)
        blocked = self.apply(expected=2)
        self.assertEqual("PLAN_STALE", blocked["error"])
        self.assertEqual(before, inventory(self.vault))

    def test_missing_and_corrupt_base_cache_block_plan_without_vault_writes(
        self,
    ) -> None:
        baseline = self.cache / "baselines" / (("9" * 40) + ".zip")
        original = baseline.read_bytes()
        before = inventory(self.vault)
        baseline.unlink()
        blocked = run_cli(
            "plan",
            "--vault",
            self.vault,
            "--cache-root",
            self.cache,
            "--target-root",
            self.target,
            "--path-policy",
            self.policy,
            "--product-contract",
            self.product_contract,
            "--skill-compatibility",
            self.compatibility,
            "--bundle-manifest",
            self.target_bundle,
            expected=2,
        )
        self.assertEqual("BASELINE_CACHE_MISSING", blocked["error"])
        self.assertEqual(before, inventory(self.vault))
        baseline.write_bytes(original + b"tampered")
        blocked = run_cli(
            "plan",
            "--vault",
            self.vault,
            "--cache-root",
            self.cache,
            "--target-root",
            self.target,
            "--path-policy",
            self.policy,
            "--product-contract",
            self.product_contract,
            "--skill-compatibility",
            self.compatibility,
            "--bundle-manifest",
            self.target_bundle,
            expected=2,
        )
        self.assertEqual("BASELINE_CACHE_DIGEST_MISMATCH", blocked["error"])
        self.assertEqual(before, inventory(self.vault))

    def test_tampered_receipt_is_rejected(self) -> None:
        applied = self.apply()
        receipt_path = Path(applied["receipt"])
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["after"]["CLAUDE.md"] = sha256(self.vault / "CLAUDE.md")
        receipt["plan_id"] = "0" * 64
        write_json(receipt_path, receipt)
        blocked = run_cli(
            "verify",
            "--vault",
            self.vault,
            "--receipt",
            receipt_path,
            expected=2,
        )
        self.assertEqual("TRANSACTION_RECEIPT_TAMPERED", blocked["error"])


if __name__ == "__main__":
    unittest.main()

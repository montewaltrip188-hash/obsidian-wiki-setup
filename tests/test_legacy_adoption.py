from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from tests.test_vault_transaction import approval_for, run_cli
from tests.test_vault_update import contract_files, inventory, write_json, write_policy


def historical_bundle(source: Path, output: Path, *, version: str, marker: str) -> Path:
    value = json.loads(source.read_text(encoding="utf-8"))
    value["bundle_version"] = version
    value["candidate_id"] = marker * 64
    value["components"]["product"]["commit"] = marker * 40
    value["components"]["product"]["tree"] = marker.upper() * 40
    value["components"]["wiki_skills"]["commit"] = (str(int(marker) + 1) * 40)
    return write_json(output, value)


def catalog(path: Path, *entries: tuple[str, Path, Path]) -> Path:
    return write_json(
        path,
        {
            "catalog_format": 1,
            "baselines": [
                {
                    "id": identifier,
                    "product_root": str(product_root.resolve()),
                    "bundle_manifest": str(bundle_manifest.resolve()),
                }
                for identifier, product_root, bundle_manifest in entries
            ],
        },
    )


def adoption_approval(plan: dict, baseline_id: str) -> dict:
    selected = next(item for item in plan["candidates"] if item["baseline_id"] == baseline_id)
    return {
        "approval_format": 1,
        "approved_at": "2026-08-28T00:00:00Z",
        "baseline_id": baseline_id,
        "baseline_sha256": selected["baseline_sha256"],
        "plan_id": plan["plan_id"],
        "subject": "synthetic-legacy-adoption",
    }


class LegacyAdoptionTests(unittest.TestCase):
    def test_plan_recommends_only_one_exact_baseline_and_ignores_huge_customer_content(self):
        with tempfile.TemporaryDirectory(prefix="u4-legacy-plan-") as temporary:
            root = Path(temporary)
            vault = root / "vault"
            baseline = root / "baseline"
            vault.mkdir()
            baseline.mkdir()
            for target in (vault, baseline):
                (target / "CLAUDE.md").write_text("legacy rules\n", encoding="utf-8")
                (target / "AGENTS.md").write_text("legacy agents\n", encoding="utf-8")
            raw = vault / "raw" / "huge-customer.bin"
            raw.parent.mkdir()
            with raw.open("wb") as stream:
                stream.seek(512 * 1024 * 1024 - 1)
                stream.write(b"x")
            raw_before = raw.stat()
            contracts = root / "contracts"
            _product, _compatibility, target_bundle = contract_files(contracts)
            old_bundle = historical_bundle(
                target_bundle, contracts / "old-bundle.json", version="2.0.1", marker="8"
            )
            policy = write_policy(contracts)
            catalog_path = catalog(
                contracts / "legacy-catalog.json", ("v2.0.1", baseline, old_bundle)
            )
            vault_before = inventory(baseline)

            started = time.perf_counter()
            plan = run_cli(
                "legacy-plan",
                "--vault", vault,
                "--path-policy", policy,
                "--catalog", catalog_path,
            )
            elapsed = time.perf_counter() - started

            self.assertEqual("adoption_approval_required", plan["status"])
            self.assertEqual("v2.0.1", plan["recommended_baseline_id"])
            self.assertTrue(plan["candidates"][0]["exact_match"])
            self.assertEqual([], plan["candidates"][0]["uncertainties"])
            self.assertEqual(
                {"catalog", "managed_scan", "compare", "total"},
                set(plan["timing_ms"]),
            )
            self.assertGreaterEqual(plan["timing_ms"]["total"], 0)
            self.assertLess(elapsed, 3.0)
            self.assertEqual(raw_before.st_size, raw.stat().st_size)
            self.assertEqual(raw_before.st_mtime_ns, raw.stat().st_mtime_ns)
            self.assertFalse((vault / ".juanyong-ai").exists())
            self.assertEqual(vault_before, inventory(baseline))

    def test_ambiguous_or_modified_baselines_are_listed_without_recommendation(self):
        with tempfile.TemporaryDirectory(prefix="u4-legacy-ambiguous-") as temporary:
            root = Path(temporary)
            vault = root / "vault"
            first = root / "first"
            second = root / "second"
            for target in (vault, first, second):
                target.mkdir()
                (target / "CLAUDE.md").write_text("same\n", encoding="utf-8")
                (target / "AGENTS.md").write_text("same\n", encoding="utf-8")
            contracts = root / "contracts"
            _product, _compatibility, target_bundle = contract_files(contracts)
            first_bundle = historical_bundle(
                target_bundle, contracts / "first-bundle.json", version="2.0.0", marker="8"
            )
            second_bundle = historical_bundle(
                target_bundle, contracts / "second-bundle.json", version="2.0.1", marker="6"
            )
            policy = write_policy(contracts)
            catalog_path = catalog(
                contracts / "legacy-catalog.json",
                ("v2.0.0", first, first_bundle),
                ("v2.0.1", second, second_bundle),
            )
            ambiguous = run_cli(
                "legacy-plan", "--vault", vault, "--path-policy", policy, "--catalog", catalog_path
            )
            self.assertIsNone(ambiguous["recommended_baseline_id"])
            self.assertEqual(2, sum(item["exact_match"] for item in ambiguous["candidates"]))

            (vault / "CLAUDE.md").write_text("customer modified\n", encoding="utf-8")
            modified = run_cli(
                "legacy-plan", "--vault", vault, "--path-policy", policy, "--catalog", catalog_path
            )
            self.assertIsNone(modified["recommended_baseline_id"])
            self.assertTrue(all(item["uncertainties"] for item in modified["candidates"]))

    def test_approved_adoption_enables_normal_upgrade_and_rollback(self):
        with tempfile.TemporaryDirectory(prefix="u4-legacy-lifecycle-") as temporary:
            root = Path(temporary)
            vault = root / "vault"
            baseline = root / "baseline"
            target = root / "target"
            cache = root / "cache"
            artifacts = root / "artifacts"
            for directory in (vault, baseline, target, artifacts):
                directory.mkdir()
            for directory in (vault, baseline):
                (directory / "CLAUDE.md").write_text("legacy rules\n", encoding="utf-8")
                (directory / "AGENTS.md").write_text("legacy agents\n", encoding="utf-8")
            (target / "CLAUDE.md").write_text("new rules\n", encoding="utf-8")
            (target / "AGENTS.md").write_text("new agents\n", encoding="utf-8")
            customer = vault / "wiki" / "customer.md"
            customer.parent.mkdir()
            customer.write_text("customer-owned\n", encoding="utf-8")
            customer_before = customer.read_bytes()
            contracts = root / "contracts"
            product, compatibility, target_bundle = contract_files(contracts)
            old_bundle = historical_bundle(
                target_bundle, contracts / "old-bundle.json", version="2.0.1", marker="8"
            )
            policy = write_policy(contracts)
            catalog_path = catalog(
                contracts / "legacy-catalog.json", ("v2.0.1", baseline, old_bundle)
            )
            plan = run_cli(
                "legacy-plan", "--vault", vault, "--path-policy", policy, "--catalog", catalog_path
            )
            plan_path = write_json(artifacts / "legacy-plan.json", plan)
            wrong = adoption_approval(plan, "v2.0.1")
            wrong["baseline_sha256"] = "0" * 64
            wrong_path = write_json(artifacts / "wrong-approval.json", wrong)
            blocked = run_cli(
                "legacy-adopt",
                "--vault", vault,
                "--cache-root", cache,
                "--path-policy", policy,
                "--catalog", catalog_path,
                "--plan", plan_path,
                "--approval", wrong_path,
                expected=2,
            )
            self.assertEqual("LEGACY_APPROVAL_MISMATCH", blocked["error"])
            self.assertFalse((vault / ".juanyong-ai").exists())
            self.assertFalse(cache.exists())

            approval_path = write_json(
                artifacts / "legacy-approval.json", adoption_approval(plan, "v2.0.1")
            )
            adopted = run_cli(
                "legacy-adopt",
                "--vault", vault,
                "--cache-root", cache,
                "--path-policy", policy,
                "--catalog", catalog_path,
                "--plan", plan_path,
                "--approval", approval_path,
            )
            self.assertEqual("completed", adopted["status"])
            state = json.loads((vault / ".juanyong-ai" / "product-state.json").read_text(encoding="utf-8"))
            self.assertEqual("2.0.1", state["bundle"]["version"])

            upgrade_plan = run_cli(
                "plan",
                "--vault", vault,
                "--cache-root", cache,
                "--target-root", target,
                "--path-policy", policy,
                "--product-contract", product,
                "--skill-compatibility", compatibility,
                "--bundle-manifest", target_bundle,
            )
            upgrade_plan_path = write_json(artifacts / "upgrade-plan.json", upgrade_plan)
            upgrade_approval_path = write_json(
                artifacts / "upgrade-approval.json", approval_for(upgrade_plan)
            )
            applied = run_cli(
                "apply",
                "--vault", vault,
                "--cache-root", cache,
                "--target-root", target,
                "--path-policy", policy,
                "--product-contract", product,
                "--skill-compatibility", compatibility,
                "--bundle-manifest", target_bundle,
                "--plan", upgrade_plan_path,
                "--approval", upgrade_approval_path,
            )
            run_cli("verify", "--vault", vault, "--receipt", applied["receipt"])
            run_cli(
                "rollback",
                "--vault", vault,
                "--cache-root", cache,
                "--receipt", applied["receipt"],
            )
            self.assertEqual("legacy rules\n", (vault / "CLAUDE.md").read_text(encoding="utf-8"))
            self.assertEqual(customer_before, customer.read_bytes())


if __name__ == "__main__":
    unittest.main()

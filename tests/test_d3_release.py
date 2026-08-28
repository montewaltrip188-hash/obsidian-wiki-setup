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
from unittest import mock

from tests.test_release_signing import create_test_keypair, public_key_id, pwsh


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "release" / "d3_release.py"
SIGN = ROOT / "release" / "sign-manifest.ps1"
sys.path.insert(0, str(ROOT / "release"))
import d3_release  # noqa: E402


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_cli(*args: object, expected: int = 0, environment: dict | None = None) -> dict:
    completed = subprocess.run(
        [sys.executable, str(CLI), *map(str, args)],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        env={**os.environ, "PYTHONUTF8": "1", **(environment or {})},
        check=False,
    )
    if completed.returncode != expected:
        raise AssertionError(completed.stderr or completed.stdout)
    return json.loads(completed.stdout if expected == 0 else completed.stderr)


def candidate(path: Path, candidate_id: str, platform_name: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "bundle-manifest.json",
            json.dumps(
                {
                    "bundle_version": "2.1.0",
                    "candidate_id": candidate_id,
                    "release_state": "unreleased_candidate",
                }
            ),
        )
        archive.writestr(
            "runtime/SBOM.json",
            json.dumps({"platform": platform_name, "runtime_id": "fixture"}),
        )


def sealed(payload: dict) -> dict:
    result = dict(payload)
    result["receipt_sha256"] = digest(result)
    return result


def receipt(
    path: Path, *, target: str, candidate_id: str, candidate_sha256: str, plan_id: str
) -> None:
    machine = {
        "windows-x64": "amd64",
        "macos-x64": "x86_64",
        "macos-arm64": "arm64",
    }[target]
    runner = {
        "github_repository": None,
        "github_run_attempt": None,
        "github_run_id": None,
        "github_sha": None,
        "github_workflow_ref": None,
    }
    if target.startswith("macos-"):
        runner = {
            "github_repository": "montewaltrip188-hash/obsidian-wiki-setup",
            "github_run_attempt": "1",
            "github_run_id": "12345",
            "github_sha": "1" * 40,
            "github_workflow_ref": "montewaltrip188-hash/obsidian-wiki-setup/.github/workflows/d3-macos-candidate.yml@refs/heads/main",
        }
    payload = {
        "architecture": machine,
        "bundle_version": "2.1.0",
        "candidate_id": candidate_id,
        "candidate_sha256": candidate_sha256,
        "dependencies": {
            "jieba": "0.42.1",
            "numpy": "2.5.2",
            "python": "3.12.14",
            "requests": "2.34.2",
        },
        "install_status": "installed",
        "plan_id": plan_id,
        "query_status": "completed",
        "receipt_type": "d3-candidate-acceptance",
        "runner": runner,
        "runtime_id": "cpython-3.12.14+20260825",
        "schema_version": 1,
        "status": "completed",
        "target": target,
        "undo_status": "undone",
        "verify_status": "verified",
    }
    path.write_text(json.dumps(sealed(payload)), encoding="utf-8")


def fixture(root: Path) -> tuple[Path, dict[str, Path]]:
    workspace = root / "d2"
    (workspace / "candidates" / "windows" / "first").mkdir(parents=True)
    (workspace / "candidates" / "macos" / "first").mkdir(parents=True)
    windows = workspace / "candidates" / "windows" / "first" / "candidate.zip"
    macos = workspace / "candidates" / "macos" / "first" / "candidate.zip"
    candidate(windows, "a" * 64, "windows")
    candidate(macos, "b" * 64, "macos")
    payload = {
        "bundle_version": "2.1.0",
        "candidates": {
            "windows": {
                "candidate_id": "a" * 64,
                "candidate_zip_sha256": sha256(windows),
                "candidate_zip_size": windows.stat().st_size,
                "first_candidate_zip": "candidates/windows/first/candidate.zip",
                "platform": "windows",
                "reproducible": True,
            },
            "macos": {
                "candidate_id": "b" * 64,
                "candidate_zip_sha256": sha256(macos),
                "candidate_zip_size": macos.stat().st_size,
                "first_candidate_zip": "candidates/macos/first/candidate.zip",
                "platform": "macos",
                "reproducible": True,
            },
        },
        "next_action": "run_approval_required",
        "orchestrator_format": 1,
        "release_gates": {
            "keyword_runtime": {
                "automatic_network_install": False,
                "offline_baseline": "keyword",
                "runtime_id": "cpython-3.12.14+20260825",
                "status": "ready",
                "targets": ["windows-x64", "macos-x64", "macos-arm64"],
            }
        },
        "release_state": "unreleased_candidate",
        "sources": {
            "installer": {"commit": "1" * 40},
            "product": {"commit": "2" * 40},
            "skill": {"commit": "3" * 40},
        },
        "status": "planned",
    }
    payload["plan_id"] = digest(payload)
    plan = sealed(payload)
    release_plan = workspace / "release-plan.json"
    release_plan.write_text(json.dumps(plan), encoding="utf-8")
    receipts = {}
    for target, item in (
        ("windows-x64", plan["candidates"]["windows"]),
        ("macos-x64", plan["candidates"]["macos"]),
        ("macos-arm64", plan["candidates"]["macos"]),
    ):
        path = root / f"{target}-receipt.json"
        receipt(
            path,
            target=target,
            candidate_id=item["candidate_id"],
            candidate_sha256=item["candidate_zip_sha256"],
            plan_id=plan["plan_id"],
        )
        receipts[target] = path
    return release_plan, receipts


@unittest.skipUnless(os.name == "nt", "签名闭环当前由 Windows 维护端验证")
class D3ReleaseTests(unittest.TestCase):
    def test_prepare_rejects_receipt_from_wrong_cpu_architecture(self):
        with tempfile.TemporaryDirectory(prefix="d3-release-architecture-") as temporary:
            root = Path(temporary)
            release_plan, receipts = fixture(root)
            wrong = json.loads(receipts["macos-arm64"].read_text(encoding="utf-8"))
            wrong.pop("receipt_sha256")
            wrong["architecture"] = "x86_64"
            receipts["macos-arm64"].write_text(
                json.dumps(sealed(wrong)), encoding="utf-8"
            )
            attestation = root / "attestation.sigstore.json"
            attestation.write_text("{}\n", encoding="utf-8")

            blocked = run_cli(
                "prepare",
                "--release-plan", release_plan,
                "--windows-receipt", receipts["windows-x64"],
                "--macos-x64-receipt", receipts["macos-x64"],
                "--macos-arm64-receipt", receipts["macos-arm64"],
                "--macos-x64-attestation", attestation,
                "--macos-arm64-attestation", attestation,
                "--output", root / "release-candidate",
                expected=2,
            )

            self.assertEqual("D3_ACCEPTANCE_RECEIPT_MISMATCH", blocked["error"])

    def test_prepare_sign_and_verify_release_candidate_assets(self):
        with tempfile.TemporaryDirectory(prefix="d3-release-") as temporary:
            root = Path(temporary)
            release_plan, receipts = fixture(root)
            release_dir = root / "release-candidate"
            macos_x64_attestation = root / "macos-x64-attestation.sigstore.json"
            macos_arm64_attestation = root / "macos-arm64-attestation.sigstore.json"
            macos_x64_attestation.write_text("{}\n", encoding="utf-8")
            macos_arm64_attestation.write_text("{}\n", encoding="utf-8")
            private_key, protected_passphrase, public_key = create_test_keypair(root)
            expected_key_id = public_key_id(public_key)
            signing_policy = {
                "algorithm": "RSA-SHA256-PKCS1-v1_5",
                "key_id": expected_key_id,
                "minimum_rsa_bits": 3072,
                "private_key_storage": "encrypted-pkcs8-dpapi-current-user",
                "public_key": "release/release-signing-public-key.pem",
                "public_key_path": public_key,
                "schema_version": 1,
            }

            prepare_args = d3_release.parser().parse_args([
                "prepare",
                "--release-plan", str(release_plan),
                "--windows-receipt", str(receipts["windows-x64"]),
                "--macos-x64-receipt", str(receipts["macos-x64"]),
                "--macos-arm64-receipt", str(receipts["macos-arm64"]),
                "--macos-x64-attestation", str(macos_x64_attestation),
                "--macos-arm64-attestation", str(macos_arm64_attestation),
                "--output", str(release_dir),
            ])
            attestations = {
                macos_x64_attestation: {
                    "bundle_sha256": sha256(macos_x64_attestation),
                    "verified_attestations": 1,
                },
                macos_arm64_attestation: {
                    "bundle_sha256": sha256(macos_arm64_attestation),
                    "verified_attestations": 1,
                },
            }
            with mock.patch.object(
                d3_release, "load_signing_policy", return_value=signing_policy
            ), mock.patch.object(
                d3_release,
                "verify_attestation",
                side_effect=lambda _receipt, bundle, _plan: attestations[bundle],
            ):
                prepared = d3_release.prepare(prepare_args)

            self.assertEqual("prepared", prepared["status"])
            self.assertEqual("signature_required", prepared["next_action"])
            manifest = release_dir / "release-manifest.json"
            pwsh(
                "-File", SIGN,
                "-ManifestPath", manifest,
                "-SignaturePath", release_dir / "release-manifest.sig",
                "-PrivateKeyPath", private_key,
                "-ProtectedPassphrasePath", protected_passphrase,
            )
            verify_args = d3_release.parser().parse_args(
                ["verify", "--release-dir", str(release_dir)]
            )
            with mock.patch.object(
                d3_release, "load_signing_policy", return_value=signing_policy
            ):
                verified = d3_release.verify(verify_args)
            self.assertEqual("verified", verified["status"])
            self.assertEqual("2.1.0", verified["bundle_version"])
            self.assertEqual(10, verified["verified_files"])

            asset = release_dir / "assets" / "obsidian-llm-wiki-2.1.0-windows-x64.zip"
            asset.write_bytes(asset.read_bytes() + b"tamper")
            with mock.patch.object(
                d3_release, "load_signing_policy", return_value=signing_policy
            ), self.assertRaisesRegex(
                d3_release.D3Error, "^D3_RELEASE_ASSET_DRIFT$"
            ):
                d3_release.verify(verify_args)

            asset.write_bytes(asset.read_bytes()[:-len(b"tamper")])
            manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
            manifest_value["bundle_version"] = "9.9.9"
            manifest.write_bytes(
                (
                    json.dumps(
                    manifest_value,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    )
                    + "\n"
                ).encode("utf-8")
            )
            (release_dir / "release-manifest.sig").unlink()
            pwsh(
                "-File", SIGN,
                "-ManifestPath", manifest,
                "-SignaturePath", release_dir / "release-manifest.sig",
                "-PrivateKeyPath", private_key,
                "-ProtectedPassphrasePath", protected_passphrase,
            )
            with mock.patch.object(
                d3_release, "load_signing_policy", return_value=signing_policy
            ), self.assertRaisesRegex(
                d3_release.D3Error, "^D3_RELEASE_MANIFEST_INVALID$"
            ):
                d3_release.verify(verify_args)


if __name__ == "__main__":
    unittest.main()

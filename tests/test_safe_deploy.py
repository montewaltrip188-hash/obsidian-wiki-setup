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


REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOYER = REPO_ROOT / "extract-vault.py"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tree_digest(files: dict[str, bytes]) -> str:
    records = []
    for path in sorted(files, key=lambda item: item.encode("utf-8")):
        data = files[path]
        records.append(f"{path}\0{len(data)}\0{sha256_bytes(data)}\n".encode("utf-8"))
    return sha256_bytes(b"".join(records))


def make_candidate(root: Path, files: dict[str, bytes]) -> tuple[Path, Path]:
    archive = root / "vault.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path, data in files.items():
            bundle.writestr(f"vault/{path}", data)

    archive_bytes = archive.read_bytes()
    manifest = {
        "schema_version": 1,
        "archive": {
            "sha256": sha256_bytes(archive_bytes),
            "size": len(archive_bytes),
        },
        "vault": {
            "tree_sha256": tree_digest(files),
            "files": [
                {"path": path, "size": len(data), "sha256": sha256_bytes(data)}
                for path, data in files.items()
            ],
        },
    }
    manifest_path = root / "install-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return archive, manifest_path


def make_custom_candidate(
    root: Path,
    members: list[tuple[zipfile.ZipInfo | str, bytes]],
    manifest_files: dict[str, bytes],
) -> tuple[Path, Path]:
    archive = root / "vault.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, data in members:
            bundle.writestr(name, data)
    archive_bytes = archive.read_bytes()
    manifest = {
        "schema_version": 1,
        "archive": {"sha256": sha256_bytes(archive_bytes), "size": len(archive_bytes)},
        "vault": {
            "tree_sha256": tree_digest(manifest_files),
            "files": [
                {"path": path, "size": len(data), "sha256": sha256_bytes(data)}
                for path, data in manifest_files.items()
            ],
        },
    }
    manifest_path = root / "install-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return archive, manifest_path


def deploy(archive: Path, manifest: Path, target: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    return subprocess.run(
        [
            sys.executable,
            str(DEPLOYER),
            "deploy",
            "--archive",
            str(archive),
            "--manifest",
            str(manifest),
            "--target",
            str(target),
            *extra,
        ],
        text=True,
        capture_output=True,
        encoding="utf-8",
        env=environment,
    )


def cleanup_backup(target: Path, backup: Path, deploy_receipt: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    return subprocess.run(
        [
            sys.executable,
            str(DEPLOYER),
            "cleanup-backup",
            "--target",
            str(target),
            "--backup",
            str(backup),
            "--deploy-receipt",
            str(deploy_receipt),
        ],
        text=True,
        capture_output=True,
        encoding="utf-8",
        env=environment,
    )


def finalize_runtime_config(
    target: Path, deploy_receipt: Path
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    return subprocess.run(
        [
            sys.executable,
            str(DEPLOYER),
            "finalize-runtime-config",
            "--target",
            str(target),
            "--deploy-receipt",
            str(deploy_receipt),
        ],
        text=True,
        capture_output=True,
        encoding="utf-8",
        env=environment,
    )


class SafeDeployTests(unittest.TestCase):
    def test_new_target_is_verified_and_switched_in(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive, manifest = make_candidate(
                root,
                {"CLAUDE.md": b"rules\n", "wiki/index.md": "索引\n".encode()},
            )
            target = root / "ObsidianVault"

            result = deploy(archive, manifest, target)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((target / "CLAUDE.md").read_bytes(), b"rules\n")
            self.assertEqual((target / "wiki/index.md").read_text(encoding="utf-8"), "索引\n")
            receipts = list(root.glob(".ObsidianVault.deploy-receipt-*.json"))
            self.assertEqual(len(receipts), 1)
            receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "completed")
            self.assertEqual(receipt["archive_sha256"], sha256_bytes(archive.read_bytes()))
            self.assertEqual(receipt["tree_sha256"], tree_digest({"CLAUDE.md": b"rules\n", "wiki/index.md": "索引\n".encode()}))

    def test_unsafe_archive_members_are_rejected_before_extraction(self) -> None:
        cases: list[tuple[str, list[tuple[zipfile.ZipInfo | str, bytes]], dict[str, bytes]]] = [
            ("parent traversal", [("vault/../escaped.txt", b"escape")], {"../escaped.txt": b"escape"}),
            ("absolute path", [("/absolute.txt", b"escape")], {"/absolute.txt": b"escape"}),
            ("drive path", [("C:/drive.txt", b"escape")], {"C:/drive.txt": b"escape"}),
            (
                "case collision",
                [("vault/A.md", b"A"), ("vault/a.md", b"a")],
                {"A.md": b"A", "a.md": b"a"},
            ),
            (
                "unicode collision",
                [("vault/é.md", b"nfc"), ("vault/e\u0301.md", b"nfd")],
                {"é.md": b"nfc", "e\u0301.md": b"nfd"},
            ),
        ]
        symlink = zipfile.ZipInfo("vault/link")
        symlink.create_system = 3
        symlink.external_attr = 0o120777 << 16
        cases.append(("symlink", [(symlink, b"../outside")], {"link": b"../outside"}))
        cases.append(
            (
                "exact duplicate",
                [("vault/repeated.md", b"first"), ("vault/repeated.md", b"second")],
                {"repeated.md": b"second"},
            )
        )
        cases.extend(
            [
                ("windows device", [("vault/NUL.txt", b"device")], {"NUL.txt": b"device"}),
                ("windows device nested", [("vault/docs/COM1", b"device")], {"docs/COM1": b"device"}),
                ("trailing dot", [("vault/trailing.", b"dot")], {"trailing.": b"dot"}),
                ("trailing space", [("vault/trailing ", b"space")], {"trailing ": b"space"}),
                ("control character", [("vault/bad\x01.md", b"control")], {"bad\x01.md": b"control"}),
            ]
        )

        for label, members, manifest_files in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                archive, manifest = make_custom_candidate(root, members, manifest_files)
                target = root / "ObsidianVault"

                result = deploy(archive, manifest, target)

                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(target.exists())
                self.assertFalse((root / "escaped.txt").exists())
                receipts = list(root.glob(".ObsidianVault.deploy-receipt-*.json"))
                self.assertEqual(len(receipts), 1)
                self.assertEqual(json.loads(receipts[0].read_text(encoding="utf-8"))["status"], "failed")

    def test_raw_nul_in_zip_member_name_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive, manifest = make_custom_candidate(
                root,
                [("vault/good.md", b"good"), ("vault/a.txt", b"hidden")],
                {"good.md": b"good"},
            )
            mutated = archive.read_bytes().replace(b"vault/a.txt", b"vault/\x00.txt")
            self.assertEqual(mutated.count(b"vault/\x00.txt"), 2)
            archive.write_bytes(mutated)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["archive"] = {"sha256": sha256_bytes(mutated), "size": len(mutated)}
            manifest.write_text(json.dumps(payload), encoding="utf-8")

            result = deploy(archive, manifest, root / "ObsidianVault")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("NUL", result.stderr)

    def test_archive_hash_mismatch_fails_without_creating_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive, manifest = make_candidate(root, {"CLAUDE.md": b"original"})
            with archive.open("ab") as stream:
                stream.write(b"tampered")
            target = root / "ObsidianVault"

            result = deploy(archive, manifest, target)

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(target.exists())
            receipt = json.loads(next(root.glob(".ObsidianVault.deploy-receipt-*.json")).read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "failed")
            self.assertIn("归档", receipt["error"])

    def test_missing_archive_still_leaves_failure_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive, manifest = make_candidate(root, {"CLAUDE.md": b"content"})
            archive.unlink()
            target = root / "ObsidianVault"

            result = deploy(archive, manifest, target)

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(target.exists())
            receipt = json.loads(next(root.glob(".ObsidianVault.deploy-receipt-*.json")).read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "failed")
            self.assertIn("vault.zip", receipt["error"])

    def test_noncanonical_manifest_contract_is_rejected(self) -> None:
        mutations = {
            "extra field": lambda payload: payload.update({"unexpected": True}),
            "boolean size": lambda payload: payload["archive"].update({"size": True}),
            "uppercase digest": lambda payload: payload["archive"].update(
                {"sha256": payload["archive"]["sha256"].upper()}
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                archive, manifest = make_candidate(root, {"CLAUDE.md": b"content"})
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                mutate(payload)
                manifest.write_text(json.dumps(payload), encoding="utf-8")

                result = deploy(archive, manifest, root / "ObsidianVault")

                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((root / "ObsidianVault").exists())

    def test_existing_target_is_refused_by_default_and_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive, manifest = make_candidate(root, {"new.md": b"new"})
            target = root / "ObsidianVault"
            target.mkdir()
            (target / "old.md").write_bytes(b"old")

            result = deploy(archive, manifest, target)

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((target / "old.md").read_bytes(), b"old")
            self.assertFalse((target / "new.md").exists())
            self.assertEqual(list(root.glob(".ObsidianVault.backup-*")), [])

    def test_explicit_existing_target_upgrade_keeps_verified_backup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive, manifest = make_candidate(root, {"new.md": b"new"})
            target = root / "ObsidianVault"
            target.mkdir()
            (target / "old.md").write_bytes(b"old")

            result = deploy(archive, manifest, target, "--allow-existing")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((target / "new.md").read_bytes(), b"new")
            self.assertFalse((target / "old.md").exists())
            backups = list(root.glob(".ObsidianVault.backup-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual((backups[0] / "old.md").read_bytes(), b"old")
            receipt = json.loads(next(root.glob(".ObsidianVault.deploy-receipt-*.json")).read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "completed")
            self.assertEqual(Path(receipt["backup"]).resolve(), backups[0].resolve())

    def test_backup_cleanup_requires_separate_receipt_bound_command(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive, manifest = make_candidate(root, {"new.md": b"new"})
            target = root / "ObsidianVault"
            target.mkdir()
            (target / "old.md").write_bytes(b"old")
            deployed = deploy(archive, manifest, target, "--allow-existing")
            self.assertEqual(deployed.returncode, 0, deployed.stderr)
            backup = next(root.glob(".ObsidianVault.backup-*"))
            deploy_receipt = next(root.glob(".ObsidianVault.deploy-receipt-*.json"))

            result = cleanup_backup(target, backup, deploy_receipt)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(backup.exists())
            self.assertEqual((target / "new.md").read_bytes(), b"new")
            cleanup_receipts = list(root.glob(".ObsidianVault.cleanup-receipt-*.json"))
            self.assertEqual(len(cleanup_receipts), 1)
            self.assertEqual(
                json.loads(cleanup_receipts[0].read_text(encoding="utf-8"))["status"],
                "completed",
            )

    def test_claudian_runtime_config_can_be_finalized_then_backup_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            original_config = b'{"claudePath":"bundled-default"}\n'
            runtime_config = b'{"claudePath":"C:/Users/test/bin/claude.exe"}\n'
            archive, manifest = make_candidate(
                root,
                {
                    "CLAUDE.md": b"rules\n",
                    ".obsidian/plugins/claudian/data.json": original_config,
                },
            )
            target = root / "ObsidianVault"
            target.mkdir()
            (target / "old.md").write_bytes(b"old")
            deployed = deploy(archive, manifest, target, "--allow-existing")
            self.assertEqual(deployed.returncode, 0, deployed.stderr)
            backup = next(root.glob(".ObsidianVault.backup-*"))
            deploy_receipt = next(root.glob(".ObsidianVault.deploy-receipt-*.json"))
            config_path = target / ".obsidian/plugins/claudian/data.json"
            config_path.write_bytes(runtime_config)

            finalized = finalize_runtime_config(target, deploy_receipt)

            self.assertEqual(finalized.returncode, 0, finalized.stderr)
            receipt = json.loads(deploy_receipt.read_text(encoding="utf-8"))
            self.assertEqual(receipt["tree_sha256"], tree_digest({
                "CLAUDE.md": b"rules\n",
                ".obsidian/plugins/claudian/data.json": original_config,
            }))
            self.assertEqual(
                receipt["runtime_config_finalization"]["allowed_paths"],
                [".obsidian/plugins/claudian/data.json"],
            )
            self.assertEqual(
                receipt["expected_current_tree_sha256"],
                tree_digest({
                    "CLAUDE.md": b"rules\n",
                    ".obsidian/plugins/claudian/data.json": runtime_config,
                }),
            )

            cleaned = cleanup_backup(target, backup, deploy_receipt)

            self.assertEqual(cleaned.returncode, 0, cleaned.stderr)
            self.assertFalse(backup.exists())

    def test_runtime_config_finalize_refuses_non_whitelisted_drift_and_keeps_backup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive, manifest = make_candidate(
                root,
                {
                    "CLAUDE.md": b"rules\n",
                    ".obsidian/plugins/claudian/data.json": b"{}\n",
                },
            )
            target = root / "ObsidianVault"
            target.mkdir()
            (target / "old.md").write_bytes(b"old")
            deployed = deploy(archive, manifest, target, "--allow-existing")
            self.assertEqual(deployed.returncode, 0, deployed.stderr)
            backup = next(root.glob(".ObsidianVault.backup-*"))
            deploy_receipt = next(root.glob(".ObsidianVault.deploy-receipt-*.json"))
            receipt_before = deploy_receipt.read_bytes()
            (target / ".obsidian/plugins/claudian/data.json").write_bytes(b'{"claudePath":"runtime"}\n')
            (target / "CLAUDE.md").write_bytes(b"unauthorized drift\n")

            result = finalize_runtime_config(target, deploy_receipt)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("漂移", result.stderr)
            self.assertTrue(backup.is_dir())
            self.assertEqual(deploy_receipt.read_bytes(), receipt_before)

    def test_runtime_config_finalize_requires_receipt_target_binding(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive, manifest = make_candidate(
                root,
                {".obsidian/plugins/claudian/data.json": b"{}\n"},
            )
            deployed_target = root / "DeployedVault"
            deployed = deploy(archive, manifest, deployed_target)
            self.assertEqual(deployed.returncode, 0, deployed.stderr)
            deploy_receipt = next(root.glob(".DeployedVault.deploy-receipt-*.json"))
            other_target = root / "OtherVault"
            other_target.mkdir()
            config = other_target / ".obsidian/plugins/claudian/data.json"
            config.parent.mkdir(parents=True)
            config.write_bytes(b'{"claudePath":"runtime"}\n')

            result = finalize_runtime_config(other_target, deploy_receipt)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("不匹配", result.stderr)

    def test_backup_cleanup_refuses_when_current_target_drifted(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive, manifest = make_candidate(root, {"new.md": b"new"})
            target = root / "ObsidianVault"
            target.mkdir()
            (target / "old.md").write_bytes(b"old")
            deployed = deploy(archive, manifest, target, "--allow-existing")
            self.assertEqual(deployed.returncode, 0, deployed.stderr)
            backup = next(root.glob(".ObsidianVault.backup-*"))
            deploy_receipt = next(root.glob(".ObsidianVault.deploy-receipt-*.json"))
            (target / "new.md").write_bytes(b"user changed after deploy")

            result = cleanup_backup(target, backup, deploy_receipt)

            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(backup.is_dir())
            self.assertEqual((backup / "old.md").read_bytes(), b"old")
            self.assertEqual((target / "new.md").read_bytes(), b"user changed after deploy")

    def test_platform_setup_scripts_delegate_only_to_safe_deployer(self) -> None:
        windows = (REPO_ROOT / "setup-win.ps1").read_text(encoding="utf-8")
        mac = (REPO_ROOT / "setup-mac.sh").read_text(encoding="utf-8")
        for label, content in (("Windows", windows), ("macOS", mac)):
            self.assertIn("extract-vault.py", content, label)
            self.assertIn("deploy-manifest.json", content, label)
            self.assertNotIn("install-manifest.json", content, label)
            self.assertRegex(content, r"\bdeploy\b", label)
            self.assertIn("--allow-existing", content, label)
        self.assertNotIn("Expand-Archive", windows)
        self.assertNotIn("Remove-Item $defaultVaultPath -Recurse", windows)
        self.assertNotIn("rm -rf \"$DEFAULT_VAULT\"", mac)
        self.assertNotIn("zipfile.ZipFile", mac)

    def test_platform_setup_scripts_install_and_verify_three_core_skills(self) -> None:
        windows = (REPO_ROOT / "setup-win.ps1").read_text(encoding="utf-8")
        mac = (REPO_ROOT / "setup-mac.sh").read_text(encoding="utf-8")
        for label, content in (("Windows", windows), ("macOS", mac)):
            self.assertIn("manage_wiki_skills.py", content, label)
            self.assertIn("claudecode-wiki-skills", content, label)
            self.assertRegex(content, r"\binstall\b", label)
            self.assertRegex(content, r"\bverify\b", label)
            self.assertRegex(content, r"\brollback\b", label)
            self.assertRegex(content, r"\buninstall\b", label)
            self.assertNotIn("--include-ima", content, label)
        self.assertLess(windows.index("$skillManager install"), windows.index('$vaultDeployer, "deploy"'))
        self.assertLess(mac.index('"$SKILL_MANAGER" install'), mac.index('"$VAULT_DEPLOYER" deploy'))

    def test_declining_existing_vault_upgrade_exits_without_claiming_completion(self) -> None:
        windows = (REPO_ROOT / "setup-win.ps1").read_text(encoding="utf-8")
        mac = (REPO_ROOT / "setup-mac.sh").read_text(encoding="utf-8")

        self.assertRegex(
            windows,
            r'if \(\$overwrite -ne \'y\'[\s\S]*?\) \{\s*Write-Host "  安装未完成：已保留现有 Vault，未执行部署"[\s\S]*?exit 2\s*\}',
        )
        self.assertRegex(
            mac,
            r'if \[ "\$OVERWRITE" != "y" \][\s\S]*?yellow "  安装未完成：已保留现有 Vault，未执行部署"\s*exit 2\s*',
        )

    def test_skill_verify_failure_restores_state_for_install_and_upgrade(self) -> None:
        windows = (REPO_ROOT / "setup-win.ps1").read_text(encoding="utf-8")
        mac = (REPO_ROOT / "setup-mac.sh").read_text(encoding="utf-8")

        windows_verify_start = windows.index("$skillVerifyOutput =")
        windows_verify_end = windows.index("$skillVerify =", windows_verify_start)
        windows_failure = windows[windows_verify_start:windows_verify_end]
        self.assertIn('$verifyExit = $LASTEXITCODE', windows_failure)
        self.assertIn('"install" { "uninstall" }', windows_failure)
        self.assertIn('"upgrade" { "rollback" }', windows_failure)
        self.assertIn('$skillManager $restoreCommand --home $env:USERPROFILE', windows_failure)
        self.assertIn('exit $verifyExit', windows_failure)

        mac_verify_start = mac.index('if ! SKILL_VERIFY=')
        mac_verify_end = mac.index('SKILL_NAMES=', mac_verify_start)
        mac_failure = mac[mac_verify_start:mac_verify_end]
        self.assertIn('install) RESTORE_COMMAND="uninstall"', mac_failure)
        self.assertIn('upgrade) RESTORE_COMMAND="rollback"', mac_failure)
        self.assertIn('"$SKILL_MANAGER" "$RESTORE_COMMAND" --home "$HOME"', mac_failure)
        self.assertIn('exit 1', mac_failure)


if __name__ == "__main__":
    unittest.main()

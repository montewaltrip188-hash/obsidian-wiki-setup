import json
import hashlib
import gzip
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "tools" / "install_candidate.py"
WRAPPER = ROOT / "scripts" / "install-candidate.ps1"


def run(*args, cwd=None, check=True):
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    return subprocess.run(
        [str(arg) for arg in args],
        cwd=cwd,
        check=check,
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=environment,
    )


def make_repo(root: Path, name: str, files=None):
    repo = root / name
    repo.mkdir()
    run("git", "init", "-q", repo)
    run("git", "-C", repo, "config", "user.name", "D0 Test")
    run("git", "-C", repo, "config", "user.email", "d0@example.invalid")
    for relative, content in (files or {"README.md": name}).items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    run("git", "-C", repo, "add", ".")
    run("git", "-C", repo, "commit", "-qm", "fixture")
    commit = run("git", "-C", repo, "rev-parse", "HEAD").stdout.strip()
    tree = run("git", "-C", repo, "rev-parse", "HEAD^{tree}").stdout.strip()
    return repo, commit, tree


def deterministic_zip(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = (0o100644 << 16)
            archive.writestr(info, content)
    return output.getvalue()


def deterministic_tar_gz(files):
    tar_output = io.BytesIO()
    with tarfile.open(fileobj=tar_output, mode="w") as archive:
        for name, content in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o755 if name.endswith(("python.exe", "python3")) else 0o644
            info.mtime = 0
            archive.addfile(info, io.BytesIO(content))
    return gzip.compress(tar_output.getvalue(), mtime=0)


def synthetic_runtime_files():
    runtime_windows = deterministic_tar_gz(
        {
            "python/LICENSE.txt": b"fixture python license\n",
            "python/Lib/site-packages/.keep": b"",
            "python/python.exe": b"fixture-python-exe\n",
        }
    )
    runtime_macos = deterministic_tar_gz(
        {
            "python/LICENSE.txt": b"fixture python license\n",
            "python/lib/python3.12/site-packages/.keep": b"",
            "python/bin/python3": b"#!/bin/sh\nexit 0\n",
        }
    )
    wheel = deterministic_zip(
        {
            "fixture_dep/__init__.py": b"VERSION = '1.0.0'\n",
            "fixture_dep-1.0.0.dist-info/METADATA": b"Name: fixture-dep\nVersion: 1.0.0\n",
            "fixture_dep-1.0.0.dist-info/licenses/vendor/deeply/nested/source/component/LICENSE": b"fixture wheel license\n",
        }
    )
    jieba = deterministic_tar_gz(
        {"jieba-0.42.1/jieba/__init__.py": b"__version__ = '0.42.1'\n"}
    )
    jieba_license = b"fixture jieba license\n"

    def asset(filename, content):
        return {
            "filename": filename,
            "repo_path": f"tests/runtime-fixtures/{filename}",
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }

    runtime_windows_asset = asset("fixture-python-windows.tar.gz", runtime_windows)
    runtime_macos_asset = asset("fixture-python-macos.tar.gz", runtime_macos)
    wheel_asset = asset("fixture-dep.whl", wheel)
    jieba_asset = asset("fixture-jieba.tar.gz", jieba)
    license_asset = asset("fixture-jieba-LICENSE", jieba_license)
    bom = {
        "schema_version": 1,
        "runtime_id": "fixture-python-3.12",
        "provider": "fixture",
        "provider_release": "fixture",
        "python_version": "3.12.0",
        "archive_flavor": "install_only",
        "policy": {
            "client_network_install": False,
            "modify_system_python": False,
            "modify_customer_content": False,
            "require_asset_sha256": True,
            "require_licenses": True,
            "require_sbom": True,
        },
        "locked_versions": {"fixture-dep": "1.0.0", "jieba": "0.42.1"},
        "targets": {
            "windows-x64": {
                "platform": "windows",
                "architecture": "x86_64",
                "interpreter": "python/python.exe",
                "site_packages": "python/Lib/site-packages",
                "asset": runtime_windows_asset,
            },
            "macos-x64": {
                "platform": "macos",
                "architecture": "x86_64",
                "interpreter": "python/bin/python3",
                "site_packages": "python/lib/python3.12/site-packages",
                "asset": runtime_macos_asset,
            },
            "macos-arm64": {
                "platform": "macos",
                "architecture": "aarch64",
                "interpreter": "python/bin/python3",
                "site_packages": "python/lib/python3.12/site-packages",
                "asset": runtime_macos_asset,
            },
        },
        "packages": {
            "fixture-dep": {
                "version": "1.0.0",
                "license": "MIT",
                "install_mode": "wheel",
                "asset": wheel_asset,
            },
            "jieba": {
                "version": "0.42.1",
                "license": "MIT",
                "install_mode": "sdist_vendored",
                "asset": jieba_asset,
                "license_asset": license_asset,
                "source_prefix": "jieba-0.42.1/jieba/",
            },
        },
    }
    return {
        "contracts/offline-keyword-runtime-bom.json": json.dumps(bom) + "\n",
        "contracts/offline-keyword-runtime-bom.schema.json": "{}\n",
        "tests/runtime-fixtures/fixture-python-windows.tar.gz": runtime_windows,
        "tests/runtime-fixtures/fixture-python-macos.tar.gz": runtime_macos,
        "tests/runtime-fixtures/fixture-dep.whl": wheel,
        "tests/runtime-fixtures/fixture-jieba.tar.gz": jieba,
        "tests/runtime-fixtures/fixture-jieba-LICENSE": jieba_license,
    }


def installer_contract_files():
    files = {
        "activation-public-key.xml": "<RSAKeyValue>public only</RSAKeyValue>\n",
        "change-model.bat": "@echo off\n",
        "change-model.ps1": "Write-Output 'change model'\n",
        "change-model.sh": "#!/bin/sh\necho change model\n",
        "contracts/install-candidate.schema.json": "{}\n",
        "contracts/deploy-manifest.schema.json": "{}\n",
        "contracts/wiki-skill-lifecycle.json": "{}\n",
        "contracts/runtime-contract.schema.json": "{}\n",
        "contracts/compatibility.schema.json": "{}\n",
        "contracts/bundle-manifest.schema.json": "{}\n",
        "contracts/bundle-release.schema.json": "{}\n",
        "contracts/update-path-policy.schema.json": "{}\n",
        "contracts/product-state.schema.json": "{}\n",
        "contracts/update-plan.schema.json": "{}\n",
        "contracts/update-approval.schema.json": "{}\n",
        "contracts/update-transaction-receipt.schema.json": "{}\n",
        "contracts/joint-update-plan.schema.json": "{}\n",
        "contracts/joint-update-approval.schema.json": "{}\n",
        "contracts/joint-update-receipt.schema.json": "{}\n",
        "contracts/legacy-catalog.schema.json": "{}\n",
        "contracts/legacy-adoption-plan.schema.json": "{}\n",
        "contracts/legacy-adoption-approval.schema.json": "{}\n",
        "contracts/legacy-adoption-receipt.schema.json": "{}\n",
        "extract-vault.py": "print('extract')\n",
        "install.bat": "@echo off\n",
        "revoked-activation-ids.txt": "# none\n",
        "scripts/manage-wiki-skills.ps1": "Write-Output 'manage'\n",
        "scripts/manage-wiki-skills.sh": "#!/bin/sh\necho manage\n",
        "scripts/vault-update.ps1": "Write-Output 'vault update'\n",
        "scripts/vault-update.sh": "#!/bin/sh\necho vault update\n",
        "scripts/joint-update.ps1": "Write-Output 'joint update'\n",
        "scripts/joint-update.sh": "#!/bin/sh\necho joint update\n",
        "scripts/install-candidate.ps1": "Write-Output 'maintainer wrapper'\n",
        "setup-mac.sh": "#!/bin/sh\necho install\n",
        "setup-win.ps1": "Write-Output 'install'\n",
        "tests/not-customer-payload.txt": "maintenance only\n",
        "tools/manage_wiki_skills.py": "print('manage')\n",
        "tools/vault_update.py": "print('vault update')\n",
        "tools/joint_update.py": "print('joint update')\n",
        "tools/verify_keyword_runtime.py": "print('runtime probe')\n",
        "release/bundle-release.json": json.dumps(
            {
                "manifest_format": 1,
                "release_state": "unreleased_candidate",
                "bundle_version": None,
                "product_schema_version": "1.0.0",
                "wiki_skills_version": "2.0.1",
                "repositories": {
                    "product": "example/product",
                    "wiki_skills": "example/skills",
                    "installer": "example/installer",
                },
            }
        ) + "\n",
    }
    files.update(synthetic_runtime_files())
    return files


def product_contract_files():
    return {
        "AGENTS.md": "agents\n",
        "CLAUDE.md": "product\n",
        "schema/daily-review-rules.md": "daily\n",
        "schema/domain-rules.md": "domain\n",
        "schema/lint-rules.md": "lint\n",
        "schema/runtime-contract.json": json.dumps({"schema_version": "1.0.0"}) + "\n",
        "schema/templates.md": "templates\n",
        "schema/update-policy.json": "{}\n",
    }


def skill_contract_files():
    return {
        "core/design-juan-wiki/SKILL.md": "design\n",
        "core/wiki-hybrid-search/SKILL.md": "query\n",
        "core/ocr-and-documents/SKILL.md": "ocr\n",
        "COMPATIBILITY.json": json.dumps({"runtime_version": "2.0.1"}) + "\n",
    }


class InstallCandidateCliTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("pwsh"), "需要 PowerShell 7")
    def test_powershell_wrapper_forces_utf8_for_python_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            product, _, _ = make_repo(root, "product")
            skill, skill_commit, _ = make_repo(root, "skill")
            installer, installer_commit, _ = make_repo(root, "installer")
            environment = os.environ.copy()
            environment.pop("PYTHONUTF8", None)
            environment.pop("PYTHONIOENCODING", None)
            failed = subprocess.run(
                [
                    "pwsh", "-NoProfile", "-File", str(WRAPPER), "plan",
                    "--product-repo", str(product), "--product-ref", "HEAD",
                    "--skill-repo", str(skill), "--skill-ref", skill_commit,
                    "--installer-repo", str(installer), "--installer-ref", installer_commit,
                    "--platform", "windows", "--output", str(root / "plan.json"),
                ],
                check=False,
                text=True,
                encoding="utf-8",
                capture_output=True,
                env=environment,
            )
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("40 位", failed.stderr)

    def test_plan_rejects_branch_and_abbreviated_refs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            product, product_commit, _ = make_repo(root, "product")
            skill, skill_commit, _ = make_repo(root, "skill")
            installer, installer_commit, _ = make_repo(root, "installer")
            for bad_ref in ("HEAD", product_commit[:12]):
                with self.subTest(ref=bad_ref):
                    output = root / f"{bad_ref}.json"
                    failed = run(
                        sys.executable, CLI, "plan",
                        "--product-repo", product, "--product-ref", bad_ref,
                        "--skill-repo", skill, "--skill-ref", skill_commit,
                        "--installer-repo", installer, "--installer-ref", installer_commit,
                        "--platform", "windows", "--output", output,
                        check=False,
                    )
                    self.assertNotEqual(failed.returncode, 0)
                    self.assertIn("40 位", failed.stderr)
                    self.assertFalse(output.exists())

    def test_plan_binds_exact_commits_trees_and_platform(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            product, product_commit, product_tree = make_repo(root, "product")
            skill, skill_commit, skill_tree = make_repo(root, "skill")
            installer, installer_commit, installer_tree = make_repo(root, "installer")
            output = root / "plan.json"

            run(
                sys.executable,
                CLI,
                "plan",
                "--product-repo",
                product,
                "--product-ref",
                product_commit,
                "--skill-repo",
                skill,
                "--skill-ref",
                skill_commit,
                "--installer-repo",
                installer,
                "--installer-ref",
                installer_commit,
                "--platform",
                "windows",
                "--output",
                output,
            )

            plan = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(plan["schema_version"], 1)
            self.assertEqual(plan["platform"], "windows")
            self.assertEqual(
                plan["sources"],
                {
                    "installer": {
                        "commit": installer_commit,
                        "repo": str(installer.resolve()),
                        "tree": installer_tree,
                    },
                    "product": {
                        "commit": product_commit,
                        "repo": str(product.resolve()),
                        "tree": product_tree,
                    },
                    "skill": {
                        "commit": skill_commit,
                        "repo": str(skill.resolve()),
                        "tree": skill_tree,
                    },
                },
            )

    def test_build_uses_only_git_objects_and_is_reproducible(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            product, product_commit, _ = make_repo(
                root,
                "product",
                {
                    "AGENTS.md": "agents\n",
                    "CLAUDE.md": "committed product\n",
                    "schema/daily-review-rules.md": "daily\n",
                    "schema/domain-rules.md": "domain\n",
                    "schema/lint-rules.md": "lint\n",
                    "schema/runtime-contract.json": json.dumps(
                        {"schema_version": "1.0.0"}
                    ) + "\n",
                    "schema/templates.md": "templates\n",
                    "schema/update-policy.json": "{}\n",
                },
            )
            skill, skill_commit, _ = make_repo(
                root,
                "skill",
                {
                    "core/design-juan-wiki/SKILL.md": "design\n",
                    "core/wiki-hybrid-search/SKILL.md": "query\n",
                    "core/ocr-and-documents/SKILL.md": "ocr\n",
                    "COMPATIBILITY.json": json.dumps(
                        {"runtime_version": "2.0.1"}
                    ) + "\n",
                    "references/shared.md": "shared\n",
                },
            )
            installer, installer_commit, _ = make_repo(
                root,
                "installer",
                installer_contract_files(),
            )
            plan = root / "plan.json"
            run(
                sys.executable,
                CLI,
                "plan",
                "--product-repo",
                product,
                "--product-ref",
                product_commit,
                "--skill-repo",
                skill,
                "--skill-ref",
                skill_commit,
                "--installer-repo",
                installer,
                "--installer-ref",
                installer_commit,
                "--platform",
                "windows",
                "--output",
                plan,
            )

            # 工作树被污染后，候选仍必须来自已绑定的 Git blob。
            (product / "CLAUDE.md").write_text("dirty product\n", encoding="utf-8")
            first = root / "candidate-one"
            second = root / "candidate-two"
            run(sys.executable, CLI, "build", "--plan", plan, "--staging", first)
            run(sys.executable, CLI, "build", "--plan", plan, "--staging", second)

            self.assertEqual(
                (first / "payload/vault/CLAUDE.md").read_text(encoding="utf-8"),
                "committed product\n",
            )
            self.assertTrue(
                (first / "payload/skills/claudecode-wiki-skills/references/shared.md").is_file()
            )
            self.assertTrue((first / "payload/installer/setup-win.ps1").is_file())
            self.assertFalse((first / "payload/installer/setup-mac.sh").exists())
            self.assertFalse(
                (first / "payload/installer/scripts/install-candidate.ps1").exists()
            )
            self.assertFalse((first / "payload/installer/tests").exists())
            self.assertEqual(
                hashlib.sha256((first / "candidate.zip").read_bytes()).hexdigest(),
                hashlib.sha256((second / "candidate.zip").read_bytes()).hexdigest(),
            )
            for archive_path in (first / "candidate.zip", first / "vault.zip"):
                with zipfile.ZipFile(archive_path) as archive:
                    self.assertTrue(
                        all(
                            item.compress_type == zipfile.ZIP_STORED
                            for item in archive.infolist()
                        ),
                        f"{archive_path.name} 必须使用跨 zlib 版本稳定的 ZIP_STORED",
                    )
            deploy = json.loads((first / "deploy-manifest.json").read_text(encoding="utf-8"))
            vault_archive = (first / "vault.zip").read_bytes()
            self.assertEqual(
                deploy["archive"],
                {
                    "sha256": hashlib.sha256(vault_archive).hexdigest(),
                    "size": len(vault_archive),
                },
            )
            tree_input = b"".join(
                (
                    item["path"].encode("utf-8")
                    + b"\0"
                    + str(item["size"]).encode("ascii")
                    + b"\0"
                    + item["sha256"].encode("ascii")
                    + b"\n"
                )
                for item in sorted(deploy["vault"]["files"], key=lambda item: item["path"])
            )
            self.assertEqual(
                deploy["vault"]["tree_sha256"], hashlib.sha256(tree_input).hexdigest()
            )
            with zipfile.ZipFile(first / "vault.zip") as archive:
                self.assertTrue(archive.namelist())
                self.assertTrue(all(name.startswith("vault/") for name in archive.namelist()))
            extracted = root / "customer-extracted"
            with zipfile.ZipFile(first / "candidate.zip") as archive:
                names = archive.namelist()
                archive.extractall(extracted)
            for required in (
                "manifest.json",
                "deploy-manifest.json",
                "vault.zip",
                "setup-win.ps1",
                "install.bat",
                "change-model.bat",
                "change-model.ps1",
                "activation-public-key.xml",
                "revoked-activation-ids.txt",
                "extract-vault.py",
                "tools/manage_wiki_skills.py",
                "tools/vault_update.py",
                "tools/joint_update.py",
                "scripts/manage-wiki-skills.ps1",
                "scripts/manage-wiki-skills.sh",
                "scripts/vault-update.ps1",
                "scripts/vault-update.sh",
                "scripts/joint-update.ps1",
                "scripts/joint-update.sh",
                "contracts/deploy-manifest.schema.json",
                "contracts/product-state.schema.json",
                "contracts/joint-update-plan.schema.json",
                "contracts/joint-update-approval.schema.json",
                "contracts/joint-update-receipt.schema.json",
                "contracts/legacy-catalog.schema.json",
                "contracts/legacy-adoption-plan.schema.json",
                "contracts/legacy-adoption-approval.schema.json",
                "contracts/legacy-adoption-receipt.schema.json",
                "bundle-manifest.json",
                "runtime/SBOM.json",
                "runtime/THIRD-PARTY-NOTICES.md",
                "runtime/runtime-install-manifest.json",
                "runtime/targets/windows-x64/python/python.exe",
                "runtime/targets/windows-x64/python/Lib/site-packages/fixture_dep/__init__.py",
                "runtime/targets/windows-x64/python/Lib/site-packages/jieba/__init__.py",
                "skills/claudecode-wiki-skills/core/design-juan-wiki/SKILL.md",
            ):
                self.assertTrue((extracted / required).is_file(), required)
            bundle_manifest = json.loads(
                (extracted / "bundle-manifest.json").read_text(encoding="utf-8")
            )
            manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["candidate_id"], bundle_manifest["candidate_id"])
            self.assertEqual(
                manifest["sources"]["product"]["commit"],
                bundle_manifest["components"]["product"]["commit"],
            )
            self.assertEqual(
                manifest["sources"]["skill"]["commit"],
                bundle_manifest["components"]["wiki_skills"]["commit"],
            )
            self.assertEqual("unreleased_candidate", bundle_manifest["release_state"])
            self.assertIsNone(bundle_manifest["bundle_version"])
            self.assertFalse(any(name.startswith("payload/") for name in names))
            self.assertFalse(any(name.startswith("vault/") for name in names))
            self.assertTrue(
                any(name.startswith("runtime/licenses/windows-x64/fixture-dep/") for name in names)
            )
            self.assertFalse(
                any("dist-info/licenses/vendor/deeply/nested" in name for name in names)
            )

            mac_plan = root / "mac-plan.json"
            mac_staging = root / "candidate-macos"
            run(
                sys.executable, CLI, "plan",
                "--product-repo", product, "--product-ref", product_commit,
                "--skill-repo", skill, "--skill-ref", skill_commit,
                "--installer-repo", installer, "--installer-ref", installer_commit,
                "--platform", "macos", "--output", mac_plan,
            )
            run(sys.executable, CLI, "build", "--plan", mac_plan, "--staging", mac_staging)
            with zipfile.ZipFile(mac_staging / "candidate.zip") as archive:
                mac_names = set(archive.namelist())
            self.assertIn("setup-mac.sh", mac_names)
            self.assertIn("change-model.sh", mac_names)
            self.assertNotIn("setup-win.ps1", mac_names)
            self.assertNotIn("install.bat", mac_names)
            self.assertNotIn("change-model.ps1", mac_names)
            self.assertIn(
                "runtime/targets/macos-x64/python/bin/python3", mac_names
            )
            self.assertIn(
                "runtime/targets/macos-arm64/python/bin/python3", mac_names
            )
            self.assertRegex(manifest["candidate_id"], r"^[0-9a-f]{64}$")
            self.assertEqual(manifest["default_skills"], [
                "design-juan-wiki",
                "wiki-hybrid-search",
                "ocr-and-documents",
            ])
            self.assertEqual(manifest["optional_skills"], ["ima-skill"])
            self.assertNotIn("repo", manifest["sources"]["product"])

    def test_verify_detects_payload_and_zip_byte_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            product, product_commit, _ = make_repo(
                root,
                "product",
                product_contract_files(),
            )
            skill, skill_commit, _ = make_repo(
                root,
                "skill",
                skill_contract_files(),
            )
            installer, installer_commit, _ = make_repo(
                root,
                "installer",
                installer_contract_files(),
            )
            plan = root / "plan.json"
            staging = root / "candidate"
            run(
                sys.executable, CLI, "plan",
                "--product-repo", product, "--product-ref", product_commit,
                "--skill-repo", skill, "--skill-ref", skill_commit,
                "--installer-repo", installer, "--installer-ref", installer_commit,
                "--platform", "windows", "--output", plan,
            )
            run(sys.executable, CLI, "build", "--plan", plan, "--staging", staging)
            run(sys.executable, CLI, "verify", "--staging", staging)

            payload = staging / "payload/vault/CLAUDE.md"
            original_payload = payload.read_bytes()
            payload.write_bytes(original_payload + b"tampered")
            failed = run(
                sys.executable, CLI, "verify", "--staging", staging, check=False
            )
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("不匹配", failed.stderr)

            payload.write_bytes(original_payload)
            archive = staging / "candidate.zip"
            archive.write_bytes(archive.read_bytes() + b"tampered")
            failed = run(
                sys.executable, CLI, "verify", "--staging", staging, check=False
            )
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("candidate.zip", failed.stderr)

    def test_build_blocks_legacy_archive_lfs_secrets_private_keys_and_links(self):
        bad_files = {
            "legacy_archive": ("installer", "vault.zip", "old archive"),
            "lfs_pointer": (
                "product",
                "large.bin",
                "version https://git-lfs.github.com/spec/v1\n"
                "oid sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
                "size 123\n",
            ),
            "download_token": (
                "installer",
                "download.txt",
                "https://gitee.example/file?access_token="
                "abcdefghijklmnop"
                "qrstuvwxyz123456\n",
            ),
            "quoted_token": (
                "installer",
                "download.ps1",
                "$GITEE_TOKEN = '"
                "abcdefghijklmnop"
                "qrstuvwxyz123456'\n",
            ),
            "private_key_path": (
                "installer",
                "activation-private-key.xml",
                "must never ship\n",
            ),
            "private_key_content": (
                "installer",
                "issuer-note.txt",
                "-----BEGIN "
                "PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----\n",
            ),
        }
        for case, (source_name, bad_path, bad_content) in bad_files.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                product_files = product_contract_files()
                skill_files = skill_contract_files()
                installer_files = installer_contract_files()
                {"product": product_files, "skill": skill_files, "installer": installer_files}[
                    source_name
                ][bad_path] = bad_content
                product, product_commit, _ = make_repo(root, "product", product_files)
                skill, skill_commit, _ = make_repo(root, "skill", skill_files)
                installer, installer_commit, _ = make_repo(root, "installer", installer_files)
                plan = root / "plan.json"
                run(
                    sys.executable, CLI, "plan",
                    "--product-repo", product, "--product-ref", product_commit,
                    "--skill-repo", skill, "--skill-ref", skill_commit,
                    "--installer-repo", installer, "--installer-ref", installer_commit,
                    "--platform", "windows", "--output", plan,
                )
                staging = root / "candidate"
                failed = run(
                    sys.executable, CLI, "build", "--plan", plan,
                    "--staging", staging, check=False,
                )
                self.assertNotEqual(failed.returncode, 0)
                self.assertFalse(staging.exists())

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            product, product_commit, _ = make_repo(
                root,
                "product",
                product_contract_files(),
            )
            skill, skill_commit, _ = make_repo(
                root,
                "skill",
                skill_contract_files(),
            )
            installer, _, _ = make_repo(
                root,
                "installer",
                installer_contract_files(),
            )
            blob = subprocess.run(
                ["git", "-C", str(installer), "hash-object", "-w", "--stdin"],
                input=b"target",
                check=True,
                capture_output=True,
            ).stdout.decode("ascii").strip()
            run("git", "-C", installer, "update-index", "--add", "--cacheinfo", f"120000,{blob},link")
            run("git", "-C", installer, "commit", "-qm", "add link")
            installer_commit = run("git", "-C", installer, "rev-parse", "HEAD").stdout.strip()
            plan = root / "plan.json"
            run(
                sys.executable, CLI, "plan",
                "--product-repo", product, "--product-ref", product_commit,
                "--skill-repo", skill, "--skill-ref", skill_commit,
                "--installer-repo", installer, "--installer-ref", installer_commit,
                "--platform", "windows", "--output", plan,
            )
            staging = root / "candidate"
            failed = run(
                sys.executable, CLI, "build", "--plan", plan,
                "--staging", staging, check=False,
            )
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("mode=120000", failed.stderr)
            self.assertFalse(staging.exists())

    def test_verify_blocks_traversal_absolute_paths_and_case_collisions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            product, product_commit, _ = make_repo(
                root,
                "product",
                product_contract_files(),
            )
            skill, skill_commit, _ = make_repo(
                root,
                "skill",
                skill_contract_files(),
            )
            installer, installer_commit, _ = make_repo(
                root,
                "installer",
                installer_contract_files(),
            )
            plan = root / "plan.json"
            original = root / "candidate-original"
            run(
                sys.executable, CLI, "plan",
                "--product-repo", product, "--product-ref", product_commit,
                "--skill-repo", skill, "--skill-ref", skill_commit,
                "--installer-repo", installer, "--installer-ref", installer_commit,
                "--platform", "windows", "--output", plan,
            )
            run(sys.executable, CLI, "build", "--plan", plan, "--staging", original)

            for case in ("traversal", "absolute", "collision", "unicode_collision"):
                with self.subTest(case=case):
                    staging = root / f"candidate-{case}"
                    shutil.copytree(original, staging)
                    manifest_path = staging / "manifest.json"
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if case == "traversal":
                        manifest["files"][0]["path"] = "payload/../escape"
                    elif case == "absolute":
                        manifest["files"][0]["path"] = "/absolute/escape"
                    elif case == "collision":
                        manifest["files"][1]["path"] = manifest["files"][0]["path"].upper()
                    else:
                        manifest["files"][0]["path"] = "payload/vault/caf\u00e9.md"
                        manifest["files"][1]["path"] = "payload/vault/cafe\u0301.md"
                    identity = dict(manifest)
                    identity.pop("candidate_id")
                    manifest["candidate_id"] = hashlib.sha256(
                        json.dumps(
                            identity,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                    manifest_path.write_bytes(
                        (
                            json.dumps(
                                manifest, ensure_ascii=False, indent=2, sort_keys=True
                            )
                            + "\n"
                        ).encode("utf-8")
                    )
                    failed = run(
                        sys.executable, CLI, "verify", "--staging", staging, check=False
                    )
                    self.assertNotEqual(failed.returncode, 0)
                    if case in ("collision", "unicode_collision"):
                        self.assertIn("碰撞", failed.stderr)


if __name__ == "__main__":
    unittest.main()

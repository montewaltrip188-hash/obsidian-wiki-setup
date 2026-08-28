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


def run(*args, cwd=None):
    return subprocess.run(
        [str(arg) for arg in args],
        cwd=cwd,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
        env={**os.environ, "PYTHONUTF8": "1"},
    )


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@unittest.skipUnless(os.environ.get("D0_PRODUCT_REPO"), "需要显式提供真实三仓库基线")
class RealCandidateFlowTest(unittest.TestCase):
    def test_real_commits_build_deploy_install_verify_and_uninstall(self):
        product_repo = Path(os.environ["D0_PRODUCT_REPO"])
        skill_repo = Path(os.environ["D0_SKILL_REPO"])
        product_ref = os.environ["D0_PRODUCT_REF"]
        skill_ref = os.environ["D0_SKILL_REF"]
        installer_ref = run("git", "-C", REPO_ROOT, "rev-parse", "HEAD").stdout.strip()
        builder = REPO_ROOT / "tools" / "install_candidate.py"

        for platform, setup_entry in (
            ("windows", "setup-win.ps1"),
            ("macos", "setup-mac.sh"),
        ):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory(
                prefix=f"d0-real-{platform}-"
            ) as temporary:
                root = Path(temporary)
                plan = root / "plan.json"
                run(
                    sys.executable,
                    builder,
                    "plan",
                    "--product-repo",
                    product_repo,
                    "--product-ref",
                    product_ref,
                    "--skill-repo",
                    skill_repo,
                    "--skill-ref",
                    skill_ref,
                    "--installer-repo",
                    REPO_ROOT,
                    "--installer-ref",
                    installer_ref,
                    "--platform",
                    platform,
                    "--output",
                    plan,
                )

                candidates = []
                for index in (1, 2):
                    staging = root / f"candidate-{index}"
                    run(sys.executable, builder, "build", "--plan", plan, "--staging", staging)
                    run(sys.executable, builder, "verify", "--staging", staging)
                    candidates.append(staging)

                self.assertEqual(
                    sha256(candidates[0] / "candidate.zip"),
                    sha256(candidates[1] / "candidate.zip"),
                )
                self.assertEqual(
                    sha256(candidates[0] / "vault.zip"),
                    sha256(candidates[1] / "vault.zip"),
                )

                package = root / "package"
                with zipfile.ZipFile(candidates[0] / "candidate.zip") as archive:
                    archive.extractall(package)
                for required in (
                    setup_entry,
                    "vault.zip",
                    "deploy-manifest.json",
                    "extract-vault.py",
                    "tools/manage_wiki_skills.py",
                    "skills/claudecode-wiki-skills/VERSION",
                ):
                    self.assertTrue((package / required).exists(), required)

                vault_target = root / "vault-target"
                deploy_receipt = root / "deploy-receipt.json"
                run(
                    sys.executable,
                    package / "extract-vault.py",
                    "deploy",
                    "--archive",
                    package / "vault.zip",
                    "--manifest",
                    package / "deploy-manifest.json",
                    "--target",
                    vault_target,
                    "--receipt",
                    deploy_receipt,
                )
                self.assertEqual(
                    json.loads(deploy_receipt.read_text(encoding="utf-8"))["status"],
                    "completed",
                )
                self.assertTrue((vault_target / "CLAUDE.md").is_file())
                self.assertTrue((vault_target / "schema" / "domain-rules.md").is_file())

                home = root / "home"
                manager = package / "tools" / "manage_wiki_skills.py"
                source = package / "skills" / "claudecode-wiki-skills"
                plan_result = json.loads(
                    run(
                        sys.executable,
                        manager,
                        "plan",
                        "--source",
                        source,
                        "--home",
                        home,
                    ).stdout
                )
                self.assertEqual(plan_result["action"], "install")
                install_result = json.loads(
                    run(
                        sys.executable,
                        manager,
                        "install",
                        "--source",
                        source,
                        "--home",
                        home,
                    ).stdout
                )
                self.assertFalse(install_result["keyword_runtime_ready"])
                self.assertEqual(
                    install_result["keyword_runtime_error"],
                    "KEYWORD_RUNTIME_UNPROVISIONED",
                )
                verify_result = json.loads(
                    run(sys.executable, manager, "verify", "--home", home).stdout
                )
                self.assertEqual(
                    verify_result["skills"],
                    ["design-juan-wiki", "wiki-hybrid-search", "ocr-and-documents"],
                )
                run(sys.executable, manager, "uninstall", "--home", home)
                self.assertFalse(
                    (home / ".agents" / "packages" / "claudecode-wiki-skills").exists()
                )


if __name__ == "__main__":
    unittest.main()

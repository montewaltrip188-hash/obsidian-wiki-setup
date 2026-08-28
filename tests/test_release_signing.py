from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SIGN = ROOT / "release" / "sign-manifest.ps1"
VERIFY = ROOT / "release" / "verify-manifest.ps1"


def pwsh(*args: object, expected: int = 0) -> dict:
    completed = subprocess.run(
        ["pwsh", "-NoProfile", *map(str, args)],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        env={**os.environ, "PYTHONUTF8": "1"},
        check=False,
    )
    if completed.returncode != expected:
        raise AssertionError(completed.stderr or completed.stdout)
    stream = completed.stdout if expected == 0 else completed.stderr
    return json.loads(stream)


def create_test_keypair(root: Path, bits: int = 3072) -> tuple[Path, Path]:
    private_key = root / "test-private.pem"
    public_key = root / "test-public.pem"
    command = (
        f"$rsa=[Security.Cryptography.RSA]::Create({bits});"
        f"[IO.File]::WriteAllText('{private_key}',"
        "$rsa.ExportPkcs8PrivateKeyPem(),[Text.UTF8Encoding]::new($false));"
        f"[IO.File]::WriteAllText('{public_key}',"
        "$rsa.ExportSubjectPublicKeyInfoPem(),[Text.UTF8Encoding]::new($false));"
        "$rsa.Dispose()"
    )
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr or completed.stdout)
    return private_key, public_key


def public_key_id(public_key: Path) -> str:
    command = (
        "$rsa=[Security.Cryptography.RSA]::Create();"
        f"$rsa.ImportFromPem([IO.File]::ReadAllText('{public_key}'));"
        "$value=[Convert]::ToHexString("
        "[Security.Cryptography.SHA256]::HashData("
        "$rsa.ExportSubjectPublicKeyInfo())).ToLowerInvariant();"
        "$rsa.Dispose();[Console]::Out.Write($value)"
    )
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr or completed.stdout)
    return completed.stdout.strip()


@unittest.skipUnless(os.name == "nt", "发布签名维护端当前使用 Windows .NET RSA")
class ReleaseSigningTests(unittest.TestCase):
    def test_external_rsa_key_signs_manifest_and_tamper_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="d3-signing-") as temporary:
            root = Path(temporary)
            private_key, public_key = create_test_keypair(root)
            manifest = root / "release-manifest.json"
            signature = root / "release-manifest.sig"
            manifest.write_text(
                '{"bundle_version":"2.1.0","manifest_format":1}\n',
                encoding="utf-8",
            )

            signed = pwsh(
                "-File", SIGN,
                "-ManifestPath", manifest,
                "-SignaturePath", signature,
                "-PrivateKeyPath", private_key,
            )
            verified = pwsh(
                "-File", VERIFY,
                "-ManifestPath", manifest,
                "-SignaturePath", signature,
                "-PublicKeyPath", public_key,
            )

            self.assertEqual("signed", signed["status"])
            self.assertEqual("verified", verified["status"])
            self.assertEqual(signed["key_id"], verified["key_id"])
            self.assertRegex(signed["key_id"], r"^[0-9a-f]{64}$")
            manifest.write_text('{"bundle_version":"2.1.1"}\n', encoding="utf-8")
            blocked = pwsh(
                "-File", VERIFY,
                "-ManifestPath", manifest,
                "-SignaturePath", signature,
                "-PublicKeyPath", public_key,
                expected=2,
            )
            self.assertEqual("blocked", blocked["status"])
            self.assertEqual("RELEASE_SIGNATURE_INVALID", blocked["error"])

    def test_repository_ignores_release_private_keys_but_not_public_key(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("release-signing-private", ignore)
        self.assertNotIn("release-signing-public-key.pem", ignore)

    def test_signer_rejects_rsa_key_smaller_than_3072_bits(self):
        with tempfile.TemporaryDirectory(prefix="d3-weak-signing-") as temporary:
            root = Path(temporary)
            private_key, _ = create_test_keypair(root, bits=2048)
            manifest = root / "release-manifest.json"
            manifest.write_text("{}\n", encoding="utf-8")
            blocked = pwsh(
                "-File", SIGN,
                "-ManifestPath", manifest,
                "-SignaturePath", root / "release-manifest.sig",
                "-PrivateKeyPath", private_key,
                expected=2,
            )
            self.assertEqual("RELEASE_RSA_KEY_TOO_SMALL", blocked["error"])

    def test_signer_accepts_external_private_key_on_another_volume(self):
        other_roots = [
            Path(f"{letter}:\\")
            for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ"
            if Path(f"{letter}:\\").is_dir()
            and Path(f"{letter}:\\").drive.casefold() != ROOT.drive.casefold()
        ]
        if not other_roots:
            self.skipTest("没有第二个可写文件卷")
        with tempfile.TemporaryDirectory(
            prefix="d3-cross-volume-signing-", dir=other_roots[0]
        ) as temporary:
            root = Path(temporary)
            private_key, _ = create_test_keypair(root)
            manifest = root / "release-manifest.json"
            manifest.write_text("{}\n", encoding="utf-8")
            signed = pwsh(
                "-File", SIGN,
                "-ManifestPath", manifest,
                "-SignaturePath", root / "release-manifest.sig",
                "-PrivateKeyPath", private_key,
            )
            self.assertEqual("signed", signed["status"])

    def test_macos_verifier_uses_public_key_and_rsa_sha256(self):
        verifier = (ROOT / "release" / "verify-manifest.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('openssl dgst -sha256 -verify "$public_key"', verifier)
        self.assertIn("openssl rsa -pubin", verifier)
        self.assertIn("RELEASE_RSA_KEY_TOO_SMALL", verifier)
        self.assertNotIn("private_key", verifier)


if __name__ == "__main__":
    unittest.main()

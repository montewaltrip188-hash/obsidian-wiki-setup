from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_KEY_ID = "c1f596094a9a54ada888502a2ab7ef6bc5fecf82d4281dd4bbae2ae7bc9d9938"
EXPECTED_PEM_RAW_SHA256 = "a350fcd7160d8ca6a06d73da95061ae026424eed5b4cfb13dd21eec8cd465d3b"
EXPECTED_XML_RAW_SHA256 = "3ab5cb740f3e92d3230561fe231f0f761e5fa1c3c058483a2ed2a48071b4245b"

import sys

sys.path.insert(0, str(ROOT / "release"))
import promote_stable  # noqa: E402


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def raw_text_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def read_tlv(data: bytes, offset: int) -> tuple[int, bytes, int]:
    tag = data[offset]
    length_octet = data[offset + 1]
    cursor = offset + 2
    if length_octet & 0x80:
        width = length_octet & 0x7F
        length = int.from_bytes(data[cursor : cursor + width], "big")
        cursor += width
    else:
        length = length_octet
    end = cursor + length
    if end > len(data):
        raise ValueError("DER 长度越界")
    return tag, data[cursor:end], end


def parse_rsa_spki(pem_path: Path) -> tuple[bytes, int]:
    lines = [
        line.strip()
        for line in pem_path.read_text(encoding="ascii").splitlines()
        if not line.startswith("-----")
    ]
    der = base64.b64decode("".join(lines), validate=True)
    outer_tag, outer, outer_end = read_tlv(der, 0)
    if outer_tag != 0x30 or outer_end != len(der):
        raise ValueError("公钥不是规范 SPKI")
    algorithm_tag, _, cursor = read_tlv(outer, 0)
    bit_tag, bit_string, cursor = read_tlv(outer, cursor)
    if algorithm_tag != 0x30 or bit_tag != 0x03 or cursor != len(outer) or bit_string[:1] != b"\x00":
        raise ValueError("公钥 SPKI 结构无效")
    rsa_tag, rsa_body, rsa_end = read_tlv(bit_string[1:], 0)
    if rsa_tag != 0x30 or rsa_end != len(bit_string) - 1:
        raise ValueError("RSA 公钥结构无效")
    modulus_tag, modulus, cursor = read_tlv(rsa_body, 0)
    exponent_tag, exponent, cursor = read_tlv(rsa_body, cursor)
    if modulus_tag != 0x02 or exponent_tag != 0x02 or cursor != len(rsa_body):
        raise ValueError("RSA 参数结构无效")
    return der, int.from_bytes(modulus, "big"), int.from_bytes(exponent, "big")


class StableReleaseTests(unittest.TestCase):
    def test_promoter_cli_writes_pointer_with_python_39_compatible_create_only_io(self):
        pointer = {
            "bundle_version": "2.1.0",
            "manifest": {"sha256": "a" * 64},
            "tag": "v2.1.0",
        }
        with tempfile.TemporaryDirectory(prefix="stable-pointer-cli-") as temporary:
            output = Path(temporary) / "stable.json"
            argv = [
                "promote_stable.py",
                "--release-dir",
                temporary,
                "--tag",
                "v2.1.0",
                "--output",
                str(output),
            ]
            with mock.patch.object(promote_stable, "build_pointer", return_value=pointer), mock.patch.object(
                sys, "argv", argv
            ):
                self.assertEqual(0, promote_stable.main())
            self.assertEqual(pointer, json.loads(output.read_text(encoding="utf-8")))
            with mock.patch.object(promote_stable, "build_pointer", return_value=pointer), mock.patch.object(
                sys, "argv", argv
            ):
                self.assertEqual(2, promote_stable.main())

    def test_public_pem_xml_and_downloaders_pin_the_same_production_key(self):
        pem = ROOT / "release" / "release-signing-public-key.pem"
        xml = ROOT / "release" / "release-signing-public-key.xml"
        self.assertEqual(EXPECTED_PEM_RAW_SHA256, raw_text_sha256(pem))
        self.assertEqual(EXPECTED_XML_RAW_SHA256, raw_text_sha256(xml))
        der, pem_modulus, pem_exponent = parse_rsa_spki(pem)
        self.assertEqual(EXPECTED_KEY_ID, hashlib.sha256(der).hexdigest())
        xml_root = ET.fromstring(xml.read_text(encoding="utf-8"))
        xml_modulus = int.from_bytes(
            base64.b64decode(xml_root.findtext("Modulus", "")), "big"
        )
        xml_exponent = int.from_bytes(
            base64.b64decode(xml_root.findtext("Exponent", "")), "big"
        )
        self.assertEqual(pem_modulus, xml_modulus)
        self.assertEqual(pem_exponent, xml_exponent)
        self.assertEqual(65537, xml_exponent)

        windows = (ROOT / "download-win.ps1").read_text(encoding="utf-8")
        macos = (ROOT / "download-mac.sh").read_text(encoding="utf-8")
        for script in (windows, macos):
            self.assertIn(EXPECTED_KEY_ID, script)
            self.assertIn(EXPECTED_PEM_RAW_SHA256, script)
            self.assertIn("release-manifest", script)
            self.assertIn("stable.json", script)
            self.assertNotIn("gitee.com", script)
            self.assertNotIn("access_token", script.casefold())
        self.assertIn(EXPECTED_XML_RAW_SHA256, windows)
        self.assertIn("VerifyData", windows)
        self.assertIn("openssl dgst -sha256 -verify", macos)
        self.assertNotIn('rm -rf "$d"', macos)

    def test_promoter_builds_pointer_only_from_stable_signed_manifest_records(self):
        with tempfile.TemporaryDirectory(prefix="stable-pointer-") as temporary:
            release_dir = Path(temporary)
            assets = release_dir / "assets"
            assets.mkdir()
            windows = assets / "obsidian-llm-wiki-2.1.0-windows-x64.zip"
            macos = assets / "obsidian-llm-wiki-2.1.0-macos-universal.zip"
            windows.write_bytes(b"windows")
            macos.write_bytes(b"macos")
            records = [
                {
                    "path": f"assets/{path.name}",
                    "sha256": sha256(path),
                    "size": path.stat().st_size,
                }
                for path in (windows, macos)
            ]
            manifest = {
                "bundle_version": "2.1.0",
                "files": records,
                "release_state": "stable",
                "required_signature": {"key_id": EXPECTED_KEY_ID},
            }
            (release_dir / "release-manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            (release_dir / "release-manifest.sig").write_bytes(b"signature")
            with mock.patch.object(promote_stable, "verify_release"):
                pointer = promote_stable.build_pointer(release_dir, "v2.1.0")
            self.assertEqual("stable", pointer["release_state"])
            self.assertEqual(EXPECTED_KEY_ID, pointer["trust"]["key_id"])
            self.assertEqual(EXPECTED_PEM_RAW_SHA256, pointer["trust"]["pem"]["sha256"])
            self.assertEqual(624, pointer["trust"]["pem"]["size"])
            self.assertEqual(EXPECTED_XML_RAW_SHA256, pointer["trust"]["xml"]["sha256"])
            self.assertEqual(sha256(windows), pointer["assets"]["windows-x64"]["sha256"])
            self.assertEqual(sha256(macos), pointer["assets"]["macos-universal"]["sha256"])
            self.assertTrue(pointer["manifest"]["url"].endswith("/v2.1.0/release-manifest.json"))

            manifest["release_state"] = "unreleased_candidate"
            (release_dir / "release-manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            with mock.patch.object(promote_stable, "verify_release"), self.assertRaisesRegex(
                promote_stable.PromoteError, "^STABLE_MANIFEST_NOT_PROMOTABLE$"
            ):
                promote_stable.build_pointer(release_dir, "v2.1.0")

    def test_stable_schema_and_release_contract_are_closed(self):
        schema = json.loads(
            (ROOT / "contracts" / "stable-release.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual("stable", schema["properties"]["channel"]["const"])
        self.assertEqual(
            "montewaltrip188-hash/obsidian-wiki-setup",
            schema["properties"]["repository"]["const"],
        )
        bundle = json.loads(
            (ROOT / "release" / "bundle-release.json").read_text(encoding="utf-8")
        )
        self.assertEqual("2.1.0", bundle["bundle_version"])
        self.assertEqual("stable", bundle["release_state"])
        stable = json.loads(
            (ROOT / "release" / "stable.json").read_text(encoding="utf-8")
        )
        self.assertEqual(EXPECTED_PEM_RAW_SHA256, stable["trust"]["pem"]["sha256"])
        self.assertEqual(624, stable["trust"]["pem"]["size"])


if __name__ == "__main__":
    unittest.main()

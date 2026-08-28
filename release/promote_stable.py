#!/usr/bin/env python3
"""从已验证的 stable D3 目录生成客户可见的稳定指针。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path


REPOSITORY = "montewaltrip188-hash/obsidian-wiki-setup"
TAG = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
ROOT = Path(__file__).resolve().parent.parent


class PromoteError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def load_json(path: Path, code: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PromoteError(code) from exc
    if not isinstance(value, dict):
        raise PromoteError(code)
    return value


def verify_release(release_dir: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("d3_release.py")),
            "verify",
            "--release-dir",
            str(release_dir),
        ],
        text=True,
        encoding="utf-8",
        errors="strict",
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise PromoteError("STABLE_D3_VERIFICATION_FAILED")


def file_record(path: Path, name: str, url: str, expected: dict | None = None) -> dict:
    if not path.is_file() or path.is_symlink():
        raise PromoteError("STABLE_FILE_MISSING")
    record = {
        "name": name,
        "sha256": sha256(path),
        "size": path.stat().st_size,
        "url": url,
    }
    if expected and (
        record["sha256"] != expected.get("sha256")
        or record["size"] != expected.get("size")
    ):
        raise PromoteError("STABLE_FILE_DRIFT")
    return record


def raw_text_record(path: Path, name: str, url: str) -> dict:
    """按 Git 文本对象的 LF 规范记录 raw/tag 文件，避免 checkout 换行污染。"""
    if not path.is_file() or path.is_symlink():
        raise PromoteError("STABLE_FILE_MISSING")
    canonical = path.read_bytes().replace(b"\r\n", b"\n")
    if b"\r" in canonical:
        raise PromoteError("STABLE_TEXT_LINE_ENDING_INVALID")
    return {
        "name": name,
        "sha256": hashlib.sha256(canonical).hexdigest(),
        "size": len(canonical),
        "url": url,
    }


def build_pointer(release_dir: Path, tag: str) -> dict:
    if not TAG.fullmatch(tag):
        raise PromoteError("STABLE_TAG_INVALID")
    release_dir = release_dir.resolve(strict=True)
    verify_release(release_dir)
    manifest_path = release_dir / "release-manifest.json"
    signature_path = release_dir / "release-manifest.sig"
    manifest = load_json(manifest_path, "STABLE_MANIFEST_INVALID")
    if (
        manifest.get("bundle_version") != tag.removeprefix("v")
        or manifest.get("release_state") != "stable"
        or not HEX64.fullmatch(
            str(manifest.get("required_signature", {}).get("key_id", ""))
        )
    ):
        raise PromoteError("STABLE_MANIFEST_NOT_PROMOTABLE")
    records = {
        item.get("path"): item
        for item in manifest.get("files", [])
        if isinstance(item, dict)
    }
    version = manifest["bundle_version"]
    windows_name = f"obsidian-llm-wiki-{version}-windows-x64.zip"
    macos_name = f"obsidian-llm-wiki-{version}-macos-universal.zip"
    for required_asset in (f"assets/{windows_name}", f"assets/{macos_name}"):
        if not isinstance(records.get(required_asset), dict):
            raise PromoteError("STABLE_MANIFEST_ASSET_MISSING")
    release_base = f"https://github.com/{REPOSITORY}/releases/download/{tag}"
    raw_base = f"https://raw.githubusercontent.com/{REPOSITORY}/{tag}/release"
    pem_path = ROOT / "release" / "release-signing-public-key.pem"
    xml_path = ROOT / "release" / "release-signing-public-key.xml"
    pointer = {
        "assets": {
            "macos-universal": file_record(
                release_dir / "assets" / macos_name,
                macos_name,
                f"{release_base}/{macos_name}",
                records.get(f"assets/{macos_name}"),
            ),
            "windows-x64": file_record(
                release_dir / "assets" / windows_name,
                windows_name,
                f"{release_base}/{windows_name}",
                records.get(f"assets/{windows_name}"),
            ),
        },
        "bundle_version": version,
        "channel": "stable",
        "manifest": file_record(
            manifest_path,
            "release-manifest.json",
            f"{release_base}/release-manifest.json",
        ),
        "pointer_format": 1,
        "release_state": "stable",
        "repository": REPOSITORY,
        "signature": file_record(
            signature_path,
            "release-manifest.sig",
            f"{release_base}/release-manifest.sig",
        ),
        "tag": tag,
        "trust": {
            "key_id": manifest["required_signature"]["key_id"],
            "pem": raw_text_record(
                pem_path,
                pem_path.name,
                f"{raw_base}/{pem_path.name}",
            ),
            "xml": raw_text_record(
                xml_path,
                xml_path.name,
                f"{raw_base}/{xml_path.name}",
            ),
        },
    }
    return pointer


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--release-dir", required=True, type=Path)
    result.add_argument("--tag", required=True)
    result.add_argument("--output", required=True, type=Path)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        pointer = build_pointer(args.release_dir, args.tag)
        output = args.output.resolve()
        if output.exists():
            raise PromoteError("STABLE_OUTPUT_EXISTS")
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(
                json.dumps(pointer, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            )
        print(
            json.dumps(
                {
                    "bundle_version": pointer["bundle_version"],
                    "manifest_sha256": pointer["manifest"]["sha256"],
                    "status": "stable_pointer_created",
                    "tag": pointer["tag"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (PromoteError, OSError) as exc:
        error = str(exc) if isinstance(exc, PromoteError) else "STABLE_INTERNAL_ERROR"
        print(
            json.dumps({"error": error, "status": "blocked"}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

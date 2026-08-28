#!/usr/bin/env python3
"""经候选清单验证后，以同级 staging 原子部署 Obsidian Vault。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import unicodedata
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class DeployError(Exception):
    """候选包或部署状态不满足安全合同。"""


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CLOCK$",
    "CONIN$",
    "CONOUT$",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DeployError(f"manifest JSON 含重复键：{key}")
        result[key] = value
    return result


def require_exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DeployError(f"manifest {label} 必须是对象")
    actual = set(value)
    if actual != expected:
        raise DeployError(
            f"manifest {label} 字段不符合合同；缺少={sorted(expected - actual)}，额外={sorted(actual - expected)}"
        )
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reject_raw_nul_member_names(path: Path) -> None:
    """在 zipfile 将 NUL 截断前，直接检查中央目录中的原始文件名。"""
    data = path.read_bytes()
    eocd = data.rfind(b"PK\x05\x06", max(0, len(data) - 65557))
    if eocd < 0 or eocd + 22 > len(data):
        raise DeployError("候选归档缺少有效 ZIP 中央目录")
    comment_length = int.from_bytes(data[eocd + 20 : eocd + 22], "little")
    if eocd + 22 + comment_length != len(data):
        raise DeployError("候选归档 EOCD 或注释边界无效")
    if any(data[eocd + offset : eocd + offset + 2] != b"\x00\x00" for offset in (4, 6)):
        raise DeployError("候选归档不得使用多磁盘 ZIP")
    entries = int.from_bytes(data[eocd + 10 : eocd + 12], "little")
    central_size = int.from_bytes(data[eocd + 12 : eocd + 16], "little")
    cursor = int.from_bytes(data[eocd + 16 : eocd + 20], "little")
    if entries == 0xFFFF or central_size == 0xFFFFFFFF or cursor == 0xFFFFFFFF:
        raise DeployError("当前部署器不接受 ZIP64 候选包")
    central_end = cursor + central_size
    if central_end > eocd or cursor < 0:
        raise DeployError("候选归档中央目录边界无效")
    for _ in range(entries):
        if cursor + 46 > central_end or data[cursor : cursor + 4] != b"PK\x01\x02":
            raise DeployError("候选归档中央目录条目无效")
        name_length = int.from_bytes(data[cursor + 28 : cursor + 30], "little")
        extra_length = int.from_bytes(data[cursor + 30 : cursor + 32], "little")
        comment_length = int.from_bytes(data[cursor + 32 : cursor + 34], "little")
        name_start = cursor + 46
        name_end = name_start + name_length
        if name_end > central_end:
            raise DeployError("候选归档文件名越过中央目录边界")
        if b"\x00" in data[name_start:name_end]:
            raise DeployError("候选归档成员路径含 NUL")
        cursor = name_end + extra_length + comment_length
    if cursor != central_end:
        raise DeployError("候选归档中央目录大小不一致")


def tree_digest(records: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in sorted(records, key=lambda entry: entry["path"].encode("utf-8")):
        digest.update(
            f"{item['path']}\0{item['size']}\0{item['sha256']}\n".encode("utf-8")
        )
    return digest.hexdigest()


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=strict_json_object
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeployError(f"无法读取候选 manifest：{exc}") from exc
    if manifest.get("schema_version") != 1:
        raise DeployError("不支持的 manifest schema_version")
    require_exact_keys(manifest, {"schema_version", "archive", "vault"}, "根对象")
    archive = require_exact_keys(manifest["archive"], {"sha256", "size"}, "archive")
    vault = require_exact_keys(manifest["vault"], {"tree_sha256", "files"}, "vault")
    if not isinstance(archive["size"], int) or isinstance(archive["size"], bool) or archive["size"] < 1:
        raise DeployError("manifest archive.size 必须是正整数")
    if not isinstance(archive["sha256"], str) or not SHA256_PATTERN.fullmatch(archive["sha256"]):
        raise DeployError("manifest archive.sha256 必须是 64 位小写十六进制")
    if not isinstance(vault["tree_sha256"], str) or not SHA256_PATTERN.fullmatch(vault["tree_sha256"]):
        raise DeployError("manifest vault.tree_sha256 必须是 64 位小写十六进制")
    return manifest


def validated_relative_path(raw_path: str, *, source: str) -> str:
    if not isinstance(raw_path, str) or not raw_path:
        raise DeployError(f"{source} 含空路径")
    if "\x00" in raw_path:
        raise DeployError(f"{source} 路径含 NUL")
    path = raw_path.replace("\\", "/")
    if path.startswith("/") or path.startswith("//") or re.match(r"^[A-Za-z]:", path):
        raise DeployError(f"{source} 含绝对路径或盘符：{raw_path!r}")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise DeployError(f"{source} 含空段、点段或父目录段：{raw_path!r}")
    for part in parts:
        if any(ord(character) < 32 or ord(character) == 127 for character in part):
            raise DeployError(f"{source} 含控制字符：{raw_path!r}")
        if any(character in '<>:"|?*' for character in part):
            raise DeployError(f"{source} 含 Windows 非法字符：{raw_path!r}")
        if part.endswith((".", " ")):
            raise DeployError(f"{source} 含 Windows 会折叠的尾随点或空格：{raw_path!r}")
        device_name = part.split(".", 1)[0].upper()
        if device_name in WINDOWS_RESERVED_NAMES:
            raise DeployError(f"{source} 含 Windows 保留设备名：{raw_path!r}")
    return "/".join(parts)


def collision_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def validate_archive_members(bundle: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, str]]:
    validated: list[tuple[zipfile.ZipInfo, str]] = []
    seen: dict[str, str] = {}
    file_paths: set[str] = set()
    directory_paths: set[str] = set()
    for info in bundle.infolist():
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(unix_mode)
        if file_type == stat.S_IFLNK:
            raise DeployError(f"归档含符号链接：{info.filename!r}")
        if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise DeployError(f"归档含不支持的特殊文件：{info.filename!r}")

        raw = info.filename.replace("\\", "/")
        if raw in ("vault", "vault/"):
            continue
        if not raw.startswith("vault/"):
            raise DeployError(f"归档成员不在唯一 vault/ 根目录下：{info.filename!r}")
        relative_raw = raw[6:].rstrip("/") if info.is_dir() else raw[6:]
        relative = validated_relative_path(relative_raw, source="归档")
        key = collision_key(relative)
        previous = seen.get(key)
        if previous is not None:
            raise DeployError(f"归档路径重复、大小写或 Unicode 碰撞：{previous!r} / {relative!r}")
        seen[key] = relative
        parts = relative.split("/")
        ancestor_keys = {collision_key("/".join(parts[:index])) for index in range(1, len(parts))}
        if ancestor_keys & file_paths:
            raise DeployError(f"归档文件与子路径冲突：{relative!r}")
        if info.is_dir():
            if key in file_paths:
                raise DeployError(f"归档文件与目录冲突：{relative!r}")
            directory_paths.add(key)
            continue
        if key in directory_paths:
            raise DeployError(f"归档目录与文件冲突：{relative!r}")
        directory_paths.update(ancestor_keys)
        file_paths.add(key)
        validated.append((info, relative))
    return validated


def verified_manifest_records(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    try:
        records = manifest["vault"]["files"]
        expected_tree = manifest["vault"]["tree_sha256"]
    except (KeyError, TypeError) as exc:
        raise DeployError("manifest 缺少 vault 文件树") from exc
    if not isinstance(records, list) or not records:
        raise DeployError("manifest vault.files 必须是非空数组")
    result: dict[str, dict[str, Any]] = {}
    seen: dict[str, str] = {}
    for item in records:
        require_exact_keys(item, {"path", "size", "sha256"}, "vault.files[]")
        try:
            path = item["path"]
            size = item["size"]
            digest = item["sha256"]
        except (KeyError, TypeError) as exc:
            raise DeployError("manifest 文件记录不完整") from exc
        if (
            not isinstance(path, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or not SHA256_PATTERN.fullmatch(digest)
        ):
            raise DeployError("manifest 文件记录类型无效")
        path = validated_relative_path(path, source="manifest")
        key = collision_key(path)
        if key in seen:
            raise DeployError(f"manifest 路径重复、大小写或 Unicode 碰撞：{seen[key]!r} / {path!r}")
        seen[key] = path
        result[path] = {"path": path, "size": size, "sha256": digest}
    if tree_digest(list(result.values())) != str(expected_tree).lower():
        raise DeployError("manifest 文件树摘要不匹配")
    return result


def write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    temp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    temp.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def remove_owned_staging(path: Path, parent: Path, target_name: str) -> None:
    """只清理由本次部署生成、且仍位于已解析同级目录的 staging。"""
    if not path.exists():
        return
    if path.is_symlink() or path.parent.resolve(strict=True) != parent:
        raise DeployError("拒绝清理未解析或越界的 staging")
    if not path.name.startswith(f".{target_name}.staging-"):
        raise DeployError("拒绝清理非本次命名空间的目录")
    shutil.rmtree(path)


def scan_regular_tree(root: Path) -> list[dict[str, Any]]:
    """不跟随链接或 reparse point，生成目录中普通文件的部署摘要记录。"""
    records: list[dict[str, Any]] = []
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                entry_stat = entry.stat(follow_symlinks=False)
                attributes = getattr(entry_stat, "st_file_attributes", 0)
                if entry.is_symlink() or attributes & reparse_flag:
                    raise DeployError(f"文件树含链接或 reparse point：{entry.path}")
                entry_path = Path(entry.path)
                relative = entry_path.relative_to(root).as_posix()
                validated_relative_path(relative, source="已部署 Vault")
                if stat.S_ISDIR(entry_stat.st_mode):
                    pending.append(entry_path)
                elif stat.S_ISREG(entry_stat.st_mode):
                    records.append(
                        {
                            "path": relative,
                            "size": entry_stat.st_size,
                            "sha256": sha256_file(entry_path),
                        }
                    )
                else:
                    raise DeployError(f"文件树含非普通文件：{entry.path}")
    return records


def deploy(args: argparse.Namespace) -> int:
    archive_input = Path(args.archive).expanduser().absolute()
    manifest_input = Path(args.manifest).expanduser().absolute()
    target = Path(args.target).expanduser().absolute()
    if not target.name or target.parent == target:
        raise DeployError("拒绝把文件系统根目录作为 Vault 目标")
    parent = target.parent.resolve(strict=True)
    target = parent / target.name
    if args.receipt:
        receipt_input = Path(args.receipt).expanduser().absolute()
        receipt_parent = receipt_input.parent.resolve(strict=True)
        if receipt_parent != parent:
            raise DeployError("部署回执必须位于目标的已解析同级目录")
        receipt_path = receipt_parent / receipt_input.name
    else:
        receipt_path = parent / f".{target.name}.deploy-receipt-{uuid.uuid4().hex}.json"
    staging = parent / f".{target.name}.staging-{uuid.uuid4().hex}"
    backup: Path | None = None
    switched = False
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "operation": "deploy",
        "status": "failed",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "archive": str(archive_input),
        "manifest": str(manifest_input),
        "target": str(target),
        "staging": str(staging),
        "backup": None,
    }
    try:
        archive = archive_input.resolve(strict=True)
        manifest_path = manifest_input.resolve(strict=True)
        receipt["archive"] = str(archive)
        receipt["manifest"] = str(manifest_path)
        target_exists = target.exists() or target.is_symlink()
        if target_exists and not args.allow_existing:
            raise DeployError("目标已存在；默认拒绝覆盖，必须显式使用 --allow-existing")
        if target_exists and (target.is_symlink() or not target.is_dir()):
            raise DeployError("显式升级只接受非链接的现有 Vault 目录")

        manifest = load_manifest(manifest_path)
        expected_archive = manifest.get("archive", {})
        actual_size = archive.stat().st_size
        actual_archive_digest = sha256_file(archive)
        if expected_archive.get("size") != actual_size:
            raise DeployError("候选归档大小与 manifest 不一致")
        if str(expected_archive.get("sha256", "")).lower() != actual_archive_digest:
            raise DeployError("候选归档 SHA-256 与 manifest 不一致")
        reject_raw_nul_member_names(archive)
        expected_files = verified_manifest_records(manifest)

        actual_files: list[dict[str, Any]] = []
        with zipfile.ZipFile(archive) as bundle:
            validated_members = validate_archive_members(bundle)
            member_by_path = {relative: info for info, relative in validated_members}
            if set(member_by_path) != set(expected_files):
                raise DeployError("归档文件集合与 manifest 不一致")
            for relative, info in member_by_path.items():
                if info.file_size != expected_files[relative]["size"]:
                    raise DeployError(f"归档成员大小与 manifest 不一致：{relative!r}")
            staging.mkdir()
            for info, relative in validated_members:
                destination = staging / Path(relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source, destination.open("wb") as output:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
                actual_files.append(
                    {
                        "path": relative,
                        "size": destination.stat().st_size,
                        "sha256": sha256_file(destination),
                    }
                )
        actual_by_path = {item["path"]: item for item in actual_files}
        if actual_by_path != expected_files:
            raise DeployError("解压后的逐文件树与 manifest 不一致")
        actual_tree = tree_digest(actual_files)
        if actual_tree != manifest["vault"]["tree_sha256"].lower():
            raise DeployError("解压后的文件树摘要与 manifest 不一致")

        if target_exists:
            backup = parent / f".{target.name}.backup-{uuid.uuid4().hex}"
            receipt["backup"] = str(backup)
            receipt["status"] = "switching"
            write_receipt(receipt_path, receipt)
            os.replace(target, backup)
        try:
            os.replace(staging, target)
            switched = True
        except Exception:
            if backup is not None and backup.exists() and not target.exists():
                os.replace(backup, target)
                receipt["restored"] = True
            raise
        receipt.update(
            {
                "status": "completed",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "archive_sha256": actual_archive_digest,
                "tree_sha256": actual_tree,
                "staging": None,
            }
        )
        write_receipt(receipt_path, receipt)
        print(json.dumps({"status": "completed", "target": str(target), "receipt": str(receipt_path)}, ensure_ascii=False))
        return 0
    except Exception as exc:
        if switched and target.exists():
            failed_candidate = parent / f".{target.name}.failed-candidate-{uuid.uuid4().hex}"
            try:
                os.replace(target, failed_candidate)
                receipt["failed_candidate"] = str(failed_candidate)
                if backup is not None and backup.exists():
                    os.replace(backup, target)
                    receipt["restored"] = True
            except Exception as restore_exc:
                receipt["restore_error"] = str(restore_exc)
        try:
            remove_owned_staging(staging, parent, target.name)
            receipt["staging"] = None
        except Exception as cleanup_exc:
            receipt["cleanup_error"] = str(cleanup_exc)
        receipt.update(
            {
                "status": "failed",
                "error": str(exc),
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        write_receipt(receipt_path, receipt)
        print(f"部署失败：{exc}；回执：{receipt_path}", file=sys.stderr)
        return 1


def cleanup_backup(args: argparse.Namespace) -> int:
    target_input = Path(args.target).expanduser().absolute()
    parent = target_input.parent.resolve(strict=True)
    target = parent / target_input.name
    backup_input = Path(args.backup).expanduser().absolute()
    backup = backup_input.parent.resolve(strict=True) / backup_input.name
    deploy_receipt_path = Path(args.deploy_receipt).expanduser().resolve(strict=True)
    cleanup_receipt_path = (
        Path(args.receipt).expanduser().absolute()
        if args.receipt
        else parent / f".{target.name}.cleanup-receipt-{uuid.uuid4().hex}.json"
    )
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "operation": "cleanup_backup",
        "status": "failed",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "target": str(target),
        "backup": str(backup),
        "deploy_receipt": str(deploy_receipt_path),
    }
    quarantine: Path | None = None
    try:
        if backup.parent != parent:
            raise DeployError("backup 与目标不在同一已解析目录")
        if not backup.name.startswith(f".{target.name}.backup-"):
            raise DeployError("backup 不属于目标的受管命名空间")
        if target.is_symlink() or not target.is_dir():
            raise DeployError("当前目标不是可验证的真实目录")
        if backup.is_symlink() or not backup.is_dir():
            raise DeployError("backup 不是可验证的真实目录")
        try:
            deploy_receipt = json.loads(deploy_receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DeployError(f"无法读取部署回执：{exc}") from exc
        if deploy_receipt.get("status") != "completed":
            raise DeployError("部署回执不是 completed 状态")
        receipt_target = Path(str(deploy_receipt.get("target", ""))).resolve(strict=True)
        receipt_backup = Path(str(deploy_receipt.get("backup", ""))).resolve(strict=True)
        if receipt_target != target.resolve(strict=True) or receipt_backup != backup.resolve(strict=True):
            raise DeployError("部署回执与 target/backup 不匹配")
        expected_target_tree = deploy_receipt.get("tree_sha256")
        if not isinstance(expected_target_tree, str) or not SHA256_PATTERN.fullmatch(expected_target_tree):
            raise DeployError("部署回执缺少规范的目标树摘要")
        if tree_digest(scan_regular_tree(target)) != expected_target_tree:
            raise DeployError("当前目标文件树已漂移，拒绝清理唯一回退备份")
        scan_regular_tree(backup)

        quarantine = parent / f".{target.name}.backup-cleanup-{uuid.uuid4().hex}"
        os.replace(backup, quarantine)
        try:
            shutil.rmtree(quarantine)
        except Exception:
            if quarantine.exists() and not backup.exists():
                os.replace(quarantine, backup)
            raise
        receipt.update(
            {
                "status": "completed",
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        write_receipt(cleanup_receipt_path, receipt)
        print(json.dumps({"status": "completed", "receipt": str(cleanup_receipt_path)}, ensure_ascii=False))
        return 0
    except Exception as exc:
        receipt.update({"error": str(exc), "completed_at": datetime.now(timezone.utc).isoformat()})
        try:
            write_receipt(cleanup_receipt_path, receipt)
        except Exception as receipt_exc:
            print(f"备份清理失败：{exc}；回执写入也失败：{receipt_exc}", file=sys.stderr)
            return 1
        print(f"备份清理失败：{exc}；回执：{cleanup_receipt_path}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    deploy_parser = subparsers.add_parser("deploy", help="验证候选包并安全部署 Vault")
    deploy_parser.add_argument("--archive", required=True)
    deploy_parser.add_argument("--manifest", required=True)
    deploy_parser.add_argument("--target", required=True)
    deploy_parser.add_argument("--receipt")
    deploy_parser.add_argument(
        "--allow-existing",
        action="store_true",
        help="显式允许把现有真实目录先原子移动到同级 backup；backup 不自动清理",
    )
    deploy_parser.set_defaults(handler=deploy)

    cleanup_parser = subparsers.add_parser(
        "cleanup-backup",
        help="依据已完成部署回执，显式清理一个保留的同级 backup",
    )
    cleanup_parser.add_argument("--target", required=True)
    cleanup_parser.add_argument("--backup", required=True)
    cleanup_parser.add_argument("--deploy-receipt", required=True)
    cleanup_parser.add_argument("--receipt")
    cleanup_parser.set_defaults(handler=cleanup_backup)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.handler(args)
    except (DeployError, OSError) as exc:
        print(f"安全部署参数无效：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""从精确 Git 对象组装并验证可复现的安装候选。"""

import argparse
import hashlib
import io
import json
import re
import stat
import subprocess
import sys
import tarfile
import unicodedata
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from pathlib import PurePosixPath


FULL_COMMIT = re.compile(r"^[0-9a-fA-F]{40}$")
SOURCE_PREFIXES = {
    "product": "payload/vault",
    "skill": "payload/skills/claudecode-wiki-skills",
    "installer": "payload/installer",
}
REQUIRED_PATHS = {
    "product": {
        "AGENTS.md",
        "CLAUDE.md",
        "schema/daily-review-rules.md",
        "schema/domain-rules.md",
        "schema/lint-rules.md",
        "schema/runtime-contract.json",
        "schema/templates.md",
        "schema/update-policy.json",
    },
    "skill": {
        "core/design-juan-wiki/SKILL.md",
        "core/wiki-hybrid-search/SKILL.md",
        "core/ocr-and-documents/SKILL.md",
        "COMPATIBILITY.json",
    },
    "installer": {"scripts/install-candidate.ps1"},
}
INSTALLER_COMMON_REQUIRED = {
    "activation-public-key.xml",
    "contracts/deploy-manifest.schema.json",
    "contracts/install-candidate.schema.json",
    "contracts/offline-keyword-runtime-bom.json",
    "contracts/offline-keyword-runtime-bom.schema.json",
    "contracts/wiki-skill-lifecycle.json",
    "contracts/runtime-contract.schema.json",
    "contracts/compatibility.schema.json",
    "contracts/bundle-manifest.schema.json",
    "contracts/bundle-release.schema.json",
    "contracts/update-path-policy.schema.json",
    "contracts/product-state.schema.json",
    "contracts/update-plan.schema.json",
    "contracts/update-approval.schema.json",
    "contracts/update-transaction-receipt.schema.json",
    "contracts/joint-update-plan.schema.json",
    "contracts/joint-update-approval.schema.json",
    "contracts/joint-update-receipt.schema.json",
    "contracts/legacy-catalog.schema.json",
    "contracts/legacy-adoption-plan.schema.json",
    "contracts/legacy-adoption-approval.schema.json",
    "contracts/legacy-adoption-receipt.schema.json",
    "extract-vault.py",
    "revoked-activation-ids.txt",
    "scripts/install-candidate.ps1",
    "scripts/manage-wiki-skills.ps1",
    "scripts/manage-wiki-skills.sh",
    "scripts/vault-update.ps1",
    "scripts/vault-update.sh",
    "scripts/joint-update.ps1",
    "scripts/joint-update.sh",
    "tools/manage_wiki_skills.py",
    "tools/vault_update.py",
    "tools/joint_update.py",
    "tools/verify_keyword_runtime.py",
    "release/bundle-release.json",
}
INSTALLER_PLATFORM_REQUIRED = {
    "windows": {
        "change-model.bat",
        "change-model.ps1",
        "install.bat",
        "setup-win.ps1",
    },
    "macos": {"change-model.sh", "setup-mac.sh"},
}
INSTALLER_COMMON_PAYLOAD = {
    "activation-public-key.xml",
    "extract-vault.py",
    "revoked-activation-ids.txt",
    "scripts/manage-wiki-skills.ps1",
    "scripts/manage-wiki-skills.sh",
    "scripts/vault-update.ps1",
    "scripts/vault-update.sh",
    "scripts/joint-update.ps1",
    "scripts/joint-update.sh",
    "tools/manage_wiki_skills.py",
    "tools/vault_update.py",
    "tools/joint_update.py",
    "tools/verify_keyword_runtime.py",
    "release/bundle-release.json",
}
RUNTIME_BOM_PATH = "contracts/offline-keyword-runtime-bom.json"
INSTALLER_PLATFORM_PAYLOAD = {
    "windows": {
        "change-model.bat",
        "change-model.ps1",
        "install.bat",
        "setup-win.ps1",
    },
    "macos": {"change-model.sh", "setup-mac.sh"},
}
SECRET_PATTERNS = (
    re.compile(rb"(?i)(?:access[_-]?token|gitee[_-]?token)\s*(?:=|:)\s*['\"]?[A-Za-z0-9_-]{24,}"),
    re.compile(rb"(?i)[?&]access_token=[A-Za-z0-9_-]{24,}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


class CandidateError(RuntimeError):
    pass


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        encoding="utf-8",
        errors="strict",
        capture_output=True,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise CandidateError(f"Git 命令失败：{detail}")
    return result.stdout.strip()


def git_bytes(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise CandidateError(f"Git 命令失败：{detail}")
    return result.stdout


def resolve_source(repo_text: str, ref: str):
    repo = Path(repo_text).resolve()
    if not FULL_COMMIT.fullmatch(ref):
        raise CandidateError("版本必须是精确的 40 位 Git commit")
    commit = git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if commit.lower() != ref.lower():
        raise CandidateError("版本没有解析为所请求的精确 commit")
    return {
        "commit": commit,
        "repo": str(repo),
        "tree": git(repo, "rev-parse", f"{commit}^{{tree}}"),
    }


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(pretty_json_bytes(value).decode("utf-8"))


def pretty_json_bytes(value) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def canonical_json(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def validate_relative_path(path: str):
    pure = PurePosixPath(path)
    if not path or "\\" in path or pure.is_absolute() or any(
        part in ("", ".", "..") for part in pure.parts
    ):
        raise CandidateError(f"不安全的 Git 路径：{path!r}")
    reserved = {"con", "prn", "aux", "nul"} | {
        f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
    }
    for part in pure.parts:
        stem = part.split(".", 1)[0].casefold()
        if (
            any(ord(character) < 32 or ord(character) == 127 for character in part)
            or ":" in part
            or part.endswith((" ", "."))
            or stem in reserved
        ):
            raise CandidateError(f"跨平台不安全的 Git 路径：{path!r}")


def collision_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def validate_blob(source_name: str, path: str, content: bytes):
    lowered = path.casefold()
    if source_name == "installer" and lowered.endswith(".zip"):
        raise CandidateError(f"安装器仓库禁止跟踪手工归档：{path}")
    basename = PurePosixPath(lowered).name
    if (
        "private-key" in lowered
        or "private_key" in lowered
        or basename == ".env"
        or PurePosixPath(lowered).suffix in (".key", ".pem", ".p12", ".pfx")
    ):
        raise CandidateError(f"私钥路径禁止进入候选：{path}")
    if content.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
        raise CandidateError(f"Git LFS 指针禁止进入候选：{path}")
    if any(pattern.search(content) for pattern in SECRET_PATTERNS):
        raise CandidateError(f"疑似凭据或私钥禁止进入候选：{path}")


def read_git_files(source_name: str, source, platform: str):
    repo = Path(source["repo"])
    commit = source["commit"]
    resolved = resolve_source(str(repo), commit)
    if resolved["tree"] != source["tree"]:
        raise CandidateError(f"{source_name} 的 tree 与计划不一致")
    raw = git_bytes(repo, "ls-tree", "-r", "-z", "--full-tree", commit)
    files = []
    seen = set()
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, object_type, object_id = metadata.decode("ascii").split(" ")
        try:
            path = raw_path.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise CandidateError(f"{source_name} 含非 UTF-8 路径") from exc
        validate_relative_path(path)
        key = collision_key(path)
        if key in seen:
            raise CandidateError(f"{source_name} 含大小写碰撞路径：{path}")
        seen.add(key)
        if object_type != "blob" or mode not in ("100644", "100755"):
            raise CandidateError(f"不支持的 Git 条目：{path}（mode={mode}）")
        content = git_bytes(repo, "cat-file", "blob", object_id)
        validate_blob(source_name, path, content)
        files.append((path, mode, content))
    actual = {path for path, _, _ in files}
    required = set(REQUIRED_PATHS[source_name])
    if source_name == "installer":
        required.update(INSTALLER_COMMON_REQUIRED)
        required.update(INSTALLER_PLATFORM_REQUIRED[platform])
    missing = sorted(required - actual)
    if missing:
        raise CandidateError(f"{source_name} 缺少必需合同：{', '.join(missing)}")
    return files


def include_in_candidate(source_name: str, source_path: str, platform: str) -> bool:
    if source_name != "installer":
        return True
    if source_path.startswith("contracts/") and source_path.endswith((".json", ".md")):
        return True
    return source_path in (
        INSTALLER_COMMON_PAYLOAD | INSTALLER_PLATFORM_PAYLOAD[platform]
    )


def make_zip_bytes(entries) -> bytes:
    buffer = io.BytesIO()
    # 发布归档使用 ZIP_STORED，避免不同 Python/zlib 版本对相同输入产生
    # 不同压缩字节；文件顺序、时间戳和权限也在下方固定。
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for relative, content, mode in entries:
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            permissions = 0o755 if mode == "100755" else 0o644
            info.external_attr = (stat.S_IFREG | permissions) << 16
            archive.writestr(info, content, compress_type=zipfile.ZIP_STORED)
    return buffer.getvalue()


def make_deploy_artifacts(product_files):
    records = []
    vault_entries = []
    for source_path, mode, content in sorted(product_files, key=lambda item: item[0]):
        sha256 = hashlib.sha256(content).hexdigest()
        records.append(
            {"path": source_path, "sha256": sha256, "size": len(content)}
        )
        vault_entries.append((f"vault/{source_path}", content, mode))
    tree_material = b"".join(
        record["path"].encode("utf-8")
        + b"\0"
        + str(record["size"]).encode("ascii")
        + b"\0"
        + record["sha256"].encode("ascii")
        + b"\n"
        for record in records
    )
    archive = make_zip_bytes(vault_entries)
    deploy = {
        "archive": {
            "sha256": hashlib.sha256(archive).hexdigest(),
            "size": len(archive),
        },
        "schema_version": 1,
        "vault": {
            "files": records,
            "tree_sha256": hashlib.sha256(tree_material).hexdigest(),
        },
    }
    return archive, deploy


def customer_path(record):
    source = record["source"]
    source_path = record["source_path"]
    if source == "product":
        return None
    if source == "skill":
        expected = f"payload/skills/claudecode-wiki-skills/{source_path}"
        if record["path"] != expected:
            raise CandidateError(f"Skill 映射路径无效：{record['path']}")
        return f"skills/claudecode-wiki-skills/{source_path}"
    if source == "installer":
        expected = f"payload/installer/{source_path}"
        if record["path"] != expected:
            raise CandidateError(f"安装器映射路径无效：{record['path']}")
        return source_path
    raise CandidateError(f"未知候选来源：{source}")


def make_customer_zip(staging: Path, records, runtime_records=None) -> bytes:
    entries = [
        ("manifest.json", (staging / "manifest.json").read_bytes(), "100644"),
        (
            "bundle-manifest.json",
            (staging / "bundle-manifest.json").read_bytes(),
            "100644",
        ),
        (
            "deploy-manifest.json",
            (staging / "deploy-manifest.json").read_bytes(),
            "100644",
        ),
        ("vault.zip", (staging / "vault.zip").read_bytes(), "100644"),
    ]
    seen = {collision_key(path) for path, _, _ in entries}
    customer_files = []
    for record in records:
        destination = customer_path(record)
        if destination is None:
            continue
        validate_relative_path(destination)
        key = collision_key(destination)
        if key in seen:
            raise CandidateError(f"客户候选含 Unicode/大小写路径碰撞：{destination}")
        seen.add(key)
        customer_files.append(
            (
                destination,
                (staging / Path(record["path"])).read_bytes(),
                record["mode"],
            )
        )
    entries.extend(sorted(customer_files, key=lambda item: item[0]))
    for record in runtime_records or []:
        relative = record["path"]
        validate_relative_path(relative)
        key = collision_key(relative)
        if key in seen:
            raise CandidateError(f"客户候选含运行时路径碰撞：{relative}")
        seen.add(key)
        entries.append(
            (
                relative,
                (staging / Path(relative)).read_bytes(),
                record["mode"],
            )
        )
    return make_zip_bytes(entries)


def json_contract(content: bytes, label: str) -> dict:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CandidateError(f"{label} 不是有效 JSON") from exc
    if not isinstance(value, dict):
        raise CandidateError(f"{label} 必须是 JSON 对象")
    return value


def asset_bytes(asset: dict, installer_source: dict, cache: Path) -> bytes:
    if not isinstance(asset, dict):
        raise CandidateError("离线运行时资产合同无效")
    filename = asset.get("filename")
    expected_sha = str(asset.get("sha256", ""))
    expected_size = asset.get("size")
    if (
        not isinstance(filename, str)
        or PurePosixPath(filename).name != filename
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha)
        or not isinstance(expected_size, int)
        or expected_size < 0
    ):
        raise CandidateError("离线运行时资产元数据无效")
    repo_path = asset.get("repo_path")
    url = asset.get("url")
    if bool(repo_path) == bool(url):
        raise CandidateError("离线运行时资产必须二选一绑定 repo_path 或 HTTPS URL")
    if repo_path:
        validate_relative_path(repo_path)
        try:
            content = git_bytes(
                Path(installer_source["repo"]),
                "show",
                f"{installer_source['commit']}:{repo_path}",
            )
        except CandidateError as exc:
            raise CandidateError(f"离线运行时仓库资产不可读：{filename}") from exc
    else:
        if not isinstance(url, str) or not url.startswith("https://"):
            raise CandidateError("离线运行时外部资产必须使用 HTTPS")
        cache.mkdir(parents=True, exist_ok=True)
        cached = cache / filename
        if cached.exists():
            if not cached.is_file() or cached.is_symlink():
                raise CandidateError(f"离线运行时缓存路径无效：{filename}")
            content = cached.read_bytes()
        else:
            partial = cache / f".{filename}.partial"
            if partial.exists():
                raise CandidateError(f"离线运行时缓存存在未完成下载：{filename}")
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "obsidian-wiki-setup-runtime-builder/1"},
            )
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    content = response.read()
                partial.write_bytes(content)
                partial.replace(cached)
            except (OSError, urllib.error.URLError) as exc:
                if partial.exists():
                    partial.unlink()
                raise CandidateError(f"离线运行时资产下载失败：{filename}") from exc
    if len(content) != expected_size:
        raise CandidateError(f"离线运行时资产大小不匹配：{filename}")
    if hashlib.sha256(content).hexdigest() != expected_sha:
        raise CandidateError(f"离线运行时资产 SHA-256 不匹配：{filename}")
    return content


def normalized_link_target(member_name: str, link_name: str) -> str:
    if PurePosixPath(link_name).is_absolute():
        raise CandidateError(f"运行时归档含绝对链接：{member_name}")
    parts = []
    for part in (PurePosixPath(member_name).parent / link_name).parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise CandidateError(f"运行时归档链接越界：{member_name}")
            parts.pop()
        else:
            parts.append(part)
    target = "/".join(parts)
    validate_relative_path(target)
    return target


def tar_file_entries(content: bytes, label: str):
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
            members = {member.name: member for member in archive.getmembers()}

            def resolve(member, seen):
                if member.name in seen:
                    raise CandidateError(f"{label} 含循环链接：{member.name}")
                if member.isfile():
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise CandidateError(f"{label} 文件不可读：{member.name}")
                    return stream.read(), member.mode
                if member.issym() or member.islnk():
                    target_name = normalized_link_target(member.name, member.linkname)
                    target = members.get(target_name)
                    if target is None:
                        raise CandidateError(f"{label} 链接目标缺失：{member.name}")
                    return resolve(target, {*seen, member.name})
                raise CandidateError(f"{label} 含不支持的归档条目：{member.name}")

            entries = []
            for member in archive.getmembers():
                if member.isdir():
                    continue
                validate_relative_path(member.name)
                data, source_mode = resolve(member, set())
                mode = "100755" if source_mode & 0o111 else "100644"
                entries.append((member.name, mode, data))
            return entries
    except (tarfile.TarError, OSError) as exc:
        raise CandidateError(f"{label} 不是有效的 tar.gz") from exc


def wheel_file_entries(content: bytes, label: str):
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = []
            for info in archive.infolist():
                if info.is_dir():
                    continue
                validate_relative_path(info.filename)
                if ".data/" in info.filename:
                    raise CandidateError(f"{label} 含不支持的 wheel .data 条目")
                file_type = (info.external_attr >> 16) & 0o170000
                if file_type == stat.S_IFLNK:
                    raise CandidateError(f"{label} 含链接条目：{info.filename}")
                permissions = (info.external_attr >> 16) & 0o777
                mode = "100755" if permissions & 0o111 else "100644"
                entries.append((info.filename, mode, archive.read(info)))
            return entries
    except (zipfile.BadZipFile, OSError) as exc:
        raise CandidateError(f"{label} 不是有效 wheel") from exc


def is_license_path(path: str) -> bool:
    """识别 wheel 内应归档而不应保留深层安装路径的许可证文件。"""
    lowered = path.casefold()
    basename = PurePosixPath(path).name.casefold()
    return "/licenses/" in f"/{lowered}" or basename.startswith(
        ("license", "copying", "notice")
    )


def flattened_license_path(
    target_name: str, package_name: str, path: str, content: bytes
) -> str:
    """为许可证生成短、稳定且内容寻址的候选包路径。"""
    normalized_package = re.sub(r"[-_.]+", "-", package_name).strip("-").casefold()
    basename = re.sub(r"[^A-Za-z0-9._-]", "_", PurePosixPath(path).name)[:48]
    if not basename:
        basename = "LICENSE"
    digest = hashlib.sha256(content).hexdigest()[:16]
    return f"runtime/licenses/{target_name}/{normalized_package}/{digest}-{basename}"


def runtime_file_record(path: str, mode: str, content: bytes) -> dict:
    return {
        "mode": mode,
        "path": path,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }


def runtime_tree_sha256(records: list[dict]) -> str:
    material = b"".join(
        record["path"].encode("utf-8")
        + b"\0"
        + str(record["size"]).encode("ascii")
        + b"\0"
        + record["sha256"].encode("ascii")
        + b"\n"
        for record in sorted(records, key=lambda item: item["path"])
    )
    return hashlib.sha256(material).hexdigest()


def package_asset(package: dict, target_name: str) -> dict:
    mode = package.get("install_mode")
    if mode == "wheel":
        return package.get("asset")
    if mode == "wheel_by_target":
        assets = package.get("assets")
        if not isinstance(assets, dict):
            raise CandidateError("平台 wheel 合同无效")
        return assets.get(target_name)
    raise CandidateError("非 wheel 包不能通过 package_asset 解析")


def build_runtime_payload(
    bom_content: bytes,
    installer_source: dict,
    platform: str,
    cache: Path,
):
    bom = json_contract(bom_content, "离线关键词运行时 BOM")
    if (
        bom.get("schema_version") != 1
        or bom.get("archive_flavor") != "install_only"
        or bom.get("policy")
        != {
            "client_network_install": False,
            "modify_customer_content": False,
            "modify_system_python": False,
            "require_asset_sha256": True,
            "require_licenses": True,
            "require_sbom": True,
        }
    ):
        raise CandidateError("离线关键词运行时 BOM 策略无效")
    targets = bom.get("targets")
    packages = bom.get("packages")
    if not isinstance(targets, dict) or not isinstance(packages, dict):
        raise CandidateError("离线关键词运行时 BOM 缺少目标或依赖")
    selected = sorted(
        name for name, target in targets.items()
        if isinstance(target, dict) and target.get("platform") == platform
    )
    expected_targets = ["windows-x64"] if platform == "windows" else ["macos-arm64", "macos-x64"]
    if selected != expected_targets:
        raise CandidateError("离线关键词运行时平台目标不完整")

    all_entries = []
    flattened_licenses = {}
    target_receipts = {}
    for target_name in selected:
        target = targets[target_name]
        root = f"runtime/targets/{target_name}"
        runtime_archive = asset_bytes(target.get("asset"), installer_source, cache)
        runtime_entries = tar_file_entries(runtime_archive, f"{target_name} Python 运行时")
        if not any(PurePosixPath(path).name.startswith("LICENSE") for path, _, _ in runtime_entries):
            raise CandidateError(f"{target_name} Python 运行时缺少许可证")
        target_entries = [
            (f"{root}/{path}", mode, content)
            for path, mode, content in runtime_entries
        ]
        site_packages = target.get("site_packages")
        interpreter = target.get("interpreter")
        if not isinstance(site_packages, str) or not isinstance(interpreter, str):
            raise CandidateError(f"{target_name} 运行时路径合同无效")
        validate_relative_path(site_packages)
        validate_relative_path(interpreter)

        for package_name, package in sorted(packages.items()):
            if not isinstance(package, dict):
                raise CandidateError(f"依赖合同无效：{package_name}")
            install_mode = package.get("install_mode")
            if install_mode in {"wheel", "wheel_by_target"}:
                asset = package_asset(package, target_name)
                wheel = asset_bytes(asset, installer_source, cache)
                extracted = wheel_file_entries(wheel, f"{package_name} wheel")
                license_entries = [entry for entry in extracted if is_license_path(entry[0])]
                if not license_entries:
                    raise CandidateError(f"依赖 wheel 缺少许可证：{package_name}")
                target_entries.extend(
                    (f"{root}/{site_packages}/{path}", mode, content)
                    for path, mode, content in extracted
                    if not is_license_path(path)
                )
                for path, _, content in license_entries:
                    license_path = flattened_license_path(
                        target_name, package_name, path, content
                    )
                    existing = flattened_licenses.get(license_path)
                    if existing is not None and existing != content:
                        raise CandidateError(f"许可证短路径碰撞：{license_path}")
                    flattened_licenses[license_path] = content
            elif install_mode == "sdist_vendored":
                source = asset_bytes(package.get("asset"), installer_source, cache)
                prefix = package.get("source_prefix")
                if not isinstance(prefix, str) or not prefix.endswith("/"):
                    raise CandidateError(f"vendored 依赖前缀无效：{package_name}")
                vendored = []
                for path, mode, content in tar_file_entries(source, f"{package_name} sdist"):
                    if path.startswith(prefix):
                        relative = path[len(prefix):]
                        if relative:
                            vendored.append(
                                (f"{root}/{site_packages}/{package_name}/{relative}", mode, content)
                            )
                if not vendored:
                    raise CandidateError(f"vendored 依赖源码缺失：{package_name}")
                license_content = asset_bytes(package.get("license_asset"), installer_source, cache)
                version = package.get("version")
                dist_info = f"{root}/{site_packages}/{package_name}-{version}.dist-info"
                target_entries.extend(vendored)
                target_entries.extend(
                    [
                        (f"{dist_info}/licenses/LICENSE", "100644", license_content),
                        (
                            f"{dist_info}/METADATA",
                            "100644",
                            f"Metadata-Version: 2.1\nName: {package_name}\nVersion: {version}\nLicense: {package.get('license')}\n".encode("utf-8"),
                        ),
                    ]
                )
            else:
                raise CandidateError(f"未知依赖安装模式：{package_name}")

        seen = set()
        for path, _, _ in target_entries:
            validate_relative_path(path)
            key = collision_key(path)
            if key in seen:
                raise CandidateError(f"离线运行时文件碰撞：{path}")
            seen.add(key)
        records_before_descriptor = [
            runtime_file_record(path[len(root) + 1:], mode, content)
            for path, mode, content in sorted(target_entries, key=lambda item: item[0])
        ]
        interpreter_path = interpreter
        if interpreter_path not in {record["path"] for record in records_before_descriptor}:
            raise CandidateError(f"离线运行时解释器缺失：{target_name}")
        descriptor = {
            "files": records_before_descriptor,
            "interpreter": interpreter,
            "runtime_id": bom.get("runtime_id"),
            "schema_version": 1,
            "site_packages": site_packages,
            "target": target_name,
            "tree_sha256": runtime_tree_sha256(records_before_descriptor),
        }
        descriptor_content = pretty_json_bytes(descriptor)
        descriptor_path = f"{root}/.runtime-target.json"
        target_entries.append((descriptor_path, "100644", descriptor_content))
        all_entries.extend(target_entries)
        target_receipts[target_name] = {
            "descriptor_sha256": hashlib.sha256(descriptor_content).hexdigest(),
            "interpreter": interpreter,
            "target_root": root,
            "tree_sha256": descriptor["tree_sha256"],
        }

    license_entries = [
        (path, "100644", content)
        for path, content in sorted(flattened_licenses.items())
    ]
    all_entries.extend(license_entries)
    license_records = [
        runtime_file_record(path, mode, content)
        for path, mode, content in license_entries
    ]
    bom_sha256 = hashlib.sha256(bom_content).hexdigest()
    sbom = {
        "bom": bom,
        "bom_sha256": bom_sha256,
        "flattened_license_files": license_records,
        "schema_version": 1,
        "selected_targets": selected,
    }
    notices = [
        "# 第三方组件与许可证",
        "",
        f"- Python runtime: {bom.get('runtime_id')}（归档内保留 Python 及其依赖许可证）",
    ]
    for name, package in sorted(packages.items()):
        notices.append(f"- {name} {package.get('version')}: {package.get('license')}")
    notices.extend(
        [
            "",
            "Python 与 vendored 依赖许可证保留在各目标运行时中；wheel 许可证按内容寻址归档在 runtime/licenses/<target>/<package>/。",
            "",
        ]
    )
    install_manifest = {
        "bom_sha256": bom_sha256,
        "runtime_id": bom.get("runtime_id"),
        "schema_version": 1,
        "targets": target_receipts,
    }
    shared = [
        ("runtime/SBOM.json", "100644", pretty_json_bytes(sbom)),
        ("runtime/THIRD-PARTY-NOTICES.md", "100644", "\n".join(notices).encode("utf-8")),
        ("runtime/runtime-install-manifest.json", "100644", pretty_json_bytes(install_manifest)),
    ]
    all_entries.extend(shared)
    records = [
        runtime_file_record(path, mode, content)
        for path, mode, content in sorted(all_entries, key=lambda item: item[0])
    ]
    identity = {
        "bom_sha256": bom_sha256,
        "files": records,
        "runtime_id": bom.get("runtime_id"),
        "targets": target_receipts,
    }
    return all_entries, identity


def build_bundle_manifest(candidate_id: str, sources: dict, content_by_source: dict) -> dict:
    try:
        product_contract = json_contract(
            content_by_source[("product", "schema/runtime-contract.json")],
            "产品运行时合同",
        )
        skill_compatibility = json_contract(
            content_by_source[("skill", "COMPATIBILITY.json")],
            "Skill 兼容合同",
        )
        release = json_contract(
            content_by_source[("installer", "release/bundle-release.json")],
            "bundle 发布合同",
        )
        repositories = release["repositories"]
    except KeyError as exc:
        raise CandidateError("三层版本合同缺少必需字段") from exc
    if (
        release.get("manifest_format") != 1
        or release.get("release_state") not in {"unreleased_candidate", "stable"}
        or set(repositories) != {"product", "wiki_skills", "installer"}
        or product_contract.get("schema_version")
        != release.get("product_schema_version")
        or skill_compatibility.get("runtime_version")
        != release.get("wiki_skills_version")
    ):
        raise CandidateError("三层版本合同不闭合")
    runtime = product_contract.get("runtime")
    if isinstance(runtime, dict):
        tested = runtime.get("tested")
        supports = skill_compatibility.get("supports")
        supported_product = any(
            isinstance(item, dict)
            and item.get("product_id") == product_contract.get("product_id")
            for item in supports or []
        )
        if (
            runtime.get("id") != skill_compatibility.get("runtime_id")
            or not isinstance(tested, dict)
            or tested.get("version") != skill_compatibility.get("runtime_version")
            or tested.get("commit") != sources["skill"]["commit"]
            or not supported_product
        ):
            raise CandidateError("三层版本合同与冻结 Skill 来源不一致")
    if release["release_state"] == "stable" and not release.get("bundle_version"):
        raise CandidateError("stable bundle 必须分配版本")
    if (
        release["release_state"] == "stable"
        and skill_compatibility.get("release_state") not in {None, "stable"}
    ):
        raise CandidateError("stable bundle 不能绑定未发布的 Skill 候选")
    compatibility_receipt = hashlib.sha256(
        canonical_json(
            {
                "product_contract": product_contract,
                "skill_compatibility": skill_compatibility,
            }
        )
    ).hexdigest()
    return {
        "bundle_version": release.get("bundle_version"),
        "candidate_id": candidate_id,
        "compatibility_receipt_sha256": compatibility_receipt,
        "components": {
            "installer": {
                "commit": sources["installer"]["commit"],
                "repository": repositories["installer"],
                "tree": sources["installer"]["tree"],
            },
            "product": {
                "commit": sources["product"]["commit"],
                "repository": repositories["product"],
                "schema_version": release["product_schema_version"],
                "tree": sources["product"]["tree"],
            },
            "wiki_skills": {
                "commit": sources["skill"]["commit"],
                "repository": repositories["wiki_skills"],
                "tree": sources["skill"]["tree"],
                "version": release["wiki_skills_version"],
            },
        },
        "manifest_format": 1,
        "release_state": release["release_state"],
    }


def load_plan(path: Path):
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CandidateError(f"无法读取计划：{exc}") from exc
    if plan.get("schema_version") != 1 or plan.get("platform") not in ("windows", "macos"):
        raise CandidateError("计划格式或平台无效")
    if set(plan.get("sources", {})) != set(SOURCE_PREFIXES):
        raise CandidateError("计划必须同时绑定 product、skill、installer")
    return plan


def command_build(args):
    staging = Path(args.staging)
    if staging.exists():
        raise CandidateError("staging 必须是尚不存在的新目录")
    plan = load_plan(Path(args.plan))
    collected = []
    collision_keys = set()
    for source_name in ("product", "skill", "installer"):
        for source_path, mode, content in read_git_files(
            source_name, plan["sources"][source_name], plan["platform"]
        ):
            if not include_in_candidate(source_name, source_path, plan["platform"]):
                continue
            destination = f"{SOURCE_PREFIXES[source_name]}/{source_path}"
            key = collision_key(destination)
            if key in collision_keys:
                raise CandidateError(f"候选路径碰撞：{destination}")
            collision_keys.add(key)
            collected.append((source_name, source_path, destination, mode, content))

    sources = {
        name: {
            "commit": plan["sources"][name]["commit"],
            "tree": plan["sources"][name]["tree"],
        }
        for name in sorted(SOURCE_PREFIXES)
    }
    records = [
        {
            "mode": mode,
            "path": destination,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
            "source": source_name,
            "source_path": source_path,
        }
        for source_name, source_path, destination, mode, content in sorted(
            collected, key=lambda item: item[2]
        )
    ]
    content_by_source = {
        (source_name, source_path): content
        for source_name, source_path, _, _, content in collected
    }
    runtime_cache = (
        Path(args.runtime_cache).expanduser().resolve()
        if args.runtime_cache
        else staging.parent / "runtime-cache"
    )
    runtime_entries, runtime_identity = build_runtime_payload(
        content_by_source[("installer", RUNTIME_BOM_PATH)],
        plan["sources"]["installer"],
        plan["platform"],
        runtime_cache,
    )
    identity = {
        "default_skills": [
            "design-juan-wiki",
            "wiki-hybrid-search",
            "ocr-and-documents",
        ],
        "files": records,
        "offline_query_baseline": "keyword",
        "optional_skills": ["ima-skill"],
        "platform": plan["platform"],
        "runtime": runtime_identity,
        "schema_version": 1,
        "sources": sources,
    }
    manifest = dict(identity)
    manifest["candidate_id"] = hashlib.sha256(canonical_json(identity)).hexdigest()
    bundle_manifest = build_bundle_manifest(
        manifest["candidate_id"], sources, content_by_source
    )

    staging.mkdir(parents=True)
    for _, _, destination, mode, content in collected:
        target = staging / Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        if mode == "100755":
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    for destination, mode, content in runtime_entries:
        target = staging / Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        if mode == "100755":
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    write_json(staging / "manifest.json", manifest)
    write_json(staging / "bundle-manifest.json", bundle_manifest)
    product_files = [
        (source_path, mode, content)
        for source_name, source_path, _, mode, content in collected
        if source_name == "product"
    ]
    vault_archive, deploy_manifest = make_deploy_artifacts(product_files)
    (staging / "vault.zip").write_bytes(vault_archive)
    write_json(staging / "deploy-manifest.json", deploy_manifest)
    (staging / "candidate.zip").write_bytes(
        make_customer_zip(staging, records, runtime_identity["files"])
    )


def command_verify(args):
    staging = Path(args.staging)
    manifest_path = staging / "manifest.json"
    archive_path = staging / "candidate.zip"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CandidateError(f"manifest.json 无法读取：{exc}") from exc
    if manifest_bytes != pretty_json_bytes(manifest):
        raise CandidateError("manifest.json 不是规范化 JSON")
    expected_manifest_keys = {
        "candidate_id",
        "default_skills",
        "files",
        "offline_query_baseline",
        "optional_skills",
        "platform",
        "runtime",
        "schema_version",
        "sources",
    }
    if set(manifest) != expected_manifest_keys:
        raise CandidateError("manifest.json 字段集合不符合 v1 合同")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("platform") not in ("windows", "macos")
        or manifest.get("offline_query_baseline") != "keyword"
        or manifest.get("default_skills")
        != ["design-juan-wiki", "wiki-hybrid-search", "ocr-and-documents"]
        or manifest.get("optional_skills") != ["ima-skill"]
    ):
        raise CandidateError("manifest.json 的平台或 Skill 合同无效")
    sources = manifest.get("sources")
    if not isinstance(sources, dict) or set(sources) != set(SOURCE_PREFIXES):
        raise CandidateError("manifest.json 的三仓来源合同无效")
    for name, source in sources.items():
        if (
            not isinstance(source, dict)
            or set(source) != {"commit", "tree"}
            or not FULL_COMMIT.fullmatch(str(source.get("commit", "")))
            or not FULL_COMMIT.fullmatch(str(source.get("tree", "")))
        ):
            raise CandidateError(f"manifest.json 的 {name} 来源无效")
    candidate_id = manifest.pop("candidate_id", None)
    expected_id = hashlib.sha256(canonical_json(manifest)).hexdigest()
    if candidate_id != expected_id:
        raise CandidateError("manifest.json 的 candidate_id 不匹配")
    records = manifest.get("files")
    if not isinstance(records, list):
        raise CandidateError("manifest.json 缺少 files 清单")

    expected_paths = set()
    expected_keys = set()
    for record in records:
        if (
            not isinstance(record, dict)
            or set(record)
            != {"mode", "path", "sha256", "size", "source", "source_path"}
            or not isinstance(record.get("path"), str)
        ):
            raise CandidateError("manifest.json 含无效文件记录")
        if (
            record.get("source") not in SOURCE_PREFIXES
            or not isinstance(record.get("source_path"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256", "")))
            or not isinstance(record.get("size"), int)
            or record["size"] < 0
        ):
            raise CandidateError("manifest.json 含无效文件元数据")
        relative = record["path"]
        validate_relative_path(relative)
        validate_relative_path(record["source_path"])
        key = collision_key(relative)
        if key in expected_keys:
            raise CandidateError(f"manifest.json 含 Unicode/大小写路径碰撞：{relative}")
        if not relative.startswith("payload/"):
            raise CandidateError(f"manifest.json 含越界路径：{relative}")
        expected_keys.add(key)
        expected_paths.add(relative)

    product_files = []
    for record in records:
        relative = record["path"]
        path = staging / Path(relative)
        if path.is_symlink() or not path.is_file():
            raise CandidateError(f"候选文件缺失或为链接：{relative}")
        content = path.read_bytes()
        if len(content) != record.get("size"):
            raise CandidateError(f"文件大小不匹配：{relative}")
        if hashlib.sha256(content).hexdigest() != record.get("sha256"):
            raise CandidateError(f"文件 SHA-256 不匹配：{relative}")
        if record.get("mode") not in ("100644", "100755"):
            raise CandidateError(f"文件 mode 无效：{relative}")
        if record.get("source") == "product":
            expected_product_path = f"payload/vault/{record.get('source_path', '')}"
            if relative != expected_product_path:
                raise CandidateError(f"产品映射路径无效：{relative}")
            product_files.append((record["source_path"], record["mode"], content))

    actual_paths = set()
    payload = staging / "payload"
    if not payload.is_dir():
        raise CandidateError("候选缺少 payload 目录")
    for path in payload.rglob("*"):
        if path.is_symlink():
            raise CandidateError(f"候选包含链接：{path.relative_to(staging).as_posix()}")
        if path.is_file():
            actual_paths.add(path.relative_to(staging).as_posix())
    if actual_paths != expected_paths:
        raise CandidateError("payload 文件集合与 manifest.json 不一致")
    runtime = manifest.get("runtime")
    if (
        not isinstance(runtime, dict)
        or set(runtime) != {"bom_sha256", "files", "runtime_id", "targets"}
        or not re.fullmatch(r"[0-9a-f]{64}", str(runtime.get("bom_sha256", "")))
        or not isinstance(runtime.get("runtime_id"), str)
        or not isinstance(runtime.get("targets"), dict)
        or not isinstance(runtime.get("files"), list)
    ):
        raise CandidateError("manifest.json 的离线运行时合同无效")
    runtime_paths = set()
    runtime_keys = set()
    for record in runtime["files"]:
        if (
            not isinstance(record, dict)
            or set(record) != {"mode", "path", "sha256", "size"}
            or record.get("mode") not in ("100644", "100755")
            or not isinstance(record.get("path"), str)
            or not record["path"].startswith("runtime/")
            or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256", "")))
            or not isinstance(record.get("size"), int)
            or record["size"] < 0
        ):
            raise CandidateError("manifest.json 含无效运行时文件记录")
        validate_relative_path(record["path"])
        key = collision_key(record["path"])
        if key in runtime_keys:
            raise CandidateError(f"manifest.json 含运行时路径碰撞：{record['path']}")
        runtime_keys.add(key)
        runtime_paths.add(record["path"])
        path = staging / Path(record["path"])
        if path.is_symlink() or not path.is_file():
            raise CandidateError(f"离线运行时文件缺失或为链接：{record['path']}")
        content = path.read_bytes()
        if len(content) != record["size"] or hashlib.sha256(content).hexdigest() != record["sha256"]:
            raise CandidateError(f"离线运行时文件 SHA-256 不匹配：{record['path']}")
    actual_runtime_paths = {
        path.relative_to(staging).as_posix()
        for path in (staging / "runtime").rglob("*")
        if path.is_file()
    }
    if actual_runtime_paths != runtime_paths:
        raise CandidateError("runtime 文件集合与 manifest.json 不一致")
    expected_vault_archive, expected_deploy = make_deploy_artifacts(product_files)
    try:
        actual_vault_archive = (staging / "vault.zip").read_bytes()
        deploy_bytes = (staging / "deploy-manifest.json").read_bytes()
        actual_deploy = json.loads(deploy_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CandidateError(f"部署子清单无法读取：{exc}") from exc
    if actual_vault_archive != expected_vault_archive:
        raise CandidateError("vault.zip 字节与产品 Git 对象不一致")
    if actual_deploy != expected_deploy:
        raise CandidateError("deploy-manifest.json 与产品 Git 对象不一致")
    if deploy_bytes != pretty_json_bytes(expected_deploy):
        raise CandidateError("deploy-manifest.json 不是规范化 JSON")
    content_by_source = {
        (record["source"], record["source_path"]):
        (staging / Path(record["path"])).read_bytes()
        for record in records
    }
    expected_bundle = build_bundle_manifest(candidate_id, sources, content_by_source)
    bundle_path = staging / "bundle-manifest.json"
    try:
        bundle_bytes = bundle_path.read_bytes()
        actual_bundle = json.loads(bundle_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CandidateError(f"bundle-manifest.json 无法读取：{exc}") from exc
    if actual_bundle != expected_bundle:
        raise CandidateError("bundle-manifest.json 与三仓来源不一致")
    if bundle_bytes != pretty_json_bytes(expected_bundle):
        raise CandidateError("bundle-manifest.json 不是规范化 JSON")
    expected_archive = make_customer_zip(staging, records, runtime["files"])
    try:
        actual_archive = archive_path.read_bytes()
    except OSError as exc:
        raise CandidateError(f"candidate.zip 无法读取：{exc}") from exc
    if actual_archive != expected_archive:
        raise CandidateError("candidate.zip 字节与确定性重建结果不一致")
    print(json.dumps({"candidate_id": candidate_id, "status": "verified"}, ensure_ascii=False))


def command_plan(args):
    plan = {
        "platform": args.platform,
        "schema_version": 1,
        "sources": {
            "installer": resolve_source(args.installer_repo, args.installer_ref),
            "product": resolve_source(args.product_repo, args.product_ref),
            "skill": resolve_source(args.skill_repo, args.skill_ref),
        },
    }
    write_json(Path(args.output), plan)


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="绑定三个仓库的精确版本")
    plan.add_argument("--product-repo", required=True)
    plan.add_argument("--product-ref", required=True)
    plan.add_argument("--skill-repo", required=True)
    plan.add_argument("--skill-ref", required=True)
    plan.add_argument("--installer-repo", required=True)
    plan.add_argument("--installer-ref", required=True)
    plan.add_argument("--platform", choices=("windows", "macos"), required=True)
    plan.add_argument("--output", required=True)
    plan.set_defaults(handler=command_plan)
    build = commands.add_parser("build", help="从 Git 对象构建全新安装候选")
    build.add_argument("--plan", required=True)
    build.add_argument("--staging", required=True)
    build.add_argument("--runtime-cache")
    build.set_defaults(handler=command_build)
    verify = commands.add_parser("verify", help="验证候选的清单、文件与确定性压缩包")
    verify.add_argument("--staging", required=True)
    verify.set_defaults(handler=command_verify)
    return root


def main():
    args = parser().parse_args()
    try:
        args.handler(args)
    except CandidateError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

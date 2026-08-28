#!/usr/bin/env python3
"""跨运行时 Wiki Skill 安装生命周期管理器。"""

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


CORE_SKILLS = ("design-juan-wiki", "wiki-hybrid-search", "ocr-and-documents")
PACKAGE_NAME = "claudecode-wiki-skills"
KEYWORD_RUNTIME_ERROR = "KEYWORD_RUNTIME_UNPROVISIONED"
MUTATION_LOCK_ERROR = "INSTALL_TRANSACTION_LOCKED"


class LifecycleError(Exception):
    """可预期且应 fail closed 的生命周期错误。"""


class MutationLock:
    """用内核文件锁串行化同一 HOME 的生命周期写操作。"""

    def __init__(self, home, operation):
        self.home = Path(os.path.realpath(home.absolute()))
        self.operation = operation
        self.path = self.home / ".agents" / "locks" / f"{PACKAGE_NAME}.lock"
        self.owner_id = uuid.uuid4().hex
        self.stream = None
        self.audit_error = None

    @staticmethod
    def _lock_stream(stream):
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_stream(stream):
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _read_metadata(self):
        self.stream.seek(1)
        try:
            raw = self.stream.read().decode("utf-8", errors="strict").strip("\x00\r\n ")
        except UnicodeDecodeError as error:
            raise LifecycleError("INSTALL_TRANSACTION_LOCK_METADATA_INVALID") from error
        if not raw:
            return None
        try:
            metadata = json.loads(raw)
        except json.JSONDecodeError as error:
            raise LifecycleError("INSTALL_TRANSACTION_LOCK_METADATA_INVALID") from error
        if not isinstance(metadata, dict):
            raise LifecycleError("INSTALL_TRANSACTION_LOCK_METADATA_INVALID")
        return metadata

    def _write_metadata(self, metadata):
        payload = (json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        # 第 1 字节是 Windows msvcrt.locking 的永久哨兵，不能截断。
        self.stream.seek(1)
        self.stream.truncate()
        self.stream.write(payload)
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.stream.seek(0)

    @staticmethod
    def _previous_owner_summary(metadata):
        if not metadata:
            return None
        return {
            "owner_id": metadata.get("owner_id"),
            "pid": metadata.get("pid"),
            "created_at": metadata.get("created_at"),
            "released_at": metadata.get("released_at"),
        }

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.seek(0, os.SEEK_END)
        if self.stream.tell() == 0:
            self.stream.write(b"\x00")
            self.stream.flush()
        try:
            self._lock_stream(self.stream)
        except OSError as error:
            self.stream.close()
            self.stream = None
            raise LifecycleError(MUTATION_LOCK_ERROR) from error

        try:
            # 旧元数据只用于审计。能取得内核锁即证明旧进程已释放或崩溃，
            # 因而无需按时间猜测并破坏可能仍然存活的锁。
            stale_audit_error = None
            try:
                previous = self._read_metadata()
            except LifecycleError as error:
                if str(error) != "INSTALL_TRANSACTION_LOCK_METADATA_INVALID":
                    raise
                # 只有成功取得内核独占锁后，损坏元数据才可判定为 stale audit。
                # 活跃进程持锁时执行流在 _lock_stream 已经停止，绝不会走到这里。
                previous = None
                stale_audit_error = "PREVIOUS_LOCK_AUDIT_CORRUPT"
            now = time.time()
            metadata = {
                "schema_version": 1,
                "package": PACKAGE_NAME,
                "owner_id": self.owner_id,
                "pid": os.getpid(),
                "hostname": socket.gethostname(),
                "operation": self.operation,
                "home": str(self.home),
                "created_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "created_unix": now,
                "previous_owner": self._previous_owner_summary(previous),
            }
            if stale_audit_error:
                metadata["previous_audit_error"] = stale_audit_error
            self._write_metadata(metadata)
            held_file = os.environ.get("WIKI_SKILL_TEST_LOCK_HELD_FILE")
            release_file = os.environ.get("WIKI_SKILL_TEST_LOCK_RELEASE_FILE")
            if held_file or release_file:
                if not held_file or not release_file:
                    raise LifecycleError("INSTALL_TRANSACTION_TEST_HOOK_INVALID")
                Path(held_file).write_text(self.owner_id + "\n", encoding="utf-8")
                deadline = time.monotonic() + 30
                while not Path(release_file).exists():
                    if time.monotonic() >= deadline:
                        raise LifecycleError("INSTALL_TRANSACTION_TEST_HOOK_TIMEOUT")
                    time.sleep(0.01)
            return self
        except Exception:
            self._unlock_stream(self.stream)
            self.stream.close()
            self.stream = None
            raise

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            metadata = self._read_metadata()
            if not metadata or metadata.get("owner_id") != self.owner_id:
                raise LifecycleError("INSTALL_TRANSACTION_LOCK_OWNERSHIP_LOST")
            else:
                metadata["released_at"] = datetime.now(timezone.utc).isoformat()
                metadata["released_unix"] = time.time()
                if (
                    os.environ.get(
                        "WIKI_SKILL_TEST_FORCE_LOCK_RELEASE_AUDIT_FAILURE"
                    )
                    == "1"
                ):
                    raise OSError("forced lock release audit failure")
                if (
                    os.environ.get(
                        "WIKI_SKILL_TEST_FORCE_LOCK_RELEASE_AUDIT_PARTIAL_WRITE"
                    )
                    == "1"
                ):
                    self.stream.seek(1)
                    self.stream.truncate()
                    self.stream.write(b'{"partial_release_audit":')
                    self.stream.flush()
                    os.fsync(self.stream.fileno())
                    raise OSError("forced partial lock release audit write")
                self._write_metadata(metadata)
        except Exception:
            # 事务结果已在内核锁保护下提交。释放审计只能 best effort；
            # 不能在提交后把成功伪装成失败，使调用方拿不到 undo receipt。
            self.audit_error = "LOCK_RELEASE_AUDIT_WRITE_FAILED"
        finally:
            self._unlock_stream(self.stream)
            self.stream.close()
            self.stream = None
        return False


def capability_receipt(skill_installed):
    return {
        "skill_installed": skill_installed,
        "keyword_runtime_ready": False,
        "keyword_runtime_status": "blocked_missing_interpreter_and_locked_dependencies",
        "keyword_runtime_error": KEYWORD_RUNTIME_ERROR,
        "vector_capability": "optional",
    }


def attach_lock_audit(result, transaction):
    if transaction.audit_error:
        result["lock_audit_error"] = transaction.audit_error
    return result


def make_parser():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--source", required=True, type=Path)
    plan.add_argument("--home", required=True, type=Path)
    plan.add_argument("--include-ima", action="store_true")
    install = commands.add_parser("install")
    install.add_argument("--source", required=True, type=Path)
    install.add_argument("--home", required=True, type=Path)
    install.add_argument("--include-ima", action="store_true")
    install.add_argument("--allow-copy-fallback", action="store_true")
    install.add_argument("--link-mode", choices=("auto", "copy"), default="auto")
    verify = commands.add_parser("verify")
    verify.add_argument("--home", required=True, type=Path)
    rollback = commands.add_parser("rollback")
    rollback.add_argument("--home", required=True, type=Path)
    uninstall = commands.add_parser("uninstall")
    uninstall.add_argument("--home", required=True, type=Path)
    undo = commands.add_parser("undo")
    undo.add_argument("--home", required=True, type=Path)
    undo.add_argument("--receipt", required=True, type=Path)
    undo_check = commands.add_parser("undo-check")
    undo_check.add_argument("--home", required=True, type=Path)
    undo_check.add_argument("--receipt", required=True, type=Path)
    return parser


def inspect_source(args):
    source = args.source.absolute()
    version_file = source / "VERSION"
    if not version_file.is_file():
        raise LifecycleError("源包缺少 VERSION")
    version = version_file.read_text(encoding="utf-8-sig").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", version):
        raise LifecycleError("VERSION 不是安全的目录名")
    skills = list(CORE_SKILLS)
    if args.include_ima:
        skills.append("ima-skill")
    for name in skills:
        entry = source / ("external" if name == "ima-skill" else "core") / name / "SKILL.md"
        if not entry.is_file():
            raise LifecycleError(f"源包缺少 Skill 入口：{name}")
    if not (source / "core" / "wiki-hybrid-search" / "scripts" / "wiki_search.py").is_file():
        raise LifecycleError("源包缺少关键词 Query 入口脚本")
    return source, version, skills


def build_plan(args):
    source, version, skills = inspect_source(args)
    owned_roots, runtime_aliases = detect_runtime_layout(args.home.absolute())
    plan = {
        "status": "ready",
        "version": version,
        "skills": skills,
        "include_ima": args.include_ima,
        "offline_baseline": "keyword",
        **capability_receipt(False),
        "source": str(source),
        "home": str(args.home.absolute()),
        "runtime_aliases": runtime_aliases,
        "owned_roots": {owner: str(root) for owner, root in owned_roots.items()},
    }
    return assess_plan(plan, args.home.absolute())


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inventory(root):
    files = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.relative_to(root).parts:
            continue
        relative = path.relative_to(root).as_posix()
        if relative == ".wiki-skill-install.json":
            continue
        files[relative] = {"sha256": sha256(path), "size": path.stat().st_size}
    return files


def json_fingerprint(payload):
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def raw_managed_snapshot(home):
    """不调用公共 verify，直接重算回执所绑定的全部原始状态。"""
    home = home.absolute()
    package = home / ".agents" / "packages" / PACKAGE_NAME
    state_path = package / "state.json"
    actual_roots, actual_aliases = detect_runtime_layout(home)
    actual_owned_roots = {
        owner: str(root) for owner, root in actual_roots.items()
    }
    if not state_path.exists():
        if package.exists():
            raise LifecycleError("发现无状态包目录，拒绝生成事务快照")
        return {
            "installed": False,
            "active_version": None,
            "state": None,
            "package_fingerprint": None,
            "entries_fingerprint": None,
            "runtime_aliases": actual_aliases,
            "owned_roots": actual_owned_roots,
            "generation": None,
        }

    _, _, state = load_state(home)
    entries = {}
    for owner, configured in state["entries"].items():
        entries[owner] = {}
        root = Path(state["owned_roots"][owner])
        for name, ownership in configured.items():
            destination = root / name
            mode = ownership["mode"]
            entries[owner][name] = {
                "mode": mode,
                "target": os.path.normcase(os.path.realpath(destination)),
                "fingerprint": inventory(destination) if mode == "copy" else None,
                "skill_md_sha256": sha256(destination / "SKILL.md"),
            }
    return {
        "installed": True,
        "active_version": state["active_version"],
        "state": state,
        "package_fingerprint": json_fingerprint(inventory(package)),
        "entries_fingerprint": json_fingerprint(entries),
        "runtime_aliases": actual_aliases,
        "owned_roots": actual_owned_roots,
        "generation": state.get("install_generation"),
    }


def managed_snapshot(home):
    """返回可由回执绑定的完整受管状态；安装路径仍必须先通过公共验收。"""
    home = home.absolute()
    package = home / ".agents" / "packages" / PACKAGE_NAME
    if (package / "state.json").exists():
        verify(argparse.Namespace(home=home))
    return raw_managed_snapshot(home)


def write_undo_receipt(home, transaction_id, action, changed, before, after):
    receipt_dir = home / ".agents" / "receipts" / PACKAGE_NAME
    receipt_path = receipt_dir / f"{transaction_id}.undo.json"
    payload = {
        "schema_version": 1,
        "receipt_type": "wiki-skill-install-undo",
        "package": PACKAGE_NAME,
        "transaction_id": transaction_id,
        "home": str(Path(os.path.realpath(home.absolute()))),
        "action": action,
        "changed": changed,
        "before": before,
        "after": after,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    payload["receipt_digest"] = json_fingerprint(payload)
    atomic_json(receipt_path, payload)
    return receipt_path


def load_undo_receipt(path, home):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LifecycleError("UNDO_RECEIPT_INVALID") from error
    digest = payload.pop("receipt_digest", None)
    if digest != json_fingerprint(payload):
        raise LifecycleError("UNDO_RECEIPT_INTEGRITY_MISMATCH")
    payload["receipt_digest"] = digest
    expected_home = str(Path(os.path.realpath(home.absolute())))
    if (
        payload.get("schema_version") != 1
        or payload.get("receipt_type") != "wiki-skill-install-undo"
        or payload.get("package") != PACKAGE_NAME
        or payload.get("home") != expected_home
        or not re.fullmatch(r"[0-9a-f]{32}", str(payload.get("transaction_id", "")))
        or not isinstance(payload.get("changed"), bool)
        or not isinstance(payload.get("before"), dict)
        or not isinstance(payload.get("after"), dict)
    ):
        raise LifecycleError("UNDO_RECEIPT_CONTRACT_MISMATCH")
    return payload


def ensure_windows_path_budget(source, versions_root, version):
    if os.name != "nt":
        return
    bases = (versions_root / version, versions_root / (".staging-" + "0" * 32))
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if ".git" in relative.parts or "__pycache__" in relative.parts:
            continue
        for base in bases:
            if len(str(base / relative)) >= 240:
                raise LifecycleError("Windows 目标路径过长；请改用更短的合成 HOME 或启用系统长路径支持")


def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def entry_source(version_root, name):
    group = "external" if name == "ima-skill" else "core"
    return version_root / group / name


def runtime_roots(home):
    return {
        "codex": home / ".agents" / "skills",
        "claude": home / ".claude" / "skills",
    }


def detect_runtime_layout(home):
    owned_roots = {}
    runtime_aliases = {}
    identities = {}
    for runtime, root in runtime_roots(home).items():
        identity = os.path.normcase(os.path.realpath(root))
        owner = identities.get(identity)
        if owner is None:
            owner = runtime
            identities[identity] = owner
            owned_roots[owner] = root
        runtime_aliases[runtime] = owner
    return owned_roots, runtime_aliases


def preflight_roots(owned_roots, skills):
    for owner, root in owned_roots.items():
        if root.exists() and not root.is_dir():
            raise LifecycleError(f"{owner} Skill 根路径不是目录，拒绝替换：{root}")
        for name in skills:
            destination = root / name
            if destination.exists() or destination.is_symlink():
                raise LifecycleError(f"发现同名未知入口，拒绝覆盖：{destination}")


def assess_plan(plan, home):
    package = home / ".agents" / "packages" / PACKAGE_NAME
    state_path = package / "state.json"
    target_root = package / "versions" / plan["version"]
    owned_roots = {owner: Path(path) for owner, path in plan["owned_roots"].items()}
    if not state_path.exists():
        if package.exists():
            raise LifecycleError("发现无状态包目录，拒绝接管")
        preflight_roots(owned_roots, plan["skills"])
        plan["action"] = "install"
        plan["active_version"] = None
        return plan

    verify(argparse.Namespace(home=home))
    _, _, state = load_state(home)
    if state["owned_skills"] != plan["skills"]:
        raise LifecycleError("已有受管安装的 Skill 集合与候选不兼容")
    plan.update(capability_receipt(True))
    plan["active_version"] = state["active_version"]
    if state["active_version"] == plan["version"]:
        current = package / "versions" / plan["version"]
        if inventory(Path(plan["source"])) != inventory(current):
            raise LifecycleError("相同版本号对应不同文件，拒绝覆盖")
        plan["action"] = "already_installed"
        return plan
    if target_root.exists():
        raise LifecycleError("目标版本目录已存在但不是活动版本，拒绝覆盖")
    plan["action"] = "upgrade"
    return plan


def create_copy_wrapper(source, destination):
    skill_file = source / "SKILL.md"
    text = skill_file.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    frontmatter = []
    if lines and lines[0].strip() == "---":
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                frontmatter = lines[: index + 1]
                break
    if not frontmatter:
        raise LifecycleError(f"copy fallback 的 Skill 缺少 frontmatter：{skill_file}")
    destination.mkdir(parents=True)
    canonical = str(skill_file.absolute())
    wrapper = "\n".join(frontmatter) + "\n\n# 受管发现包装器\n\n"
    wrapper += f"请读取权威 Skill 文件 `{canonical}`，并以该文件所在目录为基准解析它的全部相对引用。\n"
    (destination / "SKILL.md").write_text(wrapper, encoding="utf-8")
    (destination / ".managed-wiki-skill-target").write_text(canonical + "\n", encoding="utf-8")


def create_directory_link(source, destination, allow_copy_fallback, link_mode="auto"):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if link_mode == "copy":
        if not allow_copy_fallback:
            raise LifecycleError("copy fallback 必须显式使用 --allow-copy-fallback 授权")
        create_copy_wrapper(source, destination)
        return "copy"
    try:
        if os.name == "nt":
            completed = subprocess.run(
                ["cmd", "/d", "/c", "mklink", "/J", str(destination), str(source)],
                capture_output=True,
            )
            if completed.returncode != 0:
                raise OSError("mklink /J 执行失败")
            return "junction"
        os.symlink(source, destination, target_is_directory=True)
        return "symlink"
    except OSError as error:
        if not allow_copy_fallback:
            raise LifecycleError(f"创建目录链接失败；未授权 copy fallback：{destination}") from error
        create_copy_wrapper(source, destination)
        return "copy"


def recreate_owned_entry(source, destination, mode):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if mode == "copy":
        create_copy_wrapper(source, destination)
        return mode
    if mode == "junction" and os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(destination), str(source)],
            capture_output=True,
        )
        if completed.returncode != 0:
            raise LifecycleError(f"恢复 junction 失败：{destination}")
        return mode
    if mode == "symlink":
        os.symlink(source, destination, target_is_directory=True)
        return mode
    raise LifecycleError(f"无法恢复入口模式：{mode}")


def remove_owned_entry(path, mode):
    if mode in {"junction", "symlink"}:
        if mode == "junction" and os.name == "nt":
            os.rmdir(path)
        else:
            path.unlink()
    elif mode == "copy":
        shutil.rmtree(path)
    else:
        raise LifecycleError(f"未知入口模式：{mode}")


def install(args):
    source, version, _ = inspect_source(args)
    home = args.home.absolute()
    package = home / ".agents" / "packages" / PACKAGE_NAME
    ensure_windows_path_budget(source, package / "versions", version)
    with MutationLock(home, "install") as transaction:
        result = install_locked(args, transaction.owner_id)
    return attach_lock_audit(result, transaction)


def install_locked(args, transaction_id):
    plan = build_plan(args)
    home = args.home.absolute()
    owned_roots = {owner: Path(path) for owner, path in plan["owned_roots"].items()}
    package = home / ".agents" / "packages" / PACKAGE_NAME
    version_root = package / "versions" / plan["version"]
    state_path = package / "state.json"
    before = managed_snapshot(home)
    previous_state = None
    if state_path.exists():
        verify(argparse.Namespace(home=home))
        _, _, previous_state = load_state(home)
        if previous_state["owned_skills"] != plan["skills"]:
            raise LifecycleError("升级不得改变 Skill 集合；请先卸载后按新合同安装")
        existing_modes = {
            ownership["mode"]
            for configured in previous_state["entries"].values()
            for ownership in configured.values()
        }
        if args.link_mode == "copy" and existing_modes != {"copy"}:
            raise LifecycleError("升级不得隐式执行入口模式迁移")
        if previous_state["active_version"] == plan["version"]:
            current = package / "versions" / plan["version"]
            if inventory(args.source.absolute()) != inventory(current):
                raise LifecycleError("相同版本号对应不同文件，拒绝覆盖")
            if previous_state["schema_version"] == 1:
                migrated_state = dict(previous_state)
                migrated_state["schema_version"] = 2
                migrated_state["install_generation"] = transaction_id
                try:
                    atomic_json(state_path, migrated_state)
                    after = managed_snapshot(home)
                    undo_receipt = write_undo_receipt(
                        home, transaction_id, "migrate", True, before, after
                    )
                except Exception:
                    atomic_json(state_path, previous_state)
                    raise
                return {
                    "status": "migrated",
                    "action": "migrate",
                    "changed": True,
                    "transaction_id": transaction_id,
                    "undo_receipt": str(undo_receipt),
                    "version": plan["version"],
                    "skills": plan["skills"],
                    "state": str(state_path),
                    **capability_receipt(True),
                }
            after = managed_snapshot(home)
            undo_receipt = write_undo_receipt(
                home, transaction_id, "already_installed", False, before, after
            )
            return {
                "status": "already_installed",
                "action": "already_installed",
                "changed": False,
                "transaction_id": transaction_id,
                "undo_receipt": str(undo_receipt),
                "version": plan["version"],
                "skills": plan["skills"],
                "state": str(state_path),
                **capability_receipt(True),
            }
    else:
        if version_root.exists():
            raise LifecycleError("发现无状态版本目录，拒绝接管")
        preflight_roots(owned_roots, plan["skills"])
    if version_root.exists():
        raise LifecycleError("目标版本目录已存在，拒绝覆盖")

    staging = package / "versions" / (".staging-" + uuid.uuid4().hex)
    created_entries = []
    removed_previous = []
    published_version = False
    try:
        staging.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(args.source.absolute(), staging, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
        manifest = {
            "schema_version": 1,
            "package": PACKAGE_NAME,
            "version": plan["version"],
            "skills": plan["skills"],
            "capabilities": {
                "offline_baseline": "keyword",
                "keyword_runtime_ready": False,
                "keyword_runtime_status": "blocked_missing_interpreter_and_locked_dependencies",
                "keyword_runtime_error": KEYWORD_RUNTIME_ERROR,
                "vector": "optional",
            },
            "files": inventory(staging),
        }
        atomic_json(staging / ".wiki-skill-install.json", manifest)
        staging.replace(version_root)
        published_version = True

        if previous_state:
            for owner, configured in previous_state["entries"].items():
                root = Path(previous_state["owned_roots"][owner])
                for name, ownership in configured.items():
                    destination = root / name
                    remove_owned_entry(destination, ownership["mode"])
                    removed_previous.append((destination, ownership))

        entries = {}
        for owner, root in owned_roots.items():
            entries[owner] = {}
            for name in plan["skills"]:
                destination = root / name
                if previous_state:
                    mode = recreate_owned_entry(
                        entry_source(version_root, name),
                        destination,
                        previous_state["entries"][owner][name]["mode"],
                    )
                else:
                    mode = create_directory_link(
                        entry_source(version_root, name),
                        destination,
                        args.allow_copy_fallback,
                        args.link_mode,
                    )
                created_entries.append((destination, mode))
                entries[owner][name] = {
                    "mode": mode,
                    "target": str(entry_source(version_root, name)),
                    "fingerprint": inventory(destination) if mode == "copy" else None,
                }
        state = {
            "schema_version": 2,
            "package": PACKAGE_NAME,
            "install_generation": transaction_id,
            "active_version": plan["version"],
            "previous_versions": (
                [
                    *previous_state.get("previous_versions", []),
                    previous_state["active_version"],
                ]
                if previous_state
                else []
            ),
            "owned_skills": plan["skills"],
            "runtime_aliases": plan["runtime_aliases"],
            "owned_roots": plan["owned_roots"],
            "entries": entries,
        }
        atomic_json(state_path, state)
        action = "upgrade" if previous_state else "install"
        after = managed_snapshot(home)
        undo_receipt = write_undo_receipt(
            home, transaction_id, action, True, before, after
        )
    except Exception:
        for destination, mode in reversed(created_entries):
            if destination.exists() or destination.is_symlink():
                remove_owned_entry(destination, mode)
        for destination, ownership in removed_previous:
            if not destination.exists() and not destination.is_symlink():
                recreate_owned_entry(Path(ownership["target"]), destination, ownership["mode"])
        if staging.exists():
            shutil.rmtree(staging)
        if published_version and version_root.exists():
            shutil.rmtree(version_root)
        if previous_state:
            atomic_json(state_path, previous_state)
        elif package.exists():
            shutil.rmtree(package)
        raise
    return {
        "status": "upgraded" if previous_state else "installed",
        "action": action,
        "changed": True,
        "transaction_id": transaction_id,
        "undo_receipt": str(undo_receipt),
        "version": plan["version"],
        "skills": plan["skills"],
        "state": str(state_path),
        **capability_receipt(True),
    }


def load_state(home):
    package = home / ".agents" / "packages" / PACKAGE_NAME
    state_path = package / "state.json"
    if not state_path.is_file():
        raise LifecycleError("未找到受管安装状态")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LifecycleError("安装状态不可读") from error
    if state.get("package") != PACKAGE_NAME or not state.get("active_version"):
        raise LifecycleError("安装状态合同不兼容")
    schema_version = state.get("schema_version")
    if schema_version == 1:
        legacy_generation = state.get("install_generation")
        if legacy_generation is not None and not re.fullmatch(
            r"[0-9a-f]{32}", str(legacy_generation)
        ):
            raise LifecycleError("schema v1 的过渡 generation 不合法")
    elif schema_version == 2:
        if not re.fullmatch(
            r"[0-9a-f]{32}", str(state.get("install_generation", ""))
        ):
            raise LifecycleError("schema v2 缺少合法 install_generation")
    else:
        raise LifecycleError("安装状态 schema 不兼容")
    return package, state_path, state


def verify(args):
    if os.environ.get("WIKI_SKILL_TEST_FORCE_VERIFY_FAILURE") == "1":
        raise LifecycleError("TEST_FORCED_VERIFY_FAILURE")
    home = args.home.absolute()
    package, state_path, state = load_state(home)
    version_root = package / "versions" / state["active_version"]
    manifest_path = version_root / ".wiki-skill-install.json"
    if not manifest_path.is_file():
        raise LifecycleError("活动版本缺少安装清单")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LifecycleError("活动版本安装清单不可读") from error
    if manifest.get("version") != state["active_version"]:
        raise LifecycleError("活动版本与安装清单不一致")
    if manifest.get("skills") != state.get("owned_skills"):
        raise LifecycleError("安装清单与所有权状态不一致")
    if inventory(version_root) != manifest.get("files"):
        raise LifecycleError("活动版本文件指纹漂移")

    roots, runtime_aliases = detect_runtime_layout(home)
    expected_owned_roots = {owner: str(root) for owner, root in roots.items()}
    if state.get("runtime_aliases") != runtime_aliases:
        raise LifecycleError("运行时根目录别名状态漂移")
    if state.get("owned_roots") != expected_owned_roots:
        raise LifecycleError("唯一受管根目录状态漂移")
    configured_entries = state.get("entries", {})
    if set(configured_entries) != set(roots):
        raise LifecycleError("运行时入口所有权状态不完整")
    for configured in configured_entries.values():
        if set(configured) != set(state["owned_skills"]):
            raise LifecycleError("Skill 入口所有权状态不完整")
    for runtime, configured in configured_entries.items():
        if runtime not in roots:
            raise LifecycleError(f"存在未知运行时：{runtime}")
        for name, ownership in configured.items():
            destination = roots[runtime] / name
            if not destination.exists():
                raise LifecycleError(f"受管入口缺失：{destination}")
            mode = ownership.get("mode")
            if mode in {"junction", "symlink"}:
                expected = os.path.normcase(os.path.realpath(ownership["target"]))
                actual = os.path.normcase(os.path.realpath(destination))
                if actual != expected:
                    raise LifecycleError(f"受管入口目标漂移：{destination}")
            elif mode == "copy":
                if inventory(destination) != ownership.get("fingerprint"):
                    raise LifecycleError(f"受管 copy 入口指纹漂移：{destination}")
            else:
                raise LifecycleError(f"未知入口模式：{mode}")
            if not (destination / "SKILL.md").is_file():
                raise LifecycleError(f"受管入口缺少 SKILL.md：{destination}")
    return {
        "status": "verified",
        "version": state["active_version"],
        "skills": state["owned_skills"],
        "state": str(state_path),
        "state_schema_version": state["schema_version"],
        "offline_baseline": manifest["capabilities"]["offline_baseline"],
        **capability_receipt(True),
    }


def rollback(args):
    with MutationLock(args.home.absolute(), "rollback") as transaction:
        result = rollback_locked(args, transaction.owner_id)
    return attach_lock_audit(result, transaction)


def rollback_locked(args, transaction_id):
    home = args.home.absolute()
    verify(argparse.Namespace(home=home))
    package, state_path, state = load_state(home)
    if not state.get("previous_versions"):
        raise LifecycleError("没有可回滚的上一版本")
    target_version = state["previous_versions"][-1]
    target_root = package / "versions" / target_version
    manifest_path = target_root / ".wiki-skill-install.json"
    if not manifest_path.is_file():
        raise LifecycleError("回滚目标缺少安装清单")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("files") != inventory(target_root):
        raise LifecycleError("回滚目标文件指纹漂移")
    if manifest.get("skills") != state["owned_skills"]:
        raise LifecycleError("回滚目标 Skill 集合与当前合同不一致")

    old_entries = []
    new_entries = []
    try:
        for owner, configured in state["entries"].items():
            root = Path(state["owned_roots"][owner])
            for name, ownership in configured.items():
                destination = root / name
                remove_owned_entry(destination, ownership["mode"])
                old_entries.append((destination, ownership))
        entries = {}
        for owner, configured in state["entries"].items():
            entries[owner] = {}
            for name, ownership in configured.items():
                destination = Path(state["owned_roots"][owner]) / name
                mode = recreate_owned_entry(entry_source(target_root, name), destination, ownership["mode"])
                new_entries.append((destination, mode))
                entries[owner][name] = {
                    "mode": mode,
                    "target": str(entry_source(target_root, name)),
                    "fingerprint": inventory(destination) if mode == "copy" else None,
                }
        state["active_version"] = target_version
        state["previous_versions"] = state["previous_versions"][:-1]
        state["entries"] = entries
        state["schema_version"] = 2
        state["install_generation"] = transaction_id
        atomic_json(state_path, state)
    except Exception:
        for destination, mode in reversed(new_entries):
            if destination.exists() or destination.is_symlink():
                remove_owned_entry(destination, mode)
        for destination, ownership in old_entries:
            if not destination.exists() and not destination.is_symlink():
                recreate_owned_entry(Path(ownership["target"]), destination, ownership["mode"])
        raise
    return {
        "status": "rolled_back",
        "version": target_version,
        "skills": state["owned_skills"],
        "state": str(state_path),
        **capability_receipt(True),
    }


def uninstall(args):
    with MutationLock(args.home.absolute(), "uninstall") as transaction:
        result = uninstall_locked(args)
    return attach_lock_audit(result, transaction)


def uninstall_locked(args):
    home = args.home.absolute()
    verify(argparse.Namespace(home=home))
    package, _, state = load_state(home)
    expected_package = home / ".agents" / "packages" / PACKAGE_NAME
    if package != expected_package:
        raise LifecycleError("安装包路径越界，拒绝卸载")
    removed = []
    try:
        for owner, configured in state["entries"].items():
            root = Path(state["owned_roots"][owner])
            for name, ownership in configured.items():
                destination = root / name
                remove_owned_entry(destination, ownership["mode"])
                removed.append((destination, ownership))
        shutil.rmtree(package)
    except Exception:
        if package.exists():
            for destination, ownership in removed:
                if not destination.exists() and not destination.is_symlink():
                    recreate_owned_entry(Path(ownership["target"]), destination, ownership["mode"])
        raise
    return {
        "status": "uninstalled",
        "version": state["active_version"],
        "skills": state["owned_skills"],
        **capability_receipt(False),
    }


def undo(args):
    home = args.home.absolute()
    with MutationLock(home, "undo") as transaction:
        result = undo_locked(args)
    return attach_lock_audit(result, transaction)


def undo_check(args):
    """只读确认安装回执仍与当前受管 Skill 状态精确绑定。"""
    home = args.home.absolute()
    receipt = load_undo_receipt(args.receipt.absolute(), home)
    try:
        current = raw_managed_snapshot(home)
    except (LifecycleError, OSError, KeyError, TypeError) as error:
        raise LifecycleError(f"UNDO_AFTER_STATE_DRIFT: {error}") from error
    if current != receipt["after"]:
        raise LifecycleError("UNDO_AFTER_STATE_DRIFT")
    return {
        "status": "undo_ready",
        "transaction_id": receipt["transaction_id"],
        "changed": receipt["changed"],
    }


def undo_locked(args):
    home = args.home.absolute()
    receipt = load_undo_receipt(args.receipt.absolute(), home)
    try:
        current = raw_managed_snapshot(home)
    except (LifecycleError, OSError, KeyError, TypeError) as error:
        raise LifecycleError(f"UNDO_AFTER_STATE_DRIFT: {error}") from error
    if current != receipt["after"]:
        raise LifecycleError("UNDO_AFTER_STATE_DRIFT")
    if not receipt["changed"]:
        return {
            "status": "undo_noop",
            "transaction_id": receipt["transaction_id"],
            "changed": False,
        }

    if receipt.get("action") == "migrate":
        return undo_migration_locked(home, receipt)
    if receipt["before"].get("installed"):
        return undo_upgrade_locked(home, receipt)
    return undo_fresh_locked(home, receipt)


def undo_migration_locked(home, receipt):
    before = receipt["before"]
    after = receipt["after"]
    before_state = before.get("state")
    after_state = after.get("state")
    if (
        not isinstance(before_state, dict)
        or not isinstance(after_state, dict)
        or before_state.get("schema_version") != 1
        or (
            before_state.get("install_generation") is not None
            and not re.fullmatch(
                r"[0-9a-f]{32}", str(before_state.get("install_generation"))
            )
        )
        or after_state.get("schema_version") != 2
        or before.get("active_version") != after.get("active_version")
    ):
        raise LifecycleError("UNDO_RECEIPT_MIGRATION_CONTRACT_MISMATCH")
    _, state_path, current_state = load_state(home)
    if current_state != after_state:
        raise LifecycleError("UNDO_AFTER_STATE_DRIFT")
    try:
        atomic_json(state_path, before_state)
        if raw_managed_snapshot(home) != before:
            raise LifecycleError("UNDO_BEFORE_STATE_MISMATCH")
    except Exception:
        atomic_json(state_path, after_state)
        raise
    return {
        "status": "undone",
        "transaction_id": receipt["transaction_id"],
        "changed": True,
        "version": before_state["active_version"],
        "skills": before_state["owned_skills"],
        **capability_receipt(True),
    }


def undo_fresh_locked(home, receipt):
    if receipt.get("action") != "install" or receipt["before"].get("installed"):
        raise LifecycleError("UNDO_RECEIPT_INSTALL_CONTRACT_MISMATCH")
    package, _, state = load_state(home)
    backup_root = home / ".agents" / "undo-staging"
    package_backup = backup_root / f"{receipt['transaction_id']}.package-backup"
    if package_backup.exists():
        raise LifecycleError("UNDO_TEMPORARY_BACKUP_COLLISION")
    removed = []
    try:
        backup_root.mkdir(parents=True, exist_ok=True)
        package.replace(package_backup)
        for owner, configured in state["entries"].items():
            root = Path(state["owned_roots"][owner])
            for name, ownership in configured.items():
                destination = root / name
                remove_owned_entry(destination, ownership["mode"])
                removed.append((destination, ownership))
        if raw_managed_snapshot(home) != receipt["before"]:
            raise LifecycleError("UNDO_BEFORE_STATE_MISMATCH")
        shutil.rmtree(package_backup)
    except Exception:
        if package_backup.exists() and not package.exists():
            package_backup.replace(package)
        for destination, ownership in removed:
            if not destination.exists() and not destination.is_symlink():
                recreate_owned_entry(
                    Path(ownership["target"]), destination, ownership["mode"]
                )
        raise
    if backup_root.exists() and not any(backup_root.iterdir()):
        backup_root.rmdir()
    return {
        "status": "undone",
        "transaction_id": receipt["transaction_id"],
        "changed": True,
        **capability_receipt(False),
    }


def undo_upgrade_locked(home, receipt):
    before = receipt["before"]
    after = receipt["after"]
    before_state = before.get("state")
    after_state = after.get("state")
    if (
        receipt.get("action") != "upgrade"
        or not isinstance(before_state, dict)
        or not isinstance(after_state, dict)
        or before.get("active_version") == after.get("active_version")
    ):
        raise LifecycleError("UNDO_RECEIPT_UPGRADE_CONTRACT_MISMATCH")

    package, state_path, current_state = load_state(home)
    if current_state != after_state:
        raise LifecycleError("UNDO_AFTER_STATE_DRIFT")
    before_version = before["active_version"]
    after_version = after["active_version"]
    before_root = package / "versions" / before_version
    after_root = package / "versions" / after_version
    before_manifest_path = before_root / ".wiki-skill-install.json"
    if not before_manifest_path.is_file() or not after_root.is_dir():
        raise LifecycleError("UNDO_VERSION_STATE_INCOMPLETE")
    try:
        before_manifest = json.loads(before_manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LifecycleError("UNDO_BEFORE_MANIFEST_INVALID") from error
    if (
        before_manifest.get("version") != before_version
        or before_manifest.get("skills") != before_state.get("owned_skills")
        or before_manifest.get("files") != inventory(before_root)
    ):
        raise LifecycleError("UNDO_BEFORE_VERSION_DRIFT")
    if (
        before_state.get("package") != PACKAGE_NAME
        or before_state.get("owned_roots") != after_state.get("owned_roots")
        or before_state.get("runtime_aliases") != after_state.get("runtime_aliases")
        or before_state.get("owned_skills") != after_state.get("owned_skills")
    ):
        raise LifecycleError("UNDO_BEFORE_STATE_CONTRACT_MISMATCH")

    removed_after = []
    created_before = []
    backup_root = home / ".agents" / "undo-staging"
    version_backup = backup_root / f"{receipt['transaction_id']}.version-backup"
    if version_backup.exists():
        raise LifecycleError("UNDO_TEMPORARY_BACKUP_COLLISION")
    try:
        backup_root.mkdir(parents=True, exist_ok=True)
        for owner, configured in after_state["entries"].items():
            root = Path(after_state["owned_roots"][owner])
            for name, ownership in configured.items():
                destination = root / name
                remove_owned_entry(destination, ownership["mode"])
                removed_after.append((destination, ownership))

        entries = {}
        for owner, configured in before_state["entries"].items():
            entries[owner] = {}
            root = Path(before_state["owned_roots"][owner])
            for name, ownership in configured.items():
                destination = root / name
                source = entry_source(before_root, name)
                expected_target = os.path.normcase(os.path.realpath(ownership["target"]))
                if os.path.normcase(os.path.realpath(source)) != expected_target:
                    raise LifecycleError("UNDO_BEFORE_ENTRY_TARGET_MISMATCH")
                mode = recreate_owned_entry(source, destination, ownership["mode"])
                created_before.append((destination, mode))
                entries[owner][name] = {
                    "mode": mode,
                    "target": str(source),
                    "fingerprint": inventory(destination) if mode == "copy" else None,
                }
        restored_state = dict(before_state)
        restored_state["entries"] = entries
        atomic_json(state_path, restored_state)
        after_root.replace(version_backup)
        if raw_managed_snapshot(home) != before:
            raise LifecycleError("UNDO_BEFORE_STATE_MISMATCH")
        shutil.rmtree(version_backup)
    except Exception:
        for destination, mode in reversed(created_before):
            if destination.exists() or destination.is_symlink():
                remove_owned_entry(destination, mode)
        if version_backup.exists() and not after_root.exists():
            version_backup.replace(after_root)
        atomic_json(state_path, after_state)
        for destination, ownership in removed_after:
            if not destination.exists() and not destination.is_symlink():
                recreate_owned_entry(
                    Path(ownership["target"]), destination, ownership["mode"]
                )
        raise
    if backup_root.exists() and not any(backup_root.iterdir()):
        backup_root.rmdir()
    return {
        "status": "undone",
        "transaction_id": receipt["transaction_id"],
        "changed": True,
        "version": before_version,
        "skills": before_state["owned_skills"],
        **capability_receipt(True),
    }


def main():
    try:
        args = make_parser().parse_args()
        if args.command == "plan":
            result = build_plan(args)
        elif args.command == "install":
            result = install(args)
        elif args.command == "verify":
            result = verify(args)
        elif args.command == "rollback":
            result = rollback(args)
        elif args.command == "uninstall":
            result = uninstall(args)
        elif args.command == "undo":
            result = undo(args)
        elif args.command == "undo-check":
            result = undo_check(args)
        else:
            raise LifecycleError("不支持的命令")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except LifecycleError as error:
        print(json.dumps({"status": "blocked", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
    except (OSError, shutil.Error, json.JSONDecodeError) as error:
        print(
            json.dumps(
                {"status": "blocked", "error": "OPERATION_FAILED", "detail": str(error)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2)


if __name__ == "__main__":
    main()

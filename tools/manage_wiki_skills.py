#!/usr/bin/env python3
"""跨运行时 Wiki Skill 安装生命周期管理器。"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path


CORE_SKILLS = ("design-juan-wiki", "wiki-hybrid-search", "ocr-and-documents")
PACKAGE_NAME = "claudecode-wiki-skills"
KEYWORD_RUNTIME_ERROR = "KEYWORD_RUNTIME_UNPROVISIONED"


class LifecycleError(Exception):
    """可预期且应 fail closed 的生命周期错误。"""


def capability_receipt(skill_installed):
    return {
        "skill_installed": skill_installed,
        "keyword_runtime_ready": False,
        "keyword_runtime_status": "blocked_missing_interpreter_and_locked_dependencies",
        "keyword_runtime_error": KEYWORD_RUNTIME_ERROR,
        "vector_capability": "optional",
    }


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
    return parser


def build_plan(args):
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
    plan = build_plan(args)
    home = args.home.absolute()
    owned_roots = {owner: Path(path) for owner, path in plan["owned_roots"].items()}
    package = home / ".agents" / "packages" / PACKAGE_NAME
    version_root = package / "versions" / plan["version"]
    ensure_windows_path_budget(args.source.absolute(), package / "versions", plan["version"])
    state_path = package / "state.json"
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
            return {
                "status": "already_installed",
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
            "schema_version": 1,
            "package": PACKAGE_NAME,
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
    except Exception:
        for destination, mode in reversed(created_entries):
            if destination.exists() or destination.is_symlink():
                remove_owned_entry(destination, mode)
        for destination, ownership in removed_previous:
            if not destination.exists() and not destination.is_symlink():
                recreate_owned_entry(Path(ownership["target"]), destination, ownership["mode"])
        if staging.exists():
            shutil.rmtree(staging)
        if version_root.exists():
            shutil.rmtree(version_root)
        raise
    return {
        "status": "upgraded" if previous_state else "installed",
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
    return package, state_path, state


def verify(args):
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
        "offline_baseline": manifest["capabilities"]["offline_baseline"],
        **capability_receipt(True),
    }


def rollback(args):
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

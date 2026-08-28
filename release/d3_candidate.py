#!/usr/bin/env python3
"""D3 macOS 双架构候选真机验收公共入口。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
TARGET_MACHINES = {
    "windows-x64": {"amd64", "x86_64"},
    "macos-arm64": {"arm64", "aarch64"},
    "macos-x64": {"x86_64", "amd64"},
}


class D3Error(RuntimeError):
    pass


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def file_sha256(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def load_json(path: Path, error: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise D3Error(error) from exc
    if not isinstance(value, dict):
        raise D3Error(error)
    return value


def validate_release_plan(plan: dict) -> None:
    receipt_sha256 = plan.get("receipt_sha256")
    unsealed = {key: value for key, value in plan.items() if key != "receipt_sha256"}
    if not HEX64.fullmatch(str(receipt_sha256)) or digest(unsealed) != receipt_sha256:
        raise D3Error("D2_PLAN_SEAL_INVALID")
    plan_id = plan.get("plan_id")
    without_plan_id = {key: value for key, value in unsealed.items() if key != "plan_id"}
    if not HEX64.fullmatch(str(plan_id)) or digest(without_plan_id) != plan_id:
        raise D3Error("D2_PLAN_ID_INVALID")
    runtime_gate = plan.get("release_gates", {}).get("keyword_runtime", {})
    sources = plan.get("sources", {})
    source_commits_ready = all(
        isinstance(sources.get(component), dict)
        and HEX40.fullmatch(str(sources[component].get("commit", "")))
        for component in ("product", "skill", "installer")
    )
    candidates = plan.get("candidates", {})
    candidates_ready = all(
        isinstance(candidates.get(platform_name), dict)
        and candidates[platform_name].get("platform") == platform_name
        and candidates[platform_name].get("reproducible") is True
        and HEX64.fullmatch(str(candidates[platform_name].get("candidate_id", "")))
        and HEX64.fullmatch(
            str(candidates[platform_name].get("candidate_zip_sha256", ""))
        )
        and isinstance(candidates[platform_name].get("candidate_zip_size"), int)
        and candidates[platform_name]["candidate_zip_size"] > 0
        for platform_name in ("windows", "macos")
    )
    if (
        plan.get("orchestrator_format") != 1
        or plan.get("status") != "planned"
        or plan.get("next_action") != "run_approval_required"
        or plan.get("release_state") != "unreleased_candidate"
        or plan.get("bundle_version") != "2.1.0"
        or runtime_gate.get("status") != "ready"
        or runtime_gate.get("automatic_network_install") is not False
        or runtime_gate.get("offline_baseline") != "keyword"
        or runtime_gate.get("runtime_id") != "cpython-3.12.14+20260825"
        or runtime_gate.get("targets")
        != ["windows-x64", "macos-x64", "macos-arm64"]
        or not source_commits_ready
        or not candidates_ready
    ):
        raise D3Error("D2_PLAN_NOT_READY_FOR_D3")


def zip_entry_map(candidate: Path) -> tuple[zipfile.ZipFile, dict[str, zipfile.ZipInfo]]:
    try:
        archive = zipfile.ZipFile(candidate)
    except (OSError, zipfile.BadZipFile) as exc:
        raise D3Error("MACOS_CANDIDATE_ZIP_INVALID") from exc
    entries: dict[str, zipfile.ZipInfo] = {}
    casefolded_entries: set[str] = set()
    try:
        for info in archive.infolist():
            name = info.filename
            pure = PurePosixPath(name)
            file_type = (info.external_attr >> 16) & 0o170000
            if (
                not name
                or "\\" in name
                or pure.is_absolute()
                or ".." in pure.parts
                or file_type == stat.S_IFLNK
                or name.casefold() in casefolded_entries
            ):
                raise D3Error("MACOS_CANDIDATE_UNSAFE_ENTRY")
            entries[name] = info
            casefolded_entries.add(name.casefold())
        return archive, entries
    except Exception:
        archive.close()
        raise


def json_from_zip(archive: zipfile.ZipFile, entries: dict, name: str) -> dict:
    if name not in entries or entries[name].is_dir():
        raise D3Error("MACOS_CANDIDATE_CONTRACT_MISSING")
    try:
        value = json.loads(archive.read(entries[name]).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, KeyError) as exc:
        raise D3Error("MACOS_CANDIDATE_CONTRACT_INVALID") from exc
    if not isinstance(value, dict):
        raise D3Error("MACOS_CANDIDATE_CONTRACT_INVALID")
    return value


def preflight(release_plan_path: Path, candidate: Path, target: str) -> dict:
    plan = load_json(release_plan_path, "D2_PLAN_INVALID")
    validate_release_plan(plan)
    candidate = candidate.resolve(strict=True)
    platform_name = "windows" if target == "windows-x64" else "macos"
    expected = plan.get("candidates", {}).get(platform_name, {})
    candidate_sha256 = file_sha256(candidate)
    if (
        candidate.stat().st_size != expected.get("candidate_zip_size")
        or candidate_sha256 != expected.get("candidate_zip_sha256")
    ):
        raise D3Error("MACOS_CANDIDATE_DIGEST_MISMATCH")
    archive, entries = zip_entry_map(candidate)
    try:
        manifest = json_from_zip(archive, entries, "manifest.json")
        bundle = json_from_zip(archive, entries, "bundle-manifest.json")
        descriptor_name = f"runtime/targets/{target}/.runtime-target.json"
        descriptor = json_from_zip(archive, entries, descriptor_name)
        candidate_id = expected.get("candidate_id")
        sources = plan.get("sources", {})
        components = bundle.get("components", {})
        runtime_target = manifest.get("runtime", {}).get("targets", {}).get(target, {})
        interpreter = runtime_target.get("interpreter")
        interpreter_entry = f"runtime/targets/{target}/{interpreter}"
        if (
            target not in TARGET_MACHINES
            or manifest.get("platform") != platform_name
            or manifest.get("candidate_id") != candidate_id
            or bundle.get("candidate_id") != candidate_id
            or bundle.get("bundle_version") != plan.get("bundle_version")
            or bundle.get("release_state") != "unreleased_candidate"
            or components.get("installer", {}).get("commit") != sources.get("installer", {}).get("commit")
            or components.get("product", {}).get("commit") != sources.get("product", {}).get("commit")
            or components.get("wiki_skills", {}).get("commit") != sources.get("skill", {}).get("commit")
            or descriptor.get("target") != target
            or descriptor.get("runtime_id") != manifest.get("runtime", {}).get("runtime_id")
            or descriptor.get("interpreter") != interpreter
            or interpreter_entry not in entries
        ):
            raise D3Error("MACOS_CANDIDATE_CONTRACT_MISMATCH")
        return {
            "bundle_version": plan["bundle_version"],
            "candidate_id": candidate_id,
            "candidate_sha256": candidate_sha256,
            "plan_id": plan["plan_id"],
            "runtime_id": descriptor["runtime_id"],
            "status": "ready",
            "target": target,
        }
    finally:
        archive.close()


def safe_extract(candidate: Path, destination: Path) -> None:
    archive, entries = zip_entry_map(candidate)
    try:
        for name, info in entries.items():
            target = destination.joinpath(*PurePosixPath(name).parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            permissions = (info.external_attr >> 16) & 0o777
            if permissions:
                target.chmod(permissions)
    finally:
        archive.close()


def run_json(*arguments: object, cwd: Path | None = None) -> dict:
    completed = subprocess.run(
        [str(item) for item in arguments],
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="strict",
        capture_output=True,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise D3Error("MACOS_ACCEPTANCE_COMMAND_FAILED:" + (detail[-1] if detail else "unknown"))
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise D3Error("MACOS_ACCEPTANCE_RECEIPT_INVALID") from exc
    if not isinstance(value, dict):
        raise D3Error("MACOS_ACCEPTANCE_RECEIPT_INVALID")
    return value


def atomic_json(path: Path, value: dict) -> None:
    if path.exists():
        raise D3Error("MACOS_RECEIPT_OUTPUT_EXISTS")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    if temporary.exists():
        raise D3Error("MACOS_RECEIPT_TEMP_COLLISION")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def run_acceptance(args: argparse.Namespace) -> dict:
    ready = preflight(args.release_plan, args.candidate, args.target)
    if args.target.startswith("macos-") and sys.platform != "darwin":
        raise D3Error("MACOS_HOST_REQUIRED")
    if args.target == "windows-x64" and os.name != "nt":
        raise D3Error("WINDOWS_HOST_REQUIRED")
    machine = platform.machine().casefold()
    if machine not in TARGET_MACHINES[args.target]:
        raise D3Error("MACOS_MACHINE_TARGET_MISMATCH")
    candidate_before = file_sha256(args.candidate)
    with tempfile.TemporaryDirectory(prefix="d3-macos-acceptance-") as temporary:
        root = Path(temporary)
        extracted = root / "candidate"
        extracted.mkdir()
        safe_extract(args.candidate, extracted)
        runtime_root = extracted / "runtime" / "targets" / args.target
        descriptor = load_json(runtime_root / ".runtime-target.json", "MACOS_RUNTIME_DESCRIPTOR_INVALID")
        runtime_python = runtime_root / descriptor["interpreter"]
        query_script = (
            extracted
            / "skills"
            / "claudecode-wiki-skills"
            / "core"
            / "wiki-hybrid-search"
            / "scripts"
            / "wiki_search.py"
        )
        verifier = extracted / "tools" / "verify_keyword_runtime.py"
        manager = extracted / "tools" / "manage_wiki_skills.py"
        source = extracted / "skills" / "claudecode-wiki-skills"
        home = root / "customer-home"
        python_version = re.match(r"^cpython-([^+]+)\+", ready["runtime_id"])
        if not python_version:
            raise D3Error("MACOS_RUNTIME_ID_INVALID")
        probe = run_json(
            runtime_python,
            "-B",
            verifier,
            "--runtime-python", runtime_python,
            "--query-script", query_script,
            "--runtime-root", runtime_root,
            "--expected-python", python_version.group(1),
        )
        plan = run_json(
            runtime_python, "-B", manager, "plan",
            "--source", source, "--home", home, "--runtime-source", runtime_root,
        )
        install = run_json(
            runtime_python, "-B", manager, "install",
            "--source", source, "--home", home, "--runtime-source", runtime_root,
            "--link-mode", "copy", "--allow-copy-fallback",
        )
        verified = run_json(runtime_python, "-B", manager, "verify", "--home", home)
        undo_check = run_json(
            runtime_python, "-B", manager, "undo-check",
            "--home", home, "--receipt", install["undo_receipt"],
        )
        undone = run_json(
            runtime_python, "-B", manager, "undo",
            "--home", home, "--receipt", install["undo_receipt"],
        )
        installed_version = install.get("version")
        version_root = (
            home / ".agents" / "packages" / "claudecode-wiki-skills"
            / "versions" / str(installed_version)
        )
        if (
            probe.get("status") != "completed"
            or probe.get("runtime_tree_unchanged") is not True
            or probe.get("synthetic_vault_unchanged") is not True
            or plan.get("action") != "install"
            or plan.get("version") != installed_version
            or plan.get("runtime", {}).get("target") != args.target
            or plan.get("runtime", {}).get("runtime_id") != ready["runtime_id"]
            or install.get("status") != "installed"
            or verified.get("status") != "verified"
            or verified.get("keyword_runtime_ready") is not True
            or undo_check.get("status") != "undo_ready"
            or undone.get("status") != "undone"
            or version_root.exists()
        ):
            raise D3Error("MACOS_ACCEPTANCE_INCOMPLETE")
    if file_sha256(args.candidate) != candidate_before:
        raise D3Error("MACOS_CANDIDATE_MUTATED")
    payload = {
        **ready,
        "architecture": machine,
        "dependencies": probe.get("dependencies"),
        "install_status": install.get("status"),
        "query_status": probe.get("status"),
        "receipt_type": "d3-candidate-acceptance",
        "runner": {
            "github_repository": os.environ.get("GITHUB_REPOSITORY"),
            "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "github_run_id": os.environ.get("GITHUB_RUN_ID"),
            "github_sha": os.environ.get("GITHUB_SHA"),
            "github_workflow_ref": os.environ.get("GITHUB_WORKFLOW_REF"),
        },
        "schema_version": 1,
        "status": "completed",
        "undo_status": undone.get("status"),
        "verify_status": verified.get("status"),
    }
    payload["receipt_sha256"] = digest(payload)
    atomic_json(args.output, payload)
    return payload


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    for command in ("preflight", "run"):
        item = commands.add_parser(command)
        item.add_argument("--release-plan", required=True, type=Path)
        item.add_argument("--candidate", required=True, type=Path)
        item.add_argument("--target", required=True, choices=sorted(TARGET_MACHINES))
        if command == "run":
            item.add_argument("--output", required=True, type=Path)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "preflight":
            result = preflight(args.release_plan, args.candidate, args.target)
        else:
            result = run_acceptance(args)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (D3Error, OSError, KeyError, TypeError, ValueError) as exc:
        error = str(exc) if isinstance(exc, D3Error) else "D3_INTERNAL_VALIDATION_ERROR"
        print(json.dumps({"error": error, "status": "blocked"}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

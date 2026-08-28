#!/usr/bin/env python3
"""三仓组合发布编排：D2 只读 plan / status 公共接缝。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
PLAN_NAME = "release-plan.json"


class OrchestratorError(RuntimeError):
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


def run(*arguments: object, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        [str(item) for item in arguments],
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="strict",
        capture_output=True,
        env={**os.environ, "PYTHONUTF8": "1"},
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        summary = detail[-1] if detail else f"exit={completed.returncode}"
        raise OrchestratorError(f"COMMAND_FAILED:{summary}")
    return completed.stdout.strip()


def git(repo: Path, *arguments: str) -> str:
    return run("git", "-C", repo, *arguments)


def exact_source(repo: Path, commit: str) -> dict:
    try:
        resolved = repo.expanduser().resolve(strict=True)
    except OSError as exc:
        raise OrchestratorError("SOURCE_REPOSITORY_INVALID") from exc
    if not resolved.is_dir() or not HEX40.fullmatch(commit):
        raise OrchestratorError("SOURCE_REF_NOT_EXACT_COMMIT")
    try:
        actual = git(resolved, "rev-parse", f"{commit}^{{commit}}")
        tree = git(resolved, "rev-parse", f"{commit}^{{tree}}")
    except OrchestratorError as exc:
        raise OrchestratorError("SOURCE_REF_UNAVAILABLE") from exc
    if actual != commit or not HEX40.fullmatch(tree):
        raise OrchestratorError("SOURCE_REF_NOT_EXACT_COMMIT")
    return {"commit": commit, "repo": str(resolved), "tree": tree}


def atomic_json(path: Path, value: object) -> None:
    temporary = path.parent / f".{path.name}.tmp"
    if temporary.exists():
        raise OrchestratorError("ORCHESTRATOR_TEMP_COLLISION")
    data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def clone_exact(source: dict, destination: Path) -> dict:
    run("git", "clone", "--quiet", "--no-local", "--no-checkout", source["repo"], destination)
    git(destination, "checkout", "--quiet", "--detach", source["commit"])
    head = git(destination, "rev-parse", "HEAD")
    tree = git(destination, "rev-parse", "HEAD^{tree}")
    if head != source["commit"] or tree != source["tree"]:
        raise OrchestratorError("FRESH_CLONE_REF_MISMATCH")
    if git(destination, "status", "--porcelain"):
        raise OrchestratorError("FRESH_CLONE_DIRTY")
    return {
        **source,
        "clone": destination.as_posix(),
    }


def relative_to_workspace(path: Path, workspace: Path) -> str:
    try:
        return path.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError as exc:
        raise OrchestratorError("ARTIFACT_OUTSIDE_WORKSPACE") from exc


def build_platform(
    workspace: Path,
    clones: dict[str, dict],
    platform: str,
) -> dict:
    builder = Path(clones["installer"]["clone"]) / "tools" / "install_candidate.py"
    if not builder.is_file():
        raise OrchestratorError("PINNED_INSTALLER_BUILDER_MISSING")
    platform_root = workspace / "candidates" / platform
    platform_root.mkdir(parents=True)
    candidate_plan = platform_root / "candidate-plan.json"
    run(
        sys.executable,
        builder,
        "plan",
        "--product-repo",
        clones["product"]["clone"],
        "--product-ref",
        clones["product"]["commit"],
        "--skill-repo",
        clones["skill"]["clone"],
        "--skill-ref",
        clones["skill"]["commit"],
        "--installer-repo",
        clones["installer"]["clone"],
        "--installer-ref",
        clones["installer"]["commit"],
        "--platform",
        platform,
        "--output",
        candidate_plan,
    )
    stagings = []
    for label in ("first", "second"):
        staging = platform_root / label
        run(sys.executable, builder, "build", "--plan", candidate_plan, "--staging", staging)
        run(sys.executable, builder, "verify", "--staging", staging)
        stagings.append(staging)
    first, second = stagings
    first_candidate = first / "candidate.zip"
    second_candidate = second / "candidate.zip"
    first_vault = first / "vault.zip"
    second_vault = second / "vault.zip"
    candidate_hash = file_sha256(first_candidate)
    vault_hash = file_sha256(first_vault)
    reproducible = (
        candidate_hash == file_sha256(second_candidate)
        and vault_hash == file_sha256(second_vault)
    )
    if not reproducible:
        raise OrchestratorError("CANDIDATE_NOT_REPRODUCIBLE")
    first_manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
    second_manifest = json.loads((second / "manifest.json").read_text(encoding="utf-8"))
    if first_manifest != second_manifest:
        raise OrchestratorError("CANDIDATE_MANIFEST_DRIFT")
    return {
        "candidate_id": first_manifest["candidate_id"],
        "candidate_zip_sha256": candidate_hash,
        "candidate_zip_size": first_candidate.stat().st_size,
        "first_candidate_zip": relative_to_workspace(first_candidate, workspace),
        "first_vault_zip": relative_to_workspace(first_vault, workspace),
        "platform": platform,
        "reproducible": True,
        "second_candidate_zip": relative_to_workspace(second_candidate, workspace),
        "second_vault_zip": relative_to_workspace(second_vault, workspace),
        "vault_zip_sha256": vault_hash,
        "vault_zip_size": first_vault.stat().st_size,
    }


def sealed(payload: dict) -> dict:
    result = dict(payload)
    result["receipt_sha256"] = digest(payload)
    return result


def validate_seal(payload: dict) -> None:
    checksum = payload.get("receipt_sha256")
    unsealed = {key: value for key, value in payload.items() if key != "receipt_sha256"}
    if not HEX64.fullmatch(str(checksum)) or digest(unsealed) != checksum:
        raise OrchestratorError("ORCHESTRATOR_RECEIPT_TAMPERED")


def plan_release(args: argparse.Namespace) -> dict:
    sources = {
        "product": exact_source(args.product_repo, args.product_ref),
        "skill": exact_source(args.skill_repo, args.skill_ref),
        "installer": exact_source(args.installer_repo, args.installer_ref),
    }
    workspace = args.workspace.expanduser().resolve()
    if workspace.exists():
        raise OrchestratorError("WORKSPACE_ALREADY_EXISTS")
    workspace.mkdir(parents=True)
    clone_root = workspace / "repos"
    clone_root.mkdir()
    clones = {
        name: clone_exact(source, clone_root / name)
        for name, source in sources.items()
    }
    candidates = {
        platform: build_platform(workspace, clones, platform)
        for platform in ("windows", "macos")
    }
    candidate_ids = {item["candidate_id"] for item in candidates.values()}
    if len(candidate_ids) != 2:
        # 平台文件集合不同，candidate_id 应分别独立；相等通常意味着平台路由失效。
        raise OrchestratorError("PLATFORM_CANDIDATE_ID_COLLISION")
    release_contract_path = Path(clones["installer"]["clone"]) / "release" / "bundle-release.json"
    try:
        release_contract = json.loads(release_contract_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OrchestratorError("BUNDLE_RELEASE_CONTRACT_INVALID") from exc
    lifecycle_path = Path(clones["installer"]["clone"]) / "contracts" / "wiki-skill-lifecycle.json"
    try:
        lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8"))
        defaults = lifecycle["defaults"]
        dependency_policy = lifecycle["dependency_policy"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise OrchestratorError("WIKI_SKILL_LIFECYCLE_CONTRACT_INVALID") from exc
    keyword_runtime_ready = defaults.get("keyword_runtime_ready") is True
    runtime_id = defaults.get("keyword_runtime_id")
    runtime_targets = defaults.get("keyword_runtime_targets")
    if keyword_runtime_ready:
        bom_path = Path(clones["installer"]["clone"]) / "contracts" / "offline-keyword-runtime-bom.json"
        try:
            bom = json.loads(bom_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise OrchestratorError("OFFLINE_RUNTIME_BOM_INVALID") from exc
        if (
            defaults.get("keyword_runtime_status") != "ready"
            or defaults.get("keyword_runtime_error") is not None
            or runtime_id != bom.get("runtime_id")
            or not isinstance(runtime_targets, list)
            or len(runtime_targets) != len(set(runtime_targets))
            or set(runtime_targets) != set(bom.get("targets", {}).keys())
            or dependency_policy.get("automatic_network_install") is not False
            or dependency_policy.get("client_package_install") != "forbidden"
            or dependency_policy.get("system_python_modification") != "forbidden"
            or dependency_policy.get("bom") != "contracts/offline-keyword-runtime-bom.json"
        ):
            raise OrchestratorError("OFFLINE_RUNTIME_GATE_MISMATCH")
    release_gates = {
        "keyword_runtime": {
            "automatic_network_install": dependency_policy.get("automatic_network_install"),
            "error": defaults.get("keyword_runtime_error"),
            "offline_baseline": defaults.get("offline_baseline"),
            "ready_requires": dependency_policy.get("ready_requires"),
            "runtime_id": runtime_id,
            "targets": runtime_targets,
            "status": "ready" if keyword_runtime_ready else "blocked",
        }
    }
    if not keyword_runtime_ready:
        next_action = "runtime_provisioning_required"
    elif not release_contract.get("bundle_version"):
        next_action = "version_approval_required"
    else:
        next_action = "run_approval_required"
    stable_payload = {
        "bundle_version": release_contract.get("bundle_version"),
        "candidates": candidates,
        "next_action": next_action,
        "orchestrator_format": 1,
        "release_gates": release_gates,
        "release_state": release_contract.get("release_state"),
        "sources": {
            name: {
                key: value
                for key, value in clone.items()
                if key not in {"clone", "repo"}
            }
            for name, clone in clones.items()
        },
        "status": "planned",
    }
    stable_payload["plan_id"] = digest(stable_payload)
    receipt = sealed(stable_payload)
    atomic_json(workspace / PLAN_NAME, receipt)
    return receipt


def artifact_path(workspace: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise OrchestratorError("CANDIDATE_ARTIFACT_PATH_INVALID")
    path = (workspace / relative).resolve()
    try:
        path.relative_to(workspace.resolve())
    except ValueError as exc:
        raise OrchestratorError("CANDIDATE_ARTIFACT_PATH_INVALID") from exc
    return path


def status_release(args: argparse.Namespace) -> dict:
    workspace = args.workspace.expanduser().resolve(strict=True)
    if not workspace.is_dir():
        raise OrchestratorError("WORKSPACE_INVALID")
    try:
        receipt = json.loads((workspace / PLAN_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OrchestratorError("ORCHESTRATOR_RECEIPT_INVALID") from exc
    if not isinstance(receipt, dict):
        raise OrchestratorError("ORCHESTRATOR_RECEIPT_INVALID")
    validate_seal(receipt)
    if receipt.get("orchestrator_format") != 1 or receipt.get("status") != "planned":
        raise OrchestratorError("ORCHESTRATOR_RECEIPT_INVALID")
    for name, source in receipt.get("sources", {}).items():
        clone = workspace / "repos" / name
        if (
            not clone.is_dir()
            or git(clone, "rev-parse", "HEAD") != source.get("commit")
            or git(clone, "rev-parse", "HEAD^{tree}") != source.get("tree")
            or git(clone, "status", "--porcelain")
        ):
            raise OrchestratorError("PINNED_CLONE_DRIFT")
    for candidate in receipt.get("candidates", {}).values():
        for key in ("first_candidate_zip", "second_candidate_zip"):
            path = artifact_path(workspace, candidate.get(key))
            if not path.is_file() or file_sha256(path) != candidate.get("candidate_zip_sha256"):
                raise OrchestratorError("CANDIDATE_ARTIFACT_DRIFT")
        for key in ("first_vault_zip", "second_vault_zip"):
            path = artifact_path(workspace, candidate.get(key))
            if not path.is_file() or file_sha256(path) != candidate.get("vault_zip_sha256"):
                raise OrchestratorError("CANDIDATE_ARTIFACT_DRIFT")
    return receipt


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="三仓组合发布编排")
    commands = root.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="冻结精确 refs 并构建可复现双平台候选")
    plan.add_argument("--product-repo", required=True, type=Path)
    plan.add_argument("--product-ref", required=True)
    plan.add_argument("--skill-repo", required=True, type=Path)
    plan.add_argument("--skill-ref", required=True)
    plan.add_argument("--installer-repo", required=True, type=Path)
    plan.add_argument("--installer-ref", required=True)
    plan.add_argument("--workspace", required=True, type=Path)
    status = commands.add_parser("status", help="只读验证计划、克隆和候选状态")
    status.add_argument("--workspace", required=True, type=Path)
    return root


def emit(payload: dict, *, error: bool = False) -> int:
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        file=sys.stderr if error else sys.stdout,
    )
    return 2 if error else 0


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "plan":
            return emit(plan_release(args))
        if args.command == "status":
            return emit(status_release(args))
        raise OrchestratorError("UNKNOWN_COMMAND")
    except OrchestratorError as exc:
        return emit({"error": str(exc), "status": "blocked"}, error=True)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return emit(
            {"error": "ORCHESTRATOR_IO_ERROR", "error_type": type(exc).__name__, "status": "blocked"},
            error=True,
        )


if __name__ == "__main__":
    raise SystemExit(main())

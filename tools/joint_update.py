#!/usr/bin/env python3
"""Vault 产品规则、Wiki Skills 与派生索引的联合事务编排器。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
VAULT_UPDATE = ROOT / "tools" / "vault_update.py"
SKILL_MANAGER = ROOT / "tools" / "manage_wiki_skills.py"
HEX64 = re.compile(r"[0-9a-f]{64}")
INDEX_STATE_NAMES = (
    ".state.db",
    ".state.db-wal",
    ".state.db-shm",
    ".wiki_faiss.index",
    ".wiki_faiss_map.json",
)


class JointUpdateError(RuntimeError):
    pass


class JointRecoveryError(JointUpdateError):
    def __init__(self, message: str, receipt: Path, recovery_status: str):
        super().__init__(message)
        self.receipt = receipt
        self.recovery_status = recovery_status


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def load_json(path: Path, error: str) -> dict:
    try:
        value = json.loads(path.expanduser().resolve(strict=True).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise JointUpdateError(error) from exc
    if not isinstance(value, dict):
        raise JointUpdateError(error)
    return value


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(
            (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def sealed(payload: dict) -> dict:
    result = dict(payload)
    result["receipt_sha256"] = digest(payload)
    return result


def validate_seal(payload: dict) -> None:
    actual = payload.get("receipt_sha256")
    unsealed = {key: value for key, value in payload.items() if key != "receipt_sha256"}
    if not HEX64.fullmatch(str(actual)) or digest(unsealed) != actual:
        raise JointUpdateError("JOINT_RECEIPT_TAMPERED")


def safe_relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise JointUpdateError("STRICT_QUERY_PATH_INVALID")
    path = PurePosixPath(value)
    windows = Path(value)
    if (
        path.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise JointUpdateError("STRICT_QUERY_PATH_INVALID")
    return value


def run_json(script: Path, *arguments: object) -> dict:
    environment = dict(os.environ)
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [sys.executable, str(script), *(str(item) for item in arguments)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    raw = completed.stdout.strip() or completed.stderr.strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JointUpdateError("COMPONENT_RECEIPT_INVALID") from exc
    if completed.returncode != 0:
        raise JointUpdateError(str(payload.get("error", "COMPONENT_COMMAND_FAILED")))
    if not isinstance(payload, dict):
        raise JointUpdateError("COMPONENT_RECEIPT_INVALID")
    return payload


def parse_machine_receipt(output: str) -> dict:
    prefix = "RECEIPT_JSON:"
    for line in reversed(output.splitlines()):
        if line.startswith(prefix):
            try:
                value = json.loads(line[len(prefix) :].strip())
            except json.JSONDecodeError as exc:
                raise JointUpdateError("INDEX_RECEIPT_INVALID") from exc
            if isinstance(value, dict):
                return value
    raise JointUpdateError("INDEX_RECEIPT_MISSING")


def run_wiki_runtime(skill_source: Path, vault: Path, *arguments: str) -> dict:
    scripts = skill_source / "core" / "wiki-hybrid-search" / "scripts"
    python_script = scripts / "wiki_search.py"
    if not python_script.is_file():
        raise JointUpdateError("WIKI_RUNTIME_MISSING")
    environment = dict(os.environ)
    environment["KB_ROOT"] = str(vault)
    environment["WIKI_PYTHON"] = sys.executable
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    if os.name == "nt":
        launcher = scripts / "run-wiki-search.ps1"
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
        if not launcher.is_file() or not powershell:
            raise JointUpdateError("WINDOWS_WIKI_LAUNCHER_MISSING")
        command = [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(launcher),
            *arguments,
        ]
    else:
        command = [sys.executable, str(python_script), *arguments]
    completed = subprocess.run(
        command,
        cwd=vault,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    receipt = parse_machine_receipt(completed.stdout)
    if completed.returncode != 0 or receipt.get("status") != "completed":
        raise JointUpdateError("WIKI_RUNTIME_FAILED")
    return receipt


def installed_skill_source(home: Path) -> Path:
    state = load_json(
        home / ".agents" / "packages" / "claudecode-wiki-skills" / "state.json",
        "SKILL_STATE_INVALID",
    )
    version = state.get("active_version")
    if not isinstance(version, str) or not version:
        raise JointUpdateError("SKILL_STATE_INVALID")
    source = (
        home
        / ".agents"
        / "packages"
        / "claudecode-wiki-skills"
        / "versions"
        / version
    ).resolve(strict=True)
    if not source.is_dir():
        raise JointUpdateError("SKILL_STATE_INVALID")
    return source


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def index_snapshot(vault: Path) -> dict:
    result = {}
    for name in INDEX_STATE_NAMES:
        path = vault / name
        if path.is_symlink():
            raise JointUpdateError("JOINT_INDEX_LINK_UNSUPPORTED")
        if not path.exists():
            result[name] = None
        elif not path.is_file():
            raise JointUpdateError("JOINT_INDEX_PATH_NOT_FILE")
        else:
            stat = path.stat()
            result[name] = {
                "sha256": sha256_file(path),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
    return result


def backup_index_state(vault: Path, transaction_root: Path) -> dict:
    snapshot = index_snapshot(vault)
    backup_root = transaction_root / "index-before"
    backup_root.mkdir(parents=True)
    for name, record in snapshot.items():
        if record is None:
            continue
        backup = backup_root / name
        shutil.copy2(vault / name, backup)
        if sha256_file(backup) != record["sha256"]:
            raise JointUpdateError("JOINT_INDEX_BACKUP_MISMATCH")
    return snapshot


def validate_index_backups(transaction_root: Path, before: dict) -> None:
    backup_root = transaction_root / "index-before"
    for name, record in before.items():
        if record is None:
            continue
        backup = backup_root / name
        if not backup.is_file() or sha256_file(backup) != record["sha256"]:
            raise JointUpdateError("JOINT_INDEX_BACKUP_MISSING")


def restore_index_state(
    vault: Path,
    transaction_root: Path,
    before: dict,
    after: dict | None,
    *,
    require_after_match: bool,
) -> None:
    if require_after_match and index_snapshot(vault) != after:
        raise JointUpdateError("JOINT_INDEX_DRIFT")
    validate_index_backups(transaction_root, before)
    backup_root = transaction_root / "index-before"
    for name, record in before.items():
        destination = vault / name
        if record is None:
            if destination.exists():
                if not destination.is_file() or destination.is_symlink():
                    raise JointUpdateError("JOINT_INDEX_PATH_NOT_FILE")
                destination.unlink()
            continue
        temporary = vault / f".{name}.{uuid.uuid4().hex}.restore"
        try:
            shutil.copy2(backup_root / name, temporary)
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
    if index_snapshot(vault) != before:
        raise JointUpdateError("JOINT_INDEX_RESTORE_FAILED")


def validate_strict_query(vault: Path, source: Path, specification: dict) -> dict:
    receipt = run_wiki_runtime(source, vault, "search", specification["query"])
    paths = [str(item).replace("\\", "/") for item in receipt.get("result_paths", [])]
    if (
        receipt.get("degraded") is not False
        or receipt.get("answerability") != "candidate_supported"
        or specification["expect_path"] not in paths
    ):
        raise JointUpdateError("STRICT_QUERY_FAILED")
    expected = vault.joinpath(*PurePosixPath(specification["expect_path"]).parts)
    if not expected.is_file() or sha256_file(expected) != specification["expect_content_sha256"]:
        raise JointUpdateError("STRICT_QUERY_FULL_TEXT_MISMATCH")
    return {
        "receipt": receipt,
        "confirmed_path": specification["expect_path"],
        "confirmed_content_sha256": specification["expect_content_sha256"],
    }


def common_vault_arguments(args: argparse.Namespace) -> list[object]:
    return [
        "--vault",
        args.vault,
        "--cache-root",
        args.cache_root,
        "--target-root",
        args.target_root,
        "--path-policy",
        args.path_policy,
        "--product-contract",
        args.product_contract,
        "--skill-compatibility",
        args.skill_compatibility,
        "--bundle-manifest",
        args.bundle_manifest,
    ]


def build_joint_plan(args: argparse.Namespace) -> dict:
    vault = args.vault.expanduser().resolve(strict=True)
    skill_source = args.skill_source.expanduser().resolve(strict=True)
    home = args.home.expanduser().absolute()
    if not vault.is_dir() or not skill_source.is_dir():
        raise JointUpdateError("JOINT_ROOT_NOT_DIRECTORY")
    if not isinstance(args.query, str) or not args.query.strip():
        raise JointUpdateError("STRICT_QUERY_INVALID")
    expect_path = safe_relative(args.expect_path)
    if not HEX64.fullmatch(args.expect_content_sha256):
        raise JointUpdateError("STRICT_QUERY_CONTENT_DIGEST_INVALID")

    vault_plan = run_json(VAULT_UPDATE, "plan", *common_vault_arguments(args))
    skill_arguments: list[object] = [
        "plan",
        "--source",
        skill_source,
        "--home",
        home,
    ]
    if args.include_ima:
        skill_arguments.append("--include-ima")
    skill_plan = run_json(SKILL_MANAGER, *skill_arguments)
    index_plan = run_wiki_runtime(skill_source, vault, "index-status")

    if vault_plan.get("status") == "legacy_adoption_required":
        status = "legacy_adoption_required"
    elif vault_plan.get("status") not in {"approval_required", "up_to_date"}:
        status = "blocked"
    else:
        skill_action = skill_plan.get("action")
        needs_approval = (
            vault_plan.get("status") == "approval_required"
            or skill_action in {"install", "upgrade"}
            or index_plan.get("index_action") != "none"
        )
        status = "approval_required" if needs_approval else "up_to_date"

    payload = {
        "command": "joint-plan",
        "home": str(home),
        "include_ima": bool(args.include_ima),
        "index_plan": index_plan,
        "skill_plan": skill_plan,
        "status": status,
        "strict_query": {
            "expect_content_sha256": args.expect_content_sha256,
            "expect_path": expect_path,
            "query": args.query,
        },
        "vault": str(vault),
        "vault_plan": vault_plan,
    }
    payload["plan_id"] = digest(payload)
    return payload


def validate_approval(plan: dict, approval: dict) -> None:
    expected_keys = {
        "approval_format",
        "allow_deletes",
        "allow_index_rebuild",
        "approve_skill_change",
        "approved_at",
        "approved_changes",
        "plan_id",
        "subject",
    }
    if (
        set(approval) != expected_keys
        or approval.get("approval_format") != 1
        or approval.get("plan_id") != plan["plan_id"]
        or not isinstance(approval.get("subject"), str)
        or not approval["subject"]
        or not isinstance(approval.get("approved_at"), str)
        or not isinstance(approval.get("allow_deletes"), bool)
        or not isinstance(approval.get("allow_index_rebuild"), bool)
        or not isinstance(approval.get("approve_skill_change"), bool)
        or not isinstance(approval.get("approved_changes"), list)
    ):
        raise JointUpdateError("JOINT_APPROVAL_MISMATCH")
    expected = [
        {"change_sha256": item["change_sha256"], "path": item["path"]}
        for item in plan["vault_plan"].get("changes", [])
        if item.get("requires_approval")
    ]
    if approval["approved_changes"] != expected:
        raise JointUpdateError("JOINT_APPROVAL_MISMATCH")
    if (
        any(
            item.get("decision") in {"delete_candidate", "conflict_delete"}
            for item in plan["vault_plan"].get("changes", [])
        )
        and not approval["allow_deletes"]
    ):
        raise JointUpdateError("DELETE_NOT_APPROVED")
    if (
        plan["index_plan"].get("index_action") != "none"
        and not approval["allow_index_rebuild"]
    ):
        raise JointUpdateError("INDEX_REBUILD_NOT_APPROVED")
    if (
        plan["skill_plan"].get("action") in {"install", "upgrade"}
        and not approval["approve_skill_change"]
    ):
        raise JointUpdateError("SKILL_CHANGE_NOT_APPROVED")


def joint_cache_root(cache: Path, plan: dict, transaction_id: str) -> Path:
    vault_id = str(plan["vault_plan"].get("vault_id", ""))
    if not vault_id:
        raise JointUpdateError("VAULT_ID_MISSING")
    key = vault_id if re.fullmatch(r"[A-Za-z0-9._-]+", vault_id) else "sha256-" + hashlib.sha256(vault_id.encode()).hexdigest()
    return cache / "joint" / key / transaction_id


def apply_joint(args: argparse.Namespace) -> dict:
    supplied_plan = load_json(args.plan, "JOINT_PLAN_INVALID")
    current_plan = build_joint_plan(args)
    if supplied_plan != current_plan:
        raise JointUpdateError("JOINT_PLAN_STALE")
    if current_plan.get("status") != "approval_required":
        raise JointUpdateError("JOINT_PLAN_NOT_ACTIONABLE")
    approval = load_json(args.approval, "JOINT_APPROVAL_INVALID")
    validate_approval(current_plan, approval)

    vault = args.vault.expanduser().resolve(strict=True)
    cache = args.cache_root.expanduser().resolve(strict=True)
    home = args.home.expanduser().absolute()
    transaction_id = uuid.uuid4().hex
    transaction_root = joint_cache_root(cache, current_plan, transaction_id)
    if transaction_root.exists():
        raise JointUpdateError("JOINT_TRANSACTION_COLLISION")
    transaction_root.mkdir(parents=True)
    before_index = backup_index_state(vault, transaction_root)
    atomic_json(transaction_root / "joint-plan.json", current_plan)
    atomic_json(transaction_root / "joint-approval.json", approval)

    vault_plan_path = transaction_root / "vault-plan.json"
    vault_approval_path = transaction_root / "vault-approval.json"
    atomic_json(vault_plan_path, current_plan["vault_plan"])
    vault_approval = {
        "approval_format": 1,
        "allow_deletes": approval["allow_deletes"],
        "approved_at": approval["approved_at"],
        "approved_changes": approval["approved_changes"],
        "plan_id": current_plan["vault_plan"]["plan_id"],
        "subject": approval["subject"],
    }
    atomic_json(vault_approval_path, vault_approval)

    skill_result = None
    vault_result = None
    after_index = None
    try:
        skill_arguments: list[object] = [
            "install",
            "--source",
            args.skill_source,
            "--home",
            home,
            "--link-mode",
            args.skill_link_mode,
        ]
        if args.include_ima:
            skill_arguments.append("--include-ima")
        if args.allow_skill_copy_fallback:
            skill_arguments.append("--allow-copy-fallback")
        skill_result = run_json(SKILL_MANAGER, *skill_arguments)

        vault_result = run_json(
            VAULT_UPDATE,
            "apply",
            *common_vault_arguments(args),
            "--plan",
            vault_plan_path,
            "--approval",
            vault_approval_path,
        )
        active_source = installed_skill_source(home)
        index_status = run_wiki_runtime(active_source, vault, "index-status")
        if index_status.get("target_fingerprint") != current_plan["index_plan"].get("target_fingerprint"):
            raise JointUpdateError("INDEX_TARGET_DRIFT")
        if index_status.get("index_action") != "none":
            if not approval["allow_index_rebuild"]:
                raise JointUpdateError("INDEX_REBUILD_NOT_APPROVED")
            index_receipt = run_wiki_runtime(active_source, vault, "index")
        else:
            index_receipt = {
                "action": "index",
                "status": "completed",
                "index_fingerprint": index_status.get("target_fingerprint"),
                "no_op": True,
            }
        final_index_status = run_wiki_runtime(active_source, vault, "index-status")
        if final_index_status.get("index_action") != "none":
            raise JointUpdateError("INDEX_VERIFY_FAILED")
        strict_query = validate_strict_query(vault, active_source, current_plan["strict_query"])
        after_index = index_snapshot(vault)
        receipt = sealed(
            {
                "after_index": after_index,
                "before_index": before_index,
                "index_receipt": index_receipt,
                "index_status": final_index_status,
                "joint_receipt_format": 1,
                "operation": "apply",
                "plan_id": current_plan["plan_id"],
                "skill_undo_receipt": skill_result["undo_receipt"],
                "status": "completed",
                "strict_query": strict_query,
                "transaction_id": transaction_id,
                "vault": str(vault),
                "vault_receipt": vault_result["receipt"],
            }
        )
        receipt_path = transaction_root / "joint-apply-receipt.json"
        atomic_json(receipt_path, receipt)
        return {
            "command": "joint-apply",
            "receipt": str(receipt_path),
            "status": "completed",
            "transaction_id": transaction_id,
        }
    except Exception as exc:
        recovery_errors = []
        vault_rollback = None
        skill_undo = None
        if vault_result is not None:
            try:
                vault_rollback = run_json(
                    VAULT_UPDATE,
                    "rollback",
                    "--vault",
                    vault,
                    "--cache-root",
                    cache,
                    "--receipt",
                    vault_result["receipt"],
                )
            except Exception as recovery_error:
                recovery_errors.append(f"vault:{recovery_error}")
        try:
            restore_index_state(
                vault,
                transaction_root,
                before_index,
                after_index,
                require_after_match=False,
            )
        except Exception as recovery_error:
            recovery_errors.append(f"index:{recovery_error}")
        if skill_result is not None:
            try:
                skill_undo = run_json(
                    SKILL_MANAGER,
                    "undo",
                    "--home",
                    home,
                    "--receipt",
                    skill_result["undo_receipt"],
                )
            except Exception as recovery_error:
                recovery_errors.append(f"skill:{recovery_error}")
        if recovery_errors:
            raise JointUpdateError("JOINT_APPLY_FAILED_ROLLBACK_FAILED:" + "|".join(recovery_errors)) from exc
        failure_receipt = sealed(
            {
                "error": str(exc),
                "joint_receipt_format": 1,
                "operation": "apply_failure",
                "plan_id": current_plan["plan_id"],
                "recovery_status": "rolled_back",
                "restored_index": index_snapshot(vault),
                "skill_undo": skill_undo,
                "status": "failed",
                "transaction_id": transaction_id,
                "vault_rollback": vault_rollback,
            }
        )
        failure_path = transaction_root / "joint-failure-receipt.json"
        atomic_json(failure_path, failure_receipt)
        raise JointRecoveryError(
            f"JOINT_APPLY_FAILED_ROLLED_BACK:{exc}",
            failure_path,
            "rolled_back",
        ) from exc


def load_joint_receipt(path: Path) -> tuple[dict, Path]:
    resolved = path.expanduser().resolve(strict=True)
    receipt = load_json(resolved, "JOINT_RECEIPT_INVALID")
    validate_seal(receipt)
    if (
        receipt.get("joint_receipt_format") != 1
        or receipt.get("operation") != "apply"
        or receipt.get("status") != "completed"
        or not re.fullmatch(r"[0-9a-f]{32}", str(receipt.get("transaction_id", "")))
    ):
        raise JointUpdateError("JOINT_RECEIPT_INVALID")
    return receipt, resolved.parent


def verify_joint(args: argparse.Namespace) -> dict:
    vault = args.vault.expanduser().resolve(strict=True)
    home = args.home.expanduser().absolute()
    receipt, _transaction_root = load_joint_receipt(args.receipt)
    if receipt["vault"] != str(vault):
        raise JointUpdateError("JOINT_RECEIPT_TARGET_MISMATCH")
    run_json(VAULT_UPDATE, "verify", "--vault", vault, "--receipt", receipt["vault_receipt"])
    run_json(SKILL_MANAGER, "verify", "--home", home)
    if index_snapshot(vault) != receipt["after_index"]:
        raise JointUpdateError("JOINT_INDEX_DRIFT")
    source = installed_skill_source(home)
    status = run_wiki_runtime(source, vault, "index-status")
    if status.get("index_action") != "none":
        raise JointUpdateError("INDEX_VERIFY_FAILED")
    query = validate_strict_query(
        vault,
        source,
        {
            "query": receipt["strict_query"]["receipt"]["query"],
            "expect_path": receipt["strict_query"]["confirmed_path"],
            "expect_content_sha256": receipt["strict_query"]["confirmed_content_sha256"],
        },
    )
    return {
        "command": "joint-verify",
        "status": "verified",
        "transaction_id": receipt["transaction_id"],
        "strict_query": query,
    }


def rollback_joint(args: argparse.Namespace) -> dict:
    vault = args.vault.expanduser().resolve(strict=True)
    home = args.home.expanduser().absolute()
    cache = args.cache_root.expanduser().resolve(strict=True)
    receipt, transaction_root = load_joint_receipt(args.receipt)
    if receipt["vault"] != str(vault):
        raise JointUpdateError("JOINT_RECEIPT_TARGET_MISMATCH")
    run_json(VAULT_UPDATE, "verify", "--vault", vault, "--receipt", receipt["vault_receipt"])
    run_json(SKILL_MANAGER, "undo-check", "--home", home, "--receipt", receipt["skill_undo_receipt"])
    if index_snapshot(vault) != receipt["after_index"]:
        raise JointUpdateError("JOINT_INDEX_DRIFT")
    validate_index_backups(transaction_root, receipt["before_index"])

    vault_rollback = run_json(
        VAULT_UPDATE,
        "rollback",
        "--vault",
        vault,
        "--cache-root",
        cache,
        "--receipt",
        receipt["vault_receipt"],
    )
    restore_index_state(
        vault,
        transaction_root,
        receipt["before_index"],
        receipt["after_index"],
        require_after_match=True,
    )
    skill_undo = run_json(
        SKILL_MANAGER,
        "undo",
        "--home",
        home,
        "--receipt",
        receipt["skill_undo_receipt"],
    )
    rollback_id = uuid.uuid4().hex
    rollback_receipt = sealed(
        {
            "joint_receipt_format": 1,
            "operation": "rollback",
            "source_transaction_id": receipt["transaction_id"],
            "status": "completed",
            "transaction_id": rollback_id,
            "vault_rollback_receipt": vault_rollback["receipt"],
            "skill_undo": skill_undo,
            "restored_index": receipt["before_index"],
        }
    )
    rollback_path = transaction_root / "joint-rollback-receipt.json"
    atomic_json(rollback_path, rollback_receipt)
    return {
        "command": "joint-rollback",
        "receipt": str(rollback_path),
        "status": "completed",
        "transaction_id": rollback_id,
    }


def add_plan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--cache-root", required=True, type=Path)
    parser.add_argument("--target-root", required=True, type=Path)
    parser.add_argument("--path-policy", required=True, type=Path)
    parser.add_argument("--product-contract", required=True, type=Path)
    parser.add_argument("--skill-compatibility", required=True, type=Path)
    parser.add_argument("--bundle-manifest", required=True, type=Path)
    parser.add_argument("--skill-source", required=True, type=Path)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--include-ima", action="store_true")
    parser.add_argument("--query", required=True)
    parser.add_argument("--expect-path", required=True)
    parser.add_argument("--expect-content-sha256", required=True)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Vault 与 Wiki Skills 联合事务更新器")
    commands = root.add_subparsers(dest="command", required=True)
    add_plan_arguments(commands.add_parser("plan", help="生成严格只读联合计划"))
    apply_parser = commands.add_parser("apply", help="执行审批绑定的联合事务")
    add_plan_arguments(apply_parser)
    apply_parser.add_argument("--plan", required=True, type=Path)
    apply_parser.add_argument("--approval", required=True, type=Path)
    apply_parser.add_argument("--skill-link-mode", choices=("auto", "copy"), default="auto")
    apply_parser.add_argument("--allow-skill-copy-fallback", action="store_true")
    verify_parser = commands.add_parser("verify", help="只读验证联合事务回执")
    verify_parser.add_argument("--vault", required=True, type=Path)
    verify_parser.add_argument("--home", required=True, type=Path)
    verify_parser.add_argument("--receipt", required=True, type=Path)
    rollback_parser = commands.add_parser("rollback", help="按联合回执恢复 Vault、索引与 Skill")
    rollback_parser.add_argument("--vault", required=True, type=Path)
    rollback_parser.add_argument("--home", required=True, type=Path)
    rollback_parser.add_argument("--cache-root", required=True, type=Path)
    rollback_parser.add_argument("--receipt", required=True, type=Path)
    return root


def emit(payload: dict, *, error: bool = False) -> int:
    print(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        file=sys.stderr if error else sys.stdout,
    )
    return 2 if error else 0


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "plan":
            return emit(build_joint_plan(args))
        if args.command == "apply":
            return emit(apply_joint(args))
        if args.command == "verify":
            return emit(verify_joint(args))
        if args.command == "rollback":
            return emit(rollback_joint(args))
        raise JointUpdateError("UNKNOWN_COMMAND")
    except JointRecoveryError as exc:
        return emit(
            {
                "status": "blocked",
                "error": str(exc),
                "recovery_status": exc.recovery_status,
                "recovery_receipt": str(exc.receipt),
            },
            error=True,
        )
    except JointUpdateError as exc:
        return emit({"status": "blocked", "error": str(exc)}, error=True)
    except (OSError, UnicodeError) as exc:
        return emit(
            {
                "status": "blocked",
                "error": "IO_ERROR",
                "error_type": type(exc).__name__,
            },
            error=True,
        )


if __name__ == "__main__":
    raise SystemExit(main())

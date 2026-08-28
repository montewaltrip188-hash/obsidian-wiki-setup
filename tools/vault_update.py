#!/usr/bin/env python3
"""客户 Vault 的严格只读更新检查器。

U1 仅提供 status / check / plan；本模块不包含任何写入命令。
"""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import hashlib
import json
import re
import sys
from pathlib import Path


STATE_RELATIVE = Path(".juanyong-ai") / "product-state.json"


class UpdateError(RuntimeError):
    pass


HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?")


def valid_product_state(state: object) -> bool:
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        return False
    if not isinstance(state.get("vault_id"), str) or not state["vault_id"]:
        return False
    product = state.get("product")
    bundle = state.get("bundle")
    skills = state.get("skills")
    if not all(isinstance(item, dict) for item in (product, bundle, skills)):
        return False
    if not isinstance(product.get("repository"), str) or not product["repository"]:
        return False
    if not HEX40.fullmatch(str(product.get("base_commit", ""))):
        return False
    if not HEX40.fullmatch(str(product.get("base_tree", ""))):
        return False
    if not HEX64.fullmatch(str(product.get("baseline_sha256", ""))):
        return False
    if not isinstance(bundle.get("version"), str) or not bundle["version"]:
        return False
    if not HEX64.fullmatch(str(bundle.get("candidate_id", ""))):
        return False
    if not isinstance(skills.get("version"), str) or not skills["version"]:
        return False
    if not HEX40.fullmatch(str(skills.get("commit", ""))):
        return False
    if not HEX64.fullmatch(str(state.get("managed_inventory_sha256", ""))):
        return False
    if not isinstance(state.get("applied_migrations"), list):
        return False
    if state.get("last_transaction") is not None and not isinstance(
        state.get("last_transaction"), str
    ):
        return False
    return True


def load_json(path: Path, error_code: str) -> dict:
    try:
        value = json.loads(path.expanduser().resolve(strict=True).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UpdateError(error_code) from exc
    if not isinstance(value, dict):
        raise UpdateError(error_code)
    return value


def semver(value: object) -> tuple[int, int, int]:
    match = SEMVER.fullmatch(str(value))
    if not match:
        raise UpdateError("VERSION_CONTRACT_INVALID")
    return tuple(int(part) for part in match.groups())


def version_in_range(version: object, expression: object) -> bool:
    current = semver(version)
    if not isinstance(expression, str) or not expression.strip():
        raise UpdateError("VERSION_CONTRACT_INVALID")
    for token in expression.split():
        match = re.fullmatch(r"(>=|<=|>|<|==)(.+)", token)
        if not match:
            raise UpdateError("VERSION_CONTRACT_INVALID")
        operator, expected_text = match.groups()
        expected = semver(expected_text)
        if operator == ">=" and not current >= expected:
            return False
        if operator == "<=" and not current <= expected:
            return False
        if operator == ">" and not current > expected:
            return False
        if operator == "<" and not current < expected:
            return False
        if operator == "==" and not current == expected:
            return False
    return True


def validate_contracts(product: dict, compatibility: dict, bundle: dict) -> None:
    try:
        runtime = product["runtime"]
        tested = runtime["tested"]
        components = bundle["components"]
        bundle_product = components["product"]
        bundle_skills = components["wiki_skills"]
        supports = compatibility["supports"]
    except (KeyError, TypeError) as exc:
        raise UpdateError("VERSION_CONTRACT_INVALID") from exc
    if (
        product.get("contract_format") != 1
        or compatibility.get("contract_format") != 1
        or bundle.get("manifest_format") != 1
        or runtime.get("id") != compatibility.get("runtime_id")
        or runtime.get("id") != "claudecode-wiki-skills"
        or tested.get("version") != compatibility.get("runtime_version")
        or bundle_skills.get("version") != compatibility.get("runtime_version")
        or bundle_skills.get("commit") != tested.get("commit")
        or bundle_product.get("schema_version") != product.get("schema_version")
        or not HEX40.fullmatch(str(bundle_product.get("commit", "")))
        or not HEX40.fullmatch(str(bundle_product.get("tree", "")))
        or not HEX40.fullmatch(str(bundle_skills.get("commit", "")))
        or not HEX40.fullmatch(str(bundle_skills.get("tree", "")))
        or not HEX64.fullmatch(str(bundle.get("candidate_id", "")))
    ):
        raise UpdateError("VERSION_CONTRACT_MISMATCH")
    if bundle.get("release_state") != "stable" or bundle.get("bundle_version") is None:
        raise UpdateError("BUNDLE_VERSION_UNASSIGNED")
    if compatibility.get("release_state") not in {None, "stable"}:
        raise UpdateError("SKILL_RELEASE_UNSTABLE")
    semver(bundle["bundle_version"])
    if not version_in_range(compatibility["runtime_version"], runtime["required_range"]):
        raise UpdateError("VERSION_CONTRACT_MISMATCH")
    matching_support = [
        item
        for item in supports
        if isinstance(item, dict) and item.get("product_id") == product.get("product_id")
    ]
    if not matching_support or not any(
        version_in_range(product["schema_version"], item.get("schema_range"))
        for item in matching_support
    ):
        raise UpdateError("VERSION_CONTRACT_MISMATCH")


def load_state(vault: Path) -> dict | None:
    state_path = vault / STATE_RELATIVE
    if not state_path.is_file():
        return None
    state = load_json(state_path, "PRODUCT_STATE_INVALID")
    if not valid_product_state(state):
        raise UpdateError("PRODUCT_STATE_INVALID")
    return state


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_policy(path: Path) -> dict:
    policy = load_json(path, "PATH_POLICY_INVALID")
    if (
        policy.get("policy_format") != 1
        or not isinstance(policy.get("rules"), list)
        or not isinstance(policy.get("default"), dict)
        or not isinstance(policy.get("excluded_roots"), list)
    ):
        raise UpdateError("PATH_POLICY_INVALID")
    return policy


def path_rule(relative: str, policy: dict) -> dict:
    for rule in policy["rules"]:
        if not isinstance(rule, dict):
            raise UpdateError("PATH_POLICY_INVALID")
        exact = rule.get("paths", [])
        prefixes = rule.get("prefixes", [])
        globs = rule.get("globs", [])
        if (
            relative in exact
            or any(relative.startswith(prefix) for prefix in prefixes)
            or any(fnmatch.fnmatchcase(relative, pattern) for pattern in globs)
        ):
            return rule
    return policy["default"]


def product_paths(*roots: Path) -> list[str]:
    paths: set[str] = set()
    for root in roots:
        for candidate in root.rglob("*"):
            if candidate.is_symlink():
                raise UpdateError("PRODUCT_TREE_LINK_UNSUPPORTED")
            if candidate.is_file():
                paths.add(candidate.relative_to(root).as_posix())
    return sorted(paths, key=lambda item: item.encode("utf-8"))


def optional_bytes(root: Path, relative: str) -> bytes | None:
    path = root / Path(relative)
    if path.is_symlink():
        raise UpdateError("MANAGED_PATH_LINK_UNSUPPORTED")
    if not path.exists():
        return None
    if not path.is_file():
        raise UpdateError("MANAGED_PATH_NOT_FILE")
    return path.read_bytes()


def text_diff(before: bytes | None, after: bytes | None, relative: str) -> str | None:
    try:
        before_text = (before or b"").decode("utf-8").splitlines(keepends=True)
        after_text = (after or b"").decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return None
    return "".join(
        difflib.unified_diff(
            before_text,
            after_text,
            fromfile=f"local/{relative}",
            tofile=f"target/{relative}",
        )
    )


def decide(base: bytes | None, local: bytes | None, target: bytes | None) -> str:
    if local == target:
        return "no_op" if base == local else "no_op_converged"
    if local == base:
        if target is None:
            return "delete_candidate"
        if base is None:
            return "add_candidate"
        return "update_candidate"
    if target == base:
        return "preserve_local"
    if base is None and target is None:
        return "preserve_local"
    if target is None:
        return "conflict_delete"
    return "conflict"


def plan(args: argparse.Namespace) -> dict:
    vault = args.vault.expanduser().resolve(strict=True)
    base_root = args.base_root.expanduser().resolve(strict=True)
    target_root = args.target_root.expanduser().resolve(strict=True)
    if not all(path.is_dir() for path in (vault, base_root, target_root)):
        raise UpdateError("PLAN_ROOT_NOT_DIRECTORY")
    product = load_json(args.product_contract, "PRODUCT_CONTRACT_INVALID")
    compatibility = load_json(
        args.skill_compatibility, "SKILL_COMPATIBILITY_INVALID"
    )
    bundle = load_json(args.bundle_manifest, "BUNDLE_MANIFEST_INVALID")
    validate_contracts(product, compatibility, bundle)
    state = load_state(vault)
    if state is None:
        return {
            "command": "plan",
            "reason": "product_state_missing",
            "status": "legacy_adoption_required",
            "vault": str(vault),
        }
    policy = load_policy(args.path_policy)
    changes = []
    scanned_paths = []
    candidates = set(product_paths(base_root, target_root))
    for rule in policy["rules"]:
        if rule.get("ownership") in {"product_merge", "product_replace"}:
            candidates.update(rule.get("paths", []))
    for relative in sorted(candidates, key=lambda item: item.encode("utf-8")):
        rule = path_rule(relative, policy)
        if rule.get("ownership") not in {"product_merge", "product_replace"}:
            continue
        base = optional_bytes(base_root, relative)
        local = optional_bytes(vault, relative)
        target = optional_bytes(target_root, relative)
        if base is None and local is None and target is None:
            continue
        scanned_paths.append(relative)
        decision = decide(base, local, target)
        record = {
            "base_sha256": sha256_bytes(base) if base is not None else None,
            "decision": decision,
            "local_sha256": sha256_bytes(local) if local is not None else None,
            "ownership": rule["ownership"],
            "path": relative,
            "requires_approval": decision
            in {"add_candidate", "update_candidate", "delete_candidate", "conflict", "conflict_delete"},
            "target_sha256": sha256_bytes(target) if target is not None else None,
        }
        if rule["ownership"] == "product_merge" and local != target:
            record["target_diff"] = text_diff(local, target, relative)
        changes.append(record)
    actionable = [item for item in changes if item["requires_approval"]]
    payload = {
        "bundle_candidate_id": bundle["candidate_id"],
        "bundle_version": bundle["bundle_version"],
        "changes": changes,
        "command": "plan",
        "excluded_roots": policy["excluded_roots"],
        "scanned_paths": scanned_paths,
        "status": "approval_required" if actionable else "up_to_date",
        "vault_id": state["vault_id"],
    }
    payload["plan_id"] = sha256_bytes(canonical_json(payload))
    return payload


def emit(payload: dict, *, error: bool = False) -> int:
    stream = sys.stderr if error else sys.stdout
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=stream)
    return 2 if error else 0


def status(args: argparse.Namespace) -> dict:
    vault = args.vault.expanduser().resolve(strict=True)
    if not vault.is_dir():
        raise UpdateError("VAULT_NOT_DIRECTORY")
    state = load_state(vault)
    if state is None:
        return {
            "command": "status",
            "reason": "product_state_missing",
            "status": "legacy_adoption_required",
            "vault": str(vault),
        }
    return {
        "command": "status",
        "product_state": state,
        "status": "managed",
        "vault": str(vault),
    }


def check(args: argparse.Namespace) -> dict:
    vault = args.vault.expanduser().resolve(strict=True)
    if not vault.is_dir():
        raise UpdateError("VAULT_NOT_DIRECTORY")
    product = load_json(args.product_contract, "PRODUCT_CONTRACT_INVALID")
    compatibility = load_json(
        args.skill_compatibility, "SKILL_COMPATIBILITY_INVALID"
    )
    bundle = load_json(args.bundle_manifest, "BUNDLE_MANIFEST_INVALID")
    validate_contracts(product, compatibility, bundle)
    state = load_state(vault)
    if state is None:
        return {
            "command": "check",
            "reason": "product_state_missing",
            "status": "legacy_adoption_required",
            "target_bundle_version": bundle["bundle_version"],
            "vault": str(vault),
        }
    current = state["bundle"]["version"]
    target = bundle["bundle_version"]
    if not version_in_range(state["skills"]["version"], product["runtime"]["required_range"]):
        return {
            "command": "check",
            "current_bundle_version": current,
            "reason": "installed_skill_outside_product_range",
            "status": "unsupported_old",
            "target_bundle_version": target,
            "vault": str(vault),
        }
    changed = (
        current != target
        or state["product"]["base_commit"]
        != bundle["components"]["product"]["commit"]
        or state["skills"]["commit"]
        != bundle["components"]["wiki_skills"]["commit"]
    )
    return {
        "command": "check",
        "current_bundle_version": current,
        "status": "upgrade_available" if changed else "up_to_date",
        "target_bundle_version": target,
        "vault": str(vault),
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="客户 Vault 严格只读更新检查器")
    commands = root.add_subparsers(dest="command", required=True)
    status_parser = commands.add_parser("status", help="读取客户产品状态，不产生写入")
    status_parser.add_argument("--vault", required=True, type=Path)
    check_parser = commands.add_parser("check", help="检查已验签组合是否可用，不产生写入")
    check_parser.add_argument("--vault", required=True, type=Path)
    check_parser.add_argument("--product-contract", required=True, type=Path)
    check_parser.add_argument("--skill-compatibility", required=True, type=Path)
    check_parser.add_argument("--bundle-manifest", required=True, type=Path)
    plan_parser = commands.add_parser("plan", help="生成三方更新建议，不产生写入")
    plan_parser.add_argument("--vault", required=True, type=Path)
    plan_parser.add_argument("--base-root", required=True, type=Path)
    plan_parser.add_argument("--target-root", required=True, type=Path)
    plan_parser.add_argument("--path-policy", required=True, type=Path)
    plan_parser.add_argument("--product-contract", required=True, type=Path)
    plan_parser.add_argument("--skill-compatibility", required=True, type=Path)
    plan_parser.add_argument("--bundle-manifest", required=True, type=Path)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "status":
            return emit(status(args))
        if args.command == "check":
            return emit(check(args))
        if args.command == "plan":
            return emit(plan(args))
        raise UpdateError("UNKNOWN_COMMAND")
    except UpdateError as exc:
        return emit({"status": "blocked", "error": str(exc)}, error=True)


if __name__ == "__main__":
    raise SystemExit(main())

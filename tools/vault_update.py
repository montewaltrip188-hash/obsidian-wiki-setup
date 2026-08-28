#!/usr/bin/env python3
"""客户 Vault 的版本检查与受控事务更新器。"""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import hashlib
import io
import json
import os
import re
import stat
import sys
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath


STATE_RELATIVE = Path(".juanyong-ai") / "product-state.json"


class UpdateError(RuntimeError):
    pass


HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
SEMVER = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?"
)


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
        value = json.loads(
            path.expanduser().resolve(strict=True).read_text(encoding="utf-8")
        )
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
    if not version_in_range(
        compatibility["runtime_version"], runtime["required_range"]
    ):
        raise UpdateError("VERSION_CONTRACT_MISMATCH")
    matching_support = [
        item
        for item in supports
        if isinstance(item, dict)
        and item.get("product_id") == product.get("product_id")
    ]
    if not matching_support or not any(
        version_in_range(product["schema_version"], item.get("schema_range"))
        for item in matching_support
    ):
        raise UpdateError("VERSION_CONTRACT_MISMATCH")


def load_state(vault: Path) -> dict | None:
    state_path = guarded_path(vault, STATE_RELATIVE)
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


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def pretty_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def seal_receipt(receipt: dict) -> dict:
    sealed = dict(receipt)
    sealed["receipt_sha256"] = sha256_bytes(canonical_json(receipt))
    return sealed


def validate_receipt_seal(receipt: dict) -> None:
    digest = receipt.get("receipt_sha256")
    unsealed = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if (
        not HEX64.fullmatch(str(digest))
        or sha256_bytes(canonical_json(unsealed)) != digest
    ):
        raise UpdateError("TRANSACTION_RECEIPT_TAMPERED")


def validate_digest_map(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    for relative, digest in value.items():
        try:
            safe_relative(relative)
        except UpdateError:
            return False
        if digest is not None and not HEX64.fullmatch(str(digest)):
            return False
    return True


def validate_receipt_shape(receipt: dict) -> None:
    common = {
        "operation",
        "receipt_format",
        "receipt_sha256",
        "status",
        "transaction_id",
        "vault_id",
    }
    operation = receipt.get("operation")
    if operation == "apply":
        expected = common | {
            "after",
            "before",
            "plan_id",
            "state_after_sha256",
            "state_before_sha256",
        }
        valid = (
            set(receipt) == expected
            and validate_digest_map(receipt.get("before"))
            and validate_digest_map(receipt.get("after"))
            and HEX64.fullmatch(str(receipt.get("plan_id", "")))
            and HEX64.fullmatch(str(receipt.get("state_before_sha256", "")))
            and HEX64.fullmatch(str(receipt.get("state_after_sha256", "")))
        )
    elif operation == "rollback":
        expected = common | {
            "restored",
            "source_transaction_id",
            "state_restored_sha256",
        }
        valid = (
            set(receipt) == expected
            and validate_digest_map(receipt.get("restored"))
            and re.fullmatch(
                r"[0-9a-f]{32}", str(receipt.get("source_transaction_id", ""))
            )
            and HEX64.fullmatch(str(receipt.get("state_restored_sha256", "")))
        )
    else:
        valid = False
    if (
        not valid
        or receipt.get("receipt_format") != 1
        or receipt.get("status") != "completed"
        or not re.fullmatch(r"[0-9a-f]{32}", str(receipt.get("transaction_id", "")))
        or not isinstance(receipt.get("vault_id"), str)
        or not receipt["vault_id"]
    ):
        raise UpdateError("TRANSACTION_RECEIPT_INVALID")


def safe_relative(relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise UpdateError("MANAGED_PATH_INVALID")
    pure = PurePosixPath(relative)
    windows_path = Path(relative)
    if (
        pure.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise UpdateError("MANAGED_PATH_INVALID")
    return Path(*pure.parts)


def guarded_path(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise UpdateError("MANAGED_PATH_INVALID")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise UpdateError("MANAGED_PATH_LINK_UNSUPPORTED")
    return current


def vault_cache_key(vault_id: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9._-]+", vault_id) and vault_id not in {".", ".."}:
        return vault_id
    return f"sha256-{sha256_bytes(vault_id.encode('utf-8'))}"


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


def managed_product_files(product_root: Path, policy: dict) -> list[tuple[str, bytes]]:
    managed = []
    for relative in product_paths(product_root):
        rule = path_rule(relative, policy)
        if rule.get("ownership") not in {"product_merge", "product_replace"}:
            continue
        content = optional_bytes(product_root, relative)
        if content is None:
            continue
        managed.append((relative, content))
    return managed


def deterministic_baseline_zip(files: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for relative, content in sorted(
            files, key=lambda item: item[0].encode("utf-8")
        ):
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, content, compress_type=zipfile.ZIP_STORED)
    return buffer.getvalue()


def cached_baseline_files(cache_root: Path, state: dict) -> dict[str, bytes]:
    cache = cache_root.expanduser().resolve(strict=True)
    if not cache.is_dir():
        raise UpdateError("CACHE_ROOT_INVALID")
    baseline_path = guarded_path(
        cache, Path("baselines") / f"{state['product']['base_tree']}.zip"
    )
    try:
        content = baseline_path.read_bytes()
    except OSError as exc:
        raise UpdateError("BASELINE_CACHE_MISSING") from exc
    if sha256_bytes(content) != state["product"]["baseline_sha256"]:
        raise UpdateError("BASELINE_CACHE_DIGEST_MISMATCH")
    result: dict[str, bytes] = {}
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for item in archive.infolist():
                relative = item.filename
                pure = PurePosixPath(relative)
                if (
                    item.is_dir()
                    or not relative
                    or "\\" in relative
                    or pure.is_absolute()
                    or any(part in {"", ".", ".."} for part in pure.parts)
                ):
                    raise UpdateError("BASELINE_CACHE_PATH_INVALID")
                key = relative.casefold()
                if key in seen:
                    raise UpdateError("BASELINE_CACHE_PATH_COLLISION")
                seen.add(key)
                result[relative] = archive.read(item)
    except (OSError, zipfile.BadZipFile) as exc:
        raise UpdateError("BASELINE_CACHE_INVALID") from exc
    if not result:
        raise UpdateError("BASELINE_CACHE_EMPTY")
    return result


def managed_inventory_digest(files: list[tuple[str, bytes]]) -> str:
    records = [
        {"path": relative, "sha256": sha256_bytes(content), "size": len(content)}
        for relative, content in sorted(files, key=lambda item: item[0].encode("utf-8"))
    ]
    return sha256_bytes(canonical_json(records))


def validate_legacy_bundle(bundle: dict) -> None:
    """验证人工提供的已知历史组合；不把客户现状反推为官方 Base。"""
    try:
        product = bundle["components"]["product"]
        skills = bundle["components"]["wiki_skills"]
    except (KeyError, TypeError) as exc:
        raise UpdateError("LEGACY_BUNDLE_INVALID") from exc
    if (
        bundle.get("manifest_format") != 1
        or bundle.get("release_state") != "stable"
        or not isinstance(bundle.get("bundle_version"), str)
        or not HEX64.fullmatch(str(bundle.get("candidate_id", "")))
        or not isinstance(product.get("repository"), str)
        or not product["repository"]
        or not HEX40.fullmatch(str(product.get("commit", "")))
        or not HEX40.fullmatch(str(product.get("tree", "")))
        or not isinstance(skills.get("version"), str)
        or not HEX40.fullmatch(str(skills.get("commit", "")))
    ):
        raise UpdateError("LEGACY_BUNDLE_INVALID")
    semver(bundle["bundle_version"])
    semver(skills["version"])


def load_legacy_catalog(path: Path) -> tuple[dict, list[dict]]:
    catalog_path = path.expanduser().resolve(strict=True)
    catalog = load_json(catalog_path, "LEGACY_CATALOG_INVALID")
    if set(catalog) != {"catalog_format", "baselines"} or catalog.get("catalog_format") != 1:
        raise UpdateError("LEGACY_CATALOG_INVALID")
    baselines = catalog.get("baselines")
    if not isinstance(baselines, list) or not baselines:
        raise UpdateError("LEGACY_CATALOG_INVALID")
    resolved = []
    identifiers: set[str] = set()
    for item in baselines:
        if not isinstance(item, dict) or set(item) != {
            "id",
            "product_root",
            "bundle_manifest",
        }:
            raise UpdateError("LEGACY_CATALOG_INVALID")
        identifier = item.get("id")
        if (
            not isinstance(identifier, str)
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", identifier)
            or identifier in identifiers
        ):
            raise UpdateError("LEGACY_CATALOG_INVALID")
        identifiers.add(identifier)
        try:
            product_root = Path(item["product_root"]).expanduser().resolve(strict=True)
            manifest_path = Path(item["bundle_manifest"]).expanduser().resolve(strict=True)
        except (OSError, TypeError) as exc:
            raise UpdateError("LEGACY_CATALOG_INVALID") from exc
        if not product_root.is_dir() or not manifest_path.is_file():
            raise UpdateError("LEGACY_CATALOG_INVALID")
        bundle = load_json(manifest_path, "LEGACY_BUNDLE_INVALID")
        validate_legacy_bundle(bundle)
        resolved.append(
            {
                "id": identifier,
                "product_root": product_root,
                "manifest_path": manifest_path,
                "bundle": bundle,
            }
        )
    return catalog, resolved


def static_glob_root(pattern: str) -> str:
    wildcard = min(
        (position for token in "*?[" if (position := pattern.find(token)) >= 0),
        default=len(pattern),
    )
    prefix = pattern[:wildcard]
    if "/" not in prefix:
        return ""
    return prefix.rsplit("/", 1)[0] + "/"


def scan_managed_local_paths(vault: Path, policy: dict) -> set[str]:
    """只枚举产品受管子树，绝不递归客户内容根。"""
    result: set[str] = set()
    scan_roots: set[str] = set()
    for rule in policy["rules"]:
        if rule.get("ownership") not in {"product_merge", "product_replace"}:
            continue
        for relative in rule.get("paths", []):
            result.add(safe_relative(relative).as_posix())
        for prefix in rule.get("prefixes", []):
            normalized = safe_relative(prefix.rstrip("/")).as_posix() + "/"
            scan_roots.add(normalized)
        for pattern in rule.get("globs", []):
            root = static_glob_root(pattern)
            if not root:
                raise UpdateError("PATH_POLICY_UNBOUNDED_MANAGED_GLOB")
            scan_roots.add(safe_relative(root.rstrip("/")).as_posix() + "/")
    for relative_root in sorted(scan_roots):
        absolute_root = guarded_path(vault, Path(relative_root.rstrip("/")))
        if not absolute_root.exists():
            continue
        if absolute_root.is_symlink() or not absolute_root.is_dir():
            raise UpdateError("MANAGED_PATH_NOT_DIRECTORY")
        for candidate in absolute_root.rglob("*"):
            if candidate.is_symlink():
                raise UpdateError("MANAGED_PATH_LINK_UNSUPPORTED")
            if candidate.is_file():
                relative = candidate.relative_to(vault).as_posix()
                if path_rule(relative, policy).get("ownership") in {
                    "product_merge",
                    "product_replace",
                }:
                    result.add(relative)
    return result


def legacy_plan(args: argparse.Namespace) -> dict:
    started = time.perf_counter()
    vault = args.vault.expanduser().resolve(strict=True)
    if not vault.is_dir():
        raise UpdateError("VAULT_NOT_DIRECTORY")
    if load_state(vault) is not None:
        raise UpdateError("PRODUCT_STATE_ALREADY_EXISTS")
    policy = load_policy(args.path_policy)
    catalog, baselines = load_legacy_catalog(args.catalog)
    catalog_done = time.perf_counter()
    local_paths = scan_managed_local_paths(vault, policy)
    managed_scan_done = time.perf_counter()
    candidates = []
    for baseline in baselines:
        files = managed_product_files(baseline["product_root"], policy)
        if not files:
            raise UpdateError("MANAGED_BASELINE_EMPTY")
        file_map = dict(files)
        paths = sorted(set(file_map) | local_paths, key=lambda item: item.encode("utf-8"))
        uncertainties = []
        matched = 0
        for relative in paths:
            base = file_map.get(relative)
            local = optional_bytes(vault, relative)
            if base == local:
                matched += 1
                continue
            if base is None:
                reason = "local_only_managed_path"
            elif local is None:
                reason = "missing_local_path"
            else:
                reason = "local_modified"
            uncertainties.append(
                {
                    "base_sha256": sha256_bytes(base) if base is not None else None,
                    "local_sha256": sha256_bytes(local) if local is not None else None,
                    "path": relative,
                    "reason": reason,
                }
            )
        baseline_bytes = deterministic_baseline_zip(files)
        bundle = baseline["bundle"]
        product = bundle["components"]["product"]
        skills = bundle["components"]["wiki_skills"]
        candidates.append(
            {
                "baseline_id": baseline["id"],
                "baseline_sha256": sha256_bytes(baseline_bytes),
                "bundle_candidate_id": bundle["candidate_id"],
                "bundle_version": bundle["bundle_version"],
                "exact_match": not uncertainties,
                "managed_inventory_sha256": managed_inventory_digest(files),
                "matched_paths": matched,
                "product_commit": product["commit"],
                "product_tree": product["tree"],
                "scanned_paths": len(paths),
                "skill_commit": skills["commit"],
                "skill_version": skills["version"],
                "uncertainties": uncertainties,
            }
        )
    exact = [item["baseline_id"] for item in candidates if item["exact_match"]]
    payload = {
        "candidates": candidates,
        "catalog_sha256": sha256_bytes(canonical_json(catalog)),
        "command": "legacy-plan",
        "path_policy_sha256": sha256_bytes(args.path_policy.read_bytes()),
        "recommended_baseline_id": exact[0] if len(exact) == 1 else None,
        "status": "adoption_approval_required",
        "vault": str(vault),
    }
    payload["plan_id"] = sha256_bytes(canonical_json(payload))
    completed = time.perf_counter()
    payload["timing_ms"] = {
        "catalog": round((catalog_done - started) * 1000, 3),
        "managed_scan": round((managed_scan_done - catalog_done) * 1000, 3),
        "compare": round((completed - managed_scan_done) * 1000, 3),
        "total": round((completed - started) * 1000, 3),
    }
    return payload


def validate_legacy_approval(plan_payload: dict, approval: dict) -> dict:
    expected = {
        "approval_format",
        "approved_at",
        "baseline_id",
        "baseline_sha256",
        "plan_id",
        "subject",
    }
    if (
        set(approval) != expected
        or approval.get("approval_format") != 1
        or approval.get("plan_id") != plan_payload.get("plan_id")
        or not isinstance(approval.get("approved_at"), str)
        or not approval["approved_at"]
        or not isinstance(approval.get("subject"), str)
        or not approval["subject"]
    ):
        raise UpdateError("LEGACY_APPROVAL_MISMATCH")
    selected = next(
        (
            item
            for item in plan_payload.get("candidates", [])
            if item.get("baseline_id") == approval.get("baseline_id")
        ),
        None,
    )
    if selected is None or selected.get("baseline_sha256") != approval.get("baseline_sha256"):
        raise UpdateError("LEGACY_APPROVAL_MISMATCH")
    return selected


def legacy_adopt(args: argparse.Namespace) -> dict:
    supplied_plan = load_json(args.plan, "LEGACY_PLAN_INVALID")
    current_plan = legacy_plan(args)
    supplied_stable = {key: value for key, value in supplied_plan.items() if key != "timing_ms"}
    current_stable = {key: value for key, value in current_plan.items() if key != "timing_ms"}
    if supplied_stable != current_stable:
        raise UpdateError("LEGACY_PLAN_STALE")
    approval = load_json(args.approval, "LEGACY_APPROVAL_INVALID")
    selected = validate_legacy_approval(current_plan, approval)
    vault = args.vault.expanduser().resolve(strict=True)
    if (vault / STATE_RELATIVE).exists():
        raise UpdateError("PRODUCT_STATE_ALREADY_EXISTS")
    _catalog, baselines = load_legacy_catalog(args.catalog)
    source = next(item for item in baselines if item["id"] == selected["baseline_id"])
    policy = load_policy(args.path_policy)
    files = managed_product_files(source["product_root"], policy)
    baseline_bytes = deterministic_baseline_zip(files)
    if sha256_bytes(baseline_bytes) != selected["baseline_sha256"]:
        raise UpdateError("LEGACY_PLAN_STALE")
    cache = ensure_external_cache(vault, args.cache_root)
    bundle = source["bundle"]
    product = bundle["components"]["product"]
    skills = bundle["components"]["wiki_skills"]
    baseline_path = guarded_path(cache, Path("baselines") / f"{product['tree']}.zip")
    if baseline_path.exists():
        if baseline_path.read_bytes() != baseline_bytes:
            raise UpdateError("BASELINE_CACHE_COLLISION")
    else:
        atomic_write(baseline_path, baseline_bytes)
    transaction_id = uuid.uuid4().hex
    vault_id = uuid.uuid4().hex
    state = {
        "applied_migrations": [],
        "bundle": {
            "candidate_id": bundle["candidate_id"],
            "version": bundle["bundle_version"],
        },
        "last_transaction": transaction_id,
        "managed_inventory_sha256": selected["managed_inventory_sha256"],
        "product": {
            "base_commit": product["commit"],
            "base_tree": product["tree"],
            "baseline_sha256": selected["baseline_sha256"],
            "repository": product["repository"],
        },
        "schema_version": 1,
        "skills": {"commit": skills["commit"], "version": skills["version"]},
        "vault_id": vault_id,
    }
    receipt = seal_receipt(
        {
            "baseline_id": selected["baseline_id"],
            "baseline_sha256": selected["baseline_sha256"],
            "operation": "legacy_adopt",
            "plan_id": current_plan["plan_id"],
            "receipt_format": 1,
            "state_after_sha256": sha256_bytes(canonical_json(state)),
            "status": "completed",
            "transaction_id": transaction_id,
            "vault_id": vault_id,
        }
    )
    receipt_path = guarded_path(
        cache, Path("legacy-adoptions") / vault_cache_key(vault_id) / f"{transaction_id}.json"
    )
    atomic_write(receipt_path, pretty_json_bytes(receipt))
    state_path = guarded_path(vault, STATE_RELATIVE)
    try:
        atomic_write(state_path, pretty_json_bytes(state))
    except Exception:
        try:
            receipt_path.unlink()
        except OSError:
            pass
        raise
    return {
        "baseline_id": selected["baseline_id"],
        "baseline_path": str(baseline_path),
        "command": "legacy-adopt",
        "receipt": str(receipt_path),
        "status": "completed",
        "transaction_id": transaction_id,
        "vault_id": vault_id,
    }


def ensure_external_cache(vault: Path, cache_root: Path) -> Path:
    cache = cache_root.expanduser().resolve()
    vault_resolved = vault.resolve()
    try:
        cache.relative_to(vault_resolved)
    except ValueError:
        pass
    else:
        raise UpdateError("CACHE_ROOT_INSIDE_VAULT")
    if cache.exists() and not cache.is_dir():
        raise UpdateError("CACHE_ROOT_INVALID")
    cache.mkdir(parents=True, exist_ok=True)
    return cache


def fresh_install(args: argparse.Namespace) -> dict:
    vault = args.vault.expanduser().resolve(strict=True)
    product_root = args.product_root.expanduser().resolve(strict=True)
    if not vault.is_dir() or not product_root.is_dir():
        raise UpdateError("FRESH_INSTALL_ROOT_NOT_DIRECTORY")
    if (vault / STATE_RELATIVE).exists():
        raise UpdateError("PRODUCT_STATE_ALREADY_EXISTS")
    product = load_json(args.product_contract, "PRODUCT_CONTRACT_INVALID")
    compatibility = load_json(args.skill_compatibility, "SKILL_COMPATIBILITY_INVALID")
    bundle = load_json(args.bundle_manifest, "BUNDLE_MANIFEST_INVALID")
    validate_contracts(product, compatibility, bundle)
    policy = load_policy(args.path_policy)
    files = managed_product_files(product_root, policy)
    if not files:
        raise UpdateError("MANAGED_BASELINE_EMPTY")
    for relative, expected in files:
        if optional_bytes(vault, relative) != expected:
            raise UpdateError("FRESH_INSTALL_PRODUCT_DRIFT")
    cache = ensure_external_cache(vault, args.cache_root)
    baseline_bytes = deterministic_baseline_zip(files)
    baseline_sha256 = sha256_bytes(baseline_bytes)
    product_component = bundle["components"]["product"]
    skill_component = bundle["components"]["wiki_skills"]
    baseline_path = guarded_path(
        cache, Path("baselines") / f"{product_component['tree']}.zip"
    )
    if baseline_path.exists():
        if baseline_path.read_bytes() != baseline_bytes:
            raise UpdateError("BASELINE_CACHE_COLLISION")
    else:
        atomic_write(baseline_path, baseline_bytes)
    state = {
        "applied_migrations": [],
        "bundle": {
            "candidate_id": bundle["candidate_id"],
            "version": bundle["bundle_version"],
        },
        "last_transaction": None,
        "managed_inventory_sha256": managed_inventory_digest(files),
        "product": {
            "base_commit": product_component["commit"],
            "base_tree": product_component["tree"],
            "baseline_sha256": baseline_sha256,
            "repository": product_component["repository"],
        },
        "schema_version": 1,
        "skills": {
            "commit": skill_component["commit"],
            "version": skill_component["version"],
        },
        "vault_id": uuid.uuid4().hex,
    }
    state_path = guarded_path(vault, STATE_RELATIVE)
    atomic_write(state_path, pretty_json_bytes(state))
    return {
        "baseline_path": str(baseline_path),
        "baseline_sha256": baseline_sha256,
        "command": "fresh-install",
        "managed_inventory_sha256": state["managed_inventory_sha256"],
        "status": "completed",
        "vault_id": state["vault_id"],
    }


def optional_bytes(root: Path, relative: str) -> bytes | None:
    path = guarded_path(root, safe_relative(relative))
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
    target_root = args.target_root.expanduser().resolve(strict=True)
    if not vault.is_dir() or not target_root.is_dir():
        raise UpdateError("PLAN_ROOT_NOT_DIRECTORY")
    product = load_json(args.product_contract, "PRODUCT_CONTRACT_INVALID")
    compatibility = load_json(args.skill_compatibility, "SKILL_COMPATIBILITY_INVALID")
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
    base_root = None
    base_files = None
    if getattr(args, "base_root", None) is not None:
        base_root = args.base_root.expanduser().resolve(strict=True)
        if not base_root.is_dir():
            raise UpdateError("PLAN_ROOT_NOT_DIRECTORY")
    else:
        base_files = cached_baseline_files(args.cache_root, state)
    policy = load_policy(args.path_policy)
    changes = []
    scanned_paths = []
    candidates = set(product_paths(target_root))
    if base_root is not None:
        candidates.update(product_paths(base_root))
    else:
        candidates.update(base_files)
    for rule in policy["rules"]:
        if rule.get("ownership") in {"product_merge", "product_replace"}:
            candidates.update(rule.get("paths", []))
    for relative in sorted(candidates, key=lambda item: item.encode("utf-8")):
        rule = path_rule(relative, policy)
        if rule.get("ownership") not in {"product_merge", "product_replace"}:
            continue
        base = (
            optional_bytes(base_root, relative)
            if base_root is not None
            else base_files.get(relative)
        )
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
            in {
                "add_candidate",
                "update_candidate",
                "delete_candidate",
                "conflict",
                "conflict_delete",
            },
            "target_sha256": sha256_bytes(target) if target is not None else None,
        }
        if rule["ownership"] == "product_merge" and local != target:
            record["target_diff"] = text_diff(local, target, relative)
        record["change_sha256"] = sha256_bytes(canonical_json(record))
        changes.append(record)
    actionable = [item for item in changes if item["requires_approval"]]
    payload = {
        "bundle_candidate_id": bundle["candidate_id"],
        "bundle_version": bundle["bundle_version"],
        "changes": changes,
        "command": "plan",
        "excluded_roots": policy["excluded_roots"],
        "path_policy_sha256": sha256_bytes(args.path_policy.read_bytes()),
        "product_state_sha256": sha256_bytes(canonical_json(state)),
        "scanned_paths": scanned_paths,
        "status": "approval_required" if actionable else "up_to_date",
        "vault_id": state["vault_id"],
    }
    payload["plan_id"] = sha256_bytes(canonical_json(payload))
    return payload


def hash_at(root: Path, relative: str) -> str | None:
    content = optional_bytes(root, relative)
    return sha256_bytes(content) if content is not None else None


def acquire_lock(cache: Path, vault_id: str, transaction_id: str) -> Path:
    lock_path = guarded_path(cache, Path("locks") / f"{vault_cache_key(vault_id)}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise UpdateError("UPDATE_LOCK_BUSY") from exc
    try:
        os.write(descriptor, transaction_id.encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return lock_path


def release_lock(lock_path: Path | None) -> None:
    if lock_path is not None:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def validate_approval(plan_payload: dict, approval: dict) -> None:
    expected_keys = {
        "approval_format",
        "allow_deletes",
        "approved_at",
        "approved_changes",
        "plan_id",
        "subject",
    }
    if (
        set(approval) != expected_keys
        or approval.get("approval_format") != 1
        or approval.get("plan_id") != plan_payload["plan_id"]
        or not isinstance(approval.get("subject"), str)
        or not approval["subject"]
        or not isinstance(approval.get("approved_at"), str)
        or not isinstance(approval.get("allow_deletes"), bool)
        or not isinstance(approval.get("approved_changes"), list)
    ):
        raise UpdateError("APPROVAL_MISMATCH")
    expected = [
        {"change_sha256": item["change_sha256"], "path": item["path"]}
        for item in plan_payload["changes"]
        if item["requires_approval"]
    ]
    if (
        any(
            not isinstance(item, dict) or set(item) != {"change_sha256", "path"}
            for item in approval["approved_changes"]
        )
        or approval["approved_changes"] != expected
    ):
        raise UpdateError("APPROVAL_MISMATCH")
    if (
        any(
            item["decision"] in {"delete_candidate", "conflict_delete"}
            for item in plan_payload["changes"]
        )
        and not approval["allow_deletes"]
    ):
        raise UpdateError("DELETE_NOT_APPROVED")


def verify_hashes(vault: Path, expected: dict[str, str | None], code: str) -> None:
    for relative, digest in expected.items():
        if hash_at(vault, relative) != digest:
            raise UpdateError(code)


def backup_transaction(
    vault: Path, transaction_root: Path, expected: dict, state_content: bytes
) -> None:
    atomic_write(transaction_root / "state-before.json", state_content)
    for relative, digest in expected.items():
        content = optional_bytes(vault, relative)
        if digest is None:
            if content is not None:
                raise UpdateError("TRANSACTION_SOURCE_DRIFT")
            continue
        if content is None or sha256_bytes(content) != digest:
            raise UpdateError("TRANSACTION_SOURCE_DRIFT")
        atomic_write(transaction_root / "before" / safe_relative(relative), content)


def validate_transaction_backup(
    transaction_root: Path, before: dict, state_sha256: str
) -> None:
    try:
        state_content = (transaction_root / "state-before.json").read_bytes()
    except OSError as exc:
        raise UpdateError("BACKUP_STATE_MISSING") from exc
    if sha256_bytes(state_content) != state_sha256:
        raise UpdateError("BACKUP_STATE_DIGEST_MISMATCH")
    for relative, digest in before.items():
        if digest is None:
            continue
        try:
            content = (
                transaction_root / "before" / safe_relative(relative)
            ).read_bytes()
        except OSError as exc:
            raise UpdateError("BACKUP_MISSING") from exc
        if sha256_bytes(content) != digest:
            raise UpdateError("BACKUP_DIGEST_MISMATCH")


def restore_transaction(
    vault: Path,
    transaction_root: Path,
    before: dict,
    *,
    paths: set[str] | None = None,
    restore_state: bool = True,
) -> None:
    selected = (
        before.items()
        if paths is None
        else ((relative, before[relative]) for relative in before if relative in paths)
    )
    for relative, digest in selected:
        safe_path = safe_relative(relative)
        destination = guarded_path(vault, safe_path)
        if digest is None:
            if destination.exists():
                if not destination.is_file() or destination.is_symlink():
                    raise UpdateError("BACKUP_RESTORE_TARGET_INVALID")
                destination.unlink()
            continue
        backup = transaction_root / "before" / safe_path
        try:
            content = backup.read_bytes()
        except OSError as exc:
            raise UpdateError("BACKUP_MISSING") from exc
        if sha256_bytes(content) != digest:
            raise UpdateError("BACKUP_DIGEST_MISMATCH")
        atomic_write(destination, content)
    if restore_state:
        try:
            state_content = (transaction_root / "state-before.json").read_bytes()
        except OSError as exc:
            raise UpdateError("BACKUP_STATE_MISSING") from exc
        atomic_write(guarded_path(vault, STATE_RELATIVE), state_content)


def validate_recovery_targets(
    vault: Path,
    paths: set[str],
    before: dict,
    after: dict,
    *,
    state_allowed: set[str] | None = None,
) -> None:
    for relative in paths:
        current = hash_at(vault, relative)
        if current not in {before[relative], after[relative]}:
            raise UpdateError("RECOVERY_TARGET_DRIFT")
    if state_allowed is not None:
        current_state = sha256_bytes(guarded_path(vault, STATE_RELATIVE).read_bytes())
        if current_state not in state_allowed:
            raise UpdateError("RECOVERY_STATE_DRIFT")


def apply_update(args: argparse.Namespace) -> dict:
    vault = args.vault.expanduser().resolve(strict=True)
    target_root = args.target_root.expanduser().resolve(strict=True)
    if not vault.is_dir() or not target_root.is_dir():
        raise UpdateError("APPLY_ROOT_NOT_DIRECTORY")
    cache = ensure_external_cache(vault, args.cache_root)
    state = load_state(vault)
    if state is None:
        raise UpdateError("LEGACY_ADOPTION_REQUIRED")
    supplied_plan = load_json(args.plan, "UPDATE_PLAN_INVALID")
    current_plan = plan(args)
    if supplied_plan != current_plan:
        raise UpdateError("PLAN_STALE")
    if current_plan["status"] != "approval_required":
        raise UpdateError("PLAN_NOT_ACTIONABLE")
    if any(
        item["decision"] in {"conflict", "conflict_delete"}
        for item in current_plan["changes"]
    ):
        raise UpdateError("PLAN_HAS_CONFLICTS")
    approval = load_json(args.approval, "UPDATE_APPROVAL_INVALID")
    validate_approval(current_plan, approval)

    product = load_json(args.product_contract, "PRODUCT_CONTRACT_INVALID")
    compatibility = load_json(args.skill_compatibility, "SKILL_COMPATIBILITY_INVALID")
    bundle = load_json(args.bundle_manifest, "BUNDLE_MANIFEST_INVALID")
    validate_contracts(product, compatibility, bundle)
    policy = load_policy(args.path_policy)
    target_files = managed_product_files(target_root, policy)
    if not target_files:
        raise UpdateError("MANAGED_BASELINE_EMPTY")
    baseline_bytes = deterministic_baseline_zip(target_files)
    baseline_digest = sha256_bytes(baseline_bytes)
    target_component = bundle["components"]["product"]
    baseline_path = guarded_path(
        cache, Path("baselines") / f"{target_component['tree']}.zip"
    )
    if baseline_path.exists():
        if baseline_path.read_bytes() != baseline_bytes:
            raise UpdateError("BASELINE_CACHE_COLLISION")
    else:
        atomic_write(baseline_path, baseline_bytes)

    transaction_id = uuid.uuid4().hex
    transaction_root = guarded_path(
        cache,
        Path("backups") / vault_cache_key(state["vault_id"]) / transaction_id,
    )
    if transaction_root.exists():
        raise UpdateError("TRANSACTION_ID_COLLISION")
    actionable = [item for item in current_plan["changes"] if item["requires_approval"]]
    before = {item["path"]: item["local_sha256"] for item in actionable}
    after = {item["path"]: item["target_sha256"] for item in actionable}
    state_path = guarded_path(vault, STATE_RELATIVE)
    state_before = state_path.read_bytes()
    new_state = {
        **state,
        "bundle": {
            "candidate_id": bundle["candidate_id"],
            "version": bundle["bundle_version"],
        },
        "last_transaction": transaction_id,
        "managed_inventory_sha256": managed_inventory_digest(target_files),
        "product": {
            "base_commit": target_component["commit"],
            "base_tree": target_component["tree"],
            "baseline_sha256": baseline_digest,
            "repository": target_component["repository"],
        },
        "skills": {
            "commit": bundle["components"]["wiki_skills"]["commit"],
            "version": bundle["components"]["wiki_skills"]["version"],
        },
    }
    state_after = pretty_json_bytes(new_state)
    receipt_path = transaction_root / "apply-receipt.json"
    writes_completed = 0
    lock_path = None
    mutated: set[str] = set()
    state_mutated = False
    try:
        lock_path = acquire_lock(cache, state["vault_id"], transaction_id)
        locked_plan = plan(args)
        if supplied_plan != locked_plan:
            raise UpdateError("PLAN_STALE_DURING_APPLY")
        transaction_root.mkdir(parents=True)
        backup_transaction(vault, transaction_root, before, state_before)
        for item in actionable:
            relative = item["path"]
            if hash_at(vault, relative) != item["local_sha256"]:
                raise UpdateError("PLAN_STALE_DURING_APPLY")
            destination = guarded_path(vault, safe_relative(relative))
            target = optional_bytes(target_root, relative)
            if target is None:
                if destination.exists():
                    destination.unlink()
            else:
                atomic_write(destination, target)
            mutated.add(relative)
            writes_completed += 1
            if os.environ.get("JUNYONG_AI_TEST_MODE") == "1" and os.environ.get(
                "JUNYONG_AI_TEST_FAIL_AFTER_WRITE"
            ) == str(writes_completed):
                raise UpdateError("TEST_INJECTED_WRITE_FAILURE")
        verify_hashes(vault, after, "APPLY_WRITE_VERIFY_FAILED")
        atomic_write(state_path, state_after)
        state_mutated = True
        if (
            os.environ.get("JUNYONG_AI_TEST_MODE") == "1"
            and os.environ.get("JUNYONG_AI_TEST_FAIL_STAGE") == "after_state"
        ):
            raise UpdateError("TEST_INJECTED_STATE_FAILURE")
        receipt = seal_receipt(
            {
                "after": after,
                "before": before,
                "operation": "apply",
                "plan_id": current_plan["plan_id"],
                "receipt_format": 1,
                "state_after_sha256": sha256_bytes(state_after),
                "state_before_sha256": sha256_bytes(state_before),
                "status": "completed",
                "transaction_id": transaction_id,
                "vault_id": state["vault_id"],
            }
        )
        atomic_write(receipt_path, pretty_json_bytes(receipt))
    except Exception as exc:
        if mutated or state_mutated:
            try:
                validate_recovery_targets(
                    vault,
                    mutated,
                    before,
                    after,
                    state_allowed={
                        sha256_bytes(state_before),
                        sha256_bytes(state_after),
                    }
                    if state_mutated
                    else None,
                )
                restore_transaction(
                    vault,
                    transaction_root,
                    before,
                    paths=mutated,
                    restore_state=state_mutated,
                )
            except Exception as restore_exc:
                raise UpdateError("APPLY_FAILED_ROLLBACK_FAILED") from restore_exc
        if isinstance(exc, UpdateError) and not mutated and not state_mutated:
            raise
        if not mutated and not state_mutated:
            raise UpdateError("APPLY_FAILED_BEFORE_WRITE") from exc
        if isinstance(exc, UpdateError):
            raise UpdateError(f"APPLY_FAILED_ROLLED_BACK:{exc}") from exc
        raise UpdateError("APPLY_FAILED_ROLLED_BACK") from exc
    finally:
        release_lock(lock_path)
    return {
        "command": "apply",
        "receipt": str(receipt_path),
        "status": "completed",
        "transaction_id": transaction_id,
    }


def verify_receipt(args: argparse.Namespace) -> dict:
    vault = args.vault.expanduser().resolve(strict=True)
    if not vault.is_dir():
        raise UpdateError("VAULT_NOT_DIRECTORY")
    receipt = load_json(args.receipt, "TRANSACTION_RECEIPT_INVALID")
    validate_receipt_seal(receipt)
    validate_receipt_shape(receipt)
    operation = receipt.get("operation")
    expected = receipt.get("after") if operation == "apply" else receipt.get("restored")
    expected_state = (
        receipt.get("state_after_sha256")
        if operation == "apply"
        else receipt.get("state_restored_sha256")
    )
    if (
        operation not in {"apply", "rollback"}
        or not isinstance(expected, dict)
        or not HEX64.fullmatch(str(expected_state))
    ):
        raise UpdateError("TRANSACTION_RECEIPT_INVALID")
    verify_hashes(vault, expected, "RECEIPT_VERIFY_FAILED")
    if sha256_bytes(guarded_path(vault, STATE_RELATIVE).read_bytes()) != expected_state:
        raise UpdateError("RECEIPT_VERIFY_FAILED")
    return {
        "command": "verify",
        "operation": operation,
        "status": "verified",
        "transaction_id": receipt.get("transaction_id"),
    }


def rollback_update(args: argparse.Namespace) -> dict:
    vault = args.vault.expanduser().resolve(strict=True)
    if not vault.is_dir():
        raise UpdateError("VAULT_NOT_DIRECTORY")
    cache = ensure_external_cache(vault, args.cache_root)
    receipt_path = args.receipt.expanduser().resolve(strict=True)
    receipt = load_json(receipt_path, "TRANSACTION_RECEIPT_INVALID")
    validate_receipt_seal(receipt)
    validate_receipt_shape(receipt)
    if receipt.get("operation") != "apply":
        raise UpdateError("TRANSACTION_RECEIPT_INVALID")
    state = load_state(vault)
    if state is None or state["vault_id"] != receipt.get("vault_id"):
        raise UpdateError("ROLLBACK_VAULT_MISMATCH")
    try:
        backups_root = guarded_path(cache, Path("backups")).resolve(strict=True)
        expected_root = (
            backups_root
            / vault_cache_key(receipt["vault_id"])
            / receipt["transaction_id"]
        ).resolve(strict=True)
        expected_root.relative_to(backups_root)
    except (KeyError, OSError) as exc:
        raise UpdateError("TRANSACTION_RECEIPT_INVALID") from exc
    except ValueError as exc:
        raise UpdateError("TRANSACTION_RECEIPT_LOCATION_INVALID") from exc
    if receipt_path.parent != expected_root:
        raise UpdateError("TRANSACTION_RECEIPT_LOCATION_INVALID")
    after = receipt.get("after")
    before = receipt.get("before")
    if not isinstance(after, dict) or not isinstance(before, dict):
        raise UpdateError("TRANSACTION_RECEIPT_INVALID")
    lock_path = None
    rollback_id = uuid.uuid4().hex
    try:
        lock_path = acquire_lock(cache, state["vault_id"], rollback_id)
        verify_hashes(vault, after, "ROLLBACK_TARGET_DRIFT")
        state_after_content = guarded_path(vault, STATE_RELATIVE).read_bytes()
        if sha256_bytes(state_after_content) != receipt.get("state_after_sha256"):
            raise UpdateError("ROLLBACK_TARGET_DRIFT")
        validate_transaction_backup(
            expected_root, before, receipt.get("state_before_sha256", "")
        )
        recovery_root = expected_root / "rollback-attempts" / rollback_id
        recovery_root.mkdir(parents=True)
        backup_transaction(vault, recovery_root, after, state_after_content)
        mutated: set[str] = set()
        state_mutated = False
        try:
            for position, relative in enumerate(before, start=1):
                if hash_at(vault, relative) != after[relative]:
                    raise UpdateError("ROLLBACK_TARGET_DRIFT_DURING_ROLLBACK")
                mutated.add(relative)
                restore_transaction(
                    vault,
                    expected_root,
                    before,
                    paths={relative},
                    restore_state=False,
                )
                if os.environ.get("JUNYONG_AI_TEST_MODE") == "1" and os.environ.get(
                    "JUNYONG_AI_TEST_ROLLBACK_FAIL_AFTER_WRITE"
                ) == str(position):
                    raise UpdateError("TEST_INJECTED_ROLLBACK_FAILURE")
            if sha256_bytes(
                guarded_path(vault, STATE_RELATIVE).read_bytes()
            ) != receipt.get("state_after_sha256"):
                raise UpdateError("ROLLBACK_TARGET_DRIFT_DURING_ROLLBACK")
            restore_transaction(
                vault,
                expected_root,
                before,
                paths=set(),
                restore_state=True,
            )
            state_mutated = True
            verify_hashes(vault, before, "ROLLBACK_VERIFY_FAILED")
            state_restored_digest = sha256_bytes(
                guarded_path(vault, STATE_RELATIVE).read_bytes()
            )
            if state_restored_digest != receipt.get("state_before_sha256"):
                raise UpdateError("ROLLBACK_VERIFY_FAILED")
            rollback_receipt_path = expected_root / "rollback-receipt.json"
            rollback_receipt = seal_receipt(
                {
                    "operation": "rollback",
                    "receipt_format": 1,
                    "restored": before,
                    "source_transaction_id": receipt["transaction_id"],
                    "state_restored_sha256": state_restored_digest,
                    "status": "completed",
                    "transaction_id": rollback_id,
                    "vault_id": state["vault_id"],
                }
            )
            atomic_write(rollback_receipt_path, pretty_json_bytes(rollback_receipt))
        except Exception as exc:
            try:
                validate_recovery_targets(
                    vault,
                    mutated,
                    after,
                    before,
                    state_allowed={
                        receipt["state_after_sha256"],
                        receipt["state_before_sha256"],
                    }
                    if state_mutated
                    else None,
                )
                restore_transaction(
                    vault,
                    recovery_root,
                    after,
                    paths=mutated,
                    restore_state=state_mutated,
                )
            except Exception as recovery_exc:
                raise UpdateError("ROLLBACK_FAILED_RECOVERY_FAILED") from recovery_exc
            if isinstance(exc, UpdateError):
                raise UpdateError(f"ROLLBACK_FAILED_RESTORED:{exc}") from exc
            raise UpdateError("ROLLBACK_FAILED_RESTORED") from exc
    finally:
        release_lock(lock_path)
    return {
        "command": "rollback",
        "receipt": str(rollback_receipt_path),
        "status": "completed",
        "transaction_id": rollback_id,
    }


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
    compatibility = load_json(args.skill_compatibility, "SKILL_COMPATIBILITY_INVALID")
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
    if not version_in_range(
        state["skills"]["version"], product["runtime"]["required_range"]
    ):
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
        or state["product"]["base_commit"] != bundle["components"]["product"]["commit"]
        or state["skills"]["commit"] != bundle["components"]["wiki_skills"]["commit"]
    )
    return {
        "command": "check",
        "current_bundle_version": current,
        "status": "upgrade_available" if changed else "up_to_date",
        "target_bundle_version": target,
        "vault": str(vault),
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="客户 Vault 版本检查与受控事务更新器")
    commands = root.add_subparsers(dest="command", required=True)
    fresh_parser = commands.add_parser(
        "fresh-install", help="为新部署 Vault 写入产品状态和外部 Base 缓存"
    )
    fresh_parser.add_argument("--vault", required=True, type=Path)
    fresh_parser.add_argument("--product-root", required=True, type=Path)
    fresh_parser.add_argument("--cache-root", required=True, type=Path)
    fresh_parser.add_argument("--path-policy", required=True, type=Path)
    fresh_parser.add_argument("--product-contract", required=True, type=Path)
    fresh_parser.add_argument("--skill-compatibility", required=True, type=Path)
    fresh_parser.add_argument("--bundle-manifest", required=True, type=Path)
    legacy_plan_parser = commands.add_parser(
        "legacy-plan", help="只读比较已知历史产品树并生成旧客户纳管建议"
    )
    legacy_plan_parser.add_argument("--vault", required=True, type=Path)
    legacy_plan_parser.add_argument("--path-policy", required=True, type=Path)
    legacy_plan_parser.add_argument("--catalog", required=True, type=Path)
    legacy_adopt_parser = commands.add_parser(
        "legacy-adopt", help="按精确计划与人工审批写入旧客户产品状态"
    )
    legacy_adopt_parser.add_argument("--vault", required=True, type=Path)
    legacy_adopt_parser.add_argument("--cache-root", required=True, type=Path)
    legacy_adopt_parser.add_argument("--path-policy", required=True, type=Path)
    legacy_adopt_parser.add_argument("--catalog", required=True, type=Path)
    legacy_adopt_parser.add_argument("--plan", required=True, type=Path)
    legacy_adopt_parser.add_argument("--approval", required=True, type=Path)
    status_parser = commands.add_parser("status", help="读取客户产品状态，不产生写入")
    status_parser.add_argument("--vault", required=True, type=Path)
    check_parser = commands.add_parser(
        "check", help="检查已验签组合是否可用，不产生写入"
    )
    check_parser.add_argument("--vault", required=True, type=Path)
    check_parser.add_argument("--product-contract", required=True, type=Path)
    check_parser.add_argument("--skill-compatibility", required=True, type=Path)
    check_parser.add_argument("--bundle-manifest", required=True, type=Path)
    plan_parser = commands.add_parser("plan", help="生成三方更新建议，不产生写入")
    plan_parser.add_argument("--vault", required=True, type=Path)
    base_source = plan_parser.add_mutually_exclusive_group(required=True)
    base_source.add_argument("--base-root", type=Path)
    base_source.add_argument("--cache-root", type=Path)
    plan_parser.add_argument("--target-root", required=True, type=Path)
    plan_parser.add_argument("--path-policy", required=True, type=Path)
    plan_parser.add_argument("--product-contract", required=True, type=Path)
    plan_parser.add_argument("--skill-compatibility", required=True, type=Path)
    plan_parser.add_argument("--bundle-manifest", required=True, type=Path)
    apply_parser = commands.add_parser("apply", help="按已审批计划执行事务更新")
    apply_parser.add_argument("--vault", required=True, type=Path)
    apply_parser.add_argument("--cache-root", required=True, type=Path)
    apply_parser.add_argument("--target-root", required=True, type=Path)
    apply_parser.add_argument("--path-policy", required=True, type=Path)
    apply_parser.add_argument("--product-contract", required=True, type=Path)
    apply_parser.add_argument("--skill-compatibility", required=True, type=Path)
    apply_parser.add_argument("--bundle-manifest", required=True, type=Path)
    apply_parser.add_argument("--plan", required=True, type=Path)
    apply_parser.add_argument("--approval", required=True, type=Path)
    verify_parser = commands.add_parser("verify", help="只读验证事务回执")
    verify_parser.add_argument("--vault", required=True, type=Path)
    verify_parser.add_argument("--receipt", required=True, type=Path)
    rollback_parser = commands.add_parser("rollback", help="按回执回滚已完成事务")
    rollback_parser.add_argument("--vault", required=True, type=Path)
    rollback_parser.add_argument("--cache-root", required=True, type=Path)
    rollback_parser.add_argument("--receipt", required=True, type=Path)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "fresh-install":
            return emit(fresh_install(args))
        if args.command == "legacy-plan":
            return emit(legacy_plan(args))
        if args.command == "legacy-adopt":
            return emit(legacy_adopt(args))
        if args.command == "status":
            return emit(status(args))
        if args.command == "check":
            return emit(check(args))
        if args.command == "plan":
            return emit(plan(args))
        if args.command == "apply":
            return emit(apply_update(args))
        if args.command == "verify":
            return emit(verify_receipt(args))
        if args.command == "rollback":
            return emit(rollback_update(args))
        raise UpdateError("UNKNOWN_COMMAND")
    except UpdateError as exc:
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

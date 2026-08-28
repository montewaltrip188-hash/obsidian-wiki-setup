#!/usr/bin/env python3
"""组装、签名后验证 D3 对外发布候选。"""

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
import zipfile
from pathlib import Path, PurePosixPath

from d3_candidate import (
    D3Error,
    TARGET_MACHINES,
    digest,
    file_sha256,
    load_json,
    validate_release_plan,
)


HEX64 = re.compile(r"^[0-9a-f]{64}$")
TARGET_PLATFORM = {
    "windows-x64": "windows",
    "macos-x64": "macos",
    "macos-arm64": "macos",
}
EXPECTED_DEPENDENCIES = {
    "jieba": "0.42.1",
    "numpy": "2.5.2",
    "python": "3.12.14",
    "requests": "2.34.2",
}
SIGNATURE_ALGORITHM = "RSA-SHA256-PKCS1-v1_5"
INSTALLER_REPOSITORY = "montewaltrip188-hash/obsidian-wiki-setup"
SIGNING_POLICY_PATH = Path(__file__).with_name("release-signing-policy.json")


def safe_relative(path: str) -> Path:
    if not isinstance(path, str) or not path or "\\" in path:
        raise D3Error("D3_RELEASE_PATH_INVALID")
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts:
        raise D3Error("D3_RELEASE_PATH_INVALID")
    return Path(*pure.parts)


def load_signing_policy() -> dict:
    policy = load_json(SIGNING_POLICY_PATH, "D3_RELEASE_SIGNING_POLICY_INVALID")
    if (
        set(policy)
        != {
            "algorithm",
            "key_id",
            "minimum_rsa_bits",
            "private_key_storage",
            "public_key",
            "schema_version",
        }
        or policy.get("schema_version") != 1
        or policy.get("algorithm") != SIGNATURE_ALGORITHM
        or policy.get("minimum_rsa_bits") != 3072
        or policy.get("private_key_storage")
        != "encrypted-pkcs8-dpapi-current-user"
        or not HEX64.fullmatch(str(policy.get("key_id", "")))
        or policy.get("public_key") != "release/release-signing-public-key.pem"
    ):
        raise D3Error("D3_RELEASE_SIGNING_POLICY_INVALID")
    repository = Path(__file__).resolve().parent.parent
    public_key_entry = repository / safe_relative(policy["public_key"])
    if public_key_entry.is_symlink():
        raise D3Error("D3_RELEASE_SIGNING_POLICY_INVALID")
    public_key = public_key_entry.resolve(strict=True)
    try:
        public_key.relative_to(repository)
    except ValueError as exc:
        raise D3Error("D3_RELEASE_SIGNING_POLICY_INVALID") from exc
    if not public_key.is_file():
        raise D3Error("D3_RELEASE_SIGNING_POLICY_INVALID")
    return {**policy, "public_key_path": public_key}


def pretty_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def record(root: Path, relative: str, media_type: str, role: str) -> dict:
    path = root / safe_relative(relative)
    if not path.is_file() or path.is_symlink():
        raise D3Error("D3_RELEASE_FILE_MISSING")
    return {
        "media_type": media_type,
        "path": relative,
        "role": role,
        "sha256": file_sha256(path),
        "size": path.stat().st_size,
    }


def artifact_path(release_plan_path: Path, relative: object) -> Path:
    if not isinstance(relative, str):
        raise D3Error("D3_CANDIDATE_PATH_INVALID")
    workspace = release_plan_path.resolve().parent
    path = (workspace / safe_relative(relative)).resolve()
    try:
        path.relative_to(workspace)
    except ValueError as exc:
        raise D3Error("D3_CANDIDATE_PATH_INVALID") from exc
    return path


def load_receipt(path: Path, target: str, plan: dict) -> dict:
    receipt = load_json(path, "D3_ACCEPTANCE_RECEIPT_INVALID")
    checksum = receipt.get("receipt_sha256")
    unsealed = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if not HEX64.fullmatch(str(checksum)) or digest(unsealed) != checksum:
        raise D3Error("D3_ACCEPTANCE_RECEIPT_SEAL_INVALID")
    platform_name = TARGET_PLATFORM[target]
    candidate = plan["candidates"][platform_name]
    if (
        receipt.get("schema_version") != 1
        or receipt.get("receipt_type") != "d3-candidate-acceptance"
        or receipt.get("status") != "completed"
        or receipt.get("target") != target
        or receipt.get("bundle_version") != plan.get("bundle_version")
        or receipt.get("plan_id") != plan.get("plan_id")
        or receipt.get("candidate_id") != candidate.get("candidate_id")
        or receipt.get("candidate_sha256") != candidate.get("candidate_zip_sha256")
        or receipt.get("runtime_id") != "cpython-3.12.14+20260825"
        or str(receipt.get("architecture", "")).casefold() not in TARGET_MACHINES[target]
        or receipt.get("dependencies") != EXPECTED_DEPENDENCIES
        or receipt.get("install_status") != "installed"
        or receipt.get("query_status") != "completed"
        or receipt.get("verify_status") != "verified"
        or receipt.get("undo_status") != "undone"
    ):
        raise D3Error("D3_ACCEPTANCE_RECEIPT_MISMATCH")
    if target.startswith("macos-"):
        runner = receipt.get("runner")
        installer_commit = plan.get("sources", {}).get("installer", {}).get("commit")
        if (
            not isinstance(runner, dict)
            or runner.get("github_repository") != INSTALLER_REPOSITORY
            or runner.get("github_sha") != installer_commit
            or not str(runner.get("github_run_id", "")).isdigit()
            or not str(runner.get("github_workflow_ref", "")).startswith(
                f"{INSTALLER_REPOSITORY}/.github/workflows/d3-macos-candidate.yml@"
            )
        ):
            raise D3Error("D3_MACOS_PROVENANCE_MISSING")
    return receipt


def verify_attestation(receipt_path: Path, bundle_path: Path, plan: dict) -> dict:
    receipt = receipt_path.resolve(strict=True)
    bundle = bundle_path.resolve(strict=True)
    installer_commit = plan.get("sources", {}).get("installer", {}).get("commit")
    command = [
        "gh",
        "attestation",
        "verify",
        receipt,
        "--repo",
        INSTALLER_REPOSITORY,
        "--bundle",
        bundle,
        "--signer-workflow",
        f"{INSTALLER_REPOSITORY}/.github/workflows/d3-macos-candidate.yml",
        "--source-digest",
        installer_commit,
        "--signer-digest",
        installer_commit,
        "--deny-self-hosted-runners",
        "--format",
        "json",
    ]
    completed = subprocess.run(
        list(map(str, command)),
        text=True,
        encoding="utf-8",
        errors="strict",
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise D3Error("D3_MACOS_ATTESTATION_INVALID")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise D3Error("D3_MACOS_ATTESTATION_RECEIPT_INVALID") from exc
    if not isinstance(result, list) or not result:
        raise D3Error("D3_MACOS_ATTESTATION_RECEIPT_INVALID")
    return {"bundle_sha256": file_sha256(bundle), "verified_attestations": len(result)}


def copy_candidate(
    plan_path: Path, plan: dict, platform_name: str, output: Path, destination: str
) -> Path:
    candidate = plan["candidates"][platform_name]
    source = artifact_path(plan_path, candidate.get("first_candidate_zip"))
    if (
        not source.is_file()
        or source.stat().st_size != candidate.get("candidate_zip_size")
        or file_sha256(source) != candidate.get("candidate_zip_sha256")
    ):
        raise D3Error("D3_CANDIDATE_ASSET_DRIFT")
    target = output / safe_relative(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    if file_sha256(target) != candidate.get("candidate_zip_sha256"):
        raise D3Error("D3_CANDIDATE_COPY_DRIFT")
    return target


def copy_sbom(candidate: Path, output: Path, destination: str) -> None:
    try:
        with zipfile.ZipFile(candidate) as archive:
            info = archive.getinfo("runtime/SBOM.json")
            if info.is_dir() or info.file_size <= 0:
                raise D3Error("D3_SBOM_INVALID")
            content = archive.read(info)
            value = json.loads(content.decode("utf-8"))
            if not isinstance(value, dict):
                raise D3Error("D3_SBOM_INVALID")
    except (OSError, zipfile.BadZipFile, KeyError, UnicodeError, json.JSONDecodeError) as exc:
        raise D3Error("D3_SBOM_INVALID") from exc
    write_bytes(output / safe_relative(destination), content)


def prepare(args: argparse.Namespace) -> dict:
    plan_path = args.release_plan.resolve(strict=True)
    plan = load_json(plan_path, "D2_PLAN_INVALID")
    validate_release_plan(plan)
    receipts = {
        "windows-x64": load_receipt(args.windows_receipt, "windows-x64", plan),
        "macos-x64": load_receipt(args.macos_x64_receipt, "macos-x64", plan),
        "macos-arm64": load_receipt(args.macos_arm64_receipt, "macos-arm64", plan),
    }
    attestations = {
        "macos-x64": verify_attestation(
            args.macos_x64_receipt, args.macos_x64_attestation, plan
        ),
        "macos-arm64": verify_attestation(
            args.macos_arm64_receipt, args.macos_arm64_attestation, plan
        ),
    }
    signing_policy = load_signing_policy()
    output = args.output.resolve()
    if output.exists():
        raise D3Error("D3_RELEASE_OUTPUT_EXISTS")
    temporary = output.with_name(output.name + ".tmp-" + uuid.uuid4().hex)
    try:
        temporary.mkdir(parents=True)
        version = plan["bundle_version"]
        windows_name = f"assets/obsidian-llm-wiki-{version}-windows-x64.zip"
        macos_name = f"assets/obsidian-llm-wiki-{version}-macos-universal.zip"
        windows = copy_candidate(plan_path, plan, "windows", temporary, windows_name)
        macos = copy_candidate(plan_path, plan, "macos", temporary, macos_name)
        copy_sbom(windows, temporary, "evidence/SBOM-windows.json")
        copy_sbom(macos, temporary, "evidence/SBOM-macos.json")
        write_bytes(temporary / "evidence" / "release-plan.json", plan_path.read_bytes())
        receipt_paths = {
            "windows-x64": args.windows_receipt,
            "macos-x64": args.macos_x64_receipt,
            "macos-arm64": args.macos_arm64_receipt,
        }
        for target, source in receipt_paths.items():
            write_bytes(
                temporary / "evidence" / f"{target}-receipt.json",
                source.resolve(strict=True).read_bytes(),
            )
        attestation_paths = {
            "macos-x64": args.macos_x64_attestation,
            "macos-arm64": args.macos_arm64_attestation,
        }
        for target, source in attestation_paths.items():
            write_bytes(
                temporary / "evidence" / f"{target}-attestation.sigstore.json",
                source.resolve(strict=True).read_bytes(),
            )
        files = [
            record(temporary, windows_name, "application/zip", "candidate"),
            record(temporary, macos_name, "application/zip", "candidate"),
            record(temporary, "evidence/SBOM-windows.json", "application/json", "sbom"),
            record(temporary, "evidence/SBOM-macos.json", "application/json", "sbom"),
            record(temporary, "evidence/release-plan.json", "application/json", "d2_plan"),
        ]
        for target in ("windows-x64", "macos-x64", "macos-arm64"):
            files.append(
                record(
                    temporary,
                    f"evidence/{target}-receipt.json",
                    "application/json",
                    "acceptance_receipt",
                )
            )
        for target in ("macos-x64", "macos-arm64"):
            files.append(
                record(
                    temporary,
                    f"evidence/{target}-attestation.sigstore.json",
                    "application/vnd.dev.sigstore.bundle+json;version=0.3",
                    "provenance_attestation",
                )
            )
        manifest = {
            "bundle_version": version,
            "files": sorted(files, key=lambda item: item["path"]),
            "gates": {
                target: {
                    **(
                        {"attestation_bundle_sha256": attestations[target]["bundle_sha256"]}
                        if target in attestations
                        else {}
                    ),
                    "receipt_sha256": receipts[target]["receipt_sha256"],
                    "status": "completed",
                }
                for target in sorted(receipts)
            },
            "manifest_format": 1,
            "plan_id": plan["plan_id"],
            "release_state": "unreleased_candidate",
            "required_signature": {
                "algorithm": SIGNATURE_ALGORITHM,
                "file": "release-manifest.sig",
                "key_id": signing_policy["key_id"],
            },
            "sources": plan["sources"],
        }
        write_bytes(temporary / "release-manifest.json", pretty_json(manifest))
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    result = {
        "bundle_version": plan["bundle_version"],
        "manifest_sha256": file_sha256(output / "release-manifest.json"),
        "next_action": "signature_required",
        "status": "prepared",
        "verified_files": 10,
    }
    return result


def verify_signature(release_dir: Path, public_key: Path) -> dict:
    manifest = release_dir / "release-manifest.json"
    signature = release_dir / "release-manifest.sig"
    if os.name == "nt":
        command = [
            "pwsh", "-NoProfile", "-File",
            Path(__file__).with_name("verify-manifest.ps1"),
            "-ManifestPath", manifest,
            "-SignaturePath", signature,
            "-PublicKeyPath", public_key,
        ]
    else:
        command = [
            "sh", Path(__file__).with_name("verify-manifest.sh"),
            manifest, signature, public_key,
        ]
    completed = subprocess.run(
        list(map(str, command)), text=True, encoding="utf-8", capture_output=True, check=False
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip()
        try:
            error = json.loads(detail).get("error", "D3_RELEASE_SIGNATURE_INVALID")
        except json.JSONDecodeError:
            error = "D3_RELEASE_SIGNATURE_INVALID"
        raise D3Error(error)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise D3Error("D3_RELEASE_SIGNATURE_RECEIPT_INVALID") from exc


def verify(args: argparse.Namespace) -> dict:
    signing_policy = load_signing_policy()
    release_dir = args.release_dir.resolve(strict=True)
    manifest_path = release_dir / "release-manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = load_json(manifest_path, "D3_RELEASE_MANIFEST_INVALID")
    if manifest_bytes != pretty_json(manifest):
        raise D3Error("D3_RELEASE_MANIFEST_NOT_CANONICAL")
    if (
        manifest.get("manifest_format") != 1
        or manifest.get("bundle_version") != "2.1.0"
        or not HEX64.fullmatch(str(manifest.get("plan_id", "")))
        or manifest.get("release_state") != "unreleased_candidate"
        or not isinstance(manifest.get("required_signature"), dict)
        or manifest["required_signature"].get("algorithm") != SIGNATURE_ALGORITHM
        or manifest["required_signature"].get("file") != "release-manifest.sig"
        or set(manifest["required_signature"]) != {"algorithm", "file", "key_id"}
        or not HEX64.fullmatch(str(manifest["required_signature"].get("key_id", "")))
        or manifest["required_signature"].get("key_id") != signing_policy["key_id"]
        or not isinstance(manifest.get("files"), list)
    ):
        raise D3Error("D3_RELEASE_MANIFEST_INVALID")
    sources = manifest.get("sources")
    if not isinstance(sources, dict) or not all(
        isinstance(sources.get(component), dict)
        and re.fullmatch(r"[0-9a-f]{40}", str(sources[component].get("commit", "")))
        for component in ("product", "skill", "installer")
    ):
        raise D3Error("D3_RELEASE_MANIFEST_INVALID")
    gates = manifest.get("gates")
    if not isinstance(gates, dict) or set(gates) != set(TARGET_PLATFORM):
        raise D3Error("D3_RELEASE_MANIFEST_INVALID")
    for target in TARGET_PLATFORM:
        expected_gate_keys = {"receipt_sha256", "status"}
        if target.startswith("macos-"):
            expected_gate_keys.add("attestation_bundle_sha256")
        gate = gates[target]
        if (
            not isinstance(gate, dict)
            or set(gate) != expected_gate_keys
            or gate.get("status") != "completed"
            or not HEX64.fullmatch(str(gate.get("receipt_sha256", "")))
            or (
                target.startswith("macos-")
                and not HEX64.fullmatch(
                    str(gate.get("attestation_bundle_sha256", ""))
                )
            )
        ):
            raise D3Error("D3_RELEASE_MANIFEST_INVALID")
    version = manifest["bundle_version"]
    expected_roles = {
        f"assets/obsidian-llm-wiki-{version}-windows-x64.zip": "candidate",
        f"assets/obsidian-llm-wiki-{version}-macos-universal.zip": "candidate",
        "evidence/SBOM-windows.json": "sbom",
        "evidence/SBOM-macos.json": "sbom",
        "evidence/release-plan.json": "d2_plan",
        "evidence/windows-x64-receipt.json": "acceptance_receipt",
        "evidence/macos-x64-receipt.json": "acceptance_receipt",
        "evidence/macos-arm64-receipt.json": "acceptance_receipt",
        "evidence/macos-x64-attestation.sigstore.json": "provenance_attestation",
        "evidence/macos-arm64-attestation.sigstore.json": "provenance_attestation",
    }
    if len(manifest["files"]) != len(expected_roles):
        raise D3Error("D3_RELEASE_MANIFEST_INVALID")
    expected = {"release-manifest.json", "release-manifest.sig"}
    seen = set()
    for item in manifest["files"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"media_type", "path", "role", "sha256", "size"}
            or not HEX64.fullmatch(str(item.get("sha256", "")))
            or not isinstance(item.get("size"), int)
            or item.get("size") <= 0
            or expected_roles.get(item.get("path")) != item.get("role")
        ):
            raise D3Error("D3_RELEASE_FILE_RECORD_INVALID")
        relative = item["path"]
        path = release_dir / safe_relative(relative)
        if relative.casefold() in seen:
            raise D3Error("D3_RELEASE_FILE_COLLISION")
        seen.add(relative.casefold())
        expected.add(relative)
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size != item["size"]
            or file_sha256(path) != item["sha256"]
        ):
            raise D3Error("D3_RELEASE_ASSET_DRIFT")
    actual = {
        path.relative_to(release_dir).as_posix()
        for path in release_dir.rglob("*")
        if path.is_file()
    }
    if actual != expected:
        raise D3Error("D3_RELEASE_FILE_SET_DRIFT")
    plan = load_json(
        release_dir / "evidence" / "release-plan.json", "D3_RELEASE_PLAN_INVALID"
    )
    validate_release_plan(plan)
    if (
        plan.get("plan_id") != manifest["plan_id"]
        or plan.get("bundle_version") != manifest["bundle_version"]
        or plan.get("sources") != manifest["sources"]
    ):
        raise D3Error("D3_RELEASE_PLAN_MISMATCH")
    for target in TARGET_PLATFORM:
        receipt = load_receipt(
            release_dir / "evidence" / f"{target}-receipt.json",
            target,
            plan,
        )
        if receipt["receipt_sha256"] != manifest["gates"][target]["receipt_sha256"]:
            raise D3Error("D3_RELEASE_GATE_MISMATCH")
    for target in ("macos-x64", "macos-arm64"):
        attestation = (
            release_dir / "evidence" / f"{target}-attestation.sigstore.json"
        )
        if (
            file_sha256(attestation)
            != manifest["gates"][target]["attestation_bundle_sha256"]
        ):
            raise D3Error("D3_RELEASE_GATE_MISMATCH")
    platform_assets = {
        "windows": release_dir
        / "assets"
        / f"obsidian-llm-wiki-{manifest['bundle_version']}-windows-x64.zip",
        "macos": release_dir
        / "assets"
        / f"obsidian-llm-wiki-{manifest['bundle_version']}-macos-universal.zip",
    }
    for platform_name, asset in platform_assets.items():
        candidate = plan["candidates"][platform_name]
        if (
            asset.stat().st_size != candidate["candidate_zip_size"]
            or file_sha256(asset) != candidate["candidate_zip_sha256"]
        ):
            raise D3Error("D3_RELEASE_CANDIDATE_MISMATCH")
    signature = verify_signature(release_dir, signing_policy["public_key_path"])
    if signature.get("key_id") != manifest["required_signature"]["key_id"]:
        raise D3Error("D3_RELEASE_SIGNING_KEY_MISMATCH")
    return {
        "bundle_version": manifest["bundle_version"],
        "key_id": signature.get("key_id"),
        "manifest_sha256": file_sha256(manifest_path),
        "status": "verified",
        "verified_files": len(manifest["files"]),
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    prepare_command = commands.add_parser("prepare")
    prepare_command.add_argument("--release-plan", required=True, type=Path)
    prepare_command.add_argument("--windows-receipt", required=True, type=Path)
    prepare_command.add_argument("--macos-x64-receipt", required=True, type=Path)
    prepare_command.add_argument("--macos-arm64-receipt", required=True, type=Path)
    prepare_command.add_argument("--macos-x64-attestation", required=True, type=Path)
    prepare_command.add_argument("--macos-arm64-attestation", required=True, type=Path)
    prepare_command.add_argument("--output", required=True, type=Path)
    verify_command = commands.add_parser("verify")
    verify_command.add_argument("--release-dir", required=True, type=Path)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        result = prepare(args) if args.command == "prepare" else verify(args)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (D3Error, OSError, KeyError, TypeError, ValueError) as exc:
        error = str(exc) if isinstance(exc, D3Error) else "D3_RELEASE_INTERNAL_ERROR"
        print(json.dumps({"error": error, "status": "blocked"}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

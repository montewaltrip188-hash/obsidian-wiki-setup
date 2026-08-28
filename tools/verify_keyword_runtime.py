#!/usr/bin/env python3
"""在纯合成 Vault 中验证离线依赖与关键词 Query，绝不接触客户内容。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


LOCKED_PACKAGES = {
    "jieba": "0.42.1",
    "numpy": "2.5.2",
    "requests": "2.34.2",
}
EXPECTED_RESULT = "wiki/concepts/offline-runtime-probe.md"


class ProbeError(RuntimeError):
    pass


def file_snapshot(root: Path) -> dict[str, dict[str, object]]:
    result = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        content = path.read_bytes()
        result[relative] = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }
    return result


def run(arguments: list[str], *, environment: dict[str, str] | None = None) -> str:
    process_environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
    if environment:
        process_environment.update(environment)
    completed = subprocess.run(
        arguments,
        text=True,
        encoding="utf-8",
        errors="strict",
        capture_output=True,
        env=process_environment,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise ProbeError(detail[-1] if detail else f"exit={completed.returncode}")
    return completed.stdout


def dependency_versions(runtime_python: Path) -> dict[str, str]:
    code = (
        "import json,sys,jieba,numpy,requests;"
        "print(json.dumps({'python':'.'.join(map(str,sys.version_info[:3])),"
        "'jieba':jieba.__version__,'numpy':numpy.__version__,"
        "'requests':requests.__version__},sort_keys=True))"
    )
    try:
        value = json.loads(run([str(runtime_python), "-B", "-c", code]).strip())
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ProbeError("DEPENDENCY_PROBE_INVALID") from exc
    if not isinstance(value, dict):
        raise ProbeError("DEPENDENCY_PROBE_INVALID")
    return {str(key): str(item) for key, item in value.items()}


def prepare_synthetic_vault(root: Path) -> None:
    note = root / EXPECTED_RESULT
    note.parent.mkdir(parents=True)
    note.write_text("# 离线运行时探针\n\n离线关键词探针。\n", encoding="utf-8")
    connection = sqlite3.connect(root / ".state.db")
    try:
        connection.executescript(
            """
            CREATE TABLE wiki_embeddings (
                path TEXT NOT NULL, chunk_idx INTEGER NOT NULL,
                content_hash TEXT NOT NULL, vector BLOB NOT NULL,
                mtime REAL NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY (path, chunk_idx)
            );
            CREATE TABLE wiki_keywords (
                word TEXT NOT NULL, path TEXT NOT NULL,
                freq INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (word, path)
            );
            CREATE TABLE wiki_meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        connection.execute(
            "INSERT INTO wiki_keywords (word, path, freq) VALUES (?, ?, ?)",
            ("离线", str(note), 1),
        )
        connection.commit()
    finally:
        connection.close()


def parse_receipt(output: str) -> dict:
    for line in reversed(output.splitlines()):
        if line.startswith("RECEIPT_JSON:"):
            try:
                value = json.loads(line.split(":", 1)[1])
            except json.JSONDecodeError as exc:
                raise ProbeError("QUERY_RECEIPT_INVALID") from exc
            if isinstance(value, dict):
                return value
    raise ProbeError("QUERY_RECEIPT_MISSING")


def verify(args: argparse.Namespace) -> dict:
    runtime_python = args.runtime_python.expanduser().resolve(strict=True)
    query_script = args.query_script.expanduser().resolve(strict=True)
    runtime_root = args.runtime_root.expanduser().resolve(strict=True)
    if not runtime_python.is_file() or not query_script.is_file():
        raise ProbeError("PROBE_INPUT_INVALID")
    if not runtime_root.is_dir():
        raise ProbeError("RUNTIME_ROOT_INVALID")
    runtime_before = file_snapshot(runtime_root)
    versions = dependency_versions(runtime_python)
    if versions.get("python") != args.expected_python:
        raise ProbeError("PYTHON_VERSION_MISMATCH")
    if not args.skip_locked_package_versions:
        for package, expected in LOCKED_PACKAGES.items():
            if versions.get(package) != expected:
                raise ProbeError(f"LOCKED_PACKAGE_VERSION_MISMATCH:{package}")

    with tempfile.TemporaryDirectory(prefix="offline-query-probe-") as temporary:
        vault = Path(temporary)
        prepare_synthetic_vault(vault)
        before = file_snapshot(vault)
        environment = {
            **os.environ,
            "KB_ROOT": str(vault),
            "OLLAMA_URL": "http://127.0.0.1:1",
            "PYTHONUTF8": "1",
        }
        output = run(
            [str(runtime_python), "-B", str(query_script), "search", "离线"],
            environment=environment,
        )
        receipt = parse_receipt(output)
        after = file_snapshot(vault)
        if after != before:
            raise ProbeError("KEYWORD_QUERY_MUTATED_SYNTHETIC_VAULT")
        paths = [str(item).replace("\\", "/") for item in receipt.get("result_paths", [])]
        if (
            receipt.get("action") != "search"
            or receipt.get("status") not in {"completed", "degraded"}
            or EXPECTED_RESULT not in paths
            or receipt.get("answerability") != "candidate_supported"
        ):
            raise ProbeError("KEYWORD_QUERY_PROBE_FAILED")
    if file_snapshot(runtime_root) != runtime_before:
        raise ProbeError("KEYWORD_QUERY_MUTATED_RUNTIME_TREE")
    return {
        "dependencies": versions,
        "probe": "keyword_query",
        "result_path": EXPECTED_RESULT,
        "status": "completed",
        "runtime_tree_unchanged": True,
        "synthetic_vault_unchanged": True,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="验证受管离线关键词 Query 运行时")
    result.add_argument("--runtime-python", required=True, type=Path)
    result.add_argument("--query-script", required=True, type=Path)
    result.add_argument("--runtime-root", required=True, type=Path)
    result.add_argument("--expected-python", default="3.12.14")
    result.add_argument("--skip-locked-package-versions", action="store_true")
    return result


def main() -> int:
    try:
        receipt = verify(parser().parse_args())
    except (OSError, ProbeError, subprocess.SubprocessError) as exc:
        print(
            json.dumps({"error": str(exc), "status": "blocked"}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

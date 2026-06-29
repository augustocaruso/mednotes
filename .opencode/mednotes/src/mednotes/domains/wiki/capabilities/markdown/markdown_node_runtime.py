"""Persistent Node runtime setup for the MarkdownDB query adapter."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from pydantic import Field, StrictStr
from pydantic import ValidationError as PydanticValidationError

from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter

MARKDOWN_NODE_RUNTIME_SCHEMA = "medical-notes-workbench.markdown-node-runtime.v1"
MARKDOWN_NODE_RUNTIME_DIR = Path("node-runtime/markdown-db")
MARKDOWN_QUERY_BLOCKED_REASON = "markdown_query_index_unavailable"
MARKDOWN_QUERY_NEXT_ACTION = "Rodar /mednotes:setup para preparar o índice Markdown e repetir o workflow."
MIN_NODE_VERSION = (20, 17, 0)
MIN_NODE_VERSION_TEXT = ".".join(str(part) for part in MIN_NODE_VERSION)


class _MarkdownNodeRuntimeStatusPayload(ContractModel):
    schema_: StrictStr = Field(alias="schema", serialization_alias="schema")
    status: StrictStr
    runtime_root: StrictStr = ""
    node_modules_path: StrictStr = ""
    package_lock_hash: StrictStr = ""
    stale_reason: StrictStr = ""
    missing: list[StrictStr] = Field(default_factory=list)
    node_version: StrictStr = ""
    install_skipped_reason: StrictStr = ""


class _MarkdownNodeRuntimeReceiptPayload(ContractModel):
    schema_: StrictStr = Field(default=MARKDOWN_NODE_RUNTIME_SCHEMA, alias="schema", serialization_alias="schema")
    status: StrictStr
    package_lock_hash: StrictStr


class MarkdownNodeRuntimeUnavailable(RuntimeError):
    blocked_reason = MARKDOWN_QUERY_BLOCKED_REASON
    next_action = MARKDOWN_QUERY_NEXT_ACTION

    def __init__(self, message: str, *, payload: JsonObject | None = None) -> None:
        super().__init__(message)
        self.payload = JsonObjectAdapter.validate_python(payload or {})


def _runtime_payload(
    *,
    status: str,
    runtime_root: Path,
    node_modules_path: Path,
    package_lock_hash: str = "",
    stale_reason: str = "",
    missing: list[str] | None = None,
    node_version: str = "",
    install_skipped_reason: str = "",
) -> JsonObject:
    return _MarkdownNodeRuntimeStatusPayload(
        schema=MARKDOWN_NODE_RUNTIME_SCHEMA,
        status=status,
        runtime_root=str(runtime_root),
        node_modules_path=str(node_modules_path),
        package_lock_hash=package_lock_hash,
        stale_reason=stale_reason,
        missing=missing or [],
        node_version=node_version,
        install_skipped_reason=install_skipped_reason,
    ).to_payload()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_node_version(raw_version: str) -> tuple[int, int, int] | None:
    version = raw_version.strip()
    if version.startswith("v"):
        version = version[1:]
    parts = version.split(".")
    if len(parts) < 3:
        return None
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2].split("-", 1)[0]))
    except ValueError:
        return None


def _ensure_supported_node_version(node: str) -> str:
    proc = subprocess.run(
        [node, "--version"],
        text=True,
        capture_output=True,
        check=False,
    )
    raw_version = proc.stdout.strip()
    parsed = _parse_node_version(raw_version)
    if proc.returncode != 0 or parsed is None or parsed < MIN_NODE_VERSION:
        raise MarkdownNodeRuntimeUnavailable(
            f"Node.js {MIN_NODE_VERSION_TEXT} or newer is required to prepare the Markdown query runtime.",
            payload={
                "node": node,
                "node_version": raw_version,
                "minimum_node_version": MIN_NODE_VERSION_TEXT,
                "stderr": proc.stderr[-1000:],
            },
        )
    return raw_version


def _runtime_root(state_dir: Path) -> Path:
    return state_dir / MARKDOWN_NODE_RUNTIME_DIR


def _receipt_path(runtime_root: Path) -> Path:
    return runtime_root / "install-receipt.json"


def _receipt_current(receipt_path: Path, *, package_lock_hash: str, node_modules_path: Path) -> bool:
    if not receipt_path.exists() or not node_modules_path.joinpath("mddb").exists():
        return False
    try:
        receipt = _MarkdownNodeRuntimeReceiptPayload.model_validate(
            JsonObjectAdapter.validate_python(json.loads(receipt_path.read_text(encoding="utf-8")))
        )
    except (json.JSONDecodeError, PydanticValidationError):
        return False
    return receipt.package_lock_hash == package_lock_hash and receipt.status == "ready"


def _adjacent_node_modules_path(extension_root: Path) -> Path | None:
    candidates = (
        extension_root / "node_modules",
        extension_root.parent / "node_modules",
    )
    for candidate in candidates:
        if candidate.joinpath("mddb").exists():
            return candidate
    return None


def markdown_node_runtime_status(*, extension_root: Path, state_dir: Path) -> JsonObject:
    package_json = extension_root / "package.json"
    package_lock = extension_root / "package-lock.json"
    runtime_root = _runtime_root(state_dir)
    node_modules_path = runtime_root / "node_modules"
    receipt = _receipt_path(runtime_root)
    missing = [str(path) for path in (package_json, package_lock) if not path.exists()]
    if missing:
        return _runtime_payload(
            status="blocked",
            runtime_root=runtime_root,
            node_modules_path=node_modules_path,
            stale_reason="package_metadata_missing",
            missing=missing,
        )

    package_lock_hash = _sha256(package_lock)
    if not node_modules_path.joinpath("mddb").exists():
        adjacent_node_modules = _adjacent_node_modules_path(extension_root)
        if adjacent_node_modules is not None:
            return _runtime_payload(
                status="ready",
                runtime_root=runtime_root,
                node_modules_path=adjacent_node_modules,
                package_lock_hash=package_lock_hash,
                install_skipped_reason="adjacent_node_modules",
            )
        return _runtime_payload(
            status="missing",
            runtime_root=runtime_root,
            node_modules_path=node_modules_path,
            package_lock_hash=package_lock_hash,
            stale_reason="runtime_missing",
        )
    if not receipt.exists():
        return _runtime_payload(
            status="stale",
            runtime_root=runtime_root,
            node_modules_path=node_modules_path,
            package_lock_hash=package_lock_hash,
            stale_reason="receipt_missing",
        )
    if not _receipt_current(receipt, package_lock_hash=package_lock_hash, node_modules_path=node_modules_path):
        return _runtime_payload(
            status="stale",
            runtime_root=runtime_root,
            node_modules_path=node_modules_path,
            package_lock_hash=package_lock_hash,
            stale_reason="receipt_stale",
        )
    return _runtime_payload(
        status="ready",
        runtime_root=runtime_root,
        node_modules_path=node_modules_path,
        package_lock_hash=package_lock_hash,
    )


def ensure_markdown_node_runtime(*, extension_root: Path, state_dir: Path) -> JsonObject:
    package_json = extension_root / "package.json"
    package_lock = extension_root / "package-lock.json"
    if not package_json.exists() or not package_lock.exists():
        raise MarkdownNodeRuntimeUnavailable(
            "Markdown query runtime package metadata is missing from the extension bundle.",
            payload={"missing": [str(path) for path in (package_json, package_lock) if not path.exists()]},
        )

    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        raise MarkdownNodeRuntimeUnavailable(
            "Node.js and npm are required to prepare the Markdown query runtime.",
            payload={"node": node or "", "npm": npm or ""},
        )
    node_version = _ensure_supported_node_version(node)

    runtime_root = _runtime_root(state_dir)
    node_modules_path = runtime_root / "node_modules"
    package_lock_hash = _sha256(package_lock)
    receipt = _receipt_path(runtime_root)
    if _receipt_current(receipt, package_lock_hash=package_lock_hash, node_modules_path=node_modules_path):
        return _runtime_payload(
            status="ready",
            runtime_root=runtime_root,
            node_modules_path=node_modules_path,
            package_lock_hash=package_lock_hash,
            node_version=node_version,
            install_skipped_reason="receipt_current",
        )

    adjacent_node_modules = _adjacent_node_modules_path(extension_root)
    if adjacent_node_modules is not None:
        return _runtime_payload(
            status="ready",
            runtime_root=runtime_root,
            node_modules_path=adjacent_node_modules,
            package_lock_hash=package_lock_hash,
            node_version=node_version,
            install_skipped_reason="adjacent_node_modules",
        )

    runtime_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(package_json, runtime_root / "package.json")
    shutil.copy2(package_lock, runtime_root / "package-lock.json")
    proc = subprocess.run(
        [npm, "ci", "--omit=dev"],
        cwd=runtime_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise MarkdownNodeRuntimeUnavailable(
            "npm ci failed while preparing the Markdown query runtime.",
            payload={"returncode": proc.returncode, "stderr": proc.stderr[-4000:]},
        )
    if not node_modules_path.joinpath("mddb").exists():
        raise MarkdownNodeRuntimeUnavailable(
            "MarkdownDB package was not installed in the persistent Node runtime.",
            payload={"node_modules_path": str(node_modules_path)},
        )
    payload = _runtime_payload(
        status="ready",
        runtime_root=runtime_root,
        node_modules_path=node_modules_path,
        package_lock_hash=package_lock_hash,
        node_version=node_version,
        install_skipped_reason="",
    )
    receipt.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload

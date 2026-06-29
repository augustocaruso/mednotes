"""Python boundary for the MarkdownDB-backed chat metadata index."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field, StrictStr

from mednotes.domains.wiki.capabilities.markdown.markdown_node_runtime import (
    MarkdownNodeRuntimeUnavailable,
    ensure_markdown_node_runtime,
)
from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter
from mednotes.platform.paths import extension_root as _resolve_extension_root
from mednotes.platform.paths import user_state_dir

MARKDOWN_QUERY_BLOCKED_REASON = "markdown_query_index_unavailable"
MARKDOWN_QUERY_NEXT_ACTION = "Rodar /mednotes:setup para preparar o índice Markdown e repetir o workflow."
MARKDOWN_QUERY_CACHE_DIR = Path("cache/markdown-db")


@dataclass(frozen=True)
class ChatMetadata:
    id: str
    title: str
    url: str
    date_created: str = ""
    date_exported: str = ""


class _AdapterChatMetadata(ContractModel):
    id: StrictStr = ""
    title: StrictStr = ""
    url: StrictStr = ""
    date_created: StrictStr = ""
    date_exported: StrictStr = ""


class _LookupChatPayload(ContractModel):
    schema_: StrictStr = Field(default="", alias="schema", serialization_alias="schema")
    status: StrictStr = ""
    chat: _AdapterChatMetadata | None = None


class MarkdownQueryUnavailable(RuntimeError):
    blocked_reason = MARKDOWN_QUERY_BLOCKED_REASON
    next_action = MARKDOWN_QUERY_NEXT_ACTION

    def __init__(self, message: str, *, payload: JsonObject | None = None) -> None:
        super().__init__(message)
        self.payload = payload or {}


class MarkdownDbChatMetadataProvider:
    def __init__(
        self,
        *,
        wiki_dir: Path,
        raw_dir: Path,
        cache_dir: Path | None = None,
        node_modules_path: Path | None = None,
        adapter_path: Path | None = None,
    ) -> None:
        self.wiki_dir = wiki_dir
        self.raw_dir = raw_dir
        self.cache_dir = cache_dir or user_state_dir() / MARKDOWN_QUERY_CACHE_DIR
        self.node_modules_path = node_modules_path
        self.adapter_path = adapter_path or Path(__file__).with_name("markdown_db_adapter.mjs")

    def status(self) -> JsonObject:
        return self._run("status")

    def rebuild(self) -> JsonObject:
        return self._run("rebuild")

    def probe(self) -> JsonObject:
        return self._run("probe")

    def lookup_chat(self, chat_id: str) -> ChatMetadata | None:
        payload = self._run("lookup-chat", "--chat-id", chat_id)
        lookup = _LookupChatPayload.model_validate(payload)
        if lookup.status == "missing":
            return None
        if lookup.chat is None:
            raise MarkdownQueryUnavailable("Markdown query adapter returned invalid chat payload.", payload=payload)
        return ChatMetadata(
            id=lookup.chat.id,
            title=lookup.chat.title,
            url=lookup.chat.url,
            date_created=lookup.chat.date_created,
            date_exported=lookup.chat.date_exported,
        )

    def _run(self, command: str, *extra: str) -> JsonObject:
        env = os.environ.copy()
        if self.node_modules_path is not None:
            env["MEDNOTES_MARKDOWNDB_NODE_PATH"] = str(self.node_modules_path)
        cmd = [
            "node",
            str(self.adapter_path),
            command,
            "--wiki-dir",
            str(self.wiki_dir),
            "--raw-dir",
            str(self.raw_dir),
            "--cache-dir",
            str(self.cache_dir),
            *extra,
        ]
        try:
            proc = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
        except FileNotFoundError as exc:
            raise MarkdownQueryUnavailable(
                "Node.js is required to query the Markdown index.",
                payload=_json_object(
                    {
                        "status": "blocked",
                        "blocked_reason": MARKDOWN_QUERY_BLOCKED_REASON,
                        "next_action": MARKDOWN_QUERY_NEXT_ACTION,
                        "error": str(exc),
                    }
                ),
            ) from exc
        if proc.returncode != 0:
            raise MarkdownQueryUnavailable(
                "Markdown query adapter failed.",
                payload=_parse_payload(proc.stderr, fallback_text=proc.stderr),
            )
        return _parse_payload(proc.stdout, fallback_text=proc.stdout)


def _parse_payload(text: str, *, fallback_text: str = "") -> JsonObject:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        fallback = _json_object(
            {
                "status": "blocked",
                "blocked_reason": MARKDOWN_QUERY_BLOCKED_REASON,
                "next_action": MARKDOWN_QUERY_NEXT_ACTION,
                "error": "Markdown query adapter returned non-JSON output.",
                "raw_output": fallback_text[-4000:],
            }
        )
        raise MarkdownQueryUnavailable("Markdown query adapter returned non-JSON output.", payload=fallback) from exc
    if not isinstance(payload, dict):
        raise MarkdownQueryUnavailable(
            "Markdown query adapter returned a non-object JSON payload.",
            payload=_json_object(
                {
                    "status": "blocked",
                    "blocked_reason": MARKDOWN_QUERY_BLOCKED_REASON,
                    "next_action": MARKDOWN_QUERY_NEXT_ACTION,
                    "error": "non-object JSON payload",
                }
            ),
        )
    return _json_object(payload)


def require_markdown_query_available(
    *,
    wiki_dir: Path,
    raw_dir: Path,
    cache_dir: Path | None = None,
    node_modules_path: Path | None = None,
) -> None:
    provider = MarkdownDbChatMetadataProvider(
        wiki_dir=wiki_dir,
        raw_dir=raw_dir,
        cache_dir=cache_dir,
        node_modules_path=node_modules_path,
    )
    provider.probe()


def ensure_markdown_query_available(
    *,
    wiki_dir: Path,
    raw_dir: Path,
    cache_dir: Path | None = None,
    extension_root: Path | None = None,
    state_dir: Path | None = None,
) -> JsonObject:
    try:
        runtime = _json_object(
            ensure_markdown_node_runtime(
                extension_root=extension_root or _resolve_extension_root(),
                state_dir=state_dir or user_state_dir(),
            )
        )
    except MarkdownNodeRuntimeUnavailable as exc:
        raise MarkdownQueryUnavailable(str(exc), payload=exc.payload) from exc
    node_modules_path = runtime.get("node_modules_path")
    require_markdown_query_available(
        wiki_dir=wiki_dir,
        raw_dir=raw_dir,
        cache_dir=cache_dir,
        node_modules_path=Path(str(node_modules_path)),
    )
    return runtime


def markdown_query_blocked_payload(*, phase: str, required_inputs: list[str]) -> JsonObject:
    return _json_object(
        {
            "phase": phase,
            "status": "blocked",
            "blocked_reason": MARKDOWN_QUERY_BLOCKED_REASON,
            "next_action": MARKDOWN_QUERY_NEXT_ACTION,
            "required_inputs": required_inputs,
            "human_decision_required": False,
        }
    )


def _json_object(payload: object) -> JsonObject:
    return JsonObjectAdapter.validate_python(payload)

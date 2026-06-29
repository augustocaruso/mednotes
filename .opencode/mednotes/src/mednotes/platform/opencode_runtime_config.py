"""Project OpenCode runtime settings derived from the MedNotes user TOML.

This module owns only model/effort projection into OpenCode files. Workflow
state, recovery, retries, and apply policy remain FSM-owned.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Final

import yaml

from mednotes.platform.paths import find_config
from mednotes.platform.user_config import AgentRuntimeConfig, MedNotesUserConfig, load_user_config

SYNC_SCHEMA: Final = "mednotes.opencode-runtime-config-sync.v1"
OPENCODE_AGENT_CONFIG_FIELDS: Final = {
    "med-chat-triager": "med_chat_triager",
    "med-flashcard-maker": "med_flashcard_maker",
    "med-knowledge-architect": "med_knowledge_architect",
    "med-link-graph-curator": "med_link_graph_curator",
    "med-publish-guard": "med_publish_guard",
}


def opencode_runtime_config_for_agent(
    agent_id: str,
    user_config: MedNotesUserConfig,
    *,
    default_model: str = "",
) -> AgentRuntimeConfig:
    field_name = OPENCODE_AGENT_CONFIG_FIELDS.get(agent_id)
    if field_name is None:
        if not default_model:
            raise ValueError(f"OpenCode agent is not user-configurable: {agent_id}")
        return AgentRuntimeConfig(model=default_model)
    return getattr(user_config.agents, field_name)


def opencode_runtime_configs(
    user_config: MedNotesUserConfig,
    *,
    agent_ids: Iterable[str] | None = None,
    default_models: Mapping[str, str] | None = None,
) -> dict[str, AgentRuntimeConfig]:
    selected_agent_ids = tuple(agent_ids or OPENCODE_AGENT_CONFIG_FIELDS)
    defaults = default_models or {}
    return {
        agent_id: opencode_runtime_config_for_agent(
            agent_id,
            user_config,
            default_model=defaults.get(agent_id, ""),
        )
        for agent_id in selected_agent_ids
    }


def sync_opencode_user_config(
    *,
    project_root: Path,
    user_config_path: Path | None = None,
    agent_ids: Iterable[str] | None = None,
) -> dict[str, object]:
    project_root = project_root.resolve()
    resolved_user_config_path = user_config_path or find_config(start=project_root)
    user_config = load_user_config(resolved_user_config_path)
    runtime_by_agent = opencode_runtime_configs(user_config, agent_ids=agent_ids)
    config_path = project_root / ".opencode" / "opencode.json"

    _sync_opencode_json(config_path, runtime_by_agent=runtime_by_agent)
    _sync_agent_frontmatter(project_root / ".opencode" / "agents", runtime_by_agent=runtime_by_agent)
    return {
        "schema": SYNC_SCHEMA,
        "status": "synced",
        "project_root": str(project_root),
        "config_path": str(config_path),
        "user_config_path": str(resolved_user_config_path) if resolved_user_config_path else "",
        "agents": [
            {
                "id": agent_id,
                "model": runtime_config.model,
                "reasoning_effort": runtime_config.reasoning_effort,
            }
            for agent_id, runtime_config in sorted(runtime_by_agent.items())
        ],
    }


def _sync_opencode_json(path: Path, *, runtime_by_agent: dict[str, AgentRuntimeConfig]) -> None:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"OpenCode config invalid for runtime sync: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"OpenCode config must be an object for runtime sync: {path}")
    agent_section = raw.setdefault("agent", {})
    if not isinstance(agent_section, dict):
        raise ValueError(f"OpenCode config agent section must be an object for runtime sync: {path}")
    for agent_id, runtime_config in runtime_by_agent.items():
        entry = agent_section.setdefault(agent_id, {})
        if not isinstance(entry, dict):
            raise ValueError(f"OpenCode config agent entry must be an object for runtime sync: {agent_id}")
        entry["model"] = runtime_config.model
        entry["reasoningEffort"] = runtime_config.reasoning_effort
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sync_agent_frontmatter(agent_dir: Path, *, runtime_by_agent: dict[str, AgentRuntimeConfig]) -> None:
    for agent_id, runtime_config in runtime_by_agent.items():
        agent_path = agent_dir / f"{agent_id}.md"
        frontmatter, body = _read_agent_markdown(agent_path)
        frontmatter["model"] = runtime_config.model
        frontmatter["reasoningEffort"] = runtime_config.reasoning_effort
        next_text = _render_frontmatter(frontmatter) + "\n" + body
        if agent_path.read_text(encoding="utf-8") != next_text:
            agent_path.write_text(next_text, encoding="utf-8")


def _read_agent_markdown(path: Path) -> tuple[dict[str, object], str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"missing frontmatter: {path}")
    try:
        _start, frontmatter_text, body = text.split("---\n", 2)
    except ValueError as exc:
        raise ValueError(f"invalid frontmatter: {path}") from exc
    loaded: object = yaml.safe_load(frontmatter_text)
    if not isinstance(loaded, dict):
        raise ValueError(f"frontmatter must be an object: {path}")
    return {str(key): value for key, value in loaded.items()}, body.lstrip()


def _render_frontmatter(fields: dict[str, object]) -> str:
    lines = ["---"]
    for key in sorted(fields):
        _append_frontmatter_value(lines, key, fields[key], indent=0)
    lines.append("---")
    return "\n".join(lines) + "\n"


def _append_frontmatter_value(lines: list[str], key: str, value: object, *, indent: int) -> None:
    prefix = " " * indent
    if isinstance(value, dict):
        lines.append(f"{prefix}{key}:")
        for child_key in sorted(value):
            _append_frontmatter_value(lines, str(child_key), value[child_key], indent=indent + 2)
        return
    if isinstance(value, list):
        lines.append(f"{prefix}{key}:")
        for item in value:
            _append_frontmatter_list_item(lines, item, indent=indent + 2)
        return
    lines.append(f"{prefix}{key}: {_frontmatter_scalar(value)}")


def _append_frontmatter_list_item(lines: list[str], value: object, *, indent: int) -> None:
    prefix = " " * indent
    if isinstance(value, dict):
        lines.append(f"{prefix}-")
        for child_key in sorted(value):
            _append_frontmatter_value(lines, str(child_key), value[child_key], indent=indent + 2)
        return
    if isinstance(value, list):
        lines.append(f"{prefix}-")
        for item in value:
            _append_frontmatter_list_item(lines, item, indent=indent + 2)
        return
    lines.append(f"{prefix}- {_frontmatter_scalar(value)}")


def _frontmatter_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    text = str(value)
    if re.fullmatch(r"[A-Za-z0-9_./:@-]+", text):
        return text
    return json.dumps(text, ensure_ascii=False)

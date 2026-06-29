"""Typed user configuration loaded from the MedNotes TOML file.

The config model is intentionally narrow: it parameterizes paths, concurrency,
runtime model ids, and secret references. Workflow state, recovery, retry,
apply, and human decision remain owned by the FSM layer.
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, StrictStr

from mednotes.kernel.base import ContractModel

_TOP_LEVEL_KEYS: Final = frozenset(("paths", "parallelism", "agents", "secrets"))
ReasoningEffort = Literal["minimal", "low", "medium", "high"]


class PathsConfig(ContractModel):
    wiki_dir: StrictStr = ""
    raw_dir: StrictStr = ""


class ParallelismConfig(ContractModel):
    fix_wiki_max_parallel_rewrites: int = Field(default=3, ge=1, le=16)
    process_chats_max_parallel_triagers: int = Field(default=3, ge=1, le=16)
    process_chats_max_parallel_architects: int = Field(default=3, ge=1, le=16)
    link_max_parallel_curators: int = Field(default=3, ge=1, le=16)


class AgentRuntimeConfig(ContractModel):
    model: StrictStr = Field(min_length=1)
    reasoning_effort: ReasoningEffort = "high"


class AgentsConfig(ContractModel):
    med_chat_triager: AgentRuntimeConfig = Field(
        default_factory=lambda: AgentRuntimeConfig(
            model="antigravity/gemini-3.5-flash",
            reasoning_effort="medium",
        )
    )
    med_publish_guard: AgentRuntimeConfig = Field(
        default_factory=lambda: AgentRuntimeConfig(
            model="antigravity/gemini-3.5-flash",
            reasoning_effort="high",
        )
    )
    med_link_graph_curator: AgentRuntimeConfig = Field(
        default_factory=lambda: AgentRuntimeConfig(
            model="antigravity/gemini-3.5-flash",
            reasoning_effort="high",
        )
    )
    med_knowledge_architect: AgentRuntimeConfig = Field(
        default_factory=lambda: AgentRuntimeConfig(
            model="antigravity/gemini-3.1-pro",
            reasoning_effort="high",
        )
    )
    med_flashcard_maker: AgentRuntimeConfig = Field(
        default_factory=lambda: AgentRuntimeConfig(
            model="antigravity/gemini-3.1-pro",
            reasoning_effort="high",
        )
    )


_AGENT_CONFIG_KEYS: Final = frozenset(AgentsConfig.model_fields)


class SecretConfig(ContractModel):
    keyring_service: StrictStr = "mednotes"
    keyring_username: StrictStr = "serpapi"
    env: tuple[StrictStr, ...] = ("SERPAPI_KEY", "SERPAPI_API_KEY")


class SecretsConfig(ContractModel):
    serpapi: SecretConfig = Field(default_factory=SecretConfig)


class MedNotesUserConfig(ContractModel):
    paths: PathsConfig = Field(default_factory=PathsConfig)
    parallelism: ParallelismConfig = Field(default_factory=ParallelismConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)


def load_user_config(path: Path | None) -> MedNotesUserConfig:
    if path is None or not path.exists():
        return MedNotesUserConfig()
    raw_config = tomllib.loads(path.read_text(encoding="utf-8"))
    config_payload = {key: value for key, value in raw_config.items() if key in _TOP_LEVEL_KEYS}
    agents = raw_config.get("agents")
    if isinstance(agents, dict):
        config_payload["agents"] = {
            key: value for key, value in agents.items() if key in _AGENT_CONFIG_KEYS and isinstance(value, dict)
        }
    else:
        config_payload.pop("agents", None)
    return MedNotesUserConfig.model_validate(config_payload)

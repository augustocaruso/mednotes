from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, Field, field_validator

from mednotes.kernel.base import ContractModel

RelatedNotesEmbeddingProfileId = Literal["clean_v1", "raw_v1", "legacy_v0"]
RelatedNotesRepresentationHashBasis = Literal["profile_cleaned_markdown", "raw_markdown", "legacy_hybrid_markdown"]

_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"


def _validate_relative_posix_path(value: str) -> str:
    text = value.strip()
    if (
        not text or text.startswith(("/", "../")) or "\\" in text or "//" in text or any(part in {"", ".", ".."} for part in text.split("/"))
    ):
        raise ValueError("path must be a wiki-relative POSIX path")
    return text


class RelatedNotesExportPlugin(ContractModel):
    name: Literal["related-notes-obsidian"]
    version: str = Field(min_length=1)


class RelatedNotesExportModel(ContractModel):
    provider: str | None = None
    embedding_model: str = Field(min_length=1)
    dimension: int | None = Field(default=None, gt=0)
    embedding_profile_id: RelatedNotesEmbeddingProfileId
    embedding_profile_version: Literal[1]
    representation_hash_basis: RelatedNotesRepresentationHashBasis


class RelatedNotesExportNote(ContractModel):
    path: str = Field(min_length=1)
    title: str = Field(min_length=1)
    content_hash: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("path")
    @classmethod
    def path_must_be_relative_posix(cls, value: str) -> str:
        return _validate_relative_posix_path(value)


class RelatedNotesExportEdge(ContractModel):
    source_path: str = Field(min_length=1)
    target_path: str = Field(min_length=1)
    score: float = Field(ge=0.0, le=1.0)
    rank: int = Field(ge=1)
    source: Literal["related-notes-obsidian"] = "related-notes-obsidian"

    @field_validator("source_path", "target_path")
    @classmethod
    def path_must_be_relative_posix(cls, value: str) -> str:
        return _validate_relative_posix_path(value)


class RelatedNotesExport(ContractModel):
    schema_: Literal["medical-notes-workbench.related-notes-export.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    generated_at: datetime
    vault_root: str = Field(min_length=1)
    plugin: RelatedNotesExportPlugin
    model_info: RelatedNotesExportModel = Field(alias="model", serialization_alias="model")
    score_scale: Literal["0_to_1"]
    notes: list[RelatedNotesExportNote]
    edges: list[RelatedNotesExportEdge]

    @field_validator("generated_at")
    @classmethod
    def generated_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must include timezone")
        return value


class RelatedNotesHashMigrationModel(ContractModel):
    """Minimal model slice needed to decide legacy hash migration."""

    model_config = ConfigDict(extra="ignore", strict=True)

    embedding_profile_id: RelatedNotesEmbeddingProfileId


class RelatedNotesHashMigrationExport(ContractModel):
    """Typed migration boundary for historical plugin exports.

    Older plugin exports may lack metadata required by the full public export
    contract. Hash migration only needs the schema, profile and per-note hashes,
    so this closed decision lens avoids raw dict reads without rejecting valid
    legacy migration inputs.
    """

    model_config = ConfigDict(extra="ignore", strict=True)

    schema_: Literal["medical-notes-workbench.related-notes-export.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    model_info: RelatedNotesHashMigrationModel = Field(alias="model", serialization_alias="model")
    notes: list[RelatedNotesExportNote]


class RelatedNotesHeadlessExportSummary(ContractModel):
    schema_: str | None = Field(default=None, alias="schema", serialization_alias="schema")
    status: str = ""
    phase: str = ""
    blocked_reason: str = ""
    detail: str = ""
    export_path: str = ""
    index_path: str = ""
    wiki_dir: str = ""
    note_count: int = Field(default=0, ge=0)
    edge_count: int = Field(default=0, ge=0)
    record_count: int = Field(default=0, ge=0)
    total_note_count: int = Field(default=0, ge=0)
    fresh_record_count: int = Field(default=0, ge=0)
    stale_record_count: int = Field(default=0, ge=0)
    remaining_count: int = Field(default=0, ge=0)
    partial_record_count: int = Field(default=0, ge=0)
    embedded_count: int = Field(default=0, ge=0)
    reused_count: int = Field(default=0, ge=0)
    embedding_model: str = ""
    embedding_profile_id: str = ""
    embedding_request_delay_ms: int = Field(default=0, ge=0)
    embedding_transient_retry_count: int = Field(default=0, ge=0)
    related_notes_limit: int = Field(default=0, ge=0)
    next_retry_after_seconds: int | None = Field(default=None, ge=0)
    resume_supported: bool = False

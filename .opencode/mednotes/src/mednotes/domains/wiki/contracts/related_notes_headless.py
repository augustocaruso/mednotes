"""Typed boundaries for the headless Related Notes adapter.

The headless adapter reads Obsidian plugin JSON, a vector index and Gemini HTTP
responses. Those payloads are external data; workflow decisions must use these
models before checking status, currentness, vectors, counts or retry details.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, StrictFloat, StrictInt, StrictStr

from mednotes.kernel.base import ContractModel, JsonObject

RELATED_NOTES_HASH_MIGRATION_CACHE_SCHEMA = "medical-notes-workbench.related-notes-hash-migration-cache.v1"


class RelatedNotesHashMigrationCache(ContractModel):
    """Persisted cache entry for the clean_v1 hash migration shortcut."""

    schema_: Literal["medical-notes-workbench.related-notes-hash-migration-cache.v1"] = Field(
        alias="schema",
        serialization_alias="schema",
    )
    identity: JsonObject
    status: Literal[
        "no_legacy_clean_v1_hashes",
        "migrated_clean_v1_hashes",
        "legacy_clean_v1_vectors_missing",
    ]
    migrated_note_count: int = Field(default=0, ge=0)
    skipped_note_count: int = Field(default=0, ge=0)


class RelatedNotesHeadlessSettings(ContractModel):
    """Strict subset of plugin settings that drives embedding/relation policy."""

    model_config = ConfigDict(extra="ignore", strict=True)

    gemini_api_key: str = Field(default="", alias="geminiApiKey", serialization_alias="geminiApiKey")
    default_embedding_profile: str = Field(
        default="",
        alias="defaultEmbeddingProfile",
        serialization_alias="defaultEmbeddingProfile",
    )
    related_notes_limit: int | None = Field(
        default=None,
        alias="relatedNotesLimit",
        serialization_alias="relatedNotesLimit",
    )
    embedding_request_delay_ms: int | float | None = Field(
        default=None,
        alias="embeddingRequestDelayMs",
        serialization_alias="embeddingRequestDelayMs",
    )


class RelatedNotesVectorRecord(ContractModel):
    """One plugin vector-index record after validation."""

    model_config = ConfigDict(extra="forbid", strict=True)

    path: StrictStr = ""
    title: StrictStr = ""
    folder: StrictStr = ""
    preview: StrictStr = ""
    raw_content_hash: StrictStr = Field(default="", alias="rawContentHash", serialization_alias="rawContentHash")
    representation_hash: StrictStr = Field(
        default="",
        alias="representationHash",
        serialization_alias="representationHash",
    )
    content_hash: StrictStr = Field(default="", alias="contentHash", serialization_alias="contentHash")
    mtime: StrictInt = 0
    embedding_model: StrictStr = Field(default="", alias="embeddingModel", serialization_alias="embeddingModel")
    embedding_profile: StrictStr = Field(default="", alias="embeddingProfile", serialization_alias="embeddingProfile")
    embedding_profile_version: StrictInt = Field(
        default=0,
        alias="embeddingProfileVersion",
        serialization_alias="embeddingProfileVersion",
    )
    vector: list[StrictFloat] = Field(default_factory=list, min_length=1)
    updated_at: StrictInt = Field(default=0, alias="updatedAt", serialization_alias="updatedAt")


class RelatedNotesVectorProfile(ContractModel):
    """A profile bucket from the plugin vector index."""

    model_config = ConfigDict(extra="ignore", strict=True)

    records: dict[str, RelatedNotesVectorRecord] = Field(default_factory=dict)


class RelatedNotesVectorIndex(ContractModel):
    """Validated vector index with typed records per profile."""

    model_config = ConfigDict(extra="ignore", strict=True)

    profiles: dict[str, RelatedNotesVectorProfile] = Field(default_factory=dict)

    def records_for_profile(self, profile_id: str) -> dict[str, RelatedNotesVectorRecord]:
        profile = self.profiles[profile_id] if profile_id in self.profiles else RelatedNotesVectorProfile()
        return dict(profile.records)

    def other_profiles_payload(self, profile_id: str) -> JsonObject:
        return {
            key: value.to_payload()
            for key, value in self.profiles.items()
            if key != profile_id
        }


class GeminiEmbedding(ContractModel):
    """Gemini embedding vector payload."""

    model_config = ConfigDict(extra="ignore")

    values: list[float]


class GeminiEmbeddingResponse(ContractModel):
    """Single Gemini embedding response."""

    model_config = ConfigDict(extra="ignore")

    embedding: GeminiEmbedding


class GeminiBatchEmbeddingResponse(ContractModel):
    """Batch Gemini embedding response."""

    model_config = ConfigDict(extra="ignore")

    embeddings: list[GeminiEmbedding]


class GeminiError(ContractModel):
    """Error object returned by Gemini HTTP APIs."""

    model_config = ConfigDict(extra="ignore")

    code: int | str = ""
    status: str = ""
    message: str = ""


class GeminiErrorResponse(ContractModel):
    """Typed Gemini error wrapper used only for redacted diagnostics."""

    model_config = ConfigDict(extra="ignore")

    error: GeminiError | None = None

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictBool, StrictStr, ValidationInfo, field_validator, model_validator

from mednotes.kernel.base import ContractModel, JsonObject

ANKI_MODEL_SET_VALIDATION_SCHEMA = "medical-notes-workbench.anki-model-set-validation.v1"
FLASHCARD_REPORT_SCHEMA = "medical-notes-workbench.flashcard-report.v1"
FLASHCARD_APPLY_SCHEMA = "medical-notes-workbench.flashcard-apply-result.v1"
FLASHCARDS_TAGGING_RECEIPT_SCHEMA = "medical-notes-workbench.flashcards-tagging-receipt.v1"


class FlashcardObsidianLinkError(ValueError):
    """Raised when a candidate card carries a divergent Obsidian deeplink."""


def _clean(value: str) -> str:
    return str(value or "").strip()


class FlashcardsTaggingReceipt(ContractModel):
    """Receipt proving the Obsidian `anki` tag step ran through the FSM effect."""

    schema_id: Literal["medical-notes-workbench.flashcards-tagging-receipt.v1"] = Field(
        default=FLASHCARDS_TAGGING_RECEIPT_SCHEMA,
        alias="schema",
    )
    workflow: Literal["/flashcards"] = "/flashcards"
    effect_target: Literal["flashcards.tag_obsidian"] = "flashcards.tag_obsidian"
    status: Literal["completed", "completed_noop", "dry_run"]
    changed_files: list[str] = Field(default_factory=list)
    tag: str = Field(min_length=1)
    vault_guard_receipt: JsonObject = Field(default_factory=dict)
    records: list[JsonObject] = Field(default_factory=list)


class FlashcardsVaultGuardReceipt(ContractModel):
    """Vault guard proof required before the flashcards workflow mutates Obsidian tags."""

    schema_id: StrictStr = Field(default="", alias="schema")
    workflow: Literal["/flashcards"]
    effect_kind: Literal["run_subworkflow"]
    effect_target: Literal["flashcards.tag_obsidian"]
    resource_guard_active: StrictBool
    rollback_declared: StrictBool
    receipt_id: StrictStr = Field(min_length=1)


class FlashcardSourceNote(ContractModel):
    path: str
    title: str = ""
    absolute_path: str
    path_style: Literal["posix", "windows_drive", "windows_unc"]
    vault_root: str = ""
    vault_name: str = ""
    vault_relative_path: str = ""
    link_mode: Literal["vault_file", "absolute_path"] = "absolute_path"
    deeplink: str
    deeplink_mode: Literal["vault_file", "absolute_path"]
    deeplink_candidates: list[str] = Field(default_factory=list)
    deck: str
    frontmatter_tags: list[str] = Field(default_factory=list)
    inline_tags: list[str] = Field(default_factory=list)
    content_sha256: str
    tags: list[str] = Field(default_factory=list)
    already_marked_anki: bool = False
    line_count: int = Field(default=0, ge=0)
    heading_count: int = Field(default=0, ge=0)
    skip_reason: str = ""
    skip_tags: list[str] = Field(default_factory=list)

    @field_validator("path", "absolute_path", "deeplink", "deck", "content_sha256")
    @classmethod
    def _required_text(cls, value: str, info: ValidationInfo) -> str:
        cleaned = _clean(value)
        if not cleaned:
            raise ValueError(f"{info.field_name} must be non-empty")
        return cleaned


class FlashcardSourceScope(ContractModel):
    raw: str = ""
    inputs: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    skip_tags: list[str] = Field(default_factory=list)
    folders: list[str] = Field(default_factory=list)
    tag_match: str = "any"
    vault_root: str = ""
    vault_name: str = ""


class FlashcardSourceSummary(ContractModel):
    candidate_file_count: int = Field(default=0, ge=0)
    file_count: int = Field(default=0, ge=0)
    skipped_count: int = Field(default=0, ge=0)
    requires_confirmation: bool = False
    confirmation_reasons: list[str] = Field(default_factory=list)
    card_candidate_confirmation_limit: int = Field(default=0, ge=0)


class FlashcardSourceManifest(ContractModel):
    schema_id: str = Field(default="medical-notes-workbench.flashcard-sources.v1", alias="schema")
    dry_run: bool = False
    scope: FlashcardSourceScope = Field(default_factory=FlashcardSourceScope)
    summary: FlashcardSourceSummary = Field(default_factory=FlashcardSourceSummary)
    notes: list[FlashcardSourceNote] = Field(default_factory=list)
    skipped_notes: list[FlashcardSourceNote] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def note_by_path(self) -> dict[str, FlashcardSourceNote]:
        return {note.path: note for note in self.notes}


class FlashcardCandidateFields(ContractModel):
    Frente: str = ""
    Verso: str = ""
    Verso_Extra: str = Field(default="", alias="Verso Extra")
    Obsidian: str = ""
    Texto: str = ""


class FlashcardCandidateCard(ContractModel):
    source_path: str
    source_content_sha256: str
    deck: str = ""
    note_model: str
    fields: FlashcardCandidateFields

    @field_validator("source_path", "source_content_sha256", "note_model")
    @classmethod
    def _required_text(cls, value: str, info: ValidationInfo) -> str:
        cleaned = _clean(value)
        if not cleaned:
            raise ValueError(f"{info.field_name} must be non-empty")
        return cleaned


class FlashcardCandidateBatch(ContractModel):
    source_manifest: FlashcardSourceManifest
    candidate_cards: list[FlashcardCandidateCard] = Field(default_factory=list)

    @model_validator(mode="after")
    def _sources_must_match_manifest(self) -> FlashcardCandidateBatch:
        notes = self.source_manifest.note_by_path()
        for card in self.candidate_cards:
            if card.source_path not in notes:
                raise ValueError(
                    f"candidate source_path not present in source_manifest: {card.source_path}"
                )
            note = notes[card.source_path]
            if card.source_content_sha256 != note.content_sha256:
                raise ValueError(f"source_content_sha256 mismatch for {card.source_path}")
        return self


class FlashcardPreparedCard(FlashcardCandidateCard):
    card_hash: str
    source_relative_path: str = ""
    duplicate_of: str = ""


class FlashcardAnkiNoteOptions(ContractModel):
    allowDuplicate: bool = False


class FlashcardAnkiNote(ContractModel):
    deckName: str
    modelName: str
    fields: FlashcardCandidateFields
    tags: list[str] = Field(default_factory=list)
    options: FlashcardAnkiNoteOptions = Field(default_factory=FlashcardAnkiNoteOptions)


class FlashcardFindQuery(ContractModel):
    card_hash: str
    query: str


class FlashcardIndexSummary(ContractModel):
    candidate_count: int = Field(ge=0)
    new_count: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)


class FlashcardIndexCheck(ContractModel):
    schema_id: str = Field(
        default="medical-notes-workbench.flashcard-index-check.v1",
        alias="schema",
    )
    new_cards: list[FlashcardPreparedCard] = Field(default_factory=list)
    duplicate_cards: list[FlashcardPreparedCard] = Field(default_factory=list)
    summary: FlashcardIndexSummary


class FlashcardCheckedModel(ContractModel):
    name: str
    fields: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)


class FlashcardResolvedModel(ContractModel):
    ok: bool = False
    model: str = ""
    fields: list[str] = Field(default_factory=list)
    required_fields: list[str] = Field(default_factory=list)
    checked_models: list[FlashcardCheckedModel] = Field(default_factory=list)


class FlashcardModelValidation(ContractModel):
    schema_id: str = Field(default="", alias="schema")
    ok: bool
    model: str = ""
    fields: list[str] = Field(default_factory=list)
    required_fields: list[str] = Field(default_factory=list)
    checked_models: list[FlashcardCheckedModel] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    missing_kinds: list[str] = Field(default_factory=list)
    qa: FlashcardResolvedModel | None = None
    cloze: FlashcardResolvedModel | None = None
    warning: str = ""

    @model_validator(mode="after")
    def _model_set_ok_requires_complete_models(self) -> FlashcardModelValidation:
        if self.schema_id != ANKI_MODEL_SET_VALIDATION_SCHEMA or not self.ok:
            return self

        invalid_kinds: list[str] = []
        for kind, resolved_model in (("qa", self.qa), ("cloze", self.cloze)):
            if (
                resolved_model is None
                or not resolved_model.ok
                or not _clean(resolved_model.model)
            ):
                invalid_kinds.append(kind)

        if invalid_kinds:
            kinds = ", ".join(invalid_kinds)
            raise ValueError(
                "model-set validation with ok=true requires complete qa and cloze "
                f"model results; missing or invalid: {kinds}"
            )
        return self


class FlashcardPreferredModels(ContractModel):
    qa: str = ""
    cloze: str = ""

    @field_validator("qa", "cloze", mode="before")
    @classmethod
    def _optional_text(cls, value: object) -> str:
        return value.strip() if isinstance(value, str) else ""


class FlashcardModelPreference(ContractModel):
    preferred_model: str = ""
    preferred_models: FlashcardPreferredModels = Field(default_factory=FlashcardPreferredModels)

    @field_validator("preferred_model", mode="before")
    @classmethod
    def _optional_text(cls, value: object) -> str:
        return value.strip() if isinstance(value, str) else ""


class FlashcardSourceStatusRecord(ContractModel):
    path: str
    vault_relative_path: str = ""
    status: str
    current_content_sha256: str = ""
    indexed_content_sha256: str = ""
    indexed_card_count: int = Field(default=0, ge=0)


class FlashcardSourceStatusSummary(ContractModel):
    new_count: int = Field(default=0, ge=0)
    unchanged_count: int = Field(default=0, ge=0)
    changed_count: int = Field(default=0, ge=0)


class FlashcardSourceStatus(ContractModel):
    schema_id: str = Field(
        default="medical-notes-workbench.flashcards-source-status.v1",
        alias="schema",
    )
    summary: FlashcardSourceStatusSummary = Field(default_factory=FlashcardSourceStatusSummary)
    sources: list[FlashcardSourceStatusRecord] = Field(default_factory=list)


class FlashcardWriteSummary(ContractModel):
    candidate_count: int = Field(ge=0)
    new_count: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)
    changed_source_count: int = Field(ge=0)
    anki_note_count: int = Field(ge=0)


class FlashcardWritePlan(ContractModel):
    schema_id: Literal["medical-notes-workbench.flashcard-write-plan.v1"] = Field(
        default="medical-notes-workbench.flashcard-write-plan.v1",
        alias="schema",
    )
    blocked: bool = False
    blocked_reason: str = ""
    next_action: str = ""
    requires_reprocess_confirmation: bool = False
    model_validation: FlashcardModelValidation
    source_status: FlashcardSourceStatus
    index_check: FlashcardIndexCheck
    changed_sources: list[FlashcardSourceStatusRecord] = Field(default_factory=list)
    anki_find_queries: list[FlashcardFindQuery] = Field(default_factory=list)
    anki_notes: list[FlashcardAnkiNote] = Field(default_factory=list)
    new_cards: list[FlashcardPreparedCard] = Field(default_factory=list)
    duplicate_cards: list[FlashcardPreparedCard] = Field(default_factory=list)
    summary: FlashcardWriteSummary


class FlashcardAcceptedCard(FlashcardPreparedCard):
    anki_note_id: int


class FlashcardReportSummary(ContractModel):
    processed_note_count: int = Field(ge=0)
    created_card_count: int = Field(ge=0)
    duplicate_card_count: int = Field(ge=0)
    skipped_note_count: int = Field(ge=0)
    model_error_count: int = Field(ge=0)
    anki_error_count: int = Field(ge=0)
    obsidian_links_valid: bool = Field(strict=True)


class FlashcardModelErrorSummary(ContractModel):
    required_fields: list[str] = Field(default_factory=list)
    checked_models: list[FlashcardCheckedModel] = Field(default_factory=list)


class FlashcardReport(ContractModel):
    """Typed final report consumed by /flashcards FSM apply projection."""

    schema_id: Literal["medical-notes-workbench.flashcard-report.v1"] = Field(
        default=FLASHCARD_REPORT_SCHEMA,
        alias="schema",
    )
    summary: FlashcardReportSummary
    processed_sources: list[str] = Field(default_factory=list)
    duplicate_cards: list[JsonObject] = Field(default_factory=list)
    skipped_notes: list[JsonObject] = Field(default_factory=list)
    model_error: FlashcardModelErrorSummary | None = None
    anki_errors: list[str] = Field(default_factory=list)


class FlashcardApplyResult(ContractModel):
    """Typed apply command result; this is the only apply payload the FSM reads."""

    schema_id: Literal["medical-notes-workbench.flashcard-apply-result.v1"] = Field(
        default=FLASHCARD_APPLY_SCHEMA,
        alias="schema",
    )
    summary: JsonObject = Field(default_factory=dict)
    report: FlashcardReport


def normalize_candidate_batch(batch: FlashcardCandidateBatch) -> FlashcardCandidateBatch:
    notes = batch.source_manifest.note_by_path()
    normalized_cards: list[FlashcardCandidateCard] = []
    for card in batch.candidate_cards:
        note = notes[card.source_path]
        fields = card.fields.model_copy()
        if not fields.Obsidian:
            fields.Obsidian = note.deeplink
        elif fields.Obsidian != note.deeplink:
            raise FlashcardObsidianLinkError(f"divergent Obsidian link for {card.source_path}")
        normalized_cards.append(
            card.model_copy(
                update={
                    "deck": card.deck or note.deck,
                    "fields": fields,
                }
            )
        )
    return FlashcardCandidateBatch(
        source_manifest=batch.source_manifest,
        candidate_cards=normalized_cards,
    )

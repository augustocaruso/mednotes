"""Typed raw-coverage contracts for process-chats publish boundaries.

The raw coverage file is authored outside the publish adapter, so JSON is
allowed at the file boundary only. Once loaded, coverage decisions and publish
counts must read these Pydantic models instead of loose dictionaries.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Literal

from pydantic import ConfigDict, Field, StrictBool, StrictStr, model_validator

from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter

RAW_COVERAGE_SCHEMA = "medical-notes-workbench.raw-coverage.v1"
CoverageAction = Literal["planned_meaning", "not_a_note"]
RawCoverageSourceStatus = Literal["covered", "already_covered", "not_relevant"]


class RawCoverageItem(ContractModel):
    """One exhaustive coverage decision for material in a raw chat."""

    id: StrictStr = Field(min_length=1)
    action: CoverageAction
    title: StrictStr | None = None
    staged_title: StrictStr | None = None
    meaning_claim: JsonObject | None = None
    reason_code: StrictStr | None = None
    reason: StrictStr | None = None
    status: StrictStr | None = None
    detail: StrictStr | None = None
    coverage: JsonObject | None = None

    @model_validator(mode="after")
    def validate_coverage_payload(self) -> RawCoverageItem:
        if self.action == "planned_meaning":
            if not (self.title or self.staged_title):
                raise ValueError("planned_meaning coverage items require title or staged_title")
        elif self.action == "not_a_note" and not self.reason:
            raise ValueError("not_a_note coverage items require reason")
        return self

    @property
    def planned_title(self) -> str:
        return (self.staged_title or self.title or "").strip()


class RawCoverageSource(ContractModel):
    """Per-raw provenance statement for multi-source process-chats coverage."""

    raw_file: StrictStr = Field(min_length=1)
    status: RawCoverageSourceStatus
    target_title: StrictStr = ""
    target_section: StrictStr = ""
    new_information_summary: StrictStr = ""
    reference_added: StrictStr = ""
    reason: StrictStr = ""
    existing_title: StrictStr = ""

    @model_validator(mode="after")
    def validate_status_payload(self) -> RawCoverageSource:
        if self.status == "covered":
            missing = [
                field
                for field in (
                    "target_title",
                    "target_section",
                    "new_information_summary",
                    "reference_added",
                )
                if not getattr(self, field).strip()
            ]
            if missing:
                raise ValueError(f"covered source requires {', '.join(missing)}")
        elif self.status in {"already_covered", "not_relevant"} and not self.reason.strip():
            raise ValueError(f"{self.status} source requires reason")
        return self

    def compact_payload(self) -> JsonObject:
        payload = self.model_dump(mode="json", exclude_defaults=True)
        return JsonObjectAdapter.validate_python(payload)


class RawCoverage(ContractModel):
    """Canonical raw-coverage inventory consumed by publish and validation."""

    schema_id: Literal["medical-notes-workbench.raw-coverage.v1"] = Field(alias="schema")
    raw_file: StrictStr = Field(min_length=1)
    exhaustive: Literal[True]
    items: list[RawCoverageItem] = Field(min_length=1)
    raw_files: list[StrictStr] = Field(default_factory=list)
    sources: list[RawCoverageSource] = Field(default_factory=list)
    batch_id: StrictStr | None = None
    run_id: StrictStr | None = None
    note_plan_hash: StrictStr | None = None
    coverage_hash: StrictStr | None = None
    source_artifact_hash: StrictStr | None = None


class RawCoverageSummary(ContractModel):
    """Typed summary projected from a validated raw coverage inventory."""

    schema_id: Literal["medical-notes-workbench.raw-coverage.v1"] = Field(
        default=RAW_COVERAGE_SCHEMA,
        alias="schema",
    )
    status: Literal["", "valid"] = ""
    coverage_path: StrictStr = ""
    coverage_hash: StrictStr = ""
    raw_file: StrictStr = ""
    raw_files: list[StrictStr] = Field(default_factory=list)
    multi_source: StrictBool = False
    source_count: int = Field(default=0, ge=0, strict=True)
    exhaustive: StrictBool = False
    item_count: int = Field(default=0, ge=0, strict=True)
    planned_meaning_count: int = Field(default=0, ge=0, strict=True)
    not_a_note_count: int = Field(default=0, ge=0, strict=True)
    staged_note_count: int = Field(default=0, ge=0, strict=True)
    raw_file_count: int = Field(default=0, ge=0, strict=True)
    covered_count: int = Field(default=0, ge=0, strict=True)
    sources: list[RawCoverageSource] = Field(default_factory=list)
    source_status_counts: dict[str, int] = Field(default_factory=dict)
    coverage_hashes: list[StrictStr] = Field(default_factory=list)
    batch_id: StrictStr = ""
    run_id: StrictStr = ""
    note_plan_hash: StrictStr = ""
    source_artifact_hash: StrictStr = ""
    note_plan_source_count: int = Field(default=0, ge=0, strict=True)
    note_plan_hashes: dict[str, str] = Field(default_factory=dict)
    note_plan_item_count: int = Field(default=0, ge=0, strict=True)
    note_plan_planned_meaning_count: int = Field(default=0, ge=0, strict=True)
    note_plan_attach_count: int = Field(default=0, ge=0, strict=True)
    note_plan_not_a_note_count: int = Field(default=0, ge=0, strict=True)
    note_plan_needs_context_count: int = Field(default=0, ge=0, strict=True)

    def to_payload(self) -> JsonObject:
        payload = self.model_dump(mode="json", by_alias=True)
        if self.sources:
            payload["sources"] = [source.compact_payload() for source in self.sources]
        else:
            payload.pop("sources", None)
        payload = {key: value for key, value in payload.items() if value not in ("", [], {})}
        for key in (
            "multi_source",
            "source_count",
            "exhaustive",
            "item_count",
            "planned_meaning_count",
            "not_a_note_count",
            "staged_note_count",
            "raw_file_count",
            "covered_count",
        ):
            payload[key] = getattr(self, key)
        return JsonObjectAdapter.validate_python(payload)


class RawCoveragePlanBatch(ContractModel):
    """Typed lens over publish's planned-batch projection."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    raw_file: StrictStr = ""
    raw_files: list[StrictStr] = Field(default_factory=list)
    coverage: RawCoverageSummary | None = None


def coverage_summary_from_batches(plan: Sequence[RawCoveragePlanBatch]) -> RawCoverageSummary:
    """Aggregate validated planned batches without reading raw dictionaries."""

    raw_files: list[str] = []
    seen_raw_files: set[str] = set()
    source_status_counts: Counter[str] = Counter()
    coverage_hashes: list[str] = []
    item_count = 0
    planned_meaning_count = 0
    not_a_note_count = 0
    staged_note_count = 0
    source_count = 0

    for batch in plan:
        batch_raw_files = batch.raw_files or ([batch.raw_file] if batch.raw_file else [])
        for raw_file in batch_raw_files:
            raw_file_text = raw_file.strip()
            if raw_file_text and raw_file_text not in seen_raw_files:
                seen_raw_files.add(raw_file_text)
                raw_files.append(raw_file_text)
        if batch.coverage is None:
            continue
        coverage = batch.coverage
        if coverage.coverage_hash:
            coverage_hashes.append(coverage.coverage_hash)
        item_count += coverage.item_count
        planned_meaning_count += coverage.planned_meaning_count
        not_a_note_count += coverage.not_a_note_count
        staged_note_count += coverage.staged_note_count
        source_count += coverage.source_count
        source_status_counts.update(coverage.source_status_counts)

    return RawCoverageSummary(
        status="valid" if plan else "",
        raw_files=raw_files,
        multi_source=len(raw_files) > 1,
        source_count=source_count or len(raw_files),
        exhaustive=bool(plan),
        item_count=item_count,
        planned_meaning_count=planned_meaning_count,
        not_a_note_count=not_a_note_count,
        staged_note_count=staged_note_count,
        raw_file_count=len(raw_files),
        covered_count=len(raw_files),
        source_status_counts=dict(source_status_counts),
        coverage_hash=coverage_hashes[0] if len(coverage_hashes) == 1 else "",
        coverage_hashes=coverage_hashes,
    )

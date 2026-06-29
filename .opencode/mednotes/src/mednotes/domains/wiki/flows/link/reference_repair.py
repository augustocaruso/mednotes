"""Reference repair planning for the Wiki link workflow.

The planner turns graph-audit issues into an explicit note-by-note action list.
It intentionally avoids reading or emitting note body snippets; callers get
targets, lines, paths and decision packets only.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, field_validator

from mednotes.domains.wiki.capabilities.notes.raw_chats import atomic_write_text
from mednotes.domains.wiki.capabilities.vocabulary.link_terms import normalize_key, obsidian_target_name
from mednotes.domains.wiki.contracts.workflow_outcomes import (
    DecisionEvidence,
    HumanDecisionOption,
    HumanDecisionPacket,
    RejectedAutomation,
    WorkflowDecision,
    WorkflowDecisionSummary,
)
from mednotes.kernel.base import ContractModel, JsonObject

REFERENCE_REPAIR_PLAN_SCHEMA = "medical-notes-workbench.reference-repair-plan.v1"

_CATALOG_OPERATIONAL_BLOCKERS = {"catalog_invalid_json"}
_GRAPH_LINK_CODES = {"dangling_link", "ambiguous_link", "self_link"}
_WIKILINK_RE = re.compile(r"(!?)\[\[([^\]]+)\]\]")
_FENCED_RE = re.compile(r"```.*?```", re.DOTALL)
_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_RELATED_HEADING_RE = re.compile(r"(?m)^##\s+(?:🔗\s+)?Notas Relacionadas\s*$")
_NEXT_H2_RE = re.compile(r"(?m)^##\s+")
_FOOTER_RE = re.compile(r"(?m)^---\s*$")
_HEADING_LINE_RE = re.compile(r"(?m)^#{1,6}\s+.*$")
_TABLE_LINE_RE = re.compile(r"(?m)^\|.*\|$")


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _as_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _as_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


class _ReferenceRepairLooseInput(ContractModel):
    """Typed adapter for legacy graph-audit dicts consumed by reference repair."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)


class _GraphIssueInput(_ReferenceRepairLooseInput):
    code: str = ""
    target: str = ""
    raw: str = ""
    file: str = ""
    alias: str = ""
    message: str = ""
    line: int | None = None
    files: list[str] = Field(default_factory=list)
    targets: list[str] = Field(default_factory=list)

    @field_validator("code", "target", "raw", "file", "alias", "message", mode="before")
    @classmethod
    def _coerce_text(cls, value: object) -> str:
        return _as_str(value)

    @field_validator("line", mode="before")
    @classmethod
    def _coerce_line(cls, value: object) -> int | None:
        return _as_int(value)

    @field_validator("files", "targets", mode="before")
    @classmethod
    def _coerce_text_list(cls, value: object) -> list[str]:
        return _as_str_list(value)


class _StructuralEventInput(_ReferenceRepairLooseInput):
    change_type: str = ""
    old_title: str = ""
    old_path: str = ""
    replacement_title: str = ""
    replacement_path: str = ""
    title: str = ""
    path: str = ""

    @field_validator(
        "change_type",
        "old_title",
        "old_path",
        "replacement_title",
        "replacement_path",
        "title",
        "path",
        mode="before",
    )
    @classmethod
    def _coerce_text(cls, value: object) -> str:
        return _as_str(value)


class _ReferenceRepairAction(ContractModel):
    """Single planned action; only this model may drive repair decisions."""

    code: str = ""
    action: str = ""
    target: str = ""
    raw: str = ""
    reason: str = ""
    line: int | None = None
    old_target: str = ""
    new_target: str = ""
    replacement: str | None = None
    change_type: str = ""
    alias: str = ""
    targets: list[str] = Field(default_factory=list)
    message: str = ""
    candidate_files: list[str] = Field(default_factory=list)
    recommended_options: list[str] = Field(default_factory=list)
    blocks_apply: bool = False
    safe_auto_apply: bool = False
    human_decision_required: bool = False
    human_triage_required: bool = False
    operational_blocker: bool = False
    receipt_code: str = ""
    visible_text_policy: str = ""
    decision_summary: WorkflowDecisionSummary | None = None

    def compact_payload(self) -> JsonObject:
        """Serialize the action while preserving explicit null replacement values."""

        payload = self.to_payload()
        return {key: value for key, value in payload.items() if value not in ("", [])}


class _ReferenceRepairNoteActions(ContractModel):
    path: str
    action_count: int
    blocking_action_count: int
    human_decision_required: bool
    blocks_apply: bool
    actions: list[_ReferenceRepairAction]

    @classmethod
    def from_actions(cls, *, path: str, actions: list[_ReferenceRepairAction]) -> _ReferenceRepairNoteActions:
        return cls(
            path=path,
            action_count=len(actions),
            blocking_action_count=sum(1 for action in actions if action.blocks_apply),
            human_decision_required=any(action.human_decision_required for action in actions),
            blocks_apply=any(action.blocks_apply for action in actions),
            actions=actions,
        )

    def compact_payload(self) -> JsonObject:
        return {
            "path": self.path,
            "action_count": self.action_count,
            "blocking_action_count": self.blocking_action_count,
            "human_decision_required": self.human_decision_required,
            "blocks_apply": self.blocks_apply,
            "actions": [action.compact_payload() for action in self.actions],
        }


class _ReferenceRepairPlan(ContractModel):
    schema_id: Literal["medical-notes-workbench.reference-repair-plan.v1"] = Field(
        default=REFERENCE_REPAIR_PLAN_SCHEMA,
        alias="schema",
    )
    phase: Literal["reference_repair"] = "reference_repair"
    status: str = "skipped"
    package_mode: str = "diagnosis_bound"
    manual_script_allowed: bool = False
    requires_backup: bool = False
    requires_receipt: bool = True
    action_count: int = 0
    affected_note_count: int = 0
    blocking_action_count: int = 0
    human_decision_count: int = 0
    triage_count: int = 0
    human_decision_required: bool = False
    triage_required: bool = False
    note_actions: list[_ReferenceRepairNoteActions] = Field(default_factory=list)
    structural_actions: list[_ReferenceRepairAction] = Field(default_factory=list)
    catalog_actions: list[_ReferenceRepairAction] = Field(default_factory=list)
    human_decision_packets: list[HumanDecisionPacket] = Field(default_factory=list)

    def compact_payload(self) -> JsonObject:
        payload = self.to_payload()
        payload["note_actions"] = [item.compact_payload() for item in self.note_actions]
        payload["structural_actions"] = [item.compact_payload() for item in self.structural_actions]
        payload["catalog_actions"] = [item.compact_payload() for item in self.catalog_actions]
        payload["human_decision_packets"] = [item.to_payload() for item in self.human_decision_packets]
        return payload


class _ReferenceRepairAppliedAction(ContractModel):
    action: str
    old_target: str
    new_target: str | None = None
    receipt_code: str


class _ReferenceRepairApplyReport(ContractModel):
    path: str
    changed: bool
    error: str = ""
    actions: list[_ReferenceRepairAppliedAction] = Field(default_factory=list)

    def compact_payload(self) -> JsonObject:
        payload = self.to_payload()
        return {key: value for key, value in payload.items() if value not in ("", [])}


class _ReferenceRepairApplyResult(ContractModel):
    schema_id: Literal["medical-notes-workbench.reference-repair-apply.v1"] = Field(
        default="medical-notes-workbench.reference-repair-apply.v1",
        alias="schema",
    )
    phase: Literal["reference_repair"] = "reference_repair"
    status: Literal["completed"] = "completed"
    changed_file_count: int
    changed_files: list[str]
    backup_paths: list[str] = Field(default_factory=list)
    reports: list[_ReferenceRepairApplyReport]

    def compact_payload(self) -> JsonObject:
        payload = self.to_payload()
        payload["reports"] = [report.compact_payload() for report in self.reports]
        return payload


def _target_from_path(value: str) -> str:
    if not value:
        return ""
    return obsidian_target_name(value)


def _old_target_from_event(event: _StructuralEventInput) -> str:
    return event.old_title or _target_from_path(event.old_path)


def _new_target_from_event(event: _StructuralEventInput) -> str:
    return event.replacement_title or _target_from_path(event.replacement_path) or event.title or _target_from_path(event.path)


def _event_replacements(events: list[_StructuralEventInput]) -> dict[str, str | None]:
    replacements: dict[str, str | None] = {}
    for event in events:
        change_type = event.change_type
        old_target = _old_target_from_event(event)
        if not old_target:
            continue
        new_target = _new_target_from_event(event)
        if change_type in {"renamed", "moved", "merged"}:
            if new_target:
                replacements[normalize_key(old_target)] = new_target
        elif change_type == "deleted":
            replacements[normalize_key(old_target)] = new_target or None
    return replacements


def _packet_id(action: _ReferenceRepairAction, path: str = "") -> str:
    line = "" if action.line is None else str(action.line)
    parts = [
        "reference_repair",
        action.code,
        path,
        line,
        action.target,
    ]
    return ":".join(part.replace(":", "_") for part in parts if part)


def _requires_reference_decision() -> bool:
    return True


def _decision_packet(action: _ReferenceRepairAction, *, path: str = "") -> HumanDecisionPacket | None:
    if not action.human_decision_required:
        return None
    code = action.code
    target = action.target
    if code == "dangling_link":
        options = [
            HumanDecisionOption(id="replace_target", label="Apontar para nota existente"),
            HumanDecisionOption(id="remove_link", label="Remover link mantendo texto"),
            HumanDecisionOption(id="create_missing_note", label="Criar nota faltante"),
        ]
        question = f"O alvo [[{target}]] nao existe. Como reparar este link?"
    elif code == "ambiguous_link":
        options = [HumanDecisionOption(id=f"use:{candidate}", label=candidate) for candidate in action.candidate_files]
        options.append(HumanDecisionOption(id="remove_link", label="Remover link mantendo texto"))
        question = f"O alvo [[{target}]] aponta para mais de uma nota. Qual e a nota canonica?"
    elif code == "duplicate_stem":
        options = [
            HumanDecisionOption(id="choose_canonical", label="Escolher nota canonica"),
            HumanDecisionOption(id="merge_duplicates", label="Mesclar duplicatas"),
            HumanDecisionOption(id="rename_duplicates", label="Renomear duplicatas"),
        ]
        question = f"Ha multiplas notas com o alvo Obsidian [[{target}]]. Como consolidar?"
    elif code == "structural_deleted":
        options = [
            HumanDecisionOption(id="remove_link_keep_text", label="Remover link mantendo texto"),
            HumanDecisionOption(id="choose_replacement", label="Escolher nota substituta"),
            HumanDecisionOption(id="skip_file", label="Pular este arquivo"),
        ]
        question = f"A nota [[{target}]] foi removida sem substituto confirmado. Como reparar as referencias?"
    else:
        options = [
            HumanDecisionOption(id="edit_catalog", label="Corrigir catalogo"),
            HumanDecisionOption(id="skip_for_now", label="Manter como blocker"),
        ]
        question = f"Como resolver o blocker de grafo {code}?"

    resume_action = "Atualizar o vault/catalogo e rodar run-linker --diagnose novamente."
    decision = WorkflowDecision(
        kind="ask_human",
        phase="reference_repair",
        reason_code=code or "reference_repair_required",
        public_summary=question,
        developer_summary="Reference repair has no safe deterministic target for this action.",
        evidence=[
            DecisionEvidence(
                summary=question,
                technical_code=code or "reference_repair_required",
                source="reference_repair",
                affected_items=[item for item in (path, target) if item],
                candidates=[{"target": target, "path": path, "line": "" if action.line is None else str(action.line)}],
                risk="Escolha automatica pode apagar ou redirecionar referencia para alvo incorreto.",
            )
        ],
        rejected_automations=[
            RejectedAutomation(kind="auto_fix", reason_code="no_unambiguous_target", reason="Nao ha substituto unico seguro para corrigir automaticamente."),
            RejectedAutomation(kind="auto_defer", reason_code="blocks_reference_repair", reason="Pular esta acao deixa blocker operacional ativo."),
            RejectedAutomation(kind="auto_plan", reason_code="plan_needs_target", reason="O plano precisa da escolha de alvo/rota antes de aplicar."),
        ],
        next_action=resume_action,
        resume_action=resume_action,
        recommended_option_id=options[0].id if options else "manual_review",
        options=options or [HumanDecisionOption(id="manual_review", label="Revisar manualmente")],
    )
    packet = HumanDecisionPacket.model_validate(decision.to_human_decision_packet())
    packet.id = _packet_id(action, path=path)
    packet.kind = "reference_repair"
    packet.type = "reference_repair"
    packet.path = path
    packet.target = target
    packet.line = action.line
    action.decision_summary = packet.decision_summary
    return packet


def _auto_link_action(
    issue: _GraphIssueInput,
    *,
    duplicate_candidates: dict[str, list[str]],
    replacements: dict[str, str | None],
) -> _ReferenceRepairAction | None:
    code = issue.code
    target = issue.target
    raw = issue.raw
    line = issue.line
    if code not in _GRAPH_LINK_CODES or not target:
        return None
    target_key = normalize_key(target)
    has_structural_event = target_key in replacements
    replacement = replacements[target_key] if has_structural_event else None
    if code == "dangling_link" and has_structural_event and replacement is None:
        return _ReferenceRepairAction(
            code="structural_deleted",
            action="resolve_deleted_wikilink_target",
            target=target,
            old_target=target,
            raw=raw,
            reason="structural_deleted_without_replacement",
            replacement=None,
            blocks_apply=True,
            safe_auto_apply=False,
            human_decision_required=_requires_reference_decision(),
            recommended_options=["remove_link_keep_text", "choose_replacement", "skip_file"],
            line=line,
        )
    if code == "self_link":
        action = "unlink_incoming_wikilink"
        receipt_code = "unlinked_self_link"
        reason = "self_link"
    elif replacement:
        action = "rewrite_incoming_wikilink"
        receipt_code = "rewritten_ambiguous_target" if code == "ambiguous_link" else "rewritten_missing_target"
        reason = "structural_replacement"
    else:
        action = "unlink_incoming_wikilink"
        if code == "ambiguous_link":
            receipt_code = "unlinked_ambiguous_target"
            reason = "ambiguous_without_replacement"
        else:
            receipt_code = "unlinked_deleted_target"
            reason = "missing_without_replacement"
    candidate_files = duplicate_candidates[normalize_key(target)] if code == "ambiguous_link" and normalize_key(target) in duplicate_candidates else []
    return _ReferenceRepairAction(
        code=code,
        action=action,
        target=target,
        old_target=target,
        raw=raw,
        reason=reason,
        blocks_apply=False,
        safe_auto_apply=True,
        human_decision_required=False,
        receipt_code=receipt_code,
        visible_text_policy="preserve_label_or_target",
        new_target=replacement or "",
        replacement=replacement,
        line=line,
        candidate_files=candidate_files,
    )


def _catalog_action(issue: _GraphIssueInput) -> _ReferenceRepairAction:
    code = issue.code
    target = issue.target
    blocks_apply = code in _CATALOG_OPERATIONAL_BLOCKERS
    return _ReferenceRepairAction(
        code=code,
        action="operational_catalog_blocker" if blocks_apply else "skip_catalog_entry_for_linking",
        target=target,
        alias=issue.alias,
        targets=issue.targets,
        message=issue.message,
        blocks_apply=blocks_apply,
        safe_auto_apply=False,
        human_decision_required=False,
        human_triage_required=True,
        operational_blocker=blocks_apply,
        receipt_code=_catalog_receipt_code(code),
    )


def _catalog_receipt_code(code: str) -> str:
    match code:
        case "catalog_missing":
            return "catalog_skipped_missing"
        case "catalog_invalid_json":
            return "operational_blocker.catalog_invalid_json"
        case "catalog_entry_missing_target":
            return "catalog_entry_skipped_missing_target"
        case "catalog_target_missing":
            return "catalog_entry_skipped_target_missing"
        case "catalog_target_ambiguous":
            return "catalog_entry_skipped_target_ambiguous"
        case "alias_conflict":
            return "catalog_alias_skipped_conflict"
        case "generic_alias" | "short_alias":
            return "catalog_alias_skipped_low_signal"
        case "":
            return "catalog_skipped_unknown"
        case _:
            return f"catalog_skipped_{code}"


def _structural_action_from_event(event: _StructuralEventInput) -> _ReferenceRepairAction | None:
    old_target = _old_target_from_event(event)
    if not old_target:
        return None
    change_type = event.change_type
    new_target = _new_target_from_event(event)
    return _ReferenceRepairAction(
        code=f"structural_{change_type}",
        action="track_structural_link_event",
        change_type=change_type,
        old_target=old_target,
        target=old_target,
        new_target=new_target,
        replacement=new_target or None,
        blocks_apply=False,
        safe_auto_apply=True,
        human_decision_required=False,
    )


def plan_reference_repair(
    graph_issues: Sequence[object],
    *,
    structural_events: Sequence[object] | None = None,
) -> JsonObject:
    """Build a deterministic reference-repair plan from graph-audit issues."""
    issues = [_GraphIssueInput.model_validate(issue) for issue in graph_issues]
    events = [_StructuralEventInput.model_validate(event) for event in structural_events or []]
    replacements = _event_replacements(events)
    duplicate_candidates: dict[str, list[str]] = {}
    for issue in issues:
        if issue.code == "duplicate_stem":
            duplicate_candidates[normalize_key(issue.target)] = issue.files

    by_note: dict[str, list[_ReferenceRepairAction]] = {}
    structural_actions: list[_ReferenceRepairAction] = []
    catalog_actions: list[_ReferenceRepairAction] = []
    human_decision_packets: list[HumanDecisionPacket] = []

    for issue in issues:
        code = issue.code
        if code == "duplicate_stem":
            structural_actions.append(
                _ReferenceRepairAction(
                    code=code,
                    action="resolve_duplicate_obsidian_target",
                    target=issue.target,
                    candidate_files=issue.files,
                    blocks_apply=True,
                    safe_auto_apply=False,
                    human_decision_required=False,
                    recommended_options=["choose_canonical", "merge_duplicates", "rename_duplicates"],
                )
            )
            continue

        if code in {"dangling_link", "ambiguous_link", "self_link"}:
            path = issue.file
            if not path:
                continue
            action = _auto_link_action(issue, duplicate_candidates=duplicate_candidates, replacements=replacements)
            if action is None:
                continue
            by_note.setdefault(path, []).append(action)
            continue

        if code.startswith("catalog_") or code == "alias_conflict":
            action = _catalog_action(issue)
            catalog_actions.append(action)
            packet = _decision_packet(action)
            if packet:
                human_decision_packets.append(packet)

    for event in events:
        action = _structural_action_from_event(event)
        if action:
            structural_actions.append(action)

    note_actions: list[_ReferenceRepairNoteActions] = []
    for path, actions in sorted(by_note.items()):
        sorted_actions = sorted(actions, key=lambda action: (action.line or 0, action.code))
        for action in sorted_actions:
            packet = _decision_packet(action, path=path)
            if packet:
                human_decision_packets.append(packet)
        note_actions.append(_ReferenceRepairNoteActions.from_actions(path=path, actions=sorted_actions))

    note_action_count = sum(item.action_count for item in note_actions)
    catalog_action_count = len(catalog_actions)
    blocking_action_count = sum(item.blocking_action_count for item in note_actions) + sum(
        1 for action in catalog_actions if action.blocks_apply
    )
    human_decision_count = sum(
        1
        for item in note_actions
        for action in item.actions
        if action.human_decision_required
    ) + sum(1 for action in catalog_actions if action.human_decision_required)
    triage_count = sum(1 for action in catalog_actions if action.human_triage_required)

    status = "blocked" if blocking_action_count else "planned" if note_action_count or catalog_action_count else "skipped"
    plan = _ReferenceRepairPlan(
        status=status,
        action_count=note_action_count + catalog_action_count,
        affected_note_count=len(note_actions),
        blocking_action_count=blocking_action_count,
        human_decision_count=human_decision_count,
        triage_count=triage_count,
        human_decision_required=human_decision_count > 0,
        triage_required=triage_count > 0,
        note_actions=note_actions,
        structural_actions=sorted(structural_actions, key=lambda action: (action.code, action.target)),
        catalog_actions=sorted(catalog_actions, key=lambda action: (action.code, action.target)),
        human_decision_packets=human_decision_packets,
    )
    return plan.compact_payload()


def _raw_target(raw: str) -> str:
    target = raw.split("|", 1)[0].split("#", 1)[0].strip()
    return obsidian_target_name(target)


def _display_text(raw: str) -> str:
    if "|" in raw:
        return raw.rsplit("|", 1)[1].strip()
    target = raw.split("#", 1)[0].strip()
    return obsidian_target_name(target) if target else raw.strip()


def _rewrite_link(raw: str, new_target: str) -> str:
    if "|" in raw:
        display = raw.rsplit("|", 1)[1].strip()
        return f"{new_target}|{display}" if display else new_target
    return new_target


def _related_section_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for match in _RELATED_HEADING_RE.finditer(text):
        next_h2 = _NEXT_H2_RE.search(text, match.end())
        footer = _footer_match(text, match.end())
        candidates = [item.start() for item in (next_h2, footer) if item is not None]
        spans.append((match.start(), min(candidates) if candidates else len(text)))
    return spans


def _footer_match(text: str, start: int = 0) -> re.Match[str] | None:
    frontmatter = _FRONTMATTER_RE.match(text)
    search_start = max(start, frontmatter.end() if frontmatter else 0)
    return _FOOTER_RE.search(text, search_start)


def _protected_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for regex in (_FRONTMATTER_RE, _FENCED_RE, _INLINE_CODE_RE, _HTML_COMMENT_RE, _HEADING_LINE_RE, _TABLE_LINE_RE):
        spans.extend((match.start(), match.end()) for match in regex.finditer(text))
    footer = _footer_match(text)
    if footer:
        spans.append((footer.start(), len(text)))
    spans.extend(_related_section_spans(text))
    return sorted(spans)


def _inside(index: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= index < end for start, end in spans)


def _apply_actions_to_text(text: str, actions: list[_ReferenceRepairAction]) -> tuple[str, list[_ReferenceRepairAppliedAction]]:
    if not actions:
        return text, []
    protected = _protected_spans(text)
    changed: list[_ReferenceRepairAppliedAction] = []
    output: list[str] = []
    cursor = 0
    for match in _WIKILINK_RE.finditer(text):
        if _inside(match.start(), protected):
            continue
        raw = match.group(2).strip()
        target = _raw_target(raw)
        action = next(
            (
                item
                for item in actions
                if item.safe_auto_apply and normalize_key(item.old_target or item.target) == normalize_key(target)
            ),
            None,
        )
        if action is None:
            continue
        if action.action == "rewrite_incoming_wikilink" and action.new_target:
            replacement_raw = _rewrite_link(raw, action.new_target)
            replacement = f"{match.group(1)}[[{replacement_raw}]]"
        elif action.action == "unlink_incoming_wikilink":
            replacement = _display_text(raw)
        else:
            continue
        output.append(text[cursor : match.start()])
        output.append(replacement)
        cursor = match.end()
        changed.append(
            _ReferenceRepairAppliedAction(
                action=action.action,
                old_target=target,
                new_target=action.new_target or None,
                receipt_code=action.receipt_code,
            )
        )
    if not changed:
        return text, []
    output.append(text[cursor:])
    return "".join(output), changed


def apply_reference_repair_plan(wiki_dir: Path, plan: object) -> JsonObject:
    """Apply safe automatic reference-repair actions outside protected zones."""
    typed_plan = _ReferenceRepairPlan.model_validate(plan)
    changed_files: list[str] = []
    reports: list[_ReferenceRepairApplyReport] = []
    for note in typed_plan.note_actions:
        relative = note.path
        if not relative or not note.actions:
            continue
        path = wiki_dir / relative
        if not path.is_file():
            reports.append(_ReferenceRepairApplyReport(path=relative, changed=False, error="file_missing"))
            continue
        original = path.read_text(encoding="utf-8")
        updated, applied = _apply_actions_to_text(original, note.actions)
        if updated == original:
            reports.append(_ReferenceRepairApplyReport(path=relative, changed=False, actions=[]))
            continue
        atomic_write_text(path, updated)
        changed_files.append(str(path))
        reports.append(_ReferenceRepairApplyReport(path=relative, changed=True, actions=applied))
    result = _ReferenceRepairApplyResult(
        changed_file_count=len(changed_files),
        changed_files=changed_files,
        reports=reports,
    )
    return result.compact_payload()

"""Typed decision projection for `/mednotes:fix-wiki`.

`health.py` owns workflow composition, but human-decision UX must not be
fabricated from loose blocker dictionaries. This module is the typed domain
lens between blocker-resolution evidence and the canonical `WorkflowDecision`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from mednotes.domains.wiki.contracts.workflow_outcomes import DecisionEvidence, RejectedAutomation, WorkflowDecision
from mednotes.kernel.base import JsonObject, JsonObjectAdapter


class FixWikiBlockerResolutionGroup(BaseModel):
    """One blocker-resolution group that can require a human decision."""

    model_config = ConfigDict(extra="ignore", strict=True)

    route: StrictStr = "manual_review"
    reason: StrictStr = "Revisão humana necessária."
    next_action: StrictStr = "Resolver a decisão humana pendente antes de continuar."
    automatic: StrictBool = False
    sample: list[JsonObject] = Field(default_factory=list)


class FixWikiBlockerResolutionPacket(BaseModel):
    """Typed packet emitted by blocker-resolution before UX projection."""

    model_config = ConfigDict(extra="ignore", strict=True)

    groups: list[FixWikiBlockerResolutionGroup] = Field(default_factory=list)


DecisionOption = dict[Literal["id", "label"], str]


def project_fix_wiki_human_decision_packets(blocker_resolution: object) -> list[JsonObject]:
    """Project typed blocker groups into human-decision packets.

    Invalid group shapes raise Pydantic validation errors before any
    `WorkflowDecision` or public UX is built.
    """

    packet = FixWikiBlockerResolutionPacket.model_validate(blocker_resolution)
    packets: list[JsonObject] = []
    for group in packet.groups:
        if group.automatic:
            continue
        decision = _workflow_decision_for_group(group, options=_options_for_route(group.route))
        packets.append(JsonObjectAdapter.validate_python(decision.to_human_decision_packet()))
        if len(packets) >= 5:
            break
    return packets


def _workflow_decision_for_group(
    group: FixWikiBlockerResolutionGroup,
    *,
    options: list[DecisionOption],
) -> WorkflowDecision:
    question = group.reason
    next_action = group.next_action
    return WorkflowDecision(
        kind="ask_human",
        phase="fix_wiki_apply",
        reason_code=group.route,
        public_summary=question,
        developer_summary="fix-wiki reached an editorial/organizational decision that has no safe automatic route.",
        evidence=[
            DecisionEvidence(
                summary=question,
                technical_code=group.route,
                source="fix_wiki",
                candidates=[{"decision_kind": group.route, "sample": group.sample[:5]}],
                risk="Escolha automática pode aplicar rota editorial ou organizacional errada.",
            )
        ],
        rejected_automations=[
            RejectedAutomation(
                kind="auto_fix",
                reason_code="unsafe_editorial_choice",
                reason="Não há correção determinística segura para este blocker.",
            ),
            RejectedAutomation(
                kind="auto_defer",
                reason_code="blocks_fix_wiki",
                reason="Pular a decisão deixa o fix-wiki bloqueado.",
            ),
            RejectedAutomation(
                kind="auto_plan",
                reason_code="plan_needs_choice",
                reason="O plano precisa da escolha antes de aplicar a próxima fase.",
            ),
        ],
        next_action=next_action,
        resume_action=next_action,
        recommended_option_id=options[0]["id"],
        options=options,
        human_decision_kind=group.route,
    )


def _options_for_route(route: str) -> list[DecisionOption]:
    match route:
        case "note_merge_required" | "title_driven_merge_review":
            return [
                {"id": "merge_keep_canonical", "label": "Fundir e manter uma nota canônica"},
                {"id": "rename_split_topics", "label": "Renomear para separar tópicos distintos"},
            ]
        case "taxonomy_review_required":
            return [
                {"id": "choose_taxonomy", "label": "Escolher a taxonomia correta"},
                {"id": "defer_move", "label": "Adiar migração e manter como está"},
            ]
        case "taxonomy_migrate":
            return [
                {"id": "apply_taxonomy", "label": "Autorizar reorganização de pastas"},
                {"id": "review_plan", "label": "Revisar plano antes de mover pastas"},
            ]
        case "io_retry":
            return [
                {"id": "retry_now", "label": "Liberar arquivo e tentar novamente"},
                {"id": "stop_and_inspect", "label": "Parar para inspecionar o bloqueio externo"},
            ]
        case _:
            return [
                {"id": "continue_safely", "label": "Escolher a rota segura sugerida"},
                {"id": "stop_and_review", "label": "Parar e revisar manualmente"},
            ]

"""Taxonomy resolution against the existing Wiki tree."""
from __future__ import annotations

import difflib
from collections.abc import Sequence
from pathlib import Path

from mednotes.domains.wiki.capabilities.vocabulary.taxonomy.normalize import (
    _fold_taxonomy_segment,
    normalize_taxonomy,
    safe_title,
)
from mednotes.domains.wiki.capabilities.vocabulary.taxonomy.schema import (
    CANONICAL_TAXONOMY,
    TaxonomyResolution,
    _canonical_area_aliases_by_fold,
    _canonical_specialties_by_fold,
    _canonical_specialties_for_root,
)
from mednotes.domains.wiki.common import MissingPathError, ValidationError
from mednotes.domains.wiki.contracts.workflow_outcomes import DecisionEvidence, RejectedAutomation, WorkflowDecision
from mednotes.kernel.base import JsonObject, JsonObjectAdapter

_NEAR_DUPLICATE_CUTOFF = 0.9


class TaxonomyDecisionRequired(ValidationError):
    """Taxonomy could be resolved only after a human choice."""

    def __init__(self, message: str, packet: JsonObject) -> None:
        super().__init__(message)
        self.human_decision_packet = packet


def _taxonomy_option(value: str, *, consequence: str = "") -> JsonObject:
    option: JsonObject = {
        "id": _fold_taxonomy_segment(value).replace(" ", "_") or "taxonomy_option",
        "label": value,
        "value": value,
    }
    if consequence:
        option["consequence"] = consequence
    return option


def _taxonomy_decision_packet(
    *,
    kind: str,
    question: str,
    options: Sequence[JsonObject | str],
    resume_action: str,
    target_key: str,
    context: JsonObject | None = None,
    recommended_option_id: str | None = None,
) -> JsonObject:
    context_payload = JsonObjectAdapter.validate_python({"context": context or {}})["context"]
    context_object = JsonObjectAdapter.validate_python(context_payload)
    clean_options = [_normalize_taxonomy_decision_option(option, index) for index, option in enumerate(options, start=1)]
    recommended = recommended_option_id or (clean_options[0]["id"] if clean_options else "choose_existing_taxonomy")
    decision = WorkflowDecision(
        kind="ask_human",
        phase="taxonomy-resolve",
        reason_code="taxonomy_resolution_required",
        public_summary=question,
        developer_summary=f"Taxonomy resolution for {target_key} remains ambiguous after canonical matching.",
        evidence=[
            DecisionEvidence(
                summary=f"Taxonomia solicitada: {target_key}.",
                technical_code="taxonomy_resolution_required",
                source="taxonomy-resolve",
                candidates=[{"target_key": target_key, **context_object}],
                risk="Mover automaticamente pode arquivar a nota em categoria clínica errada.",
            )
        ],
        rejected_automations=[
            RejectedAutomation(kind="auto_fix", reason_code="ambiguous_taxonomy_target", reason="Nao ha categoria canonica unica para corrigir automaticamente."),
            RejectedAutomation(kind="auto_defer", reason_code="blocks_note_path", reason="Sem taxonomia resolvida nao ha caminho seguro para a nota."),
            RejectedAutomation(kind="auto_plan", reason_code="plan_needs_human_choice", reason="O plano nao consegue escolher entre categorias plausiveis sem informacao externa."),
        ],
        next_action=resume_action,
        resume_action=resume_action,
        recommended_option_id=recommended,
        options=clean_options,
    )
    packet = decision.to_human_decision_packet()
    packet["kind"] = kind
    packet["type"] = kind
    packet["target_kind"] = "taxonomy"
    packet["target_key"] = target_key
    packet_context = packet.setdefault("context", {})
    if isinstance(packet_context, dict):
        packet_context.update(context_object)
    return JsonObjectAdapter.validate_python(packet)


def _normalize_taxonomy_decision_option(option: JsonObject | str, index: int) -> JsonObject:
    if isinstance(option, dict):
        option_payload = JsonObjectAdapter.validate_python(option)
        label = str(option_payload.get("label") or option_payload.get("value") or option_payload.get("id") or f"Opção {index}")
        option_id = str(option_payload.get("id") or _fold_taxonomy_segment(label).replace(" ", "_") or f"option_{index}")
        clean: JsonObject = {"id": option_id, "label": label}
        for key in ("description", "consequence", "value", "resume_action"):
            value = option_payload.get(key)
            if value:
                clean[key] = str(value)
        return JsonObjectAdapter.validate_python(clean)
    label = str(option)
    return {
        "id": _fold_taxonomy_segment(label).replace(" ", "_") or f"option_{index}",
        "label": label,
        "value": label,
    }


def _decision_guidance_for_root(root: str) -> list[str]:
    if root == "3. Ginecologia e Obstetrícia":
        return [
            "Use Ginecologia para doenças, rastreio e cuidado ginecológico fora de gestação.",
            "Use Obstetrícia para gestação, parto, puerpério e cuidado fetal/materno gestacional.",
        ]
    return ["Escolha a especialidade canônica mais específica para o conteúdo da nota."]


def _canonicalize_taxonomy_parts(parts: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[dict[str, str], ...]]:
    roots = _canonical_area_aliases_by_fold()
    specialties = _canonical_specialties_by_fold()
    first = parts[0]
    first_folded = _fold_taxonomy_segment(first)
    canonicalized: list[JsonObject] = []

    if first_folded in roots:
        root = roots[first_folded]
        if len(parts) == 1:
            raise ValidationError(f"Taxonomy must include a specialty under canonical area: {root}")
        root_specialties = _canonical_specialties_for_root(root)
        second = parts[1]
        second_folded = _fold_taxonomy_segment(second)
        if second_folded not in root_specialties:
            options = [_taxonomy_option(value, consequence="Usar especialidade canônica existente.") for value in root_specialties.values()]
            raise TaxonomyDecisionRequired(
                f"Unknown specialty under {root}: {second}",
                _taxonomy_decision_packet(
                    kind="taxonomy_specialty_required",
                    question=f"Qual especialidade canônica sob {root} deve receber '{second}'?",
                    options=options,
                    resume_action="Reexecutar taxonomy-resolve/stage-note com a especialidade escolhida.",
                    target_key="/".join(parts),
                    context={"root": root, "requested": second, "decision_guidance": _decision_guidance_for_root(root)},
                    recommended_option_id=options[0]["id"] if options else None,
                ),
            )
        specialty = root_specialties[second_folded]
        canonical_parts = (root, specialty, *parts[2:])
        if canonical_parts[:2] != parts[:2]:
            canonicalized.append({"from": "/".join(parts[:2]), "to": "/".join(canonical_parts[:2]), "under": ""})
        return canonical_parts, tuple(canonicalized)

    if first_folded in specialties:
        root, specialty = specialties[first_folded]
        canonical_parts = (root, specialty, *parts[1:])
        canonicalized.append({"from": first, "to": "/".join(canonical_parts[:2]), "under": ""})
        return canonical_parts, tuple(canonicalized)

    root_names = ", ".join(root for root, _specialties in CANONICAL_TAXONOMY)
    options: list[JsonObject] = []
    for root, specialties in CANONICAL_TAXONOMY:
        options.append(_taxonomy_option(root, consequence="Escolher área canônica e depois uma especialidade."))
        options.extend(
            _taxonomy_option(specialty, consequence=f"Usar {root}/{specialty}.")
            for specialty in specialties[:3]
        )
    raise TaxonomyDecisionRequired(
        f"Taxonomy must start with a canonical area or known specialty. Got: {first}. "
        f"Canonical areas: {root_names}",
        _taxonomy_decision_packet(
            kind="taxonomy_root_required",
            question=f"Qual área/especialidade canônica deve receber '{first}'?",
            options=options[:12],
            resume_action="Reexecutar taxonomy-resolve/stage-note com área ou especialidade canônica.",
            target_key="/".join(parts),
            context={"requested": first},
        ),
    )


def _visible_child_dirs(path: Path) -> list[Path]:
    if not path.exists():
        return []
    return sorted(
        (child for child in path.iterdir() if child.is_dir() and not child.name.startswith(".")),
        key=lambda child: _fold_taxonomy_segment(child.name),
    )


def _suggest_existing_segments(siblings: list[Path], requested: str) -> list[str]:
    folded_to_names: dict[str, list[str]] = {}
    for sibling in siblings:
        folded_to_names.setdefault(_fold_taxonomy_segment(sibling.name), []).append(sibling.name)
    requested_folded = _fold_taxonomy_segment(requested)
    close = difflib.get_close_matches(requested_folded, list(folded_to_names), n=4, cutoff=_NEAR_DUPLICATE_CUTOFF)
    suggestions: list[str] = []
    for folded in close:
        suggestions.extend(folded_to_names[folded])
    return suggestions


def _format_suggestions(suggestions: list[str]) -> str:
    if not suggestions:
        return ""
    return " Sugestões existentes: " + ", ".join(suggestions)


def _match_existing_segment(parent: Path, requested: str) -> tuple[str | None, list[str]]:
    siblings = _visible_child_dirs(parent)
    exact = [sibling.name for sibling in siblings if sibling.name == requested]
    if exact:
        return exact[0], []

    requested_folded = _fold_taxonomy_segment(requested)
    folded_matches = [sibling.name for sibling in siblings if _fold_taxonomy_segment(sibling.name) == requested_folded]
    if len(folded_matches) == 1:
        return folded_matches[0], []
    if len(folded_matches) > 1:
        raise TaxonomyDecisionRequired(
            f"Taxonomy segment is ambiguous under {parent}: {requested}. Matches: {', '.join(folded_matches)}",
            _taxonomy_decision_packet(
                kind="taxonomy_ambiguous_segment",
                question=f"Qual pasta existente sob {parent} corresponde a '{requested}'?",
                options=[
                    _taxonomy_option(match, consequence="Usar esta pasta existente.")
                    for match in folded_matches
                ],
                resume_action="Reexecutar taxonomy-resolve/stage-note com o segmento escolhido exatamente.",
                target_key=requested,
                context={"parent": str(parent), "matches": folded_matches},
            ),
        )
    return None, _suggest_existing_segments(siblings, requested)


def _validate_taxonomy_not_title(parts: tuple[str, ...], title: str) -> None:
    title_key = _fold_taxonomy_segment(safe_title(title))
    if parts and _fold_taxonomy_segment(parts[-1]) == title_key:
        raise TaxonomyDecisionRequired(
            "Taxonomy must be the folder/category path only; do not repeat the note title "
            f"as the final folder: taxonomy {'/'.join(parts)} + title {title}",
            _taxonomy_decision_packet(
                kind="taxonomy_title_repeated",
                question="A taxonomia inclui o título da nota como pasta final. Qual correção usar?",
                options=[
                    {
                        "id": "remove_title_folder",
                        "label": "Remover o título da taxonomia",
                        "value": "/".join(parts[:-1]),
                        "consequence": "Manter o título apenas como arquivo Markdown.",
                    }
                ],
                resume_action="Reexecutar stage-note usando taxonomy sem repetir o título.",
                target_key="/".join(parts),
                context={"title": title, "taxonomy": "/".join(parts)},
            ),
        )


def resolve_taxonomy(
    wiki_dir: Path,
    taxonomy: str,
    *,
    title: str | None = None,
    allow_new_leaf: bool = True,
) -> TaxonomyResolution:
    requested_parts = normalize_taxonomy(taxonomy)
    canonical_request_parts, alias_canonicalized = _canonicalize_taxonomy_parts(requested_parts)
    if title is not None:
        _validate_taxonomy_not_title(canonical_request_parts, title)
    if not wiki_dir.exists():
        raise MissingPathError(f"Wiki dir not found: {wiki_dir}")
    if not wiki_dir.is_dir():
        raise ValidationError(f"Wiki dir is not a directory: {wiki_dir}")

    canonical_parts: list[str] = []
    canonicalized: list[JsonObject] = [JsonObjectAdapter.validate_python(item) for item in alias_canonicalized]
    new_dirs: list[str] = []
    parent = wiki_dir

    for idx, requested in enumerate(canonical_request_parts):
        is_leaf = idx == len(canonical_request_parts) - 1
        matched, suggestions = _match_existing_segment(parent, requested)
        if matched is None:
            if idx < 2:
                canonical_parts.append(requested)
                new_dirs.append("/".join(canonical_parts))
                parent = parent / requested
                continue
            if is_leaf and allow_new_leaf and canonical_parts:
                if suggestions:
                    options = [
                        _taxonomy_option(
                            suggestion,
                            consequence="Usar pasta parecida existente em vez de criar leaf nova.",
                        )
                        for suggestion in suggestions
                    ]
                    options.append(
                        {
                            "id": "create_new_leaf",
                            "label": f"Criar nova pasta '{requested}'",
                            "value": requested,
                            "consequence": "Autorizar leaf nova apesar da semelhança; revisar taxonomia depois.",
                        }
                    )
                    raise TaxonomyDecisionRequired(
                        f"New taxonomy leaf '{requested}' under {'/'.join(canonical_parts)} is too similar to "
                        f"an existing folder.{_format_suggestions(suggestions)}",
                        _taxonomy_decision_packet(
                            kind="taxonomy_new_leaf_similar",
                            question=f"Criar nova pasta '{requested}' ou usar uma pasta existente?",
                            options=options,
                            resume_action="Escolher pasta existente ou confirmar leaf nova e reexecutar stage-note.",
                            target_key="/".join((*canonical_parts, requested)),
                            context={
                                "parent": "/".join(canonical_parts),
                                "requested": requested,
                                "suggestions": suggestions,
                                "decision_guidance": [
                                    "Prefira pasta existente quando a diferença for só plural, acento, caixa, underscore ou sinônimo próximo.",
                                    "Crie leaf nova apenas quando o conceito for realmente diferente e o pai canônico estiver correto.",
                                ],
                            },
                        ),
                    )
                canonical_parts.append(requested)
                new_dirs.append("/".join(canonical_parts))
                parent = parent / requested
                continue
            location = "/".join(canonical_parts) if canonical_parts else "<wiki-root>"
            options = [
                _taxonomy_option(suggestion, consequence="Usar pasta existente.")
                for suggestion in suggestions
            ]
            if not options:
                options.append(
                    {
                        "id": "choose_existing_taxonomy",
                        "label": "Escolher pasta existente",
                        "value": location,
                        "consequence": "Listar taxonomy-tree e repetir com caminho válido.",
                    }
                )
            if is_leaf and canonical_parts:
                options.append(
                    {
                        "id": "allow_new_taxonomy_leaf",
                        "label": f"Autorizar nova pasta '{requested}'",
                        "value": requested,
                        "consequence": "Reexecutar com --allow-new-taxonomy-leaf quando essa for a decisão explícita.",
                    }
                )
            raise TaxonomyDecisionRequired(
                f"Taxonomy segment must already exist under {location}: {requested}."
                f"{_format_suggestions(suggestions)}",
                _taxonomy_decision_packet(
                    kind="taxonomy_segment_missing",
                    question=f"Qual pasta deve substituir '{requested}' sob {location}?",
                    options=options,
                    resume_action="Reexecutar taxonomy-resolve/stage-note com uma taxonomia existente ou decisão explícita de nova leaf.",
                    target_key="/".join((*canonical_parts, requested)),
                    context={"parent": location, "requested": requested, "suggestions": suggestions},
                ),
            )

        if matched != requested:
            canonicalized.append({"from": requested, "to": matched, "under": "/".join(canonical_parts)})
        canonical_parts.append(matched)
        parent = parent / matched

    resolved_parts = tuple(canonical_parts)
    if title is not None:
        _validate_taxonomy_not_title(resolved_parts, title)
    return TaxonomyResolution(
        requested_taxonomy="/".join(requested_parts),
        taxonomy="/".join(resolved_parts),
        parts=resolved_parts,
        canonicalized=tuple(canonicalized),
        new_dirs=tuple(new_dirs),
    )


def resolve_target_for_note(
    wiki_dir: Path,
    taxonomy: str,
    title: str,
    *,
    allow_new_taxonomy_leaf: bool = True,
) -> tuple[Path, TaxonomyResolution]:
    resolution = resolve_taxonomy(wiki_dir, taxonomy, title=title, allow_new_leaf=allow_new_taxonomy_leaf)
    return wiki_dir.joinpath(*resolution.parts, f"{safe_title(title)}.md"), resolution


def target_for_note(
    wiki_dir: Path,
    taxonomy: str,
    title: str,
    *,
    allow_new_taxonomy_leaf: bool = True,
) -> Path:
    target, _resolution = resolve_target_for_note(
        wiki_dir,
        taxonomy,
        title,
        allow_new_taxonomy_leaf=allow_new_taxonomy_leaf,
    )
    return target

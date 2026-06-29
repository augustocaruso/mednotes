#!/usr/bin/env python3
"""Compute Anki model install/update payloads from local templates.

Os templates HTML/CSS dos modelos `Medicina` (Q&A) e `Medicina Cloze` vivem em
`bundle/docs/anki-templates/`. Este script lê esses arquivos e emite
payloads determinísticos que o agente entrega ao Anki MCP:

- `mcp_anki-mcp_createModel` quando o modelo não existe;
- `mcp_anki-mcp_updateModelTemplates` + `mcp_anki-mcp_updateModelStyling` quando
  o modelo existe mas o HTML/CSS divergiu.

O agente continua chamando o MCP; este script só normaliza o que mandar.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

SCHEMA = "medical-notes-workbench.flashcard-install-models.v1"
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_IO = 5

DEFAULT_TEMPLATES_DIR = (
    Path(__file__).resolve().parents[4] / "docs" / "anki-templates"
)

QA_MODEL_NAME = "Medicina"
QA_FIELDS = ("Frente", "Verso", "Verso Extra", "Obsidian")
CLOZE_MODEL_NAME = "Medicina Cloze"
CLOZE_FIELDS = ("Texto", "Verso Extra", "Obsidian")

QA_CARD_NAME = "Card 1"
CLOZE_CARD_NAME = "Cloze"


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, validate_assignment=True)

    def to_payload(self) -> dict[str, object]:
        return dict(self.model_dump(mode="json", by_alias=True))


class _CardTemplateSpec(_ContractModel):
    name: str
    front: str
    back: str


class _ModelSpec(_ContractModel):
    model_name: str
    is_cloze: bool
    in_order_fields: list[str]
    css: str
    card_templates: list[_CardTemplateSpec]
    fingerprint: str


class _AnkiCardTemplatePayload(_ContractModel):
    name: str = Field(alias="Name")
    front: str = Field(alias="Front")
    back: str = Field(alias="Back")


class _CreateModelArguments(_ContractModel):
    model_name: str = Field(alias="modelName")
    is_cloze: bool = Field(alias="isCloze")
    in_order_fields: list[str] = Field(alias="inOrderFields")
    css: str
    card_templates: list[_AnkiCardTemplatePayload] = Field(alias="cardTemplates")


class _UpdateTemplatePayload(_ContractModel):
    front: str = Field(alias="Front")
    back: str = Field(alias="Back")


class _UpdateTemplatesModelPayload(_ContractModel):
    name: str
    templates: dict[str, _UpdateTemplatePayload]


class _UpdateTemplatesArguments(_ContractModel):
    model: _UpdateTemplatesModelPayload


class _UpdateStylingModelPayload(_ContractModel):
    name: str
    css: str


class _UpdateStylingArguments(_ContractModel):
    model: _UpdateStylingModelPayload


class _CreateModelAction(_ContractModel):
    kind: str
    model: str
    operation: str
    tool: str
    arguments: _CreateModelArguments
    fingerprint: str


class _UpdateModelAction(_ContractModel):
    kind: str
    model: str
    operation: str
    tool: str
    arguments: _UpdateTemplatesArguments | _UpdateStylingArguments
    fingerprint: str


class _BlockedModelAction(_ContractModel):
    kind: str
    model: str
    operation: str
    reason: str
    expected_fields: list[str]
    actual_fields: list[str]


_InstallAction = _CreateModelAction | _UpdateModelAction | _BlockedModelAction


class _ModelStatus(_ContractModel):
    kind: str
    status: str
    fingerprint: str
    previous_fingerprint: str | None
    templates_changed: bool


class _ModelDescriptor(_ContractModel):
    name: str
    fields: list[str]
    is_cloze: bool = Field(alias="isCloze")


class _InstallPlan(_ContractModel):
    schema_: str = Field(alias="schema")
    templates_dir: str
    models: dict[str, _ModelDescriptor]
    statuses: dict[str, _ModelStatus]
    fingerprints: dict[str, str]
    actions: list[_InstallAction]
    blocked: bool


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _digest(*parts: str) -> str:
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(part.encode("utf-8"))
        hasher.update(b"\x1f")
    return hasher.hexdigest()


def load_templates(templates_dir: Path) -> dict[str, _ModelSpec]:
    """Lê os arquivos do diretório de templates e devolve as specs dos modelos."""

    css = _read(templates_dir / "style.css")
    qa_front = _read(templates_dir / "qa.front.html")
    qa_back = _read(templates_dir / "qa.back.html")
    cloze_front = _read(templates_dir / "cloze.front.html")
    cloze_back = _read(templates_dir / "cloze.back.html")

    return {
        "qa": _ModelSpec(
            model_name=QA_MODEL_NAME,
            is_cloze=False,
            in_order_fields=list(QA_FIELDS),
            css=css,
            card_templates=[_CardTemplateSpec(name=QA_CARD_NAME, front=qa_front, back=qa_back)],
            fingerprint=_digest(qa_front, qa_back, css, "qa"),
        ),
        "cloze": _ModelSpec(
            model_name=CLOZE_MODEL_NAME,
            is_cloze=True,
            in_order_fields=list(CLOZE_FIELDS),
            css=css,
            card_templates=[_CardTemplateSpec(name=CLOZE_CARD_NAME, front=cloze_front, back=cloze_back)],
            fingerprint=_digest(cloze_front, cloze_back, css, "cloze"),
        ),
    }


def _create_payload(spec: _ModelSpec) -> _CreateModelArguments:
    """Argumentos canônicos para `mcp_anki-mcp_createModel`."""

    return _CreateModelArguments(
        modelName=spec.model_name,
        isCloze=spec.is_cloze,
        inOrderFields=list(spec.in_order_fields),
        css=spec.css,
        cardTemplates=[
            _AnkiCardTemplatePayload(Name=tpl.name, Front=tpl.front, Back=tpl.back)
            for tpl in spec.card_templates
        ],
    )


def _update_templates_payload(spec: _ModelSpec) -> _UpdateTemplatesArguments:
    """Argumentos para `mcp_anki-mcp_updateModelTemplates`."""

    templates = {
        tpl.name: _UpdateTemplatePayload(Front=tpl.front, Back=tpl.back)
        for tpl in spec.card_templates
    }
    return _UpdateTemplatesArguments(
        model=_UpdateTemplatesModelPayload(name=spec.model_name, templates=templates)
    )


def _update_styling_payload(spec: _ModelSpec) -> _UpdateStylingArguments:
    """Argumentos para `mcp_anki-mcp_updateModelStyling`."""

    return _UpdateStylingArguments(model=_UpdateStylingModelPayload(name=spec.model_name, css=spec.css))


def _existing_model_status(
    spec: _ModelSpec, existing_models: list[str], existing_fields: dict[str, list[str]]
) -> str:
    if spec.model_name not in existing_models:
        return "missing"
    fields = existing_fields.get(spec.model_name)
    if fields is None:
        return "unknown"
    required = list(spec.in_order_fields)
    if list(fields) != required and not set(required).issubset(set(fields)):
        return "incompatible"
    return "present"


def build_install_plan(
    templates_dir: Path,
    *,
    existing_models: list[str] | None = None,
    existing_fields: dict[str, list[str]] | None = None,
    existing_fingerprints: dict[str, str] | None = None,
) -> dict[str, object]:
    existing_models = existing_models or []
    existing_fields = existing_fields or {}
    existing_fingerprints = existing_fingerprints or {}
    specs = load_templates(templates_dir)

    actions: list[_InstallAction] = []
    statuses: dict[str, _ModelStatus] = {}
    fingerprints: dict[str, str] = {}

    for kind, spec in specs.items():
        status = _existing_model_status(spec, existing_models, existing_fields)
        fingerprint = spec.fingerprint
        fingerprints[spec.model_name] = fingerprint
        previous_fingerprint = existing_fingerprints.get(spec.model_name)
        templates_changed = previous_fingerprint != fingerprint

        if status == "missing":
            actions.append(
                _CreateModelAction(
                    kind=kind,
                    model=spec.model_name,
                    operation="createModel",
                    tool="mcp_anki-mcp_createModel",
                    arguments=_create_payload(spec),
                    fingerprint=fingerprint,
                )
            )
        elif status == "incompatible":
            actions.append(
                _BlockedModelAction(
                    kind=kind,
                    model=spec.model_name,
                    operation="blocked",
                    reason=(
                        f"Modelo {spec.model_name!r} existe mas com campos diferentes."
                        " Renomeie ou apague no Anki Desktop antes de rodar /flashcards."
                    ),
                    expected_fields=list(spec.in_order_fields),
                    actual_fields=list(existing_fields.get(spec.model_name, [])),
                )
            )
        elif templates_changed:
            actions.append(
                _UpdateModelAction(
                    kind=kind,
                    model=spec.model_name,
                    operation="updateModelTemplates",
                    tool="mcp_anki-mcp_updateModelTemplates",
                    arguments=_update_templates_payload(spec),
                    fingerprint=fingerprint,
                )
            )
            actions.append(
                _UpdateModelAction(
                    kind=kind,
                    model=spec.model_name,
                    operation="updateModelStyling",
                    tool="mcp_anki-mcp_updateModelStyling",
                    arguments=_update_styling_payload(spec),
                    fingerprint=fingerprint,
                )
            )

        statuses[spec.model_name] = _ModelStatus(
            kind=kind,
            status=status,
            fingerprint=fingerprint,
            previous_fingerprint=previous_fingerprint,
            templates_changed=templates_changed,
        )

    blocked = any(action.operation == "blocked" for action in actions)
    return _InstallPlan(
        schema=SCHEMA,
        templates_dir=str(templates_dir),
        models={
            "qa": _ModelDescriptor(name=QA_MODEL_NAME, fields=list(QA_FIELDS), isCloze=False),
            "cloze": _ModelDescriptor(name=CLOZE_MODEL_NAME, fields=list(CLOZE_FIELDS), isCloze=True),
        },
        statuses=statuses,
        fingerprints=fingerprints,
        actions=actions,
        blocked=blocked,
    ).to_payload()


def _read_json(path: str) -> object:
    if path == "-":
        return json.loads(sys.stdin.read())
    with Path(path).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path: str, data: object) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    if path == "-":
        print(text)
        return
    Path(path).write_text(text + "\n", encoding="utf-8")


def _normalize_existing(payload: object) -> tuple[list[str], dict[str, list[str]], dict[str, str]]:
    """Aceita várias formas que o agente pode passar do Anki MCP."""

    if not isinstance(payload, dict):
        return [], {}, {}
    raw_models = payload.get("models")
    fields_map: dict[str, list[str]] = {}
    if isinstance(raw_models, dict):
        models = list(raw_models.keys())
        for name, fields in raw_models.items():
            if isinstance(fields, list):
                fields_map[str(name)] = [str(field) for field in fields]
    elif isinstance(raw_models, list):
        models = []
        for entry in raw_models:
            if isinstance(entry, str):
                models.append(entry)
            elif isinstance(entry, dict):
                name = str(entry.get("name") or "")
                if not name:
                    continue
                models.append(name)
                fields = entry.get("fields")
                if isinstance(fields, list):
                    fields_map[name] = [str(field) for field in fields]
    else:
        models = []

    fingerprints_raw = payload.get("fingerprints") or payload.get("template_fingerprints") or {}
    fingerprints = {
        str(name): str(value) for name, value in fingerprints_raw.items() if isinstance(value, str)
    }
    return models, fields_map, fingerprints


def _cmd_ensure(args: argparse.Namespace) -> int:
    templates_dir = Path(args.templates_dir or os.getenv("MED_FLASHCARDS_TEMPLATES_DIR") or DEFAULT_TEMPLATES_DIR)
    if not templates_dir.exists():
        print(f"Templates dir não encontrado: {templates_dir}", file=sys.stderr)
        return EXIT_IO

    existing_payload: object = {}
    if args.existing:
        existing_payload = _read_json(args.existing)
    existing_models, existing_fields, existing_fingerprints = _normalize_existing(existing_payload)

    plan = build_install_plan(
        templates_dir,
        existing_models=existing_models,
        existing_fields=existing_fields,
        existing_fingerprints=existing_fingerprints,
    )
    _write_json(args.output, plan)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    ensure = sub.add_parser(
        "ensure",
        help="emite o plano de createModel/updateModel* para os modelos da skill",
    )
    ensure.add_argument(
        "--templates-dir",
        help=f"override do diretório de templates (default: {DEFAULT_TEMPLATES_DIR})",
    )
    ensure.add_argument(
        "--existing",
        help="JSON com o estado atual dos modelos no Anki (modelNames + modelFieldNames + fingerprints opcional); '-' para stdin",
    )
    ensure.add_argument(
        "--output",
        default="-",
        help="arquivo de saída para o plano JSON; '-' para stdout (default)",
    )
    ensure.set_defaults(func=_cmd_ensure)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_IO


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Validate Anki note model fields captured from Anki MCP calls.

The agent still calls `mcp_anki-mcp_modelNames` and
`mcp_anki-mcp_modelFieldNames`. This script validates the collected result in a
small, testable contract before any card write.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError
from typing_extensions import TypeAliasType

SCHEMA = "medical-notes-workbench.anki-model-validation.v1"
SET_SCHEMA = "medical-notes-workbench.anki-model-set-validation.v1"
DEFAULT_REQUIRED_FIELDS = ("Frente", "Verso", "Verso Extra", "Obsidian")
QA_REQUIRED_FIELDS = ("Frente", "Verso", "Verso Extra", "Obsidian")
CLOZE_REQUIRED_FIELDS = ("Texto", "Verso Extra", "Obsidian")
DEFAULT_QA_MODEL = "Medicina"
DEFAULT_CLOZE_MODEL = "Medicina Cloze"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_VALIDATION = 3
EXIT_IO = 5


class ValidatorError(Exception):
    exit_code = EXIT_IO


class UsageError(ValidatorError):
    exit_code = EXIT_USAGE


class ValidationError(ValidatorError):
    exit_code = EXIT_VALIDATION


JsonValue = TypeAliasType(
    "JsonValue",
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"],
)
JsonObject = dict[str, JsonValue]
JsonValueAdapter = TypeAdapter(JsonValue)
JsonObjectAdapter = TypeAdapter(JsonObject)


class _ContractModel(BaseModel):
    # This CLI consumes raw Anki MCP JSON, but workflow decisions use only these
    # normalized fields after validation.
    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    def to_payload(self) -> JsonObject:
        return _json_object(self.model_dump(mode="json", by_alias=True), prefix=type(self).__name__)


class _AnkiModelEntry(_ContractModel):
    name: str
    fields: list[str] = Field(default_factory=list)

    @field_validator("name", mode="before")
    @classmethod
    def _name_as_text(cls, value: object) -> str:
        return "" if value is None else str(value)

    @field_validator("fields", mode="before")
    @classmethod
    def _fields_as_text_list(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("fields must be a list")
        return [str(field) for field in value]

    @model_validator(mode="after")
    def _requires_name(self) -> _AnkiModelEntry:
        if not self.name:
            raise ValueError("model name must be non-empty")
        return self


class _AnkiModelsPayload(_ContractModel):
    models: list[_AnkiModelEntry] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _accept_mcp_shapes(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        if "models" in value:
            return value
        models: list[JsonObject] = []
        for name, fields in value.items():
            models.append({"name": str(name), "fields": JsonValueAdapter.validate_python(fields)})
        return {"models": models}


class _CheckedModel(_ContractModel):
    name: str
    fields: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)


class _ModelValidationResult(_ContractModel):
    schema_id: Literal["medical-notes-workbench.anki-model-validation.v1"] = Field(
        default=SCHEMA,
        alias="schema",
    )
    ok: bool
    model: str = ""
    fields: list[str] = Field(default_factory=list)
    required_fields: list[str] = Field(default_factory=list)
    checked_models: list[_CheckedModel] = Field(default_factory=list)
    warning: str = ""


class _ResolvedModel(_ContractModel):
    model: str = ""
    fields: list[str] = Field(default_factory=list)
    required_fields: list[str] = Field(default_factory=list)
    ok: bool
    checked_models: list[_CheckedModel] = Field(default_factory=list)


class _ModelSetValidationResult(_ContractModel):
    schema_id: Literal["medical-notes-workbench.anki-model-set-validation.v1"] = Field(
        default=SET_SCHEMA,
        alias="schema",
    )
    ok: bool
    missing_kinds: list[str] = Field(default_factory=list)
    qa: _ResolvedModel
    cloze: _ResolvedModel


def _validation_message(exc: PydanticValidationError, *, prefix: str) -> UsageError:
    first = exc.errors()[0] if exc.errors() else {}
    loc_parts = first["loc"] if "loc" in first else ()
    loc = ".".join(str(part) for part in loc_parts) or "$"
    msg = str(first["msg"] if "msg" in first else str(exc))
    return UsageError(f"{prefix}: {loc}: {msg}")


def _json_object(value: object, *, prefix: str) -> JsonObject:
    try:
        return JsonObjectAdapter.validate_python(value)
    except PydanticValidationError as exc:
        raise _validation_message(exc, prefix=prefix) from exc


def _read_json(path: str) -> JsonValue:
    if path == "-":
        raw = json.loads(sys.stdin.read())
    else:
        with Path(path).open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    try:
        return JsonValueAdapter.validate_python(raw)
    except PydanticValidationError as exc:
        raise _validation_message(exc, prefix=f"JSON payload {path}") from exc


def _json(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def _models_from_payload(payload: object) -> list[_AnkiModelEntry]:
    if not isinstance(payload, dict):
        raise UsageError("Expected JSON object with models list, or {model_name: fields} map")
    try:
        return _AnkiModelsPayload.model_validate(payload).models
    except PydanticValidationError as exc:
        raise _validation_message(exc, prefix="Anki model payload") from exc


def _validate_models_result(
    payload: object,
    *,
    required_fields: tuple[str, ...] = DEFAULT_REQUIRED_FIELDS,
    preferred_model: str | None = None,
) -> _ModelValidationResult:
    models = _models_from_payload(payload)
    checked: list[_CheckedModel] = []

    ordered = models
    if preferred_model:
        ordered = sorted(models, key=lambda item: item.name != preferred_model)

    for model in ordered:
        fields = set(model.fields)
        missing = [field for field in required_fields if field not in fields]
        record = _CheckedModel(name=model.name, fields=model.fields, missing_fields=missing)
        checked.append(record)
        if not missing and (preferred_model is None or model.name == preferred_model or not preferred_model):
            return _ModelValidationResult(
                ok=True,
                model=model.name,
                fields=model.fields,
                required_fields=list(required_fields),
                checked_models=checked,
            )

    compatible = [record for record in checked if not record.missing_fields]
    if compatible and preferred_model:
        chosen = compatible[0]
        return _ModelValidationResult(
            ok=True,
            model=chosen.name,
            fields=chosen.fields,
            required_fields=list(required_fields),
            checked_models=checked,
            warning=f"Preferred model {preferred_model!r} is missing required fields; using {chosen.name!r}.",
        )

    return _ModelValidationResult(
        ok=False,
        model="",
        fields=[],
        required_fields=list(required_fields),
        checked_models=checked,
    )


def validate_models(
    payload: object,
    *,
    required_fields: tuple[str, ...] = DEFAULT_REQUIRED_FIELDS,
    preferred_model: str | None = None,
) -> JsonObject:
    return _validate_models_result(
        payload,
        required_fields=required_fields,
        preferred_model=preferred_model,
    ).to_payload()


def validate_model_set(
    payload: object,
    *,
    qa_required_fields: tuple[str, ...] = QA_REQUIRED_FIELDS,
    cloze_required_fields: tuple[str, ...] = CLOZE_REQUIRED_FIELDS,
    preferred_qa_model: str | None = DEFAULT_QA_MODEL,
    preferred_cloze_model: str | None = DEFAULT_CLOZE_MODEL,
) -> JsonObject:
    """Valida o par Q&A + Cloze a partir de um único payload de modelos.

    O payload aceita as mesmas formas que `validate_models`: dict
    `{nome: [campos]}`, ou `{ "models": [{"name": ..., "fields": [...]}] }`.
    """

    qa_result = _validate_models_result(
        payload,
        required_fields=qa_required_fields,
        preferred_model=preferred_qa_model,
    )
    cloze_result = _validate_models_result(
        payload,
        required_fields=cloze_required_fields,
        preferred_model=preferred_cloze_model,
    )
    ok = qa_result.ok and cloze_result.ok
    missing_kinds = [
        kind
        for kind, result in (("qa", qa_result), ("cloze", cloze_result))
        if not result.ok
    ]
    result = _ModelSetValidationResult(
        ok=ok,
        missing_kinds=missing_kinds,
        qa=_ResolvedModel(
            model=qa_result.model,
            fields=qa_result.fields,
            required_fields=list(qa_required_fields),
            ok=qa_result.ok,
            checked_models=qa_result.checked_models,
        ),
        cloze=_ResolvedModel(
            model=cloze_result.model,
            fields=cloze_result.fields,
            required_fields=list(cloze_required_fields),
            ok=cloze_result.ok,
            checked_models=cloze_result.checked_models,
        ),
    )
    return result.to_payload()


def _cmd_validate(args: argparse.Namespace) -> int:
    required_fields = tuple(args.required_field or DEFAULT_REQUIRED_FIELDS)
    result = validate_models(
        _read_json(args.models_json),
        required_fields=required_fields,
        preferred_model=args.preferred_model,
    )
    _json(result)
    if not result["ok"]:
        return EXIT_VALIDATION
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="validate captured model fields")
    validate.add_argument("--models-json", required=True, help="model fields JSON file, or '-' for stdin")
    validate.add_argument("--preferred-model", help="preferred Anki model name")
    validate.add_argument(
        "--required-field",
        action="append",
        default=None,
        help="required field name; repeatable",
    )
    validate.set_defaults(func=_cmd_validate)

    validate_set = sub.add_parser(
        "validate-set",
        help="valida em conjunto os modelos Q&A e Cloze",
    )
    validate_set.add_argument(
        "--models-json", required=True, help="modelos capturados do Anki, ou '-' para stdin"
    )
    validate_set.add_argument("--qa-model", default=DEFAULT_QA_MODEL)
    validate_set.add_argument("--cloze-model", default=DEFAULT_CLOZE_MODEL)
    validate_set.set_defaults(func=_cmd_validate_set)

    return parser


def _cmd_validate_set(args: argparse.Namespace) -> int:
    result = validate_model_set(
        _read_json(args.models_json),
        preferred_qa_model=args.qa_model,
        preferred_cloze_model=args.cloze_model,
    )
    _json(result)
    return EXIT_OK if result["ok"] else EXIT_VALIDATION


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValidatorError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    except (OSError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_IO


if __name__ == "__main__":
    raise SystemExit(main())

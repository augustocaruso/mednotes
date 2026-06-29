from __future__ import annotations

from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, TypeAdapter
from pydantic import ValidationError as PydanticValidationError
from typing_extensions import TypeAliasType

from mednotes.kernel.errors import ValidationError

JsonValue = TypeAliasType(
    "JsonValue",
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"],
)
_StrictJsonObject = dict[str, JsonValue]
_StrictJsonArray = list[JsonValue]

JsonObjectAdapter = TypeAdapter(_StrictJsonObject)
JsonArrayAdapter = TypeAdapter(_StrictJsonArray)


def _validated_json_object(value: Any) -> dict[str, Any]:
    return JsonObjectAdapter.validate_python(value)


def _validated_json_array(value: Any) -> list[Any]:
    return JsonArrayAdapter.validate_python(value)


JsonObject = Annotated[dict[str, Any], AfterValidator(_validated_json_object)]
JsonArray = Annotated[list[Any], AfterValidator(_validated_json_array)]


class ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        validate_assignment=True,
    )

    def to_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json", by_alias=True)
        JsonObjectAdapter.validate_python(payload)
        return payload


def contract_error(exc: PydanticValidationError, *, prefix: str) -> ValidationError:
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(part) for part in first.get("loc", ())) or "$"
    msg = str(first.get("msg") or str(exc))
    return ValidationError(f"{prefix}: {loc}: {msg}")

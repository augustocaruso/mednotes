from __future__ import annotations

from enum import StrEnum
from urllib.parse import quote

from pydantic import Field, field_validator

from mednotes.kernel.base import ContractModel


class ObsidianPathStyle(StrEnum):
    POSIX = "posix"
    WINDOWS_DRIVE = "windows_drive"
    WINDOWS_UNC = "windows_unc"


class ObsidianLinkMode(StrEnum):
    VAULT_FILE = "vault_file"
    ABSOLUTE_PATH = "absolute_path"


class ObsidianLinkCandidate(ContractModel):
    mode: ObsidianLinkMode
    uri: str

    @field_validator("uri")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("value must be non-empty")
        return cleaned


class ObsidianLinkBuildResult(ContractModel):
    selected: ObsidianLinkCandidate
    candidates: list[ObsidianLinkCandidate] = Field(default_factory=list)
    path_style: ObsidianPathStyle
    absolute_path: str
    vault_name: str = ""
    vault_relative_path: str = ""


def detect_path_style(path_text: str) -> ObsidianPathStyle:
    if path_text.startswith("\\\\"):
        return ObsidianPathStyle.WINDOWS_UNC
    if len(path_text) >= 3 and path_text[0].isalpha() and path_text[1:3] in {":\\", ":/"}:
        return ObsidianPathStyle.WINDOWS_DRIVE
    return ObsidianPathStyle.POSIX


def to_vault_relative_posix(value: str) -> str:
    return value.replace("\\", "/").strip("/")


def _absolute_path_uri(absolute_path: str) -> str:
    return f"obsidian://open?path={quote(absolute_path, safe='')}"


def _vault_file_uri(vault_name: str, vault_relative_path: str) -> str:
    return (
        "obsidian://open?"
        f"vault={quote(vault_name, safe='')}"
        f"&file={quote(to_vault_relative_posix(vault_relative_path), safe='')}"
    )


def build_obsidian_link_result(
    *,
    absolute_path: str,
    path_style: ObsidianPathStyle | str = "",
    vault_name: str = "",
    vault_relative_path: str = "",
) -> ObsidianLinkBuildResult:
    cleaned_absolute = absolute_path.strip()
    if not cleaned_absolute:
        raise ValueError("absolute_path must be non-empty")

    style = ObsidianPathStyle(path_style or detect_path_style(cleaned_absolute))
    cleaned_vault_name = vault_name.strip()
    cleaned_relative_path = (
        to_vault_relative_posix(vault_relative_path.strip()) if vault_relative_path.strip() else ""
    )
    candidates: list[ObsidianLinkCandidate] = []
    if cleaned_vault_name and cleaned_relative_path:
        candidates.append(
            ObsidianLinkCandidate(
                mode=ObsidianLinkMode.VAULT_FILE,
                uri=_vault_file_uri(cleaned_vault_name, cleaned_relative_path),
            )
        )
    candidates.append(
        ObsidianLinkCandidate(
            mode=ObsidianLinkMode.ABSOLUTE_PATH,
            uri=_absolute_path_uri(cleaned_absolute),
        )
    )
    return ObsidianLinkBuildResult(
        selected=candidates[0],
        candidates=candidates,
        path_style=style,
        absolute_path=cleaned_absolute,
        vault_name=cleaned_vault_name,
        vault_relative_path=cleaned_relative_path,
    )


def build_obsidian_link_candidates(
    *,
    absolute_path: str,
    path_style: ObsidianPathStyle | str = "",
    vault_name: str = "",
    vault_relative_path: str = "",
) -> list[ObsidianLinkCandidate]:
    return build_obsidian_link_result(
        absolute_path=absolute_path,
        path_style=path_style,
        vault_name=vault_name,
        vault_relative_path=vault_relative_path,
    ).candidates


def build_obsidian_deeplink(
    *,
    absolute_path: str,
    path_style: ObsidianPathStyle | str = "",
    vault_name: str = "",
    vault_relative_path: str = "",
) -> str:
    return build_obsidian_link_result(
        absolute_path=absolute_path,
        path_style=path_style,
        vault_name=vault_name,
        vault_relative_path=vault_relative_path,
    ).selected.uri

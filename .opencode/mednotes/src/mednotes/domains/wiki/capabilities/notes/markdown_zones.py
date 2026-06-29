"""Protected Markdown zones shared by Wiki workflows."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MarkdownZone:
    kind: str
    start: int
    end: int
    start_line: int
    end_line: int


def protected_markdown_zones(text: str) -> list[MarkdownZone]:
    zones: list[MarkdownZone] = []
    lines = text.splitlines(keepends=True)
    offset = 0
    idx = 0
    in_math = False
    math_start = 0
    math_start_line = 1
    while idx < len(lines):
        line = lines[idx]
        stripped = line.strip().lower()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            info = stripped[3:].strip()
            kind = "fenced_mermaid" if info.startswith("mermaid") else "fenced_code"
            start = offset
            start_line = idx + 1
            offset += len(line)
            idx += 1
            while idx < len(lines):
                current = lines[idx]
                current_stripped = current.strip().lower()
                offset += len(current)
                idx += 1
                if current_stripped.startswith(marker):
                    break
            zones.append(MarkdownZone(kind=kind, start=start, end=offset, start_line=start_line, end_line=idx))
            continue
        if stripped == "$$":
            if not in_math:
                in_math = True
                math_start = offset
                math_start_line = idx + 1
            else:
                zones.append(
                    MarkdownZone(
                        kind="display_math",
                        start=math_start,
                        end=offset + len(line),
                        start_line=math_start_line,
                        end_line=idx + 1,
                    )
                )
                in_math = False
        offset += len(line)
        idx += 1
    if in_math:
        zones.append(
            MarkdownZone(
                kind="display_math",
                start=math_start,
                end=len(text),
                start_line=math_start_line,
                end_line=len(lines),
            )
        )
    return zones


def blank_protected_markdown(text: str) -> str:
    chars = list(text)
    for zone in protected_markdown_zones(text):
        for idx in range(zone.start, zone.end):
            if chars[idx] != "\n":
                chars[idx] = " "
    return "".join(chars)


def is_protected_markdown_position(text: str, position: int) -> bool:
    return any(zone.start <= position < zone.end for zone in protected_markdown_zones(text))

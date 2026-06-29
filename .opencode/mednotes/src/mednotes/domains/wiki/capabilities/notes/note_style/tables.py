"""Markdown table validation and normalization for Wiki_Medicina notes."""
from __future__ import annotations

import re

from mednotes.domains.wiki.capabilities.notes.markdown_zones import protected_markdown_zones
from mednotes.domains.wiki.capabilities.notes.note_style.models import StyleIssue

_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def check_tables(body: str, errors: list[StyleIssue]) -> None:
    protected_lines = _protected_line_numbers(body)
    for block_lines, start_line in _iter_table_blocks(body.splitlines(), protected_lines=protected_lines):
        if len(block_lines) < 2:
            continue
        protected_lines = [escape_wikilink_alias_pipes_in_table_line(line) for line in block_lines]
        parsed = [_split_table_cells(line) for line in protected_lines]
        separator_index = _first_separator_index(parsed)
        if separator_index is None:
            errors.append(
                StyleIssue(
                    "malformed_markdown_table",
                    "markdown table is missing a separator row",
                    "error",
                    line=start_line,
                )
            )
            continue

        if any(table_line_has_unescaped_wikilink_pipe(line) for line in block_lines):
            errors.append(
                StyleIssue(
                    "unescaped_wikilink_pipe_in_table",
                    "escape Obsidian wikilink alias pipes inside markdown tables",
                    "error",
                    line=start_line,
                )
            )

        expected_columns = len(_trim_trailing_empty_cells(parsed[0]))
        if expected_columns == 0:
            errors.append(
                StyleIssue("malformed_markdown_table", "markdown table header has no columns", "error", line=start_line)
            )
            continue

        for offset, cells in enumerate(parsed):
            if offset == separator_index:
                if len(cells) != expected_columns:
                    errors.append(
                        StyleIssue(
                            "malformed_markdown_table",
                            "markdown table separator column count does not match the header",
                            "error",
                            line=start_line + offset,
                        )
                    )
                continue
            if len(_trim_trailing_empty_cells(cells)) != expected_columns:
                errors.append(
                    StyleIssue(
                        "malformed_markdown_table",
                        "markdown table row column count does not match the header",
                        "error",
                        line=start_line + offset,
                    )
                )


def escape_wikilink_alias_pipes_in_tables(text: str) -> str:
    fixed_lines: list[str] = []
    protected_lines = _protected_line_numbers(text)
    for idx, line in enumerate(text.splitlines(), start=1):
        if idx not in protected_lines and _is_table_line(line):
            fixed_lines.append(escape_wikilink_alias_pipes_in_table_line(line))
        else:
            fixed_lines.append(line)
    return "\n".join(fixed_lines)


def escape_wikilink_alias_pipes_in_table_line(line: str) -> str:
    def replace(match: re.Match[str]) -> str:
        inner = match.group(1)
        if "|" not in inner:
            return match.group(0)
        inner = re.sub(r"\s*(?<!\\)\|\s*", r"\\|", inner)
        inner = re.sub(r"\s*\\\|\s*", r"\\|", inner)
        return f"[[{inner}]]"

    return _WIKILINK_RE.sub(replace, line)


def table_line_has_unescaped_wikilink_pipe(line: str) -> bool:
    for match in _WIKILINK_RE.finditer(line):
        if re.search(r"(?<!\\)\|", match.group(1)):
            return True
    return False


def normalize_markdown_tables(text: str) -> str:
    lines = text.splitlines()
    normalized: list[str] = []
    cursor = 0
    protected_lines = _protected_line_numbers(text)
    for block_lines, start_line in _iter_table_blocks(lines, protected_lines=protected_lines):
        start_index = start_line - 1
        normalized.extend(lines[cursor:start_index])
        normalized.extend(_normalize_table_block(block_lines))
        cursor = start_index + len(block_lines)
    normalized.extend(lines[cursor:])
    return "\n".join(normalized)


def _iter_table_blocks(lines: list[str], *, protected_lines: set[int] | None = None) -> list[tuple[list[str], int]]:
    protected_lines = protected_lines or set()
    blocks: list[tuple[list[str], int]] = []
    idx = 0
    while idx < len(lines):
        if idx + 1 in protected_lines or not _is_table_line(lines[idx]):
            idx += 1
            continue
        start = idx
        block: list[str] = []
        while idx < len(lines) and idx + 1 not in protected_lines and _is_table_line(lines[idx]):
            block.append(lines[idx])
            idx += 1
        if len(block) >= 2:
            blocks.append((block, start + 1))
    return blocks


def _protected_line_numbers(text: str) -> set[int]:
    lines: set[int] = set()
    for zone in protected_markdown_zones(text):
        lines.update(range(zone.start_line, zone.end_line + 1))
    return lines


def _is_table_line(line: str) -> bool:
    return line.lstrip().startswith("|")


def _normalize_table_block(lines: list[str]) -> list[str]:
    parsed = [_split_table_cells(line) for line in lines]
    separator_index = _first_separator_index(parsed)
    if separator_index is None:
        return lines

    expected_columns = len(_trim_trailing_empty_cells(parsed[0]))
    if expected_columns == 0:
        return lines

    normalized_rows: list[list[str]] = []
    for idx, cells in enumerate(parsed):
        if idx == separator_index:
            row = cells[:expected_columns]
            row.extend(["---"] * (expected_columns - len(row)))
        else:
            row = _trim_trailing_empty_cells(cells)
            if len(row) > expected_columns:
                return lines
            row.extend([""] * (expected_columns - len(row)))
        normalized_rows.append([cell.strip() for cell in row])

    widths = [3] * expected_columns
    for row in normalized_rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))

    rendered: list[str] = []
    for idx, row in enumerate(normalized_rows):
        if idx == separator_index:
            rendered.append(_render_separator_row(row, widths))
        else:
            rendered.append(_render_table_row(row, widths))
    return rendered


def _split_table_cells(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        stripped = stripped[:-1]

    cells: list[str] = []
    current: list[str] = []
    for idx, char in enumerate(stripped):
        if char == "|" and (idx == 0 or stripped[idx - 1] != "\\"):
            cells.append("".join(current))
            current = []
        else:
            current.append(char)
    cells.append("".join(current))
    return cells


def _first_separator_index(rows: list[list[str]]) -> int | None:
    for idx, cells in enumerate(rows[:3]):
        if _is_separator_row(cells):
            return idx
    return None


def _is_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells)


def _trim_trailing_empty_cells(cells: list[str]) -> list[str]:
    trimmed = list(cells)
    while trimmed and not trimmed[-1].strip():
        trimmed.pop()
    return trimmed


def _render_table_row(cells: list[str], widths: list[int]) -> str:
    return "| " + " | ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(cells)) + " |"


def _render_separator_row(cells: list[str], widths: list[int]) -> str:
    tokens = [_separator_token(cell, widths[idx]) for idx, cell in enumerate(cells)]
    return "| " + " | ".join(tokens) + " |"


def _separator_token(cell: str, width: int) -> str:
    stripped = cell.strip()
    left = stripped.startswith(":")
    right = stripped.endswith(":")
    dash_count = max(3, width - int(left) - int(right))
    token = "-" * dash_count
    if left:
        token = ":" + token
    if right:
        token = token + ":"
    return token.ljust(width)

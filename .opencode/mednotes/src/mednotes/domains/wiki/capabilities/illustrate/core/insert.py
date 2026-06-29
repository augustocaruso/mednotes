r"""Etapa 5: inserção determinística de blocos de imagem em notas Markdown.

Contrato:
- ``insert_images(text, items)`` recebe o markdown completo (com ou sem
  frontmatter) e uma lista de :class:`InsertedImage`. Para cada item:
  localiza a seção alvo via ``section_path`` e insere o bloco
  ``![[<filename>]]`` + caption no fim da seção. Em seguida aplica patch
  aditivo no frontmatter (``images_enriched``, ``images_enriched_at``,
  ``image_count``, ``image_sources``) sem reordenar nem mexer nas chaves
  existentes (``chat_id``, ``url``, ``title``, ``exported_at``, ``model``,
  ``source``, ``tags``).
- ``items`` vazio → devolve ``text`` sem mudança alguma (sem patch).
- Path ambíguo (mais de uma seção com o mesmo trilho): primeira ocorrência
  vence. Limitação documentada — não há campo ``occurrence`` ainda.
- Path inexistente: levanta :class:`SectionNotFound`.

Suporta headings ATX (``#``..``######``) e setext (``===``/``---``
underline). Linhas dentro de blocos de código fenced (``\`\`\``` ou
``~~~``) são ignoradas para fins de detecção de headings.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from mednotes.domains.wiki.capabilities.illustrate.core import frontmatter

__all__ = ["InsertedImage", "SectionNotFound", "insert_images", "parse_sections"]


@dataclass(frozen=True)
class InsertedImage:
    anchor_id: str
    section_path: list[str]
    image_filename: str
    concept: str
    source: str
    source_url: str


class SectionNotFound(LookupError):
    """``section_path`` não casa com nenhuma seção da nota."""


@dataclass
class _Section:
    start_line: int          # índice da linha do heading (0-based, em body)
    end_line: int            # exclusivo: primeira linha da próxima seção de nível ≤
    level: int
    text: str
    path: list[str] = field(default_factory=list)


_ATX_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_FENCE_RE = re.compile(r"^[ \t]*(```+|~~~+)")


def _parse_headings(body: str) -> tuple[list[str], list[_Section]]:
    """Devolve (lines_split, sections). ``lines_split`` é ``body.split('\\n')``
    (preserva trailing-newline como elemento '' final, se houver)."""
    lines = body.split("\n")
    raw: list[tuple[int, int, str]] = []  # (line_idx, level, text)
    in_fence = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            i += 1
            continue
        if in_fence:
            i += 1
            continue
        m = _ATX_RE.match(line)
        if m:
            raw.append((i, len(m.group(1)), m.group(2).strip()))
            i += 1
            continue
        # Setext: linha não-vazia seguida de '===+' (H1) ou '---+' (H2).
        stripped = line.strip()
        if stripped and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if nxt and (set(nxt) == {"="} or set(nxt) == {"-"}):
                level = 1 if nxt[0] == "=" else 2
                raw.append((i, level, stripped))
                i += 2
                continue
        i += 1

    # Constrói paths via stack de (level, text).
    sections: list[_Section] = []
    stack: list[tuple[int, str]] = []
    for line_idx, level, text in raw:
        while stack and stack[-1][0] >= level:
            stack.pop()
        path = [t for _, t in stack] + [text]
        stack.append((level, text))
        sections.append(_Section(line_idx, -1, level, text, path))

    # end_line: próxima seção de nível <= esta, ou len(lines).
    for idx, sec in enumerate(sections):
        end = len(lines)
        for j in range(idx + 1, len(sections)):
            if sections[j].level <= sec.level:
                end = sections[j].start_line
                break
        sec.end_line = end

    return lines, sections


def _find_section(sections: list[_Section], path: list[str]) -> _Section:
    for sec in sections:
        if sec.path == path:
            return sec
    raise SectionNotFound(f"section_path não encontrado: {path!r}")


def _block_for(item: InsertedImage) -> tuple[str, str]:
    """(linha do embed, linha da caption)."""
    embed = f"![[{item.image_filename}]]"
    # Normaliza concept: strip whitespace e qualquer pontuação final repetida
    # ('.', '!', '?'), evitando '..*' quando o agente passa um conceito que
    # já termina em ponto.
    concept = item.concept.strip().rstrip(".!?")
    caption = (
        f"*Figura: {concept}.* "
        f"*Fonte: {item.source} — {item.source_url}*"
    )
    return embed, caption


def _build_group(items: list[InsertedImage]) -> list[str]:
    """Linhas a inserir para um grupo na mesma seção, com '' (linha em branco)
    entre captions e nas bordas. As bordas podem ser podadas pelo caller."""
    out: list[str] = []
    for item in items:
        out.append("")
        embed, caption = _block_for(item)
        out.append(embed)
        out.append(caption)
    out.append("")
    return out


def _insert_at(lines: list[str], idx: int, group: list[str]) -> list[str]:
    """Insere ``group`` em ``lines`` na posição ``idx``, podando bordas em
    branco redundantes pra evitar linhas vazias duplicadas."""
    g = list(group)
    if idx > 0 and lines[idx - 1] == "" and g and g[0] == "":
        g.pop(0)
    if idx < len(lines) and lines[idx] == "" and g and g[-1] == "":
        g.pop()
    return lines[:idx] + g + lines[idx:]


def _build_patch(
    items: list[InsertedImage], now: datetime
) -> dict[str, object]:
    counts = Counter(item.source for item in items)
    sorted_sources = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "images_enriched": True,
        "images_enriched_at": now,
        "image_count": len(items),
        "image_sources": [
            {"source": s, "count": c} for s, c in sorted_sources
        ],
    }


def parse_sections(text: str) -> list[dict[str, object]]:
    """Wrapper público sobre o parser de headings, JSON-serializável.

    Devolve uma lista de dicts ``{section_path, level, start_line, end_line,
    text}``. ``start_line`` e ``end_line`` são índices 0-based em **body**
    (após `frontmatter.read`); ``end_line`` é exclusivo.
    """
    _, body = frontmatter.read(text)
    _, sections = _parse_headings(body)
    return [
        {
            "section_path": list(s.path),
            "level": s.level,
            "text": s.text,
            "start_line": s.start_line,
            "end_line": s.end_line,
        }
        for s in sections
    ]


def insert_images(
    text: str,
    items: Iterable[InsertedImage],
    *,
    now: datetime | None = None,
) -> str:
    items_list = list(items)
    if not items_list:
        return text

    meta, body = frontmatter.read(text)
    lines, sections = _parse_headings(body)

    # Agrupa items por seção alvo (ordem de items preservada dentro de cada grupo).
    by_section: dict[int, list[InsertedImage]] = defaultdict(list)
    for item in items_list:
        sec = _find_section(sections, list(item.section_path))
        by_section[sections.index(sec)].append(item)

    # Insere de baixo pra cima pra não invalidar end_line.
    for sec_idx in sorted(by_section.keys(), reverse=True):
        sec = sections[sec_idx]
        group = _build_group(by_section[sec_idx])
        lines = _insert_at(lines, sec.end_line, group)

    new_body = "\n".join(lines)

    if now is None:
        now = datetime.now(UTC)
    patch = _build_patch(items_list, now)
    new_meta = {**meta, **patch}
    return frontmatter.write(new_meta, new_body)

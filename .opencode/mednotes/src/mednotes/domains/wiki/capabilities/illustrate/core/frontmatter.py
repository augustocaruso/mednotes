"""Leitura e escrita aditiva do frontmatter YAML de notas Markdown.

Contrato:
- O frontmatter, quando presente, começa em ``---\\n`` na primeira linha e
  termina no próximo ``---\\n``.
- ``read(text)`` retorna ``(meta_dict, body)``. Sem frontmatter -> ``({}, text)``.
- ``write(meta, body)`` reemite o frontmatter sempre, com ``meta == {}``
  o frontmatter é omitido.
- ``update(text, patch)`` aplica patch aditivamente sobre o frontmatter
  existente sem mexer em chaves que não estão no patch e sem reordenar
  agressivamente as chaves originais.

Não usa nenhum hack contra os campos de origem do export
(``chat_id``, ``url``, ``title``, ``exported_at``, ``model``, ``source``,
``tags``); o caller é responsável por não passar essas chaves no patch.
"""
from __future__ import annotations

from typing import Any

import yaml

_FENCE = "---"


def read(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith(_FENCE + "\n") and not text.startswith(_FENCE + "\r\n"):
        return {}, text
    after_first = text.split("\n", 1)[1]
    end_idx = after_first.find("\n" + _FENCE + "\n")
    if end_idx == -1:
        end_idx_crlf = after_first.find("\n" + _FENCE + "\r\n")
        if end_idx_crlf == -1:
            return {}, text
        end_idx = end_idx_crlf
    yaml_block = after_first[:end_idx]
    rest = after_first[end_idx + len("\n" + _FENCE + "\n") :]
    meta = yaml.safe_load(yaml_block) or {}
    if not isinstance(meta, dict):
        return {}, text
    return meta, rest


def write(meta: dict[str, Any], body: str) -> str:
    if not meta:
        return body
    yaml_block = yaml.safe_dump(
        meta,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).rstrip("\n")
    return f"{_FENCE}\n{yaml_block}\n{_FENCE}\n{body}"


def update(text: str, patch: dict[str, Any]) -> str:
    meta, body = read(text)
    merged = {**meta, **patch}
    return write(merged, body)

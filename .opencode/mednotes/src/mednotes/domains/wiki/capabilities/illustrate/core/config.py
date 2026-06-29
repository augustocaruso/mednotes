"""Carrega configuração local e persistente do Medical Notes Workbench."""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from mednotes.platform.paths import (
    default_config_path as _shared_default_config_path,
)
from mednotes.platform.paths import (
    expand_path as _shared_expand_path,
)
from mednotes.platform.paths import (
    find_config as _shared_find_config,
)
from mednotes.platform.paths import (
    resolve_wiki_dir,
)
from mednotes.platform.paths import (
    user_state_dir as _shared_user_state_dir,
)

_DEFAULTS: dict[str, Any] = {
    "vault": {"path": "", "attachments_subdir": "attachments/medicina"},
    "enrichment": {
        "max_anchors_per_note": 5,
        "max_image_dimension": 1600,
        "webp_min_savings_pct": 30,
        # Idioma preferido das figuras retornadas. Afeta:
        #  - queries que o gemini gera (pt-br adiciona 1 query em PT)
        #  - params do SerpAPI (hl/gl)
        #  - regra de desempate no rerank (prefere figuras com texto no idioma)
        # Valores: "pt-br", "en" (default), "any" (sem hl/gl).
        "preferred_language": "en",
    },
    "sources": {
        "enabled": [
            "wikimedia",
            "radiopaedia",
            "nih_open_i",
            "openstax",
            "dermnet",
            "teachmeanatomy",
            "web_search",
        ],
        "top_k_per_source": 6,
    },
    # `[gemini]` é consumido pelo orquestrador (`scripts/enrich_notes.py`),
    # não pelo toolbox em si. O enricher core não invoca LLM.
    "gemini": {
        "binary": "gemini",
        "model_anchors": "gemini-2.5-pro",
        "model_rerank": "gemini-2.5-pro",
        "max_candidates_per_anchor": 12,
        "timeout_seconds": 120,
    },
    "download": {
        # User-Agent pra fetch de bytes em `download.py`.
        # Default: UA browser-like (Chrome/macOS) — destrava osmosis,
        # thehealthy.com, e similares com anti-bot básico. Wikimedia também
        # aceita (qualquer browser legítimo passa). Trocar de volta pra UA
        # identificável (`medical-notes-workbench/0.1 (...)`) é mais
        # respeitoso mas perde fontes; veja config.example.toml.
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
    },
    "cache": {
        "path": "~/Documents/medical-notes-workbench/cache.db",
        "candidates_ttl_days": 30,
    },
}


def expand_path(p: str) -> Path:
    return _shared_expand_path(p)


def user_state_dir() -> Path:
    """Diretório persistente para estado editável pelo usuário.

    A extensão Gemini CLI é auto-updatable e pode recriar
    ``~/.gemini/extensions/medical-notes-workbench``. Configuração, chaves,
    cache e venv não devem depender desse diretório volátil.
    """
    return _shared_user_state_dir()


def default_config_path() -> Path:
    return user_state_dir() / "config.toml"


def default_env_path() -> Path:
    return user_state_dir() / ".env"


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def find_config(start: Path | None = None) -> Path | None:
    return _shared_find_config(start=start)


def load(path: Path | None = None) -> dict[str, Any]:
    if path is None:
        path = find_config()
    if path is None or not path.exists():
        return dict(_DEFAULTS)
    with path.open("rb") as f:
        data = tomllib.load(f)
    return _deep_merge(_DEFAULTS, data)


def resolve_wiki_root(config_path: Path | None = None, *, start: Path | None = None) -> Path | None:
    """Return canonical wiki root for workflows that can fall back from vault.path."""
    resolution = resolve_wiki_dir(config=config_path, start=start or Path.cwd(), enable_gemini_probe=False)
    return resolution.path if resolution.ok else None


def wiki_memory_path() -> Path:
    return _shared_default_config_path()

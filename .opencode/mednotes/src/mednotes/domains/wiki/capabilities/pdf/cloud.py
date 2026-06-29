"""Optional provider registry for PDF library cloud assists."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

RESERVED_PROVIDERS = {"huggingface_free", "free_api_quota", "hosted_open_model_free_quota"}
FUNCTIONAL_PROVIDERS = {"local", "gemini_cli"}


def resolve_provider(provider: str) -> dict[str, Any]:
    if provider in RESERVED_PROVIDERS:
        return {
            "schema": "medical-notes-workbench.pdf-library-provider-receipt.v1",
            "provider": provider,
            "model": "",
            "purpose": "anchors",
            "status": "blocked",
            "blocked_reason": "provider_not_implemented",
            "quota_limited": False,
            "item_count": 0,
            "created_at": _now(),
            "next_action": "use gemini_cli or local mode",
        }
    if provider not in FUNCTIONAL_PROVIDERS:
        return {
            "schema": "medical-notes-workbench.pdf-library-provider-receipt.v1",
            "provider": provider,
            "model": "",
            "purpose": "anchors",
            "status": "blocked",
            "blocked_reason": "provider_not_implemented",
            "quota_limited": False,
            "item_count": 0,
            "created_at": _now(),
            "next_action": "use gemini_cli or local mode",
        }
    return {
        "schema": "medical-notes-workbench.pdf-library-provider-receipt.v1",
        "provider": provider,
        "model": "gemini-configured-default" if provider == "gemini_cli" else "",
        "purpose": "anchors",
        "status": "completed",
        "blocked_reason": "",
        "quota_limited": False,
        "item_count": 0,
        "created_at": _now(),
    }


def _now() -> str:
    return datetime.now(UTC).isoformat()

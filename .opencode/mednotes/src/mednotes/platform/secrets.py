"""Keyring-first secret lookup for user-owned MedNotes credentials.

This module is an adapter boundary: it may talk to OS keyrings and environment
variables, but it never decides workflow state. Callers receive typed evidence
and let the FSM or command contract decide whether a missing secret blocks.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

from keyring.errors import KeyringError
from pydantic import StrictStr

from mednotes.kernel.base import ContractModel
from mednotes.platform.user_config import SecretConfig


class KeyringLike(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...


@dataclass(frozen=True)
class SecretLookup:
    keyring: KeyringLike | None = None


class SecretLookupResult(ContractModel):
    name: StrictStr
    status: StrictStr
    source: StrictStr = ""
    value: StrictStr = ""
    blocked_reason: StrictStr = ""
    keyring_status: StrictStr = "not_checked"
    keyring_error: StrictStr = ""


def _default_keyring() -> KeyringLike | None:
    try:
        import keyring
    except ImportError:
        return None
    return keyring


def resolve_secret(name: str, config: SecretConfig, *, lookup: SecretLookup | None = None) -> SecretLookupResult:
    active_lookup = lookup or SecretLookup(keyring=_default_keyring())
    keyring_status = "not_checked"
    keyring_error = ""
    if active_lookup.keyring is not None:
        try:
            value = active_lookup.keyring.get_password(config.keyring_service, config.keyring_username)
        except KeyringError as exc:
            keyring_status = "unavailable"
            keyring_error = str(exc)
        else:
            if value:
                return SecretLookupResult(
                    name=name,
                    status="available",
                    source="keyring",
                    value=value,
                    keyring_status="available",
                )
            keyring_status = "empty"
    else:
        keyring_status = "unavailable"
        keyring_error = "keyring package or backend unavailable"

    for env_name in config.env:
        value = os.environ.get(env_name)
        if value:
            return SecretLookupResult(
                name=name,
                status="available",
                source=f"env:{env_name}",
                value=value,
                keyring_status=keyring_status,
                keyring_error=keyring_error,
            )

    return SecretLookupResult(
        name=name,
        status="missing",
        blocked_reason="secret_missing",
        keyring_status=keyring_status,
        keyring_error=keyring_error,
    )

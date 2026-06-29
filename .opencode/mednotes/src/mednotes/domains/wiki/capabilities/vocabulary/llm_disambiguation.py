"""Gemini CLI seam for contextual body-link disambiguation."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ConfigDict, Field

from mednotes.kernel.base import ContractModel, JsonObject, JsonObjectAdapter

CONTEXTUAL_ALIAS_SCHEMA = "medical-notes-workbench.contextual-alias-disambiguation.v1"
_WINDOWS_PROMPT_FILE_THRESHOLD = 6000


class LinkDisambiguationError(RuntimeError):
    """Raised when contextual alias disambiguation cannot get valid JSON."""


class LinkDisambiguationRequiresOrchestrator(LinkDisambiguationError):
    """Raised when direct Gemini CLI disambiguation is disabled for UX safety."""


class _ContextualAliasResponseFields(ContractModel):
    """Typed response envelope required from contextual alias disambiguation."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, validate_assignment=True)

    schema_id: str = Field(alias="schema")
    decisions: list[JsonObject]


def call_contextual_alias_disambiguator(
    requests: list[dict[str, Any]],
    *,
    model: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    if not _direct_gemini_disambiguation_allowed():
        raise LinkDisambiguationRequiresOrchestrator(
            "desambiguação contextual por gemini -p direta está desativada; use orquestração por agente"
        )
    prompt = _build_prompt(requests)
    raw = _call_gemini(prompt, model=model, timeout_seconds=timeout_seconds)
    return _parse_response(raw)


def _direct_gemini_disambiguation_allowed() -> bool:
    return os.getenv("MEDNOTES_ALLOW_INTERNAL_GEMINI_DISAMBIGUATION", "").strip().lower() in {"1", "true", "yes"}


def _build_prompt(requests: list[dict[str, Any]]) -> str:
    payload = {
        "schema": "medical-notes-workbench.contextual-alias-disambiguation-request.v1",
        "instructions": [
            "Você decide links de termos médicos ambíguos no corpo de notas Obsidian.",
            "Escolha somente um dos candidatos fornecidos em cada ocorrência.",
            "Nunca invente alvo, nota, meaning_id ou alias.",
            "Se o contexto for insuficiente, responda action=defer.",
            "Se o termo claramente não deve ser linkado, responda action=no_link.",
            "Use confidence entre 0 e 1.",
            f"Responda apenas JSON com schema {CONTEXTUAL_ALIAS_SCHEMA}.",
        ],
        "requests": requests,
        "response_shape": {
            "schema": CONTEXTUAL_ALIAS_SCHEMA,
            "decisions": [
                {
                    "occurrence_id": "copie da requisição",
                    "action": "link|no_link|defer",
                    "chosen_meaning_id": "meaning id do candidato escolhido, se action=link",
                    "chosen_target": "target do candidato escolhido, se action=link",
                    "confidence": 0.0,
                    "reason_code": "curto_sem_espacos",
                    "rationale_summary": "resumo redigido, sem copiar trecho clínico bruto",
                }
            ],
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _call_gemini(prompt: str, *, model: str, timeout_seconds: int) -> str:
    binary = os.getenv("MEDNOTES_GEMINI_BINARY", "gemini")
    cmd = [_resolve_gemini_binary(binary), "--skip-trust", "-m", model]
    if _prompt_needs_file(prompt):
        return _invoke_with_prompt_file(cmd, prompt, timeout_seconds=timeout_seconds)
    return _invoke([*cmd, "-p", prompt], timeout_seconds=timeout_seconds)


def _resolve_gemini_binary(binary: str) -> str:
    expanded = os.path.expandvars(os.path.expanduser(binary))
    if _is_pathish(expanded):
        return expanded
    found = shutil.which(expanded)
    if found:
        return found
    if expanded.lower() in {"gemini", "gemini.cmd"}:
        for candidate in _npm_gemini_candidates():
            if candidate.is_file():
                return str(candidate)
    return expanded


def _is_pathish(value: str) -> bool:
    return "/" in value or "\\" in value or (len(value) >= 2 and value[1] == ":")


def _npm_gemini_candidates() -> list[Path]:
    candidates: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "npm" / "gemini.cmd")
    prefix = os.environ.get("NPM_CONFIG_PREFIX")
    if prefix:
        prefix_path = Path(prefix)
        candidates.extend([prefix_path / "gemini.cmd", prefix_path / "bin" / "gemini"])
    return candidates


def _prompt_needs_file(prompt: str) -> bool:
    return os.name == "nt" and len(prompt) >= _WINDOWS_PROMPT_FILE_THRESHOLD


def _invoke_with_prompt_file(cmd: list[str], prompt: str, *, timeout_seconds: int) -> str:
    with tempfile.TemporaryDirectory(prefix="mednotes-link-disambiguation-") as tmp:
        prompt_path = Path(tmp) / "prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        return _invoke([*cmd, "--include-directories", str(prompt_path.parent), "-p", f"@{prompt_path}"], timeout_seconds=timeout_seconds)


def _invoke(cmd: list[str], *, timeout_seconds: int) -> str:
    try:
        proc = subprocess.run(
            _subprocess_command(cmd),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise LinkDisambiguationError(f"gemini CLI excedeu timeout de {timeout_seconds}s") from exc
    except FileNotFoundError as exc:
        raise LinkDisambiguationError("gemini CLI não encontrado para desambiguação contextual") from exc
    except OSError as exc:
        raise LinkDisambiguationError(f"gemini CLI não pôde ser iniciado: {exc}") from exc
    if proc.returncode != 0:
        raise LinkDisambiguationError(f"gemini CLI falhou (rc={proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


def _subprocess_command(cmd: list[str]) -> list[str]:
    if not cmd:
        return cmd
    suffix = Path(cmd[0]).suffix.lower()
    if os.name == "nt" and suffix in {".cmd", ".bat"}:
        return [os.environ.get("COMSPEC") or "cmd.exe", "/d", "/s", "/c", *cmd]
    return cmd


def _parse_response(raw: str) -> JsonObject:
    stripped = raw.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise LinkDisambiguationError(f"resposta Gemini não é JSON válido: {exc}") from exc
    payload = JsonObjectAdapter.validate_python(payload)
    fields = _ContextualAliasResponseFields.model_validate(payload)
    if fields.schema_id != CONTEXTUAL_ALIAS_SCHEMA:
        raise LinkDisambiguationError(f"resposta Gemini precisa usar schema {CONTEXTUAL_ALIAS_SCHEMA}")
    return payload

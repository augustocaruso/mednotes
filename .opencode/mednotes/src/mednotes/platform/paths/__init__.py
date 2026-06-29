"""Shared path resolution for MedNotes.

User-specific paths must live in persistent MedNotes state, not in generated
runtime bundles or workflow code. This module centralizes that rule for Wiki,
flashcard, Obsidian and enrichment workflows.
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None

from mednotes.kernel.workflow import DecisionEvidence, HumanDecisionPacket, RejectedAutomation, WorkflowDecision


def extension_root() -> Path:
    """Raiz do bundle distribuído — a pasta que contém ``scripts/`` e ``src/``.

    Fonte ÚNICA da resolução (ADR-0001 regra 10): em vez de cada módulo contar
    níveis com ``parents[N]`` (que quebram quando o módulo muda de profundidade),
    todos chamam isto. Acha o ancestral certo, logo funciona no repo (``bundle/``)
    e no artefato (``dist/``).
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "scripts").is_dir() and (parent / "src").is_dir():
            return parent
    return here.parents[4]


APP_DIR_NAME = ".mednotes"
APP_HOME_ENV_VARS = ("MEDNOTES_HOME",)
CONFIG_ENV_VARS = ("MEDNOTES_CONFIG",)
GEMINI_MEMORY_ENV_VARS = ("MEDNOTES_GEMINI_MEMORY", "MEDICAL_NOTES_GEMINI_MEMORY")
PATHS_SCHEMA = "medical-notes-workbench.paths.v1"
ENVIRONMENT_PREFLIGHT_SCHEMA = "medical-notes-workbench.environment-preflight.v1"
CONFIG_REPAIR_SCHEMA = "medical-notes-workbench.config-template-repair.v1"
ENVIRONMENT_BLOCKER_CODE = "environment_blocker.windows_path_or_venv"
GEMINI_PATH_PROBE_SCHEMA = "medical-notes-workbench.gemini-path-probe.v1"
DEFAULT_CATALOG_PATH = "~/.mednotes/CATALOGO_WIKI.json"
DEFAULT_RAW_DIR = "~/.mednotes/Chats_Raw"
_WINDOWS_PROMPT_FILE_THRESHOLD = 6000

_PATHS_BLOCK_RE = re.compile(
    rf"(?ms)^```[ \t]*toml[^\n]*\b{re.escape(PATHS_SCHEMA)}\b[^\n]*\n(?P<body>.*?)^```[ \t]*$"
)
_WINDOWS_PATH_STRING_RE = re.compile(
    r'(?m)^(\s*(?:wiki_dir|raw_dir|path|catalog_path|vocabulary_db_path)\s*=\s*)"([^"\n]*\\[^"\n]*)"'
)
_MOJIBAKE_MARKERS = (
    "Ã¡",
    "Ã¢",
    "Ã£",
    "Ã§",
    "Ã©",
    "Ãª",
    "Ã­",
    "Ã³",
    "Ã´",
    "Ãº",
    "Ã‡",
    "Ã‰",
    "â€”",
    "â€“",
    "â€œ",
    "â€",
    "Â´",
    "�",
)
_MARKDOWN_AT_REF_RE = re.compile(r"@([^\s)]+\.md)\b", re.IGNORECASE)
_MARKDOWN_LINK_REF_RE = re.compile(r"\]\(([^)#?]+\.md)(?:[#?][^)]*)?\)", re.IGNORECASE)
_BARE_MARKDOWN_CONTEXT_REF_RE = re.compile(r"\b[\w.-]+\.md\b", re.IGNORECASE)
# Probe hints for identifying a Wiki root; taxonomy policy lives in
# bundle/scripts/mednotes/wiki/taxonomy/policy.py.
_WIKI_DIR_PROBE_TOP_LEVEL_HINTS = {
    "1. Clínica Médica",
    "1. Clinica Medica",
    "2. Cirurgia",
    "3. Ginecologia e Obstetrícia",
    "3. Ginecologia e Obstetricia",
    "4. Pediatria",
    "5. Preventiva e Saúde Coletiva",
    "5. Preventiva e Saude Coletiva",
}


@dataclass(frozen=True)
class PathCandidate:
    path: Path
    source: str
    exists: bool
    is_dir: bool
    reason: str = ""
    compat_warning: str = ""
    raw_dir: Path | None = None
    confidence: str = ""

    def as_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "path": str(self.path),
            "source": self.source,
            "exists": self.exists,
            "is_dir": self.is_dir,
        }
        if self.reason:
            data["reason"] = self.reason
        if self.compat_warning:
            data["compat_warning"] = self.compat_warning
        if self.raw_dir is not None:
            data["raw_dir"] = str(self.raw_dir)
        if self.confidence:
            data["confidence"] = self.confidence
        return data


@dataclass(frozen=True)
class WikiPathResolution:
    path: Path | None
    source: str
    memory_path: Path
    config_path: Path | None
    candidates: tuple[PathCandidate, ...] = ()
    compat_warnings: tuple[str, ...] = ()
    blocked_reason: str = ""
    next_action: str = ""
    required_inputs: tuple[str, ...] = ("wiki_dir",)
    human_decision_packet: HumanDecisionPacket | None = None

    @property
    def ok(self) -> bool:
        return self.path is not None and not self.blocked_reason

    def as_payload(self, *, phase: str = "resolve_wiki_dir") -> dict[str, object]:
        payload: dict[str, object] = {
            "schema": "medical-notes-workbench.path-resolution.v1",
            "phase": phase,
            "status": "completed" if self.ok else "blocked",
            "blocked_reason": self.blocked_reason,
            "next_action": self.next_action,
            "required_inputs": list(self.required_inputs),
            "wiki_dir": str(self.path) if self.path else "",
            "wiki_source": self.source,
            "wiki_dir_source": self.source,
            "memory_path": str(self.memory_path),
            "config_path": str(self.config_path) if self.config_path else "",
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "compat_warnings": list(self.compat_warnings),
            "human_decision_required": self.human_decision_packet is not None,
        }
        if self.human_decision_packet is not None:
            packet = _human_decision_packet_payload(self.human_decision_packet)
            payload["human_decision_packet"] = packet
            payload["human_decision_packets"] = [packet]
        return payload


def expand_path(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser()


def _human_decision_packet_payload(packet: HumanDecisionPacket) -> dict[str, object]:
    """Serialize typed human-decision packets at the public JSON edge."""

    return packet.model_dump(mode="json", by_alias=True)


def user_state_dir() -> Path:
    for env_name in APP_HOME_ENV_VARS:
        value = os.environ.get(env_name)
        if value:
            return expand_path(value)
    return Path.home() / APP_DIR_NAME


def default_config_path() -> Path:
    return user_state_dir() / "config.toml"


def persistent_gemini_path() -> Path:
    for env_name in GEMINI_MEMORY_ENV_VARS:
        value = os.environ.get(env_name)
        if value:
            return expand_path(value)
    return Path.home() / ".gemini" / "GEMINI.md"


def find_config(explicit: str | os.PathLike[str] | None = None, *, start: Path | None = None) -> Path | None:
    if explicit:
        return expand_path(explicit)

    for env_name in CONFIG_ENV_VARS:
        value = os.environ.get(env_name)
        if value:
            return expand_path(value)

    if any(os.environ.get(env_name) for env_name in APP_HOME_ENV_VARS):
        return default_config_path()

    cur = (start or Path.cwd()).resolve()
    for directory in (cur, *cur.parents):
        candidate = directory / "config.toml"
        if candidate.is_file():
            return candidate

    user_config = default_config_path()
    if user_config.is_file():
        return user_config
    return user_config


def read_toml(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    if tomllib is None:
        raise RuntimeError("tomllib unavailable; use Python 3.11+ for TOML support")
    return _loads_toml_with_windows_path_fallback(path.read_text(encoding="utf-8"))


def _loads_toml_with_windows_path_fallback(text: str) -> dict[str, Any]:
    if tomllib is None:
        raise RuntimeError("tomllib unavailable; use Python 3.11+ for TOML support")
    toml = tomllib
    try:
        return toml.loads(text)
    except toml.TOMLDecodeError:
        repaired = _WINDOWS_PATH_STRING_RE.sub(
            lambda match: match.group(1) + json.dumps(match.group(2).replace("\\", "/"), ensure_ascii=False),
            text,
        )
        if repaired == text:
            raise
        return toml.loads(repaired)


def config_encoding_warnings(path: Path | None) -> list[dict[str, Any]]:
    if not path or not path.exists():
        return []
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return [
            {
                "code": "config_encoding.not_utf8",
                "path": str(path),
                "detail": str(exc),
                "next_action": "Rodar repair-config-template --json para recriar config.toml como UTF-8; nao editar manualmente durante o workflow.",
            }
        ]
    markers = sorted({marker for marker in _MOJIBAKE_MARKERS if marker in text})
    if not markers:
        return []
    return [
        {
            "code": "config_encoding.possible_mojibake",
            "path": str(path),
            "markers": markers[:8],
            "next_action": "Rodar repair-config-template --json para recriar config.toml a partir do template UTF-8; set-paths deve alterar apenas [paths].",
        }
    ]


def read_persistent_paths(memory_path: Path | None = None) -> tuple[dict[str, str], str]:
    path = memory_path or persistent_gemini_path()
    if not path.exists():
        return {}, ""
    text = path.read_text(encoding="utf-8")
    match = _PATHS_BLOCK_RE.search(text)
    if not match:
        return {}, ""
    if tomllib is None:
        return {}, "tomllib unavailable; use Python 3.11+ for legacy GEMINI.md paths"
    try:
        data = _loads_toml_with_windows_path_fallback(match.group("body"))
    except tomllib.TOMLDecodeError as exc:
        return {}, str(exc)
    paths = data.get("paths", {}) if isinstance(data.get("paths"), dict) else {}
    return {
        key: str(value).strip()
        for key, value in paths.items()
        if key in {"wiki_dir", "raw_dir"} and isinstance(value, str) and value.strip()
    }, ""


def resolve_wiki_dir(
    *,
    explicit: str | os.PathLike[str] | None = None,
    config: str | os.PathLike[str] | None = None,
    start: Path | None = None,
    context_paths: list[str | os.PathLike[str]] | tuple[str | os.PathLike[str], ...] | None = None,
    enable_gemini_probe: bool = False,
) -> WikiPathResolution:
    memory_path = persistent_gemini_path()
    config_path = find_config(config, start=start)

    def maybe_probe(resolution: WikiPathResolution) -> WikiPathResolution:
        return _maybe_gemini_path_probe(
            resolution,
            enabled=enable_gemini_probe,
            start=start,
            context_paths=context_paths,
        )

    if explicit:
        path = expand_path(explicit).resolve()
        return WikiPathResolution(
            path=path,
            source="cli",
            memory_path=memory_path,
            config_path=config_path,
            candidates=(_candidate(path, "cli", "explicit --wiki-dir"),),
        )

    env_candidate = _env_wiki_candidate()
    if env_candidate is not None:
        if env_candidate.exists and env_candidate.is_dir:
            return WikiPathResolution(
                path=env_candidate.path,
                source=env_candidate.source,
                memory_path=memory_path,
                config_path=config_path,
                candidates=(env_candidate,),
                compat_warnings=(env_candidate.compat_warning,) if env_candidate.compat_warning else (),
            )
        return maybe_probe(
            _blocked(
                "env_wiki_dir_invalid",
                "Corrigir MED_WIKI_DIR para uma pasta existente ou remover a variavel e configurar o TOML do app.",
                memory_path,
                config_path,
                candidates=(env_candidate,),
            )
        )

    config_candidate = _config_wiki_candidate(config_path)
    if config_candidate is not None:
        candidate = config_candidate
        if candidate.exists and candidate.is_dir:
            return WikiPathResolution(
                path=candidate.path,
                source=candidate.source,
                memory_path=memory_path,
                config_path=config_path,
                candidates=(candidate,),
            )
        return maybe_probe(
            _blocked(
                "config_wiki_dir_invalid",
                f"Atualizar {config_path or default_config_path()} [paths].wiki_dir com uma pasta existente.",
                memory_path,
                config_path,
                candidates=(candidate,),
            )
        )

    contextual = _contextual_candidates(start=start, context_paths=context_paths)
    distinct_contextual = _distinct_candidates(contextual)
    if len(distinct_contextual) == 1:
        candidate = distinct_contextual[0]
        return WikiPathResolution(
            path=candidate.path,
            source=candidate.source,
            memory_path=memory_path,
            config_path=config_path,
            candidates=tuple(contextual),
        )
    if len(distinct_contextual) > 1:
        return _blocked(
            "ambiguous_wiki_dir",
            f"Registrar o wiki_dir correto em {config_path or default_config_path()} [paths].wiki_dir.",
            memory_path,
            config_path,
            candidates=tuple(contextual),
            human_decision_packet=_wiki_path_choice_packet(tuple(contextual), config_path or default_config_path()),
        )
    return maybe_probe(
        _blocked(
            "missing_wiki_dir",
            f"Rodar set-paths ou adicionar [paths].wiki_dir em {config_path or default_config_path()}.",
            memory_path,
            config_path,
            candidates=(),
        )
    )


def resolve_raw_dir(
    *,
    explicit: str | os.PathLike[str] | None = None,
    config: str | os.PathLike[str] | None = None,
    start: Path | None = None,
) -> Path:
    if explicit:
        return expand_path(explicit)
    env_value = os.getenv("MED_RAW_DIR")
    if env_value:
        return expand_path(env_value)
    config_path = find_config(config, start=start)
    cfg = read_toml(config_path)
    paths = cfg.get("paths", {}) if isinstance(cfg.get("paths"), dict) else {}
    if paths.get("raw_dir"):
        return expand_path(str(paths["raw_dir"]))
    return expand_path(DEFAULT_RAW_DIR)


def plan_set_paths(
    *,
    config: str | os.PathLike[str] | Path | None = None,
    wiki_dir: str | os.PathLike[str] | Path | None,
    raw_dir: str | os.PathLike[str] | Path | None,
    agent_repair: bool = False,
) -> dict[str, Any]:
    config_path = find_config(config) or default_config_path()
    wiki_path = expand_path(wiki_dir).resolve(strict=False) if wiki_dir else None
    raw_path = expand_path(raw_dir).resolve(strict=False) if raw_dir else None
    errors: list[dict[str, str]] = []

    if wiki_path is None:
        errors.append({"field": "wiki_dir", "reason": "missing"})
    elif not wiki_path.exists() or not wiki_path.is_dir():
        errors.append({"field": "wiki_dir", "path": str(wiki_path), "reason": "not_existing_directory"})

    if raw_path is None:
        errors.append({"field": "raw_dir", "reason": "missing"})
    elif not raw_path.exists() or not raw_path.is_dir():
        errors.append({"field": "raw_dir", "path": str(raw_path), "reason": "not_existing_directory"})

    if errors:
        return {
            "schema": "medical-notes-workbench.set-paths.v1",
            "phase": "set-paths",
            "status": "blocked",
            "blocked_reason": "path_validation_failed",
            "next_action": "Escolher pastas existentes para Wiki_Medicina e Chats_Raw antes de persistir os caminhos.",
            "required_inputs": ["wiki_dir", "raw_dir"],
            "human_decision_required": False,
            "config_path": str(config_path),
            "wiki_dir": str(wiki_path) if wiki_path else "",
            "raw_dir": str(raw_path) if raw_path else "",
            "errors": errors,
        }

    assert wiki_path is not None
    assert raw_path is not None
    encoding_warnings = config_encoding_warnings(config_path)
    if any(warning.get("code") == "config_encoding.not_utf8" for warning in encoding_warnings):
        return {
            "schema": "medical-notes-workbench.set-paths.v1",
            "phase": "set-paths",
            "status": "blocked",
            "blocked_reason": "config_encoding.not_utf8",
            "next_action": "Rodar repair-config-template --json para recriar config.toml em UTF-8 antes de alterar [paths].",
            "required_inputs": ["utf8_config"],
            "human_decision_required": False,
            "config_path": str(config_path),
            "wiki_dir": str(wiki_path),
            "raw_dir": str(raw_path),
            "config_encoding_warnings": encoding_warnings,
        }
    existing = _read_paths_section(config_path)
    conflicts = _valid_existing_path_conflicts(existing, wiki_dir=wiki_path, raw_dir=raw_path)
    if agent_repair and conflicts:
        packet = _path_conflict_packet(conflicts, wiki_dir=wiki_path, raw_dir=raw_path, config_path=config_path)
        packet_payload = _human_decision_packet_payload(packet)
        return {
            "schema": "medical-notes-workbench.set-paths.v1",
            "phase": "set-paths",
            "status": "blocked",
            "blocked_reason": "path_conflict.requires_decision",
            "next_action": "O TOML do app ja aponta para caminhos validos diferentes; confirmar qual par deve permanecer antes de sobrescrever.",
            "required_inputs": ["human_decision"],
            "human_decision_required": True,
            "human_decision_packet": packet_payload,
            "human_decision_packets": [packet_payload],
            "config_path": str(config_path),
            "wiki_dir": str(wiki_path),
            "raw_dir": str(raw_path),
            "conflicts": conflicts,
            "config_encoding_warnings": encoding_warnings,
        }

    _write_paths_config(config_path, wiki_dir=wiki_path, raw_dir=raw_path)
    return {
        "schema": "medical-notes-workbench.set-paths.v1",
        "phase": "set-paths",
        "status": "updated",
        "blocked_reason": "",
        "next_action": "",
        "required_inputs": [],
        "human_decision_required": False,
        "config_path": str(config_path),
        "wiki_dir": str(wiki_path),
        "raw_dir": str(raw_path),
        "config_encoding_warnings": encoding_warnings,
    }


def repair_config_template(
    *,
    config: str | os.PathLike[str] | Path | None = None,
    template: str | os.PathLike[str] | Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    config_path = find_config(config) or default_config_path()
    template_path = expand_path(template) if template else _infer_extension_root() / "config.example.toml"
    warnings_before = config_encoding_warnings(config_path)
    if not template_path.is_file():
        return {
            "schema": CONFIG_REPAIR_SCHEMA,
            "phase": "repair-config-template",
            "status": "blocked",
            "blocked_reason": "config_template_missing",
            "next_action": "Fornecer --template apontando para config.example.toml UTF-8.",
            "required_inputs": ["template"],
            "human_decision_required": False,
            "config_path": str(config_path),
            "template_path": str(template_path),
            "warnings_before": warnings_before,
        }
    try:
        template_text = template_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return {
            "schema": CONFIG_REPAIR_SCHEMA,
            "phase": "repair-config-template",
            "status": "blocked",
            "blocked_reason": "config_template_not_utf8",
            "next_action": "Substituir o template por uma copia UTF-8 sem BOM antes de reparar config.toml.",
            "required_inputs": ["template"],
            "human_decision_required": False,
            "config_path": str(config_path),
            "template_path": str(template_path),
            "warnings_before": warnings_before,
            "error": str(exc),
        }

    existing_paths = _read_paths_section_relaxed(config_path)
    repaired_text = _replace_paths_section_values(template_text, existing_paths)
    current_text = _read_text_relaxed(config_path)[0] if config_path.exists() else ""
    changed = current_text != repaired_text
    status = "planned" if dry_run else "updated" if changed else "unchanged"
    if not dry_run and changed:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = config_path.with_name(config_path.name + ".tmp")
        tmp_path.write_text(repaired_text, encoding="utf-8")
        os.replace(tmp_path, config_path)

    return {
        "schema": CONFIG_REPAIR_SCHEMA,
        "phase": "repair-config-template",
        "status": status,
        "blocked_reason": "",
        "next_action": "",
        "required_inputs": [],
        "human_decision_required": False,
        "config_path": str(config_path),
        "template_path": str(template_path),
        "changed": changed,
        "dry_run": bool(dry_run),
        "preserved_paths": existing_paths,
        "warnings_before": warnings_before,
        "warnings_after": [] if dry_run else config_encoding_warnings(config_path),
    }


def environment_preflight(
    *,
    extension_root: str | os.PathLike[str] | None = None,
    state_dir: str | os.PathLike[str] | None = None,
    sample_paths: list[str | os.PathLike[str]] | tuple[str | os.PathLike[str], ...] | None = None,
    platform_name: str | None = None,
    python_version: tuple[int, int, int] | None = None,
    uv_path: str | None = None,
    powershell_command: str | None = None,
    require_uv: bool = True,
) -> dict[str, Any]:
    """Return a compact preflight for Python/uv/venv/path issues.

    Parameters make Windows cases testable on non-Windows CI without shelling
    out or touching the user's real PATH.
    """
    state = expand_path(state_dir) if state_dir else user_state_dir()
    extension = expand_path(extension_root) if extension_root else _infer_extension_root()
    persistent_venv = state / ".venv"
    bundle_venv = extension / ".venv" if extension else None
    detected_platform = platform_name or platform.system()
    is_windows = detected_platform.lower().startswith("win")
    version = python_version or sys.version_info[:3]
    uv = uv_path if uv_path is not None else shutil.which("uv")
    configured_venv = os.environ.get("UV_PROJECT_ENVIRONMENT", "")
    paths = [str(item) for item in (sample_paths or []) if str(item).strip()]
    if configured_venv:
        paths.append(configured_venv)
    if extension:
        paths.append(str(extension))
    paths.append(str(state))

    checks: list[dict[str, Any]] = []
    warnings: list[str] = []
    blockers: list[str] = []

    def add_check(name: str, ok: bool, detail: str = "", *, warning: bool = False) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "warning": bool(warning and ok is False)})
        if ok:
            return
        (warnings if warning else blockers).append(name)

    add_check(
        "python_version",
        tuple(version) >= (3, 11, 0),
        ".".join(str(part) for part in version),
    )
    add_check("uv_available", bool(uv), uv or "uv not found on PATH", warning=not require_uv)

    if configured_venv:
        normalized_configured = _normcase_path(expand_path(configured_venv))
        normalized_expected = _normcase_path(persistent_venv)
        add_check(
            "uv_project_environment_persistent",
            normalized_configured == normalized_expected,
            f"UV_PROJECT_ENVIRONMENT={configured_venv}; expected={persistent_venv}",
            warning=True,
        )
    else:
        add_check(
            "uv_project_environment_set",
            False,
            f"set UV_PROJECT_ENVIRONMENT to {persistent_venv}",
            warning=True,
        )

    add_check(
        "persistent_venv_exists",
        persistent_venv.exists(),
        str(persistent_venv),
        warning=True,
    )
    if bundle_venv is not None:
        add_check(
            "bundle_venv_absent",
            not bundle_venv.exists(),
            str(bundle_venv),
            warning=True,
        )

    if is_windows:
        add_check(
            "powershell_execution_policy_hint",
            True,
            "use -ExecutionPolicy Bypass with bundled setup/reset scripts",
        )
        if powershell_command:
            add_check(
                "powershell_command_quoted",
                _powershell_command_looks_quoted(powershell_command),
                powershell_command,
                warning=True,
            )
        for item in paths:
            if _path_needs_windows_quoting(item):
                add_check("windows_path_with_spaces", False, item, warning=True)
                break
        for item in paths:
            if "\r\n" in item:
                add_check("crlf_in_path_or_command", False, "CRLF detected in path/command text", warning=True)
                break
        for item in paths:
            if len(item) >= 240:
                add_check("windows_long_path_risk", False, f"{len(item)} chars", warning=True)
                break

    status = "blocked" if blockers else "completed_with_warnings" if warnings else "completed"
    next_action = ""
    if status != "completed":
        next_action = _environment_next_action(is_windows=is_windows)
    return {
        "schema": ENVIRONMENT_PREFLIGHT_SCHEMA,
        "status": status,
        "blocked_reason": ENVIRONMENT_BLOCKER_CODE if blockers else "",
        "next_action": next_action,
        "required_inputs": ["python", "uv", "persistent_venv", "wiki_dir"],
        "platform": detected_platform,
        "python": ".".join(str(part) for part in version),
        "uv_path": uv or "",
        "state_dir": str(state),
        "persistent_venv": str(persistent_venv),
        "extension_root": str(extension) if extension else "",
        "checks": checks,
        "warnings": warnings,
        "blockers": blockers,
        "setup_command": "/mednotes:setup",
        "reset_command": "scripts\\bootstrap_windows_python_uv.ps1" if is_windows else "uv sync",
    }


def _infer_extension_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _normcase_path(path_value: Path) -> str:
    return os.path.normcase(str(path_value.expanduser().resolve(strict=False)))


def _path_needs_windows_quoting(value: str) -> bool:
    text = str(value or "")
    return bool(re.search(r"\s", text) and re.match(r"^[A-Za-z]:[\\/]", text))


def _powershell_command_looks_quoted(command: str) -> bool:
    text = str(command or "")
    if not text.strip():
        return True
    windows_paths = re.findall(r"[A-Za-z]:[\\/][^;&|]+", text)
    for value in windows_paths:
        if " " in value and f'"{value}"' not in text and f"'{value}'" not in text:
            return False
    return True


def _environment_next_action(*, is_windows: bool) -> str:
    if is_windows:
        return (
            "Rodar /mednotes:setup. Se persistir no Windows, executar "
            "scripts\\bootstrap_windows_python_uv.ps1; como fallback, "
            "scripts\\reset_windows_python_uv.ps1 -FullReset."
        )
    return (
        "Rodar /mednotes:setup, configurar UV_PROJECT_ENVIRONMENT para "
        "~/.mednotes/.venv e repetir uv sync antes do workflow."
    )


def _candidate(
    path: Path,
    source: str,
    reason: str,
    compat_warning: str = "",
    *,
    raw_dir: Path | None = None,
    confidence: str = "",
) -> PathCandidate:
    return PathCandidate(
        path=path,
        source=source,
        exists=path.exists(),
        is_dir=path.is_dir(),
        reason=reason,
        compat_warning=compat_warning,
        raw_dir=raw_dir,
        confidence=confidence,
    )


def _env_wiki_candidate() -> PathCandidate | None:
    env_value = os.getenv("MED_WIKI_DIR")
    if not env_value:
        return None
    return _candidate(
        expand_path(env_value).resolve(strict=False),
        "env:MED_WIKI_DIR",
        "temporary environment override",
        "MED_WIKI_DIR e override temporario; persista o caminho em config.toml [paths].wiki_dir.",
    )


def _config_wiki_candidate(config_path: Path | None) -> PathCandidate | None:
    cfg = read_toml(config_path)
    paths = cfg.get("paths", {}) if isinstance(cfg.get("paths"), dict) else {}
    value = paths.get("wiki_dir")
    if not value:
        return None
    return _candidate(
        expand_path(str(value)).resolve(strict=False),
        "config:[paths].wiki_dir",
        f"{config_path or default_config_path()} [paths].wiki_dir",
    )


def _read_paths_section(config_path: Path | None) -> dict[str, str]:
    cfg = read_toml(config_path)
    paths = cfg.get("paths", {}) if isinstance(cfg.get("paths"), dict) else {}
    return {
        key: str(value).strip()
        for key, value in paths.items()
        if key in {"wiki_dir", "raw_dir"} and isinstance(value, str) and value.strip()
    }


def _read_paths_section_relaxed(config_path: Path | None) -> dict[str, str]:
    if not config_path or not config_path.exists() or tomllib is None:
        return {}
    text, _encoding = _read_text_relaxed(config_path)
    try:
        data = _loads_toml_with_windows_path_fallback(text)
    except tomllib.TOMLDecodeError:
        return {}
    paths = data.get("paths", {}) if isinstance(data.get("paths"), dict) else {}
    return {
        key: str(value).strip()
        for key, value in paths.items()
        if key in {"wiki_dir", "raw_dir", "catalog_path", "vocabulary_db_path"}
        and isinstance(value, str)
        and value.strip()
    }


def _read_text_relaxed(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "utf-16"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8-replace"


def _valid_existing_path_conflicts(
    existing: dict[str, str],
    *,
    wiki_dir: Path,
    raw_dir: Path,
) -> list[dict[str, str]]:
    conflicts: list[dict[str, str]] = []
    desired = {"wiki_dir": wiki_dir, "raw_dir": raw_dir}
    for field, desired_path in desired.items():
        value = existing.get(field)
        if not value:
            continue
        current = expand_path(value).resolve(strict=False)
        if current == desired_path or not current.exists() or not current.is_dir():
            continue
        conflicts.append(
            {
                "field": field,
                "current": str(current),
                "proposed": str(desired_path),
            }
        )
    return conflicts


def _path_conflict_packet(
    conflicts: list[dict[str, str]],
    *,
    wiki_dir: Path,
    raw_dir: Path,
    config_path: Path,
) -> HumanDecisionPacket:
    resume_action = "Repetir set-paths sem --agent-repair ou informar explicitamente a escolha humana."
    packet = _path_human_decision_packet(
        kind="path_conflict_choice",
        phase="set-paths",
        reason_code="path_conflict.requires_decision",
        question="O TOML do app ja tem caminhos validos. Quais caminhos devem ser mantidos?",
        developer_summary="Dois pares de paths locais validos competem; sobrescrever sem escolha pode apontar a Wiki errada.",
        resume_action=resume_action,
        options=[
            {
                "id": "keep_existing",
                "label": "Manter TOML atual",
                "value": str(config_path),
                "description": "Caminhos existentes no TOML tambem sao diretorios validos.",
            },
            {
                "id": "use_proposed",
                "label": "Usar caminhos propostos",
                "value": f"wiki_dir={wiki_dir}; raw_dir={raw_dir}",
                "description": "Caminhos propostos foram validados localmente.",
            },
        ],
        evidence=[
            DecisionEvidence(
                summary="Config atual e proposta de agente apontam para diretorios validos diferentes.",
                technical_code="path_conflict.requires_decision",
                source="mednotes.platform.paths",
                candidates=[{"conflicts": conflicts}],
                risk="wrong_vault_mutation",
            )
        ],
    )
    return packet.model_copy(update={"context": {"conflicts": conflicts, "config_path": str(config_path)}})


def _write_paths_config(config_path: Path, *, wiki_dir: Path, raw_dir: Path | None) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    old_text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    new_text = _replace_paths_section(old_text, wiki_dir=wiki_dir, raw_dir=raw_dir)
    tmp_path = config_path.with_name(config_path.name + ".tmp")
    tmp_path.write_text(new_text, encoding="utf-8")
    os.replace(tmp_path, config_path)


def _replace_paths_section(text: str, *, wiki_dir: Path, raw_dir: Path | None) -> str:
    values = {"wiki_dir": wiki_dir.as_posix()}
    if raw_dir is not None:
        values["raw_dir"] = raw_dir.as_posix()
    return _replace_paths_section_values(text, values)


def _replace_paths_section_values(text: str, values: dict[str, str]) -> str:
    lines = text.splitlines()
    section_start: int | None = None
    section_end = len(lines)
    for index, line in enumerate(lines):
        if line.strip() == "[paths]":
            section_start = index
            for next_index in range(index + 1, len(lines)):
                stripped = lines[next_index].strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    section_end = next_index
                    break
            break

    field_order = ("wiki_dir", "raw_dir", "catalog_path", "vocabulary_db_path")
    path_lines = [
        f"{field} = {json.dumps(str(values[field]), ensure_ascii=False)}"
        for field in field_order
        if str(values.get(field) or "").strip()
    ]
    if not path_lines:
        path_lines = ['wiki_dir = ""', 'raw_dir = ""']

    if section_start is None:
        prefix = lines + ([""] if lines else [])
        new_lines = [*prefix, "[paths]", *path_lines]
    else:
        remaining = [
            line
            for line in lines[section_start + 1 : section_end]
            if not re.match(r"^\s*(wiki_dir|raw_dir|catalog_path|vocabulary_db_path)\s*=", line)
        ]
        new_lines = [
            *lines[: section_start + 1],
            *path_lines,
            *remaining,
            *lines[section_end:],
        ]
    return "\n".join(new_lines).rstrip() + "\n"


def _contextual_candidates(
    *,
    start: Path | None,
    context_paths: list[str | os.PathLike[str]] | tuple[str | os.PathLike[str], ...] | None,
) -> list[PathCandidate]:
    raw_paths: list[Path] = []
    if context_paths:
        raw_paths.extend(expand_path(item) for item in context_paths)
    raw_paths.append(start or Path.cwd())

    candidates: list[PathCandidate] = []
    for raw in raw_paths:
        path = raw.resolve() if raw.exists() else raw.expanduser().resolve(strict=False)
        scan_start = path if path.is_dir() else path.parent
        for current in (scan_start, *scan_start.parents):
            if _looks_like_wiki_root(current):
                candidates.append(_candidate(current, "context", "nearest plausible Wiki root"))
                break
    return _distinct_candidates(candidates)


def _looks_like_wiki_root(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    if any((path / dirname).is_dir() for dirname in _WIKI_DIR_PROBE_TOP_LEVEL_HINTS):
        return True
    if (path / ".obsidian").is_dir() and any(item.suffix.lower() == ".md" for item in path.glob("*.md")):
        return True
    if (path / ".obsidian").is_dir() and any((path / dirname).is_dir() for dirname in _WIKI_DIR_PROBE_TOP_LEVEL_HINTS):
        return True
    return False


def _distinct_candidates(candidates: list[PathCandidate]) -> list[PathCandidate]:
    by_path: dict[str, PathCandidate] = {}
    for candidate in candidates:
        key = os.path.normcase(str(candidate.path.resolve() if candidate.path.exists() else candidate.path))
        by_path.setdefault(key, candidate)
    return list(by_path.values())


def _blocked(
    reason: str,
    action: str,
    memory_path: Path,
    config_path: Path | None,
    *,
    candidates: tuple[PathCandidate, ...],
    extra_next_action: str = "",
    compat_warnings: tuple[str, ...] = (),
    human_decision_packet: HumanDecisionPacket | None = None,
) -> WikiPathResolution:
    next_action = action if not extra_next_action else f"{action} Detalhe: {extra_next_action}"
    return WikiPathResolution(
        path=None,
        source="",
        memory_path=memory_path,
        config_path=config_path,
        candidates=candidates,
        compat_warnings=compat_warnings,
        blocked_reason=reason,
        next_action=next_action,
        human_decision_packet=human_decision_packet,
    )


def _maybe_gemini_path_probe(
    blocked: WikiPathResolution,
    *,
    enabled: bool,
    start: Path | None,
    context_paths: list[str | os.PathLike[str]] | tuple[str | os.PathLike[str], ...] | None,
) -> WikiPathResolution:
    if not enabled or not _gemini_path_probe_enabled_by_env():
        return blocked

    binary = _gemini_binary()
    if binary is None:
        return _blocked_with_probe_warning(blocked, "Gemini CLI nao encontrado para sondagem de caminhos.")

    context = _gemini_probe_context(
        start=start,
        context_paths=context_paths,
        memory_path=blocked.memory_path,
    )
    if not context.strip():
        context = "Nenhum arquivo de contexto local foi encontrado."

    retry_detail = ""
    probe_warnings: list[str] = []
    for attempt in range(2):
        prompt = _gemini_probe_prompt(blocked, retry_detail=retry_detail, attempt=attempt)
        result = _run_gemini_probe(binary, prompt, context)
        if result.get("error"):
            retry_detail = str(result["error"])
            probe_warnings.append(retry_detail)
            continue

        payload = _extract_json_object(str(result.get("stdout", "")))
        if payload is None:
            retry_detail = "A resposta do Gemini nao continha um objeto JSON parseavel."
            probe_warnings.append(retry_detail)
            continue

        candidates, invalid_reasons = _validated_probe_candidates(payload)
        if len(candidates) == 1:
            candidate = candidates[0]
            target_config = blocked.config_path or default_config_path()
            _write_paths_config(
                target_config,
                wiki_dir=candidate.path,
                raw_dir=candidate.raw_dir,
            )
            return WikiPathResolution(
                path=candidate.path,
                source="gemini_probe",
                memory_path=blocked.memory_path,
                config_path=target_config,
                candidates=(candidate,),
                compat_warnings=(
                    f"Caminhos validados via Gemini CLI e persistidos em {target_config}.",
                ),
            )
        if len(candidates) > 1:
            target_config = blocked.config_path or default_config_path()
            return _blocked(
                "ambiguous_wiki_dir",
                f"Escolher um unico wiki_dir e registrar em {target_config} [paths].wiki_dir.",
                blocked.memory_path,
                target_config,
                candidates=tuple(candidates),
                compat_warnings=tuple(probe_warnings),
                human_decision_packet=_wiki_path_choice_packet(tuple(candidates), target_config),
            )

        retry_detail = "; ".join(invalid_reasons) or "Gemini nao retornou candidatos validos."
        probe_warnings.append(retry_detail)

    return _blocked_with_probe_warning(blocked, "Sondagem Gemini sem caminho valido: " + "; ".join(probe_warnings))


def _gemini_path_probe_enabled_by_env() -> bool:
    if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("MEDNOTES_GEMINI_BINARY"):
        return False
    value = os.getenv("MEDNOTES_GEMINI_PATH_PROBE", "").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _gemini_binary() -> Path | None:
    configured = os.getenv("MEDNOTES_GEMINI_BINARY")
    if configured:
        configured_path = expand_path(configured)
        if configured_path.exists():
            return configured_path
        found = shutil.which(configured)
        return Path(found) if found else None
    found = shutil.which("gemini")
    return Path(found) if found else None


def _gemini_probe_prompt(blocked: WikiPathResolution, *, retry_detail: str, attempt: int) -> str:
    retry = ""
    if retry_detail:
        retry = (
            "\n\nTentativa anterior falhou na validacao local. "
            f"Problema: {retry_detail}. Retorne outro candidato se houver."
        )
    return (
        "Voce esta ajudando o Medical Notes Workbench a descobrir caminhos locais em tempo de execucao.\n"
        "Use apenas os arquivos de contexto fornecidos abaixo. Eles podem incluir GEMINI.md e arquivos "
        "Markdown referenciados por ele.\n"
        "Responda somente com JSON valido, sem markdown, no schema "
        f"{GEMINI_PATH_PROBE_SCHEMA}.\n"
        "Formato aceito: {\"wiki_dir\":\"/abs/Wiki_Medicina\",\"raw_dir\":\"/abs/Chats_Raw\","
        "\"confidence\":\"high|medium|low\",\"evidence\":\"arquivo/trecho curto\",\"source\":\"arquivo-de-contexto.md\"}.\n"
        "Se houver mais de uma possibilidade, responda {\"candidates\":[...]} com objetos no mesmo formato.\n"
        "Caminhos devem ser absolutos. Se nao houver evidencia suficiente, use candidates=[].\n"
        f"Bloqueio atual: {blocked.blocked_reason}. Acao esperada: {blocked.next_action}.\n"
        f"Tentativa: {attempt + 1}."
        f"{retry}\n\n"
        "CONTEXTO LOCAL:\n"
    )


def _run_gemini_probe(binary: Path, prompt: str, context: str) -> dict[str, object]:
    timeout = _probe_timeout_seconds()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return {"error": "Sondagem Gemini requer chamada síncrona fora de um event loop ativo."}
    try:
        return asyncio.run(_run_gemini_probe_async(binary, prompt + context, context, timeout))
    except RuntimeError as exc:
        return {"error": f"Falha ao iniciar sondagem async do Gemini: {exc}"}


async def _run_gemini_probe_async(binary: Path, prompt: str, context: str, timeout: float) -> dict[str, object]:
    if _gemini_probe_needs_prompt_file(binary, prompt):
        with tempfile.TemporaryDirectory(prefix="mednotes-path-probe-") as tmp:
            prompt_path = Path(tmp) / "prompt.md"
            prompt_path.write_text(prompt, encoding="utf-8")
            cmd = _gemini_probe_subprocess_command(
                [
                    str(binary),
                    "--include-directories",
                    str(prompt_path.parent),
                    "-p",
                    f"@{prompt_path}",
                    "--approval-mode",
                    "plan",
                ]
            )
            return await _communicate_gemini_probe(cmd, context, timeout)

    cmd = _gemini_probe_subprocess_command(
        [
            str(binary),
            "-p",
            prompt,
            "--approval-mode",
            "plan",
        ]
    )
    return await _communicate_gemini_probe(cmd, context, timeout)


async def _communicate_gemini_probe(cmd: list[str], context: str, timeout: float) -> dict[str, object]:
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(context.encode("utf-8")), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.communicate()
            return {"error": f"Gemini CLI excedeu {timeout:g}s na sondagem de caminhos."}
    except TimeoutError:
        return {"error": f"Gemini CLI excedeu {timeout:g}s na sondagem de caminhos."}
    except OSError as exc:
        return {"error": f"Gemini CLI nao pode ser executado: {exc}"}
    if process.returncode != 0:
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        return {"error": f"Gemini CLI retornou codigo {process.returncode}: {stderr_text[:500]}"}
    return {
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }


def _gemini_probe_needs_prompt_file(binary: Path, prompt: str) -> bool:
    if os.name != "nt":
        return False
    suffix = Path(str(binary)).suffix.lower()
    return suffix in {".cmd", ".bat"} or len(prompt) >= _WINDOWS_PROMPT_FILE_THRESHOLD


def _gemini_probe_subprocess_command(cmd: list[str]) -> list[str]:
    if not cmd:
        return cmd
    suffix = Path(cmd[0]).suffix.lower()
    if os.name == "nt" and suffix in {".cmd", ".bat"}:
        return [os.environ.get("COMSPEC") or "cmd.exe", "/d", "/s", "/c", *cmd]
    return cmd


def _probe_timeout_seconds() -> float:
    raw = os.getenv("MEDNOTES_GEMINI_PATH_PROBE_TIMEOUT", "20").strip()
    try:
        value = float(raw)
    except ValueError:
        return 20.0
    return max(1.0, min(value, 120.0))


def _extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _validated_probe_candidates(payload: dict[str, Any]) -> tuple[list[PathCandidate], list[str]]:
    raw_candidates: list[Any]
    if isinstance(payload.get("candidates"), list):
        raw_candidates = payload["candidates"]
    elif payload.get("wiki_dir") or payload.get("path"):
        raw_candidates = [payload]
    else:
        raw_candidates = []

    candidates: list[PathCandidate] = []
    invalid_reasons: list[str] = []
    for index, raw in enumerate(raw_candidates, start=1):
        if isinstance(raw, str):
            raw = {"wiki_dir": raw}
        if not isinstance(raw, dict):
            invalid_reasons.append(f"candidato {index} nao e objeto JSON")
            continue

        wiki_value = str(raw.get("wiki_dir") or raw.get("path") or "").strip()
        if not wiki_value:
            invalid_reasons.append(f"candidato {index} sem wiki_dir")
            continue

        confidence = str(raw.get("confidence") or "medium").strip().lower()
        if confidence == "low":
            invalid_reasons.append(f"{wiki_value}: confidence low")
            continue

        wiki_path = expand_path(wiki_value).resolve(strict=False)
        if not wiki_path.exists() or not wiki_path.is_dir():
            invalid_reasons.append(f"{wiki_path}: nao existe ou nao e diretorio")
            continue
        if not _looks_like_wiki_root(wiki_path):
            invalid_reasons.append(f"{wiki_path}: nao parece raiz da Wiki_Medicina")
            continue

        raw_path = _validated_optional_raw_dir(raw.get("raw_dir"))
        if raw.get("raw_dir") and raw_path is None:
            invalid_reasons.append(f"{raw.get('raw_dir')}: raw_dir nao existe ou nao e diretorio")
            continue

        evidence = str(raw.get("evidence") or raw.get("source") or "Gemini CLI path probe").strip()
        candidates.append(
            _candidate(
                wiki_path,
                "gemini_probe",
                evidence,
                raw_dir=raw_path,
                confidence=confidence,
            )
        )
    return _distinct_candidates(candidates), invalid_reasons


def _validated_optional_raw_dir(value: object) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = expand_path(value.strip()).resolve(strict=False)
    return path if path.exists() and path.is_dir() else None


def _write_persistent_paths(
    memory_path: Path,
    *,
    wiki_dir: Path,
    raw_dir: Path | None,
    evidence: str,
) -> None:
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"```toml {PATHS_SCHEMA}",
        "# Atualizado automaticamente depois de validar a sondagem Gemini CLI.",
        f"# Evidencia: {_toml_comment(evidence)}",
        "[paths]",
        f"wiki_dir = {json.dumps(wiki_dir.as_posix(), ensure_ascii=False)}",
    ]
    if raw_dir is not None:
        lines.append(f"raw_dir = {json.dumps(raw_dir.as_posix(), ensure_ascii=False)}")
    lines.append("```")
    block = "\n".join(lines) + "\n"

    old_text = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""
    if _PATHS_BLOCK_RE.search(old_text):
        new_text = _PATHS_BLOCK_RE.sub(block.rstrip("\n"), old_text, count=1)
        if not new_text.endswith("\n"):
            new_text += "\n"
    else:
        prefix = old_text.rstrip()
        new_text = (prefix + "\n\n" if prefix else "# Medical Notes Workbench local memory\n\n") + block
    memory_path.write_text(new_text, encoding="utf-8")


def _toml_comment(value: str) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")[:240]


def _blocked_with_probe_warning(blocked: WikiPathResolution, warning: str) -> WikiPathResolution:
    return WikiPathResolution(
        path=None,
        source=blocked.source,
        memory_path=blocked.memory_path,
        config_path=blocked.config_path,
        candidates=blocked.candidates,
        compat_warnings=(*blocked.compat_warnings, warning),
        blocked_reason=blocked.blocked_reason,
        next_action=blocked.next_action,
        required_inputs=blocked.required_inputs,
        human_decision_packet=blocked.human_decision_packet,
    )


def _wiki_path_choice_packet(candidates: tuple[PathCandidate, ...], config_path: Path) -> HumanDecisionPacket | None:
    distinct = _distinct_candidates(list(candidates))
    if not distinct:
        return None
    options: list[dict[str, object]] = []
    for index, candidate in enumerate(distinct, start=1):
        label = candidate.path.name or str(candidate.path)
        options.append(
            {
                "id": f"wiki_path_{index}",
                "label": label,
                "value": str(candidate.path),
                "description": f"{candidate.source}: {candidate.reason}",
            }
        )
    packet = _path_human_decision_packet(
        kind="wiki_path_choice",
        phase="resolve_wiki_dir",
        reason_code="ambiguous_wiki_dir",
        question="Qual pasta e a Wiki_Medicina correta para este usuario?",
        developer_summary="A resolucao encontrou mais de uma candidata plausivel; escolher automaticamente pode mutar a Wiki errada.",
        resume_action=(
            f"Registrar a opcao escolhida em {config_path} [paths].wiki_dir "
            "e repetir o workflow."
        ),
        options=options,
        evidence=[
            DecisionEvidence(
                summary="Mais de uma candidata de Wiki foi encontrada.",
                technical_code="ambiguous_wiki_dir",
                source="mednotes.platform.paths",
                candidates=[
                    {
                        "path": str(candidate.path),
                        "raw_dir": str(candidate.raw_dir) if candidate.raw_dir is not None else "",
                        "source": candidate.source,
                        "reason": candidate.reason,
                    }
                    for candidate in distinct
                ],
                risk="wrong_vault_mutation",
            )
        ],
    )
    return packet.model_copy(update={"context": {"config_path": str(config_path)}})


def _path_human_decision_packet(
    *,
    kind: str,
    phase: str,
    reason_code: str,
    question: str,
    developer_summary: str,
    resume_action: str,
    options: list[dict[str, object]],
    evidence: list[DecisionEvidence],
) -> HumanDecisionPacket:
    """Build path-choice packets through the canonical workflow decision model."""

    decision = WorkflowDecision(
        kind="ask_human",
        phase=phase,
        reason_code=reason_code,
        public_summary=question,
        developer_summary=developer_summary,
        evidence=evidence,
        next_action=resume_action,
        resume_action=resume_action,
        rejected_automations=[
            RejectedAutomation(
                kind="auto_fix",
                reason_code=reason_code,
                reason="Path mutation without explicit choice can target the wrong vault.",
                safe=False,
            ),
            RejectedAutomation(
                kind="auto_defer",
                reason_code=reason_code,
                reason="Deferring without a closed choice leaves setup blocked.",
                safe=False,
            ),
            RejectedAutomation(
                kind="auto_plan",
                reason_code=reason_code,
                reason="Planning cannot disambiguate user-owned local paths.",
                safe=False,
            ),
        ],
        recommended_option_id=str(options[0]["id"]),
        options=options,
        human_decision_kind=kind,
    )
    return HumanDecisionPacket.model_validate(decision.to_human_decision_packet())


def _gemini_probe_context(
    *,
    start: Path | None,
    context_paths: list[str | os.PathLike[str]] | tuple[str | os.PathLike[str], ...] | None,
    memory_path: Path,
) -> str:
    budget = _probe_context_budget()
    snippets: list[str] = []
    queue = _gemini_context_seed_files(start=start, context_paths=context_paths, memory_path=memory_path)
    seen: set[str] = set()
    used = 0

    while queue and used < budget:
        path = queue.pop(0).expanduser().resolve(strict=False)
        key = os.path.normcase(str(path))
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        remaining = max(0, budget - used)
        if remaining <= 0:
            break
        header = f"--- FILE: {path} ---\n"
        body = text[: max(0, remaining - len(header) - 2)]
        snippets.append(header + body + "\n")
        used += len(header) + len(body) + 1
        for ref in _markdown_context_references(text):
            resolved = _resolve_context_reference(path, ref)
            if resolved is not None:
                queue.append(resolved)

    return "\n".join(snippets)


def _probe_context_budget() -> int:
    raw = os.getenv("MEDNOTES_GEMINI_PATH_PROBE_CONTEXT_BYTES", "60000").strip()
    try:
        value = int(raw)
    except ValueError:
        return 60000
    return max(4096, min(value, 200000))


def _gemini_context_seed_files(
    *,
    start: Path | None,
    context_paths: list[str | os.PathLike[str]] | tuple[str | os.PathLike[str], ...] | None,
    memory_path: Path,
) -> list[Path]:
    seeds: list[Path] = []

    def add(path: Path) -> None:
        key = os.path.normcase(str(path.expanduser().resolve(strict=False)))
        if key not in {os.path.normcase(str(item.expanduser().resolve(strict=False))) for item in seeds}:
            seeds.append(path)

    add(memory_path)
    add(Path.home() / ".gemini" / "GEMINI.md")

    raw_roots: list[Path] = []
    if start is not None:
        raw_roots.append(start)
    if context_paths:
        raw_roots.extend(expand_path(item) for item in context_paths)
    raw_roots.append(Path.cwd())

    for raw in raw_roots:
        path = raw.expanduser().resolve(strict=False)
        scan_start = path if path.suffix == "" else path.parent
        if path.exists() and path.is_file():
            add(path)
            scan_start = path.parent
        for directory in (scan_start, *scan_start.parents):
            add(directory / "GEMINI.md")

    extension_root = _infer_extension_root()
    add(extension_root / "GEMINI.md")
    add(extension_root / "extension" / "GEMINI.md")
    return seeds


def _markdown_context_references(text: str) -> list[str]:
    refs: list[str] = []
    refs.extend(match.group(1) for match in _MARKDOWN_AT_REF_RE.finditer(text))
    refs.extend(match.group(1) for match in _MARKDOWN_LINK_REF_RE.finditer(text))
    refs.extend(match.group(0) for match in _BARE_MARKDOWN_CONTEXT_REF_RE.finditer(text))
    return refs


def _resolve_context_reference(base_file: Path, ref: str) -> Path | None:
    if "://" in ref:
        return None
    clean = ref.strip().strip("<>").split("#", 1)[0].split("?", 1)[0]
    if not clean:
        return None
    path = expand_path(clean)
    if not path.is_absolute():
        path = base_file.parent / path
    return path

#!/usr/bin/env python3
"""Cross-platform git policy helpers for the Obsidian vault."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None


STATE_SUBDIR = Path(".mednotes")
APP_HOME_ENV_VARS = ("MEDNOTES_HOME",)
CONFIG_ENV_VARS = ("MEDNOTES_CONFIG",)
VAULT_IDENTITY_MARKER = ".medical-notes-workbench-vault"
WORKTREE_SUBDIR = "vault-worktrees"
RESTORE_PLAN_SUBDIR = "vault-restore-plans"
GIT_IDENTITIES_FILE = "vault.git-identities.json"
GUARD_LEASE_SUBDIR = Path("vault-guard") / "leases"
GUARD_LEASE_TTL_MINUTES = 12 * 60
GIT_PROBE_TIMEOUT_SECONDS = 30
GIT_NETWORK_TIMEOUT_SECONDS = 120
GITHUB_LOGIN_TIMEOUT_SECONDS = 300
SUBPROCESS_TEXT_KWARGS = {"encoding": "utf-8", "errors": "replace"}


@dataclass(frozen=True)
class VaultHumanDecisionOption:
    """Closed option rendered in setup payloads that need human confirmation."""

    label: str
    description: str
    resume_action: str

    def to_payload(self) -> dict[str, object]:
        return {
            "label": self.label,
            "description": self.description,
            "resume_action": self.resume_action,
        }


@dataclass(frozen=True)
class VaultHumanDecisionPacket:
    """Typed local packet for the standalone vault setup script."""

    kind: str
    prompt: str
    options: tuple[VaultHumanDecisionOption, ...] = ()
    resume_action: str = ""
    current_branch: str = ""

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind,
            "prompt": self.prompt,
            "options": [option.to_payload() for option in self.options],
        }
        if self.resume_action:
            payload["resume_action"] = self.resume_action
        if self.current_branch:
            payload["current_branch"] = self.current_branch
        return payload


class VaultGitError(RuntimeError):
    """Operational error that should be shown directly to the caller."""

    def __init__(
        self,
        message: str,
        *,
        status: str = "blocked_error",
        blocked_reason: str = "error",
        next_action: str | None = None,
        required_inputs: list[str] | None = None,
        human_decision_required: bool = False,
        human_decision_packet: VaultHumanDecisionPacket | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.blocked_reason = blocked_reason
        self.next_action = next_action
        self.required_inputs = required_inputs or []
        self.human_decision_required = human_decision_required
        self.human_decision_packet = human_decision_packet

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema": "medical-notes-workbench.vault-error.v1",
            "status": self.status,
            "blocked_reason": self.blocked_reason,
            "human_message": str(self),
            "human_decision_required": self.human_decision_required,
        }
        if self.next_action:
            payload["next_action"] = self.next_action
        if self.required_inputs:
            payload["required_inputs"] = self.required_inputs
        if self.human_decision_packet:
            payload["human_decision_packet"] = self.human_decision_packet.to_payload()
        return payload


class MarkProvidedAction(argparse.Action):
    """Record whether an optional argument was explicitly provided."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | None,
        option_string: str | None = None,
    ) -> None:
        setattr(namespace, self.dest, values)
        setattr(namespace, f"{self.dest}_provided", True)


@dataclass(frozen=True)
class VaultContext:
    vault_dir: Path
    origin_url: str | None = None

    @property
    def backup_online(self) -> bool:
        return bool(self.origin_url)


@dataclass(frozen=True)
class GitIdentity:
    name: str
    email: str
    source: str


@dataclass(frozen=True)
class SetupRestorePoint:
    restore_point_id: str
    status: str
    label: str
    working_tree_clean: bool
    local_changes_present: bool


@dataclass(frozen=True)
class RunFinishRunIdResolution:
    run_id: str
    requested_run_id: str = ""
    auto_recovered: bool = False
    recovery_reason: str = ""


def _state_dir() -> Path:
    for env_name in APP_HOME_ENV_VARS:
        value = os.environ.get(env_name)
        if value:
            return Path(os.path.expandvars(value)).expanduser()
    return Path.home() / STATE_SUBDIR


def _read_first_config_line(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise VaultGitError(f"vault_resolve: nao consegui ler {path}: {exc}") from exc
    for line in lines:
        value = line.strip()
        if value:
            return value
    raise VaultGitError(f"vault_resolve: {path} esta vazio")


def resolve_vault_dir(preferred: str | None = None) -> Path:
    if preferred:
        raw = preferred
    elif os.environ.get("VAULT_DIR"):
        raw = os.environ["VAULT_DIR"]
    elif configured_wiki := _configured_wiki_dir():
        raw = str(configured_wiki)
    else:
        path_file = _state_dir() / "vault.path"
        if not path_file.is_file():
            raise VaultGitError(
                "vault_resolve: nao consegui resolver o caminho do vault.\n"
                "Defina UMA das opcoes abaixo:\n"
                "  - flag --vault-dir <path>\n"
                "  - variavel de ambiente VAULT_DIR\n"
                "  - arquivo ~/.mednotes/vault.path com o caminho absoluto"
            )
        raw = _read_first_config_line(path_file)

    vault_dir = Path(raw).expanduser()
    if not vault_dir.is_dir():
        raise VaultGitError(f"vault_resolve: {vault_dir} nao e diretorio")
    return _coerce_to_configured_git_root(vault_dir.resolve())


def _timeout_result(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=args, returncode=124, stdout="", stderr=f"timeout depois de {timeout}s")


def _windows_command_fallbacks(command: str, env: dict[str, str]) -> list[str]:
    name = Path(command).name.lower()
    candidates: list[Path] = []
    program_roots = [
        env.get("ProgramFiles"),
        env.get("ProgramFiles(x86)"),
    ]
    local_app_data = env.get("LOCALAPPDATA")
    if local_app_data:
        program_roots.append(str(Path(local_app_data) / "Programs"))

    roots = [Path(root) for root in program_roots if root]
    if name in {"git", "git.exe"}:
        for root in roots:
            candidates.extend(
                [
                    root / Path("Git") / "cmd" / "git.exe",
                    root / Path("Git") / "bin" / "git.exe",
                ]
            )
    elif name in {"gh", "gh.exe"}:
        for root in roots:
            candidates.append(root / Path("GitHub CLI") / "gh.exe")

    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        value = str(candidate)
        key = os.path.normcase(value)
        if key not in seen:
            seen.add(key)
            unique.append(value)
    return unique


def _resolve_windows_command(command: str, env: dict[str, str]) -> str | None:
    resolved = shutil.which(command, path=env.get("PATH"))
    if resolved:
        return resolved
    for candidate in _windows_command_fallbacks(command, env):
        if Path(candidate).is_file():
            return candidate
    return None


def _subprocess_command(args: list[str], env: dict[str, str]) -> list[str]:
    if os.name != "nt" or not args:
        return args
    resolved = _resolve_windows_command(args[0], env)
    if not resolved:
        return args
    resolved_command = str(resolved)
    if Path(resolved_command).suffix.lower() in {".bat", ".cmd"}:
        shell = env.get("COMSPEC") or "cmd.exe"
        return [shell, "/c", resolved_command, *args[1:]]
    return [resolved_command, *args[1:]]


def _git(
    vault_dir: Path,
    args: list[str],
    *,
    check: bool = True,
    timeout: int = GIT_PROBE_TIMEOUT_SECONDS,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    if extra_env:
        env.update(extra_env)
    command = _subprocess_command(["git", "-C", str(vault_dir), *args], env)
    try:
        result = subprocess.run(
            command,
            text=True,
            **SUBPROCESS_TEXT_KWARGS,
            capture_output=True,
            env=env,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise VaultGitError(
            "Git não encontrado. Instale Git e rode /mednotes:setup novamente.",
            status="blocked_missing_git",
            blocked_reason="missing_git",
            next_action="instalar Git e rodar /mednotes:setup novamente",
        ) from exc
    except subprocess.TimeoutExpired:
        result = _timeout_result(command, timeout)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise VaultGitError(f"git {' '.join(args)} falhou: {detail}")
    return result


def _git_without_repo(args: list[str], *, timeout: int = GIT_PROBE_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env["GIT_DIR"] = ""
    env.pop("GIT_WORK_TREE", None)
    command = _subprocess_command(["git", *args], env)
    try:
        return subprocess.run(
            command,
            cwd=Path.home(),
            text=True,
            **SUBPROCESS_TEXT_KWARGS,
            capture_output=True,
            env=env,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise VaultGitError(
            "Git não encontrado. Instale Git e rode /mednotes:setup novamente.",
            status="blocked_missing_git",
            blocked_reason="missing_git",
            next_action="instalar Git e rodar /mednotes:setup novamente",
        ) from exc
    except subprocess.TimeoutExpired:
        return _timeout_result(command, timeout)


def _norm_path(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _path_is_same_or_inside(path: Path, root: Path) -> bool:
    resolved_path = path.expanduser().resolve(strict=False)
    resolved_root = root.expanduser().resolve(strict=False)
    if os.path.normcase(str(resolved_path)) == os.path.normcase(str(resolved_root)):
        return True
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError:
        return False
    return True


def _read_app_config() -> dict[str, object]:
    config_path = _app_config_path()
    if not config_path.is_file() or tomllib is None:
        return {}
    try:
        return tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _app_config_path() -> Path:
    for env_name in CONFIG_ENV_VARS:
        value = os.environ.get(env_name)
        if value:
            return Path(os.path.expandvars(value)).expanduser()
    return _state_dir() / "config.toml"


def _configured_wiki_dir() -> Path | None:
    cfg = _read_app_config()
    paths = cfg.get("paths", {}) if isinstance(cfg.get("paths"), dict) else {}
    value = paths.get("wiki_dir") if isinstance(paths, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value).expanduser().resolve(strict=False)


def _coerce_to_configured_git_root(candidate: Path) -> Path:
    root = _repo_root(candidate)
    if root is None or _norm_path(root) == _norm_path(candidate):
        return candidate
    configured_wiki = _configured_wiki_dir()
    if configured_wiki is None:
        return candidate
    if not _path_is_same_or_inside(configured_wiki, root):
        return candidate
    if _path_is_same_or_inside(candidate, configured_wiki) or _path_is_same_or_inside(configured_wiki, candidate):
        return root
    return candidate


def _validate_configured_wiki_inside_vault(vault_dir: Path) -> None:
    configured_wiki = _configured_wiki_dir()
    if configured_wiki is None:
        return
    if not _path_is_same_or_inside(configured_wiki, vault_dir):
        raise VaultGitError(
            f"vault_validate: [paths].wiki_dir aponta para {configured_wiki}, fora da raiz Git {vault_dir}.",
            status="blocked_setup_required",
            blocked_reason="wiki_dir_outside_vault",
            next_action=(
                "rodar set-paths com a Wiki correta ou rodar /mednotes:setup apontando para "
                "a raiz Git que contém essa Wiki"
            ),
        )


def validate_vault(vault_dir: Path, *, require_remote: bool = False) -> VaultContext:
    inside = _git(vault_dir, ["rev-parse", "--is-inside-work-tree"], check=False)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise VaultGitError(
            f"vault_validate: {vault_dir} ainda nao tem protecao local configurada. "
            "Rode /mednotes:setup para preparar pontos de restauração.",
            status="blocked_setup_required",
            blocked_reason="setup_required",
            next_action="rodar /mednotes:setup antes de alterar o vault",
        )

    root = _git(vault_dir, ["rev-parse", "--show-toplevel"]).stdout.strip()
    if _norm_path(Path(root)) != _norm_path(vault_dir):
        raise VaultGitError(
            f"vault_validate: {vault_dir} nao e a raiz do repo git ({root})",
            status="blocked_wrong_repo_root",
            blocked_reason="wrong_repo_root",
            next_action="rodar set-paths com a Wiki correta ou /mednotes:setup com a raiz Git do vault",
        )

    _validate_configured_wiki_inside_vault(vault_dir)

    origin_url = _git(vault_dir, ["remote", "get-url", "origin"], check=False).stdout.strip()
    if not origin_url and require_remote:
        raise VaultGitError(
            f"vault_validate: backup online ainda nao configurado em {vault_dir}. "
            "Rode /mednotes:setup para conduzir o login GitHub ou criar repositório privado.",
            status="blocked_online_backup_required",
            blocked_reason="online_backup_required",
            next_action="rodar /mednotes:setup para ativar backup online antes do fluxo paralelo",
        )
    if not origin_url:
        return VaultContext(vault_dir=vault_dir)

    allowlist = _state_dir() / "vault.remote-allowlist"
    if allowlist.is_file():
        try:
            allowed = [
                line.strip()
                for line in allowlist.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
        except OSError as exc:
            raise VaultGitError(f"vault_validate: nao consegui ler {allowlist}: {exc}") from exc
        if origin_url not in allowed:
            raise VaultGitError(
                f'vault_validate: origin url "{origin_url}" nao consta em {allowlist}\n'
                "Isto evita push para repo errado por acidente. Se a URL e correta, adicione-a:\n"
                f'  echo "{origin_url}" >> "{allowlist}"',
                status="blocked_remote_untrusted",
                blocked_reason="remote_not_allowlisted",
                next_action="rodar /mednotes:setup para validar o backup online do vault",
            )
    else:
        print(
            f"vault_validate: aviso - vault.remote-allowlist nao existe, usando {origin_url} sem allowlist",
            file=sys.stderr,
        )

    if require_remote and not _remote_access_ok(vault_dir):
        raise VaultGitError(
            "Backup online configurado, mas inacessível agora. A proteção local continua válida; "
            "corrija login/rede/permissão do GitHub e tente novamente.",
            status="blocked_remote_unreachable",
            blocked_reason="remote_unreachable",
            next_action="rodar /mednotes:setup para revalidar o backup online",
        )

    return VaultContext(vault_dir=vault_dir, origin_url=origin_url)


def _ensure_main(vault_dir: Path, label: str) -> None:
    current = _git(vault_dir, ["symbolic-ref", "--short", "HEAD"], check=False).stdout.strip()
    if current != "main":
        shown = current or "detached"
        raise VaultGitError(f"{label}: HEAD={shown}; politica exige main direto")


def _ensure_branch(vault_dir: Path, expected_branch: str, label: str) -> None:
    current = _git(vault_dir, ["symbolic-ref", "--short", "HEAD"], check=False).stdout.strip()
    if current != expected_branch:
        shown = current or "detached"
        raise VaultGitError(f"{label}: HEAD={shown}; esperado {expected_branch}")


def _has_worktree_changes(vault_dir: Path) -> bool:
    return bool(_git(vault_dir, ["status", "--porcelain=v1"]).stdout.strip())


def _has_staged_changes(vault_dir: Path) -> bool:
    return _git(vault_dir, ["diff", "--cached", "--quiet"], check=False).returncode != 0


def _run_id() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _slug(value: str, *, lower: bool = False) -> str:
    normalized = value.strip()
    if lower:
        normalized = normalized.lower()
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized)
    normalized = re.sub(r"-+", "-", normalized).strip("-._")
    return normalized or "run"


def _parallel_run_id(raw_run_id: str | None) -> str:
    return _slug(raw_run_id or _run_id())


def _agent_slug(agent: str) -> str:
    return _slug(agent, lower=True)


def _parallel_branch(agent: str, run_id: str) -> str:
    return f"vault/{_agent_slug(agent)}/{_parallel_run_id(run_id)}"


def _validate_branch_ref(branch: str) -> None:
    if branch.strip() != branch or " " in branch:
        raise VaultGitError(f"vault_integrate: branch invalida, sem espacos: {branch!r}")
    if not branch.startswith("vault/"):
        raise VaultGitError(f"vault_integrate: branch {branch!r} deve comecar com vault/")
    checked = _git_without_repo(["check-ref-format", "--branch", branch])
    if checked.returncode != 0:
        detail = (checked.stderr or checked.stdout).strip()
        raise VaultGitError(f"vault_integrate: branch invalida {branch!r}: {detail}")


def _run_id_from_branch(branch: str) -> str:
    return branch.rsplit("/", 1)[-1]


def _worktree_dir(agent: str, run_id: str) -> Path:
    return _state_dir() / WORKTREE_SUBDIR / f"{_parallel_run_id(run_id)}-{_agent_slug(agent)}"


def _restore_plan_dir() -> Path:
    return _state_dir() / RESTORE_PLAN_SUBDIR


def _guard_lease_dir() -> Path:
    path = _state_dir() / GUARD_LEASE_SUBDIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _status_hash(vault_dir: Path) -> str:
    status = _git(vault_dir, ["status", "--porcelain=v1"]).stdout
    return "sha256:" + hashlib.sha256(status.encode("utf-8")).hexdigest()


def _guard_lease_id(agent: str, run_id: str) -> str:
    return f"{_parallel_run_id(run_id)}-{_agent_slug(agent)}"


def _dt_iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_guard_lease(vault_dir: Path, *, agent: str, workflow: str, run_id: str) -> dict[str, object]:
    created = datetime.now(UTC)
    lease_id = _guard_lease_id(agent, run_id)
    path = _guard_lease_dir() / f"{lease_id}.json"
    payload: dict[str, object] = {
        "schema": "medical-notes-workbench.vault-guard-lease.v1",
        "lease_id": lease_id,
        "vault_dir": str(vault_dir),
        "agent": _agent_slug(agent),
        "workflow": workflow,
        "run_id": _parallel_run_id(run_id),
        "status": "active",
        "created_at": _dt_iso(created),
        "expires_at": _dt_iso(created + timedelta(minutes=GUARD_LEASE_TTL_MINUTES)),
        "initial_head": _head(vault_dir),
        "initial_status_hash": _status_hash(vault_dir),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "status": "active",
        "lease_id": lease_id,
        "path": str(path),
        "expires_at": payload["expires_at"],
    }


def _close_guard_lease(vault_dir: Path, *, agent: str, run_id: str) -> dict[str, object]:
    lease_id = _guard_lease_id(agent, run_id)
    path = _guard_lease_dir() / f"{lease_id}.json"
    if not path.is_file():
        return {"status": "missing", "lease_id": lease_id, "path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload.update(
        {
            "schema": "medical-notes-workbench.vault-guard-lease.v1",
            "lease_id": lease_id,
            "vault_dir": str(vault_dir),
            "agent": _agent_slug(agent),
            "run_id": _parallel_run_id(run_id),
            "status": "closed",
            "closed_at": _now_iso(),
            "final_head": _head(vault_dir),
            "final_status_hash": _status_hash(vault_dir),
        }
    )
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"status": "closed", "lease_id": lease_id, "path": str(path)}


def _active_guard_leases(vault_dir: Path) -> list[dict[str, object]]:
    lease_dir = _guard_lease_dir()
    now = datetime.now(UTC)
    leases: list[dict[str, object]] = []
    for path in sorted(lease_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("status") != "active":
            continue
        if _norm_path(Path(str(payload.get("vault_dir") or ""))) != _norm_path(vault_dir):
            continue
        expires_raw = str(payload.get("expires_at") or "")
        try:
            expires_at = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if expires_at <= now:
            continue
        leases.append(
            {
                "lease_id": str(payload.get("lease_id") or path.stem),
                "vault_dir": str(payload.get("vault_dir") or ""),
                "agent": str(payload.get("agent") or ""),
                "workflow": str(payload.get("workflow") or ""),
                "run_id": str(payload.get("run_id") or ""),
                "status": "active",
                "created_at": str(payload.get("created_at") or ""),
                "expires_at": expires_raw,
                "path": str(path),
            }
        )
    return leases


def _run_finish_run_id(vault_dir: Path, *, agent: str, workflow: str, run_id: str | None) -> RunFinishRunIdResolution:
    if run_id:
        requested = _parallel_run_id(run_id)
        agent_slug = _agent_slug(agent)
        matches = [
            lease
            for lease in _active_guard_leases(vault_dir)
            if str(lease.get("agent") or "") == agent_slug and str(lease.get("workflow") or "") == workflow
        ]
        if not matches:
            return RunFinishRunIdResolution(run_id=requested, requested_run_id=requested)
        if any(str(lease.get("run_id") or "") == requested for lease in matches):
            return RunFinishRunIdResolution(run_id=requested, requested_run_id=requested)
        if len(matches) == 1:
            recovered = str(matches[0].get("run_id") or matches[0].get("lease_id") or _run_id())
            return RunFinishRunIdResolution(
                run_id=recovered,
                requested_run_id=requested,
                auto_recovered=True,
                recovery_reason="single_active_guard_lease",
            )
        run_ids = ", ".join(str(lease.get("run_id") or lease.get("lease_id") or "") for lease in matches)
        raise VaultGitError(
            "vault_run_finish: --run-id nao corresponde a nenhuma lease ativa deste agente/workflow.",
            status="blocked_guard_lease_mismatch",
            blocked_reason="guard_lease_mismatch",
            next_action=(
                "repetir run-finish com o --run-id literal retornado por run-start; "
                f"lease ativa encontrada: {run_ids}"
            ),
        )
    agent_slug = _agent_slug(agent)
    matches = [
        lease
        for lease in _active_guard_leases(vault_dir)
        if str(lease.get("agent") or "") == agent_slug and str(lease.get("workflow") or "") == workflow
    ]
    if len(matches) == 1:
        return RunFinishRunIdResolution(run_id=str(matches[0].get("run_id") or matches[0].get("lease_id") or _run_id()))
    if len(matches) > 1:
        run_ids = ", ".join(str(lease.get("run_id") or lease.get("lease_id") or "") for lease in matches)
        raise VaultGitError(
            "vault_run_finish: mais de uma lease ativa corresponde a este agente/workflow.",
            status="blocked_ambiguous_guard_lease",
            blocked_reason="ambiguous_guard_lease",
            next_action=f"repetir run-finish com --run-id de uma destas leases: {run_ids}",
        )
    raise VaultGitError(
        "vault_run_finish: nenhuma lease ativa encontrada para este agente/workflow.",
        status="blocked_guard_lease_missing",
        blocked_reason="guard_lease_missing",
        next_action="abrir run-start antes da mutação ou repetir run-finish com o --run-id correto",
    )


def _empty_run_id_next_action(vault_dir: Path, *, agent: str, workflow: str) -> str:
    agent_slug = _agent_slug(agent)
    run_ids = [
        str(lease.get("run_id") or lease.get("lease_id") or "")
        for lease in _active_guard_leases(vault_dir)
        if str(lease.get("agent") or "") == agent_slug and str(lease.get("workflow") or "") == workflow
    ]
    visible_ids = [run_id for run_id in run_ids if run_id]
    if visible_ids:
        return (
            "repetir run-finish com o --run-id retornado por run-start; "
            f"lease ativa encontrada: {', '.join(visible_ids)}"
        )
    return (
        "repetir run-finish depois de ler o run_id retornado por run-start; "
        "omita --run-id somente quando houver uma única lease ativa para este agente/workflow"
    )


def _run_finish_next_step(*, agent: str, workflow: str, run_id: str, title: str | None = None) -> dict[str, object]:
    return {
        "schema": "medical-notes-workbench.vault-run-finish-next-step.v1",
        "command_family": "run-finish",
        "agent": _agent_slug(agent),
        "workflow": workflow,
        "run_id": run_id,
        "title": title or _default_run_finish_title(workflow),
        "arguments": [
            "--agent",
            _agent_slug(agent),
            "--workflow",
            workflow,
            "--run-id",
            run_id,
            "--title",
            title or _default_run_finish_title(workflow),
            "--public-json",
            "--json",
        ],
        "agent_instruction": (
            "Use este run_id exatamente como esta; nao remova hifens, nao converta para o run_id do workflow "
            "e nao derive outro identificador."
        ),
    }


def run_guard_status(args: argparse.Namespace) -> int:
    vault_dir = resolve_vault_dir(args.vault_dir)
    validate_vault(vault_dir)
    leases = _active_guard_leases(vault_dir)
    payload: dict[str, object] = {
        "schema": "medical-notes-workbench.vault-guard-status.v1",
        "status": "completed",
        "vault_dir": str(vault_dir),
        "active_count": len(leases),
        "leases": leases,
    }
    _emit(args, payload, "")
    return 0


def _restore_point_label(workflow: str, *, when: str) -> str:
    if when == "before":
        return f"Ponto de restauração antes de {workflow}"
    if when == "after":
        return f"Ponto de restauração depois de {workflow}"
    return f"Ponto de restauração de {workflow}"


def _normalize_workflow_name(workflow: str) -> str:
    normalized = workflow.strip()
    if normalized.startswith("/"):
        return normalized
    if normalized.startswith("mednotes:"):
        return f"/{normalized}"
    if normalized.startswith("mednotes-"):
        return f"/mednotes:{normalized.removeprefix('mednotes-')}"
    if normalized in {"flashcards", "report"}:
        return f"/{normalized}"
    return normalized


def _default_run_finish_title(workflow: str) -> str:
    return f"Resultado de {workflow}"


def _head(vault_dir: Path) -> str:
    return _git(vault_dir, ["rev-parse", "HEAD"]).stdout.strip()


def _short_sha(vault_dir: Path, ref: str = "HEAD") -> str:
    return _git(vault_dir, ["rev-parse", "--short", ref]).stdout.strip()


def _format_block(title: str, lines: list[str]) -> str:
    if not lines:
        return f"{title}\n- nenhum"
    return title + "\n" + "\n".join(f"- {line}" for line in lines)


def _sentence(text: str, fallback: str) -> str:
    clean = text.strip()
    if not clean:
        clean = fallback
    return clean if clean.endswith((".", "!", "?")) else f"{clean}."


def _is_obsidian_operational_path(path: str) -> bool:
    clean = path.strip().strip('"')
    return clean == ".obsidian" or clean.startswith(".obsidian/")


def _status_paths(line: str) -> list[str]:
    raw = line[3:].strip() if len(line) > 3 else line.strip()
    if not raw:
        return []
    return [part.strip().strip('"') for part in raw.split(" -> ") if part.strip()]


def _split_status_for_commit_doc(lines: list[str]) -> tuple[list[str], list[str]]:
    wiki: list[str] = []
    obsidian: list[str] = []
    for line in lines:
        paths = _status_paths(line)
        target = obsidian if any(_is_obsidian_operational_path(path) for path in paths) else wiki
        target.append(line)
    return wiki, obsidian


def _split_diffstat_for_commit_doc(lines: list[str]) -> tuple[list[str], list[str]]:
    wiki: list[str] = []
    obsidian: list[str] = []
    for line in lines:
        if "|" not in line:
            continue
        path = line.split("|", 1)[0].strip()
        target = obsidian if _is_obsidian_operational_path(path) else wiki
        target.append(line)
    return wiki, obsidian


def _human_status_line(line: str) -> str:
    code = line[:2]
    paths = _status_paths(line)
    if not paths:
        return line.strip()
    path_text = " -> ".join(paths)
    if "R" in code:
        action = "renomeada/movida"
    elif "A" in code or "?" in code:
        action = "criada"
    elif "D" in code:
        action = "removida"
    elif "M" in code:
        action = "alterada"
    else:
        action = "atualizada"
    return f"{action}: {path_text}"


def _wiki_change_lines_for_delivery_record(vault_dir: Path) -> list[str]:
    status = _git(vault_dir, ["status", "--short", "--untracked-files=all"]).stdout.splitlines()
    staged_stat = _git(vault_dir, ["diff", "--cached", "--stat"]).stdout.splitlines()
    wiki_status, _obsidian_status = _split_status_for_commit_doc(status)
    wiki_staged_stat, _obsidian_staged_stat = _split_diffstat_for_commit_doc(staged_stat)

    lines = [_human_status_line(line) for line in wiki_status]
    if not lines:
        lines = [line.strip() for line in wiki_staged_stat]
    if not lines:
        return ["Mudanças da Wiki salvas neste ponto de restauração."]

    limit = 12
    if len(lines) <= limit:
        return lines
    remaining = len(lines) - limit
    suffix = "item da Wiki" if remaining == 1 else "itens da Wiki"
    return lines[:limit] + [f"mais {remaining} {suffix} neste ponto de restauração"]


def _default_delivery_record_for_commit(
    vault_dir: Path,
    *,
    title: str,
    workflow: str,
) -> str:
    summary = _sentence(title, "Mudanças da Wiki foram salvas em um ponto de restauração")
    wiki_lines = _wiki_change_lines_for_delivery_record(vault_dir)
    workflow_text = workflow.strip() or "workflow atual"
    sections = [
        "Registro de entrega",
        "",
        "Em uma frase:",
        f"- {summary}",
        "",
        "O que mudou para você:",
        *[f"- {line}" for line in wiki_lines],
        "",
        "Como conferir:",
        f"- Abra as notas listadas no Obsidian e confira o resultado de {workflow_text}.",
        "- Use /mednotes:history se precisar revisar ou restaurar este ponto.",
        "",
        "Pontos de atenção:",
        "- Este resumo cobre a Wiki; arquivos operacionais do Obsidian ficam nos detalhes abaixo quando existirem.",
        "",
        "Próxima ação:",
        "- Continuar a partir do estado salvo; se algo estiver estranho, revise o ponto em /mednotes:history.",
    ]
    return "\n".join(sections)


def _precommit_observation(vault_dir: Path) -> str:
    status = _git(vault_dir, ["status", "--short", "--untracked-files=all"]).stdout.splitlines()
    unstaged_stat = _git(vault_dir, ["diff", "--stat"]).stdout.splitlines()
    staged_stat = _git(vault_dir, ["diff", "--cached", "--stat"]).stdout.splitlines()
    wiki_status, obsidian_status = _split_status_for_commit_doc(status)
    wiki_unstaged_stat, obsidian_unstaged_stat = _split_diffstat_for_commit_doc(unstaged_stat)
    wiki_staged_stat, obsidian_staged_stat = _split_diffstat_for_commit_doc(staged_stat)

    sections = [
        _format_block("Mudancas na Wiki observadas antes do snapshot:", wiki_status),
        _format_block("Arquivos operacionais do Obsidian observados:", obsidian_status),
        _format_block("Diffstat da Wiki rastreada:", wiki_unstaged_stat),
        _format_block("Diffstat operacional do Obsidian:", obsidian_unstaged_stat),
    ]
    if staged_stat:
        sections.append(_format_block("Diffstat da Wiki ja staged antes do snapshot:", wiki_staged_stat))
        sections.append(_format_block("Diffstat operacional do Obsidian ja staged:", obsidian_staged_stat))
    return "Alteracoes observadas antes do snapshot:\n\n" + "\n\n".join(sections)


def _operational_details_for_commit(vault_dir: Path) -> str:
    status = _git(vault_dir, ["status", "--short", "--untracked-files=all"]).stdout.splitlines()
    staged_stat = _git(vault_dir, ["diff", "--cached", "--stat"]).stdout.splitlines()
    _wiki_status, obsidian_status = _split_status_for_commit_doc(status)
    _wiki_staged_stat, obsidian_staged_stat = _split_diffstat_for_commit_doc(staged_stat)
    if not obsidian_status and not obsidian_staged_stat:
        return ""
    sections = [
        _format_block("Arquivos operacionais do Obsidian:", obsidian_status),
        _format_block("Diffstat operacional do Obsidian:", obsidian_staged_stat),
    ]
    return "Detalhes operacionais fora da Wiki (gerado pelo script):\n\n" + "\n\n".join(sections)


def _sync_main(vault_dir: Path, label: str) -> str:
    if not _origin_url(vault_dir):
        return "skipped_no_remote"
    fetch = _git(vault_dir, ["fetch", "origin", "main"], check=False, timeout=GIT_NETWORK_TIMEOUT_SECONDS)
    if fetch.returncode == 0:
        rebase = _git(vault_dir, ["rebase", "origin/main"], check=False)
        if rebase.returncode != 0:
            _git(vault_dir, ["rebase", "--abort"], check=False)
            detail = (rebase.stderr or rebase.stdout).strip()
            raise VaultGitError(
                f"{label}: rebase em origin/main falhou (conflito). "
                f"Resolve manualmente e re-roda.\n{detail}"
            )
        return "synced"
    print(
        f"{label}: fetch origin/main falhou (rede/auth?); seguindo com base local",
        file=sys.stderr,
    )
    return "pending_fetch_failed"


def _push_branch(vault_dir: Path, branch: str, label: str, *, required: bool) -> bool:
    push = _git(vault_dir, ["push", "-u", "origin", branch], check=False, timeout=GIT_NETWORK_TIMEOUT_SECONDS)
    if push.returncode == 0:
        return True
    detail = (push.stderr or push.stdout).strip()
    if required:
        raise VaultGitError(f"{label}: push de {branch} falhou: {detail}")
    print(
        f"{label}: push falhou; commit local mantido, proximo run empurra o backlog",
        file=sys.stderr,
    )
    return False


def _sync_and_push(vault_dir: Path, label: str) -> str:
    sync_status = _sync_main(vault_dir, label)
    if sync_status == "skipped_no_remote":
        return sync_status
    push = _git(vault_dir, ["push", "origin", "main"], check=False, timeout=GIT_NETWORK_TIMEOUT_SECONDS)
    if push.returncode != 0:
        print(
            f"{label}: push falhou; commit local mantido, proximo run empurra o backlog",
            file=sys.stderr,
        )
        return "pending_push_failed"
    return "synced"


def _backup_status_payload(vault_dir: Path, context: VaultContext) -> dict[str, object]:
    if not context.origin_url:
        return {
            "backup_status": "skipped_no_remote",
            "sync_status": "skipped_no_remote",
            "local_checkpoints_pending_count": 0,
            "remote_changes_pending_count": 0,
        }

    fetch = _git(vault_dir, ["fetch", "origin", "main"], check=False, timeout=GIT_NETWORK_TIMEOUT_SECONDS)
    if fetch.returncode != 0:
        return {
            "backup_status": "unavailable",
            "sync_status": "pending_fetch_failed",
            "local_checkpoints_pending_count": None,
            "remote_changes_pending_count": None,
        }

    counts = _git(
        vault_dir,
        ["rev-list", "--left-right", "--count", "origin/main...HEAD"],
        check=False,
    )
    if counts.returncode != 0:
        return {
            "backup_status": "unknown",
            "sync_status": "pending_remote_state_unknown",
            "local_checkpoints_pending_count": None,
            "remote_changes_pending_count": None,
        }

    raw_counts = counts.stdout.strip().split()
    remote_pending = int(raw_counts[0]) if len(raw_counts) >= 1 else 0
    local_pending = int(raw_counts[1]) if len(raw_counts) >= 2 else 0
    if local_pending and remote_pending:
        backup_status = "diverged"
    elif local_pending:
        backup_status = "local_checkpoints_pending"
    elif remote_pending:
        backup_status = "remote_changes_pending"
    else:
        backup_status = "synced"
    return {
        "backup_status": backup_status,
        "sync_status": "synced" if backup_status == "synced" else "pending",
        "local_checkpoints_pending_count": local_pending,
        "remote_changes_pending_count": remote_pending,
    }


def _valid_git_identity(name: str, email: str) -> bool:
    return bool(name and email and "\n" not in name and "\n" not in email and "@" in email)


def _explicit_git_identity(name: str, email: str, *, source: str) -> GitIdentity:
    if not _valid_git_identity(name, email):
        raise VaultGitError(f"identidade Git invalida: {name!r} <{email!r}>")
    return GitIdentity(name=name, email=email, source=source)


def _native_git_identity_from_env() -> GitIdentity | None:
    author_name = os.environ.get("GIT_AUTHOR_NAME", "").strip()
    author_email = os.environ.get("GIT_AUTHOR_EMAIL", "").strip()
    if _valid_git_identity(author_name, author_email):
        return GitIdentity(name=author_name, email=author_email, source="native")

    committer_name = os.environ.get("GIT_COMMITTER_NAME", "").strip()
    committer_email = os.environ.get("GIT_COMMITTER_EMAIL", "").strip()
    if _valid_git_identity(committer_name, committer_email):
        return GitIdentity(name=committer_name, email=committer_email, source="native")
    return None


def _git_identities_path() -> Path:
    return _state_dir() / GIT_IDENTITIES_FILE


def _read_git_identities() -> dict[str, object]:
    path = _git_identities_path()
    if not path.is_file():
        return {"schema": "medical-notes-workbench.vault-git-identities.v1", "identities": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema": "medical-notes-workbench.vault-git-identities.v1", "identities": {}}
    if not isinstance(data, dict):
        return {"schema": "medical-notes-workbench.vault-git-identities.v1", "identities": {}}
    identities = data.get("identities")
    if not isinstance(identities, dict):
        data["identities"] = {}
    data["schema"] = "medical-notes-workbench.vault-git-identities.v1"
    return data


def _configured_git_identity(agent: str) -> GitIdentity | None:
    data = _read_git_identities()
    identities = data.get("identities")
    if not isinstance(identities, dict):
        return None
    entry = identities.get(_agent_slug(agent))
    if not isinstance(entry, dict):
        return None
    name = str(entry.get("name") or "").strip()
    email = str(entry.get("email") or "").strip()
    if not _valid_git_identity(name, email):
        return None
    return GitIdentity(name=name, email=email, source="configured")


def _persist_git_identity(agent: str, identity: GitIdentity) -> None:
    if identity.source != "native":
        return
    data = _read_git_identities()
    identities = data.setdefault("identities", {})
    if not isinstance(identities, dict):
        identities = {}
        data["identities"] = identities
    identities[_agent_slug(agent)] = {
        "name": identity.name,
        "email": identity.email,
        "captured_from": "native",
        "updated_at": _now_iso(),
    }
    path = _git_identities_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _fallback_git_identity(agent: str) -> GitIdentity:
    slug = _agent_slug(agent)
    return GitIdentity(name=slug, email=f"{slug}@medical-notes", source="fallback")


def _resolve_git_identity(agent: str) -> GitIdentity:
    native = _native_git_identity_from_env()
    if native:
        _persist_git_identity(agent, native)
        return native
    configured = _configured_git_identity(agent)
    if configured:
        return configured
    return _fallback_git_identity(agent)


def _git_identity_env(identity: GitIdentity) -> dict[str, str]:
    return {
        "GIT_AUTHOR_NAME": identity.name,
        "GIT_AUTHOR_EMAIL": identity.email,
        "GIT_COMMITTER_NAME": identity.name,
        "GIT_COMMITTER_EMAIL": identity.email,
    }


def _add_git_identity_payload(payload: dict[str, object], identity: GitIdentity) -> None:
    payload["git_identity_source"] = identity.source
    payload["git_author"] = f"{identity.name} <{identity.email}>"
    payload["git_identity_github_attribution"] = _git_identity_github_attribution(identity)


def _git_identity_github_attribution(identity: GitIdentity) -> dict[str, object]:
    email = identity.email.strip()
    lower = email.lower()
    if lower.endswith("@medical-notes"):
        return {
            "status": "local_fallback_not_github",
            "github_profile_link_expected": False,
            "human_message": (
                "Autoria operacional salva. No GitHub, este fallback local nao vira "
                "autor clicavel com avatar."
            ),
            "next_action": (
                "configure a identidade Git nativa do agente/TUI com um email associado "
                "a uma conta GitHub real ou bot antes do proximo commit"
            ),
        }

    noreply = re.fullmatch(r"(?:(\d+)\+)?([^@]+)@users\.noreply\.github\.com", lower)
    if noreply:
        numeric_id = noreply.group(1)
        if numeric_id:
            return {
                "status": "github_noreply_with_numeric_user_id",
                "github_profile_link_expected": True,
                "human_message": (
                    "Autoria GitHub reconhecivel: o email no-reply tem ID numerico "
                    "de usuario, entao o GitHub deve conseguir associar avatar e link."
                ),
                "next_action": "nenhuma acao necessaria para atribuir visualmente no GitHub",
            }
        return {
            "status": "github_noreply_without_numeric_user_id",
            "github_profile_link_expected": False,
            "human_message": (
                "Autoria Git salva, mas este no-reply generico pode aparecer no GitHub "
                "sem avatar e sem autor clicavel."
            ),
            "next_action": (
                "configure o setup GitHub nativo do agente/TUI com o no-reply exato "
                "da conta GitHub do agente/bot, no formato ID+login@users.noreply.github.com"
            ),
        }

    return {
        "status": "custom_email_must_be_verified_on_github",
        "github_profile_link_expected": False,
        "human_message": (
            "Autoria Git salva. Para o GitHub mostrar avatar, link e filtro por autor, "
            "este email precisa estar verificado em uma conta GitHub."
        ),
        "next_action": (
            "confirme que o email configurado para o agente/TUI pertence a uma conta "
            "GitHub real ou bot; se quiser privacidade, use o no-reply oficial dessa conta"
        ),
    }


def _commit(
    vault_dir: Path,
    *,
    title: str,
    messages: list[str],
    identity: GitIdentity,
) -> GitIdentity:
    message_parts = [title.rstrip()]
    message_parts.extend(message.rstrip() for message in messages if message.strip())
    commit_message = "\n\n".join(message_parts).rstrip() + "\n"
    message_file_path: str | None = None
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False) as message_file:
        message_file.write(commit_message)
        message_file_path = message_file.name
    args = [
        "-c",
        f"user.name={identity.name}",
        "-c",
        f"user.email={identity.email}",
        "commit",
        "--cleanup=verbatim",
        "-F",
        message_file_path,
    ]
    try:
        _git(vault_dir, args, extra_env=_git_identity_env(identity))
    finally:
        if message_file_path:
            Path(message_file_path).unlink(missing_ok=True)
    return identity


def _snapshot_dirty_main(
    vault_dir: Path,
    *,
    agent: str,
    workflow: str,
    run_id: str | None = None,
    restore_point_label: str | None = None,
) -> str | None:
    if not _has_worktree_changes(vault_dir):
        return None

    actual_run_id = run_id or _run_id()
    observation = _precommit_observation(vault_dir)
    _git(vault_dir, ["add", "-A"])
    restore_trailers = ""
    if restore_point_label:
        restore_trailers = (
            "\n"
            "Restore-Point: before-run\n"
            f"Restore-Point-Label: {restore_point_label}"
        )
    body = (
        "Capturado automaticamente para isolar mutacoes do humano das que o agente\n"
        "fara a seguir. Conteudo pode ser edicao manual no Obsidian, sincronizacao de\n"
        "plugin, ou trabalho em andamento.\n\n"
        f"{observation}\n\n"
        "Agent: snapshot\n"
        "Workflow: pre-agent-snapshot\n"
        f"Run-Id: {actual_run_id}\n"
        f"Triggered-By-Agent: {agent}\n"
        f"Triggered-By-Workflow: {workflow}"
        f"{restore_trailers}"
    )
    title = f"snapshot: estado antes de {agent} rodar {workflow}"
    _commit(
        vault_dir,
        title=title,
        messages=[body],
        identity=_explicit_git_identity("snapshot", "snapshot@medical-notes", source="fallback"),
    )
    return _git(vault_dir, ["rev-parse", "--short", "HEAD"]).stdout.strip()


def _body_file_text(path_value: str | None, label: str) -> str:
    if not path_value:
        return ""
    body_path = Path(path_value).expanduser()
    if not body_path.is_file():
        raise VaultGitError(f"{label}: --body-file {body_path} nao existe")
    return body_path.read_text(encoding="utf-8").rstrip()


def _emit(args: argparse.Namespace, payload: dict[str, object], text: str) -> None:
    if getattr(args, "json", False) or getattr(args, "public_json", False):
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    print(text)


def _public_run_finish_payload(payload: dict[str, object]) -> dict[str, object]:
    guard_lease = payload.get("guard_lease")
    guard_status = guard_lease.get("status") if isinstance(guard_lease, dict) else ""
    sync_status = str(payload.get("sync_status") or "")
    backup_online = bool(payload.get("backup_online"))
    message = "Proteção do vault encerrada; ponto de restauração disponível."
    if sync_status == "synced":
        message += " Backup online conferido."
    elif sync_status == "skipped_no_remote":
        message += " Backup online pendente."
    elif sync_status.startswith("pending_"):
        message += " Backup online pendente; proteção local válida."
    return {
        "schema": "medical-notes-workbench.vault-run-finish-public.v1",
        "status": payload.get("status") or "",
        "agent": payload.get("agent") or "",
        "workflow": payload.get("workflow") or "",
        "backup_online": backup_online,
        "sync_status": sync_status,
        "human_message": message,
        "version_control_safety": {
            "resource_guard_active": guard_status != "closed",
            "run_finish_seen": True,
            "restore_point_after": bool(payload.get("restore_point_id")),
            "backup_online": backup_online,
            "sync_status": sync_status,
        },
    }


def _print_context(label: str, context: VaultContext) -> None:
    origin = context.origin_url or "backup-online-pendente"
    print(f"{label}: vault={context.vault_dir} origin={origin}")


def _run_cmd(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = GIT_PROBE_TIMEOUT_SECONDS,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    command = _subprocess_command(args, env)
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            text=True,
            **SUBPROCESS_TEXT_KWARGS,
            capture_output=capture,
            env=env,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise VaultGitError(f"{args[0]} nao encontrado") from exc
    except subprocess.TimeoutExpired:
        return _timeout_result(command, timeout)


def _write_state_file(name: str, value: str) -> Path:
    state = _state_dir()
    state.mkdir(parents=True, exist_ok=True)
    path = state / name
    path.write_text(value.rstrip() + "\n", encoding="utf-8")
    return path


def _local_setup_message(
    status: str,
    blocked_reason: str | None = None,
    *,
    local_changes_present: bool = False,
) -> str:
    if status == "ready":
        message = "Proteção local pronta e backup online conectado."
    elif status == "awaiting_remote_confirmation":
        message = (
            "Proteção local pronta. Posso criar um repositório privado para ativar "
            "o backup online, mas preciso da sua confirmação."
        )
    elif status == "blocked_missing_git":
        message = "Não consegui ativar a proteção local: é preciso instalar Git primeiro."
    elif status == "blocked_wrong_repo_root":
        message = "Não alterei nada: a pasta escolhida está dentro de outro repositório."
    elif status == "blocked_branch_confirmation_required":
        message = (
            "Não alterei nada: o vault já usa uma branch diferente de main. "
            "Preciso de confirmação antes de ajustar isso."
        )
    elif blocked_reason == "github_login_required":
        message = (
            "Proteção local pronta. Para ativar o backup online, escolha entrar "
            "na sua conta do GitHub."
        )
    elif blocked_reason == "github_cli_missing":
        message = (
            "Proteção local pronta. Para ativar o backup online depois, instale o "
            "GitHub CLI e rode o setup novamente."
        )
    else:
        message = (
            "Proteção local pronta. Backup online pendente; rode o setup novamente "
            "depois de corrigir o acesso ao GitHub."
        )
    if local_changes_present and status != "blocked_missing_git":
        message += " Não alterei mudanças locais abertas no vault."
    return message


def _emit_setup(
    args: argparse.Namespace,
    *,
    status: str,
    vault_dir: Path | None,
    local_ready: bool,
    github_ready: bool,
    git_identity: GitIdentity | None = None,
    restore_point_id: str | None = None,
    restore_point_label: str | None = None,
    restore_point_status: str | None = None,
    working_tree_clean: bool | None = None,
    local_changes_present: bool | None = None,
    origin_url: str | None = None,
    proposed_private_repo: str | None = None,
    blocked_reason: str | None = None,
    next_action: str | None = None,
    human_decision_required: bool = False,
    human_decision_packet: VaultHumanDecisionPacket | None = None,
    current_branch: str | None = None,
    return_code: int = 0,
) -> int:
    changes_present = bool(local_changes_present)
    message = _local_setup_message(status, blocked_reason, local_changes_present=changes_present)
    payload: dict[str, object] = {
        "schema": "medical-notes-workbench.vault-setup.v1",
        "status": status,
        "agent": _agent_slug(args.agent),
        "workflow": args.workflow,
        "local_ready": local_ready,
        "github_ready": github_ready,
        "human_message": message,
        "human_decision_required": human_decision_required,
    }
    if vault_dir is not None:
        payload["vault_dir"] = str(vault_dir)
    if restore_point_id:
        payload["restore_point_id"] = restore_point_id
    if restore_point_label:
        payload["restore_point_label"] = restore_point_label
    if restore_point_status:
        payload["restore_point_status"] = restore_point_status
    if working_tree_clean is not None:
        payload["working_tree_clean"] = working_tree_clean
    if local_changes_present is not None:
        payload["local_changes_present"] = local_changes_present
    if origin_url:
        payload["origin_url"] = origin_url
    if proposed_private_repo:
        payload["proposed_private_repo"] = proposed_private_repo
    if blocked_reason:
        payload["blocked_reason"] = blocked_reason
    if next_action:
        payload["next_action"] = next_action
    if human_decision_packet:
        payload["human_decision_packet"] = human_decision_packet.to_payload()
    if current_branch:
        payload["current_branch"] = current_branch
    if git_identity:
        _add_git_identity_payload(payload, git_identity)
    _emit(args, payload, message)
    return return_code


def _repo_root(vault_dir: Path) -> Path | None:
    inside = _git(vault_dir, ["rev-parse", "--is-inside-work-tree"], check=False)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return None
    root = _git(vault_dir, ["rev-parse", "--show-toplevel"]).stdout.strip()
    return Path(root).resolve()


def _ensure_local_vault_repo(vault_dir: Path, *, confirm_main_branch: str | None = None) -> bool:
    root = _repo_root(vault_dir)
    created_repo = root is None
    if root is None:
        init = _git(vault_dir, ["init", "-b", "main"], check=False)
        if init.returncode != 0:
            init = _git(vault_dir, ["init"], check=False)
        if init.returncode != 0:
            detail = (init.stderr or init.stdout).strip()
            raise VaultGitError(f"vault_setup: nao consegui criar protecao local: {detail}")
        root = _repo_root(vault_dir)
    if root is None or _norm_path(root) != _norm_path(vault_dir):
        raise VaultGitError("blocked_wrong_repo_root")

    branch = _git(vault_dir, ["symbolic-ref", "--short", "HEAD"], check=False).stdout.strip()
    if not branch:
        raise VaultGitError("vault_setup: nao consigo preparar branch main em HEAD destacado")
    if branch != "main" and created_repo:
        _git(vault_dir, ["checkout", "-B", "main"])
    elif branch != "main" and confirm_main_branch == branch:
        _git(vault_dir, ["branch", "-M", "main"])
    elif branch != "main":
        raise VaultGitError(
            "O vault já tem proteção local, mas está em uma linha de trabalho diferente de main. "
            "Não renomeei nada sem confirmação.",
            status="blocked_branch_confirmation_required",
            blocked_reason="non_main_branch",
            next_action="confirmar no /mednotes:setup se posso ajustar a branch principal para main",
            human_decision_required=True,
            human_decision_packet=VaultHumanDecisionPacket(
                kind="confirm_main_branch",
                prompt="Posso ajustar a branch principal do vault para main?",
                options=(
                    VaultHumanDecisionOption(
                        label="Confirmar main",
                        description="Renomeia a branch atual do vault para main e preserva o histórico existente.",
                        resume_action=f"--confirm-main-branch {branch}",
                    ),
                ),
                resume_action=f"--confirm-main-branch {branch}",
                current_branch=branch,
            ),
        )
    return created_repo


def _has_head(vault_dir: Path) -> bool:
    return _git(vault_dir, ["rev-parse", "--verify", "HEAD"], check=False).returncode == 0


def _ensure_initial_identity_marker_if_needed(vault_dir: Path) -> None:
    if _has_head(vault_dir) or _has_worktree_changes(vault_dir):
        return
    marker_path = vault_dir / VAULT_IDENTITY_MARKER
    marker_path.write_text("Managed by Medical Notes Workbench vault setup.\n", encoding="utf-8")


def _prepare_setup_restore_point(
    vault_dir: Path,
    args: argparse.Namespace,
    *,
    created_repo: bool,
) -> SetupRestorePoint:
    has_head = _has_head(vault_dir)
    dirty = _has_worktree_changes(vault_dir)
    if created_repo or not has_head:
        _ensure_initial_identity_marker_if_needed(vault_dir)
        created = _snapshot_dirty_main(
            vault_dir,
            agent=args.agent,
            workflow=args.workflow,
            run_id=_parallel_run_id(args.run_id),
            restore_point_label="Proteção local criada a partir do estado atual",
        )
        if created:
            return SetupRestorePoint(
                restore_point_id=created,
                status="created_initial_restore_point",
                label="Proteção local criada a partir do estado atual",
                working_tree_clean=True,
                local_changes_present=False,
            )
        if _has_head(vault_dir):
            is_dirty = _has_worktree_changes(vault_dir)
            return SetupRestorePoint(
                restore_point_id=_short_sha(vault_dir),
                status="existing_history",
                label="Histórico existente preservado",
                working_tree_clean=not is_dirty,
                local_changes_present=is_dirty,
            )
    if has_head:
        status = "existing_history_with_local_changes" if dirty else "existing_history"
        label = "Histórico existente preservado; mudanças locais ainda abertas" if dirty else "Histórico existente preservado"
        return SetupRestorePoint(
            restore_point_id=_short_sha(vault_dir),
            status=status,
            label=label,
            working_tree_clean=not dirty,
            local_changes_present=dirty,
        )
    raise VaultGitError("vault_setup: nao consegui criar ponto de restauração inicial")


def _github_repo_name(vault_dir: Path, explicit: str | None) -> str:
    value = explicit or vault_dir.name
    name = _slug(value, lower=True)
    return name or "medical-notes-vault"


def _gh(
    args: list[str],
    *,
    timeout: int = GIT_PROBE_TIMEOUT_SECONDS,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _run_cmd(["gh", *args], timeout=timeout, capture=capture)


def _github_login_decision_packet() -> VaultHumanDecisionPacket:
    return VaultHumanDecisionPacket(
        kind="github_login",
        prompt="Como deseja resolver o backup online do GitHub?",
        options=(
            VaultHumanDecisionOption(
                label="Entrar no GitHub (recomendado)",
                description="Abre o fluxo oficial do GitHub CLI e tenta conectar o backup online.",
                resume_action="--start-github-login",
            ),
            VaultHumanDecisionOption(
                label="Continuar local",
                description="Mantém a proteção local pronta e deixa o backup online para depois.",
                resume_action="skip_online_backup_for_now",
            ),
        ),
    )


def _looks_like_github_origin(origin_url: str | None) -> bool:
    if not origin_url:
        return False
    normalized = origin_url.lower()
    return "github.com/" in normalized or normalized.startswith("git@github.com:")


def _github_login_required_setup(
    args: argparse.Namespace,
    *,
    vault_dir: Path,
    git_identity: GitIdentity,
    restore: SetupRestorePoint,
    origin_url: str | None = None,
) -> int:
    return _emit_setup(
        args,
        status="local_ready_github_pending",
        vault_dir=vault_dir,
        local_ready=True,
        github_ready=False,
        git_identity=git_identity,
        restore_point_id=restore.restore_point_id,
        restore_point_label=restore.label,
        restore_point_status=restore.status,
        working_tree_clean=restore.working_tree_clean,
        local_changes_present=restore.local_changes_present,
        origin_url=origin_url,
        blocked_reason="github_login_required",
        next_action="usar a opção recomendada para entrar no GitHub e concluir o backup online",
        human_decision_required=True,
        human_decision_packet=_github_login_decision_packet(),
    )


def _stdio_is_interactive() -> bool:
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def _github_owner() -> str | None:
    result = _gh(["api", "user", "--jq", ".login"], timeout=GIT_NETWORK_TIMEOUT_SECONDS)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _origin_url(vault_dir: Path) -> str | None:
    origin = _git(vault_dir, ["remote", "get-url", "origin"], check=False)
    if origin.returncode != 0:
        return None
    return origin.stdout.strip() or None


def _remote_access_ok(vault_dir: Path) -> bool:
    origin = _origin_url(vault_dir)
    if not origin:
        return False
    return _git(vault_dir, ["ls-remote", "origin"], check=False, timeout=GIT_NETWORK_TIMEOUT_SECONDS).returncode == 0


def _push_main_for_setup(vault_dir: Path) -> tuple[bool, str]:
    result = _git(vault_dir, ["push", "-u", "origin", "main"], check=False, timeout=GIT_NETWORK_TIMEOUT_SECONDS)
    if result.returncode == 0:
        return True, ""
    return False, (result.stderr or result.stdout).strip()


def _create_private_remote(vault_dir: Path, repo: str) -> tuple[bool, str]:
    result = _gh(
        [
            "repo",
            "create",
            repo,
            "--private",
            "--source",
            str(vault_dir),
            "--remote",
            "origin",
            "--push",
        ],
        timeout=GIT_NETWORK_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()
    return True, (result.stdout or "").strip()


def run_setup(args: argparse.Namespace) -> int:
    if shutil.which("git") is None:
        vault_dir = Path(args.vault_dir).expanduser().resolve() if args.vault_dir else None
        return _emit_setup(
            args,
            status="blocked_missing_git",
            vault_dir=vault_dir,
            local_ready=False,
            github_ready=False,
            next_action="instalar Git e rodar /mednotes:setup novamente",
            return_code=1,
        )

    vault_dir = resolve_vault_dir(args.vault_dir)
    try:
        created_repo = _ensure_local_vault_repo(vault_dir, confirm_main_branch=args.confirm_main_branch)
    except VaultGitError as exc:
        if str(exc) == "blocked_wrong_repo_root":
            return _emit_setup(
                args,
                status="blocked_wrong_repo_root",
                vault_dir=vault_dir,
                local_ready=False,
                github_ready=False,
                blocked_reason="wrong_repo_root",
                next_action="escolher a raiz real do vault e rodar /mednotes:setup novamente",
                return_code=1,
            )
        if exc.status == "blocked_branch_confirmation_required":
            packet = exc.human_decision_packet
            current_branch = packet.current_branch if packet is not None else ""
            return _emit_setup(
                args,
                status=exc.status,
                vault_dir=vault_dir,
                local_ready=False,
                github_ready=False,
                blocked_reason=exc.blocked_reason,
                next_action=exc.next_action,
                human_decision_required=True,
                human_decision_packet=packet,
                current_branch=current_branch,
                return_code=1,
            )
        raise

    _write_state_file("vault.path", str(vault_dir))
    restore = _prepare_setup_restore_point(vault_dir, args, created_repo=created_repo)
    git_identity = _resolve_git_identity(args.agent)

    origin = _origin_url(vault_dir)
    if origin:
        if _remote_access_ok(vault_dir):
            pushed, push_detail = _push_main_for_setup(vault_dir)
            if pushed:
                origin = _origin_url(vault_dir) or origin
                _write_state_file("vault.remote-allowlist", origin)
                return _emit_setup(
                    args,
                    status="ready",
                    vault_dir=vault_dir,
                    local_ready=True,
                    github_ready=True,
                    git_identity=git_identity,
                    restore_point_id=restore.restore_point_id,
                    restore_point_label=restore.label,
                    restore_point_status=restore.status,
                    working_tree_clean=restore.working_tree_clean,
                    local_changes_present=restore.local_changes_present,
                    origin_url=origin,
                )
            return _emit_setup(
                args,
                status="local_ready_github_pending",
                vault_dir=vault_dir,
                local_ready=True,
                github_ready=False,
                git_identity=git_identity,
                restore_point_id=restore.restore_point_id,
                restore_point_label=restore.label,
                restore_point_status=restore.status,
                working_tree_clean=restore.working_tree_clean,
                local_changes_present=restore.local_changes_present,
                origin_url=origin,
                blocked_reason="github_push_failed",
                next_action=push_detail or "corrigir permissão/proteção do repositório e rodar /mednotes:setup novamente",
            )
        if shutil.which("gh") is not None:
            auth = _gh(["auth", "status"])
            if auth.returncode != 0 and args.start_github_login and _stdio_is_interactive():
                _gh(["auth", "login"], timeout=GITHUB_LOGIN_TIMEOUT_SECONDS, capture=False)
                auth = _gh(["auth", "status"])
            if auth.returncode != 0:
                return _github_login_required_setup(
                    args,
                    vault_dir=vault_dir,
                    git_identity=git_identity,
                    restore=restore,
                    origin_url=origin,
                )
            if args.start_github_login:
                _gh(["auth", "setup-git"])
                if _remote_access_ok(vault_dir):
                    pushed, push_detail = _push_main_for_setup(vault_dir)
                    if pushed:
                        _write_state_file("vault.remote-allowlist", origin)
                        return _emit_setup(
                            args,
                            status="ready",
                            vault_dir=vault_dir,
                            local_ready=True,
                            github_ready=True,
                            git_identity=git_identity,
                            restore_point_id=restore.restore_point_id,
                            restore_point_label=restore.label,
                            restore_point_status=restore.status,
                            working_tree_clean=restore.working_tree_clean,
                            local_changes_present=restore.local_changes_present,
                            origin_url=origin,
                        )
                    return _emit_setup(
                        args,
                        status="local_ready_github_pending",
                        vault_dir=vault_dir,
                        local_ready=True,
                        github_ready=False,
                        git_identity=git_identity,
                        restore_point_id=restore.restore_point_id,
                        restore_point_label=restore.label,
                        restore_point_status=restore.status,
                        working_tree_clean=restore.working_tree_clean,
                        local_changes_present=restore.local_changes_present,
                        origin_url=origin,
                        blocked_reason="github_push_failed",
                        next_action=push_detail
                        or "corrigir permissão/proteção do repositório e rodar /mednotes:setup novamente",
                    )
        if shutil.which("gh") is None and _looks_like_github_origin(origin):
            return _emit_setup(
                args,
                status="local_ready_github_pending",
                vault_dir=vault_dir,
                local_ready=True,
                github_ready=False,
                git_identity=git_identity,
                restore_point_id=restore.restore_point_id,
                restore_point_label=restore.label,
                restore_point_status=restore.status,
                working_tree_clean=restore.working_tree_clean,
                local_changes_present=restore.local_changes_present,
                origin_url=origin,
                blocked_reason="github_cli_missing",
                next_action="instalar GitHub CLI para reparar o login do backup online",
            )
        decision_required = shutil.which("gh") is not None and _looks_like_github_origin(origin)
        return _emit_setup(
            args,
            status="local_ready_github_pending",
            vault_dir=vault_dir,
            local_ready=True,
            github_ready=False,
            git_identity=git_identity,
            restore_point_id=restore.restore_point_id,
            restore_point_label=restore.label,
            restore_point_status=restore.status,
            working_tree_clean=restore.working_tree_clean,
            local_changes_present=restore.local_changes_present,
            origin_url=origin,
            blocked_reason="github_remote_unreachable",
            next_action=(
                "usar a opção recomendada para reparar o login do GitHub e concluir o backup online"
                if decision_required
                else "corrigir login/rede/permissão do GitHub e rodar /mednotes:setup novamente"
            ),
            human_decision_required=decision_required,
            human_decision_packet=_github_login_decision_packet() if decision_required else None,
        )

    if shutil.which("gh") is None:
        return _emit_setup(
            args,
            status="local_ready_github_pending",
            vault_dir=vault_dir,
            local_ready=True,
            github_ready=False,
            git_identity=git_identity,
            restore_point_id=restore.restore_point_id,
            restore_point_label=restore.label,
            restore_point_status=restore.status,
            working_tree_clean=restore.working_tree_clean,
            local_changes_present=restore.local_changes_present,
            blocked_reason="github_cli_missing",
            next_action="instalar GitHub CLI para ativar backup online",
        )

    auth = _gh(["auth", "status"])
    if auth.returncode != 0 and args.start_github_login and _stdio_is_interactive():
        _gh(["auth", "login"], timeout=GITHUB_LOGIN_TIMEOUT_SECONDS, capture=False)
        auth = _gh(["auth", "status"])
    if auth.returncode != 0:
        return _github_login_required_setup(args, vault_dir=vault_dir, git_identity=git_identity, restore=restore)

    owner = _github_owner()
    if not owner:
        return _emit_setup(
            args,
            status="local_ready_github_pending",
            vault_dir=vault_dir,
            local_ready=True,
            github_ready=False,
            git_identity=git_identity,
            restore_point_id=restore.restore_point_id,
            restore_point_label=restore.label,
            restore_point_status=restore.status,
            working_tree_clean=restore.working_tree_clean,
            local_changes_present=restore.local_changes_present,
            blocked_reason="github_user_unknown",
            next_action="confirmar login do GitHub e rodar /mednotes:setup novamente",
        )

    proposed = f"{owner}/{_github_repo_name(vault_dir, args.repo_name)}"
    if args.confirm_create_remote != proposed:
        return _emit_setup(
            args,
            status="awaiting_remote_confirmation",
            vault_dir=vault_dir,
            local_ready=True,
            github_ready=False,
            git_identity=git_identity,
            restore_point_id=restore.restore_point_id,
            restore_point_label=restore.label,
            restore_point_status=restore.status,
            working_tree_clean=restore.working_tree_clean,
            local_changes_present=restore.local_changes_present,
            proposed_private_repo=proposed,
            next_action=f"confirmar criação do repositório privado {proposed}",
            human_decision_required=True,
        )

    created, detail = _create_private_remote(vault_dir, proposed)
    origin = _origin_url(vault_dir)
    if not created or not origin or not _remote_access_ok(vault_dir):
        return _emit_setup(
            args,
            status="local_ready_github_pending",
            vault_dir=vault_dir,
            local_ready=True,
            github_ready=False,
            git_identity=git_identity,
            restore_point_id=restore.restore_point_id,
            restore_point_label=restore.label,
            restore_point_status=restore.status,
            working_tree_clean=restore.working_tree_clean,
            local_changes_present=restore.local_changes_present,
            origin_url=origin,
            proposed_private_repo=proposed,
            blocked_reason="github_remote_create_failed",
            next_action=detail or "corrigir criação do repositório privado e rodar /mednotes:setup novamente",
        )

    _write_state_file("vault.remote-allowlist", origin)
    return _emit_setup(
        args,
        status="ready",
        vault_dir=vault_dir,
        local_ready=True,
        github_ready=True,
        git_identity=git_identity,
        restore_point_id=restore.restore_point_id,
        restore_point_label=restore.label,
        restore_point_status=restore.status,
        working_tree_clean=restore.working_tree_clean,
        local_changes_present=restore.local_changes_present,
        origin_url=origin,
        proposed_private_repo=proposed,
    )


def _trailer_value(body: str, key: str) -> str:
    prefix = f"{key}:"
    for line in reversed(body.splitlines()):
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


def _status_entries(vault_dir: Path, base_ref: str, head_ref: str, paths: list[str]) -> list[dict[str, str]]:
    args = ["diff", "--name-status", f"{base_ref}..{head_ref}", "--"]
    args.extend(paths)
    lines = _git(vault_dir, args).stdout.splitlines()
    entries: list[dict[str, str]] = []
    for line in lines:
        parts = line.split("\t")
        if not parts:
            continue
        status = parts[0]
        if status.startswith("R") and len(parts) >= 3:
            entries.append({"status": status, "path": parts[1], "new_path": parts[2]})
        elif len(parts) >= 2:
            entries.append({"status": status, "path": parts[1]})
    return entries


def _affected_files(entries: list[dict[str, str]]) -> list[str]:
    files: list[str] = []
    for entry in entries:
        path = entry.get("path", "")
        new_path = entry.get("new_path", "")
        if path:
            files.append(path)
        if new_path and new_path not in files:
            files.append(new_path)
    return files


def run_precommit(args: argparse.Namespace) -> int:
    vault_dir = resolve_vault_dir(args.vault_dir)
    context = validate_vault(vault_dir)
    _print_context("vault_precommit", context)
    _ensure_main(vault_dir, "vault_precommit")

    if not _has_worktree_changes(vault_dir):
        print("vault_precommit: working tree limpo, nada a fazer")
        return 0

    commit_sha = _snapshot_dirty_main(vault_dir, agent=args.agent, workflow=args.workflow)
    _sync_and_push(vault_dir, "vault_precommit")
    print(f"vault_precommit: snapshot criado em {commit_sha}")
    return 0


def run_commit(args: argparse.Namespace) -> int:
    vault_dir = resolve_vault_dir(args.vault_dir)
    context = validate_vault(vault_dir)
    _print_context("vault_commit", context)
    _ensure_main(vault_dir, "vault_commit")

    _git(vault_dir, ["add", "-A"])
    if not _has_staged_changes(vault_dir):
        print("vault_commit: nada a commitar")
        return 0

    body_prose = _body_file_text(args.body_file, "vault_commit")
    if not body_prose:
        body_prose = _default_delivery_record_for_commit(
            vault_dir,
            title=args.title,
            workflow=args.workflow,
        )
    operational_details = _operational_details_for_commit(vault_dir)

    run_id = args.run_id or _run_id()
    trailers = [
        f"Agent: {args.agent}",
        f"Workflow: {args.workflow}",
        f"Run-Id: {run_id}",
    ]
    optional_trailers = [
        ("Tool", args.tool),
        ("Subagent", args.subagent),
        ("Trigger-Context", args.trigger_context),
        ("Receipt", args.receipt),
        ("Notes-Touched", args.notes_touched),
    ]
    for key, value in optional_trailers:
        if value:
            trailers.append(f"{key}: {value}")

    messages = []
    if body_prose:
        messages.append(body_prose)
    if operational_details:
        messages.append(operational_details)
    messages.append("\n".join(trailers))
    _commit(
        vault_dir,
        title=args.title,
        messages=messages,
        identity=_resolve_git_identity(args.agent),
    )
    _sync_and_push(vault_dir, "vault_commit")
    commit_sha = _git(vault_dir, ["rev-parse", "--short", "HEAD"]).stdout.strip()
    print(f"vault_commit: {commit_sha}")
    return 0


def run_run_start(args: argparse.Namespace) -> int:
    vault_dir = resolve_vault_dir(args.vault_dir)
    context = validate_vault(vault_dir)
    _ensure_main(vault_dir, "vault_run_start")

    workflow = _normalize_workflow_name(args.workflow)
    run_id = _parallel_run_id(args.run_id)
    label = _restore_point_label(workflow, when="before")
    created_sha = _snapshot_dirty_main(
        vault_dir,
        agent=args.agent,
        workflow=workflow,
        run_id=run_id,
        restore_point_label=label,
    )
    sync_status = _sync_and_push(vault_dir, "vault_run_start")
    restore_point_id = created_sha or _short_sha(vault_dir)
    status = "restore_point_created" if created_sha else "restore_point_ready"
    message = "Salvei um ponto de restauração antes de começar."
    guard_lease = _write_guard_lease(vault_dir, agent=args.agent, workflow=workflow, run_id=run_id)
    payload: dict[str, object] = {
        "schema": "medical-notes-workbench.vault-run-start.v1",
        "status": status,
        "agent": _agent_slug(args.agent),
        "workflow": workflow,
        "run_id": run_id,
        "restore_point_id": restore_point_id,
        "restore_point_label": label,
        "vault_dir": str(vault_dir),
        "backup_online": context.backup_online,
        "sync_status": sync_status,
        "guard_lease": guard_lease,
        "next_finish_step": _run_finish_next_step(agent=args.agent, workflow=workflow, run_id=run_id),
        "human_message": message,
    }
    if context.origin_url:
        payload["origin_url"] = context.origin_url
    _emit(args, payload, message)
    return 0


def run_run_finish(args: argparse.Namespace) -> int:
    if not str(args.workflow or "").strip():
        raise VaultGitError(
            "vault_run_finish: --workflow e obrigatorio.",
            status="blocked",
            blocked_reason="workflow_required",
            next_action=(
                "Repetir run-finish com --workflow /mednotes:fix-wiki ou o workflow publico correto; "
                "para este fluxo use: run-finish --agent gemini-cli --workflow /mednotes:fix-wiki "
                '--run-id <run_id> --title "Reparo da Wiki_Medicina" --public-json --json.'
            ),
            required_inputs=["workflow"],
        )
    workflow = _normalize_workflow_name(args.workflow)
    vault_dir = resolve_vault_dir(args.vault_dir)
    context = validate_vault(vault_dir)
    if getattr(args, "run_id_provided", False) and not str(args.run_id or "").strip():
        raise VaultGitError(
            'vault_run_finish: --run-id foi fornecido vazio; nao use placeholder como "".',
            status="blocked_empty_run_id",
            blocked_reason="empty_run_id",
            next_action=_empty_run_id_next_action(vault_dir, agent=args.agent, workflow=workflow),
        )
    if args.branch:
        integrate_args = argparse.Namespace(
            branch=args.branch,
            agent=args.agent,
            workflow=workflow,
            run_id=args.run_id,
            vault_dir=args.vault_dir,
            json=args.json,
            semantic_output=True,
        )
        return run_integrate(integrate_args)

    _ensure_main(vault_dir, "vault_run_finish")

    run_id_resolution = _run_finish_run_id(vault_dir, agent=args.agent, workflow=workflow, run_id=args.run_id)
    run_id = run_id_resolution.run_id
    _git(vault_dir, ["add", "-A"])
    label = _restore_point_label(workflow, when="after")
    if not _has_staged_changes(vault_dir):
        sync_status = _sync_and_push(vault_dir, "vault_run_finish")
        guard_lease = _close_guard_lease(vault_dir, agent=args.agent, run_id=run_id)
        if run_id_resolution.auto_recovered:
            guard_lease["run_id_auto_recovered"] = True
        message = "Nenhuma mudança nova para salvar; o ponto de restauração atual continua válido."
        if sync_status == "synced":
            message += " O backup online foi conferido."
        elif sync_status == "skipped_no_remote":
            message += " O backup online ainda está pendente."
        elif sync_status.startswith("pending_"):
            message += " O backup online ficou pendente; a proteção local continua válida."
        payload: dict[str, object] = {
            "schema": "medical-notes-workbench.vault-run-finish.v1",
            "status": "no_changes",
            "agent": _agent_slug(args.agent),
            "workflow": workflow,
            "run_id": run_id,
            "restore_point_id": _short_sha(vault_dir),
            "restore_point_label": label,
            "backup_online": context.backup_online,
            "sync_status": sync_status,
            "guard_lease": guard_lease,
            "human_message": message,
        }
        if run_id_resolution.auto_recovered:
            payload["run_id_recovery"] = {
                "schema": "medical-notes-workbench.vault-run-id-recovery.v1",
                "status": "recovered",
                "requested_run_id": run_id_resolution.requested_run_id,
                "recovered_run_id": run_id,
                "reason": run_id_resolution.recovery_reason,
            }
        if context.origin_url:
            payload["origin_url"] = context.origin_url
        if getattr(args, "public_json", False):
            payload = _public_run_finish_payload(payload)
        _emit(args, payload, message)
        return 0

    title = str(args.title or "").strip() or _default_run_finish_title(workflow)

    body_prose = _body_file_text(args.body_file, "vault_run_finish")
    if not body_prose:
        body_prose = _default_delivery_record_for_commit(
            vault_dir,
            title=title,
            workflow=workflow,
        )
    operational_details = _operational_details_for_commit(vault_dir)
    trailers = [
        f"Agent: {_agent_slug(args.agent)}",
        f"Workflow: {workflow}",
        f"Run-Id: {run_id}",
        "Restore-Point: workflow-result",
        f"Restore-Point-Label: {label}",
    ]
    optional_trailers = [
        ("Tool", args.tool),
        ("Subagent", args.subagent),
        ("Trigger-Context", args.trigger_context),
        ("Receipt", args.receipt),
        ("Notes-Touched", args.notes_touched),
    ]
    for key, value in optional_trailers:
        if value:
            trailers.append(f"{key}: {value}")

    messages = []
    if body_prose:
        messages.append(body_prose)
    if operational_details:
        messages.append(operational_details)
    messages.append("\n".join(trailers))
    identity = _commit(
        vault_dir,
        title=title,
        messages=messages,
        identity=_resolve_git_identity(args.agent),
    )
    sync_status = _sync_and_push(vault_dir, "vault_run_finish")
    restore_point_id = _short_sha(vault_dir)
    guard_lease = _close_guard_lease(vault_dir, agent=args.agent, run_id=run_id)
    if run_id_resolution.auto_recovered:
        guard_lease["run_id_auto_recovered"] = True
    message = "Ponto de restauração salvo com o resultado do workflow."
    payload = {
        "schema": "medical-notes-workbench.vault-run-finish.v1",
        "status": "recorded",
        "agent": _agent_slug(args.agent),
        "workflow": workflow,
        "run_id": run_id,
        "restore_point_id": restore_point_id,
        "restore_point_label": label,
        "vault_dir": str(vault_dir),
        "backup_online": context.backup_online,
        "sync_status": sync_status,
        "guard_lease": guard_lease,
        "human_message": message,
    }
    if run_id_resolution.auto_recovered:
        payload["run_id_recovery"] = {
            "schema": "medical-notes-workbench.vault-run-id-recovery.v1",
            "status": "recovered",
            "requested_run_id": run_id_resolution.requested_run_id,
            "recovered_run_id": run_id,
            "reason": run_id_resolution.recovery_reason,
        }
    _add_git_identity_payload(payload, identity)
    if context.origin_url:
        payload["origin_url"] = context.origin_url
    if getattr(args, "public_json", False):
        payload = _public_run_finish_payload(payload)
    _emit(args, payload, message)
    return 0


def run_branch_start(args: argparse.Namespace) -> int:
    vault_dir = resolve_vault_dir(args.vault_dir)
    context = validate_vault(vault_dir, require_remote=True)
    if not args.json:
        _print_context("vault_branch_start", context)
    _ensure_main(vault_dir, "vault_branch_start")

    main_snapshot = _snapshot_dirty_main(vault_dir, agent=args.agent, workflow=args.workflow)
    _sync_and_push(vault_dir, "vault_branch_start")

    run_id = _parallel_run_id(args.run_id)
    branch = _parallel_branch(args.agent, run_id)
    _validate_branch_ref(branch)
    worktree_dir = _worktree_dir(args.agent, run_id)
    if worktree_dir.exists():
        raise VaultGitError(f"vault_branch_start: worktree ja existe: {worktree_dir}")
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)

    branch_exists = _git(vault_dir, ["show-ref", "--verify", f"refs/heads/{branch}"], check=False)
    if branch_exists.returncode == 0:
        raise VaultGitError(f"vault_branch_start: branch local ja existe: {branch}")
    remote_exists = _git(vault_dir, ["ls-remote", "--exit-code", "--heads", "origin", branch], check=False)
    if remote_exists.returncode == 0:
        raise VaultGitError(f"vault_branch_start: branch remota ja existe: {branch}")

    _git(vault_dir, ["worktree", "add", "-b", branch, str(worktree_dir), "HEAD"])
    payload: dict[str, object] = {
        "schema": "medical-notes-workbench.vault-branch-start.v1",
        "status": "created",
        "agent": _agent_slug(args.agent),
        "workflow": args.workflow,
        "run_id": run_id,
        "branch": branch,
        "worktree_dir": str(worktree_dir),
        "vault_dir": str(vault_dir),
        "origin_url": context.origin_url,
        "main_snapshot": main_snapshot,
    }
    _emit(
        args,
        payload,
        f"vault_branch_start: branch={branch} worktree={worktree_dir}",
    )
    return 0


def _resolve_branch_worktree(args: argparse.Namespace, run_id: str) -> Path:
    if args.vault_dir:
        return resolve_vault_dir(args.vault_dir)
    candidate = _worktree_dir(args.agent, run_id)
    if candidate.is_dir():
        return candidate.resolve()
    cwd = Path.cwd().resolve()
    if cwd.is_dir():
        return cwd
    raise VaultGitError(
        "vault_branch_commit: nao consegui resolver o worktree paralelo.\n"
        "Use --run-id <id> criado pelo branch-start ou --vault-dir <worktree>."
    )


def _current_branch(vault_dir: Path) -> str:
    return _git(vault_dir, ["symbolic-ref", "--short", "HEAD"], check=False).stdout.strip()


def _resolve_branch_commit_context(args: argparse.Namespace) -> tuple[Path, str, str, VaultContext]:
    if args.run_id:
        run_id = _parallel_run_id(args.run_id)
        branch = _parallel_branch(args.agent, run_id)
        _validate_branch_ref(branch)
        worktree_dir = _resolve_branch_worktree(args, run_id)
        context = validate_vault(worktree_dir, require_remote=True)
        return worktree_dir, branch, run_id, context

    worktree_dir = resolve_vault_dir(args.vault_dir) if args.vault_dir else Path.cwd().resolve()
    context = validate_vault(worktree_dir, require_remote=True)
    branch = _current_branch(worktree_dir)
    expected_prefix = f"vault/{_agent_slug(args.agent)}/"
    if not branch.startswith(expected_prefix):
        shown = branch or "detached"
        raise VaultGitError(
            "vault_branch_commit: --run-id ausente, entao o worktree atual precisa estar "
            f"em branch {expected_prefix}<run-id>; HEAD={shown}"
        )
    _validate_branch_ref(branch)
    return worktree_dir, branch, _run_id_from_branch(branch), context


def run_branch_commit(args: argparse.Namespace) -> int:
    worktree_dir, branch, run_id, context = _resolve_branch_commit_context(args)
    if not args.json:
        _print_context("vault_branch_commit", context)
    _ensure_branch(worktree_dir, branch, "vault_branch_commit")

    _git(worktree_dir, ["add", "-A"])
    if not _has_staged_changes(worktree_dir):
        payload: dict[str, object] = {
            "schema": "medical-notes-workbench.vault-branch-commit.v1",
            "status": "no_changes",
            "agent": _agent_slug(args.agent),
            "workflow": args.workflow,
            "run_id": run_id,
            "branch": branch,
            "worktree_dir": str(worktree_dir),
        }
        _emit(args, payload, f"vault_branch_commit: nada a commitar em {branch}")
        return 0

    body_prose = _body_file_text(args.body_file, "vault_branch_commit")
    if not body_prose:
        body_prose = _default_delivery_record_for_commit(
            worktree_dir,
            title=args.title,
            workflow=args.workflow,
        )
    operational_details = _operational_details_for_commit(worktree_dir)
    trailers = [
        f"Agent: {_agent_slug(args.agent)}",
        f"Workflow: {args.workflow}",
        f"Run-Id: {run_id}",
        f"Branch: {branch}",
    ]
    optional_trailers = [
        ("Tool", args.tool),
        ("Subagent", args.subagent),
        ("Trigger-Context", args.trigger_context),
        ("Receipt", args.receipt),
        ("Notes-Touched", args.notes_touched),
    ]
    for key, value in optional_trailers:
        if value:
            trailers.append(f"{key}: {value}")

    messages = []
    if body_prose:
        messages.append(body_prose)
    if operational_details:
        messages.append(operational_details)
    messages.append("\n".join(trailers))
    identity = _commit(
        worktree_dir,
        title=args.title,
        messages=messages,
        identity=_resolve_git_identity(args.agent),
    )
    _push_branch(worktree_dir, branch, "vault_branch_commit", required=True)
    commit_sha = _git(worktree_dir, ["rev-parse", "--short", "HEAD"]).stdout.strip()
    payload = {
        "schema": "medical-notes-workbench.vault-branch-commit.v1",
        "status": "committed",
        "agent": _agent_slug(args.agent),
        "workflow": args.workflow,
        "run_id": run_id,
        "branch": branch,
        "worktree_dir": str(worktree_dir),
        "commit": commit_sha,
        "pushed": True,
    }
    _add_git_identity_payload(payload, identity)
    _emit(args, payload, f"vault_branch_commit: {commit_sha} branch={branch}")
    return 0


def _fetch_branch(vault_dir: Path, branch: str) -> str:
    fetch = _git(
        vault_dir,
        ["fetch", "origin", f"{branch}:refs/remotes/origin/{branch}"],
        check=False,
    )
    if fetch.returncode == 0:
        return f"origin/{branch}"
    local = _git(vault_dir, ["show-ref", "--verify", f"refs/heads/{branch}"], check=False)
    if local.returncode == 0:
        return branch
    detail = (fetch.stderr or fetch.stdout).strip()
    raise VaultGitError(f"vault_integrate: nao consegui buscar {branch} em origin: {detail}")


def _merge_message(branch: str, agent: str, workflow: str, run_id: str) -> str:
    label = _restore_point_label(workflow, when="after")
    return (
        f"integra(vault): mescla {branch}\n\n"
        "Integra branch paralela do vault com merge textual limpo do Git.\n\n"
        f"Integrated-Branch: {branch}\n"
        f"Integrated-Agent: {_agent_slug(agent)}\n"
        f"Integrated-Workflow: {workflow}\n"
        f"Integrated-Run-Id: {run_id}\n"
        "Restore-Point: workflow-result\n"
        f"Restore-Point-Label: {label}"
    )


def _validate_integrated_tree(vault_dir: Path) -> None:
    status = _git(vault_dir, ["status", "--porcelain=v1"]).stdout.strip()
    if status:
        raise VaultGitError(
            "vault_integrate: merge parecia limpo, mas a arvore ficou suja; "
            f"bloqueando push.\n{status}"
        )
    unmerged = _git(vault_dir, ["diff", "--name-only", "--diff-filter=U"], check=False)
    if unmerged.stdout.strip():
        raise VaultGitError(
            "vault_integrate: merge deixou arquivos conflitados; bloqueando push.\n"
            + unmerged.stdout.strip()
        )


def run_integrate(args: argparse.Namespace) -> int:
    vault_dir = resolve_vault_dir(args.vault_dir)
    context = validate_vault(vault_dir, require_remote=True)
    semantic_output = bool(getattr(args, "semantic_output", False))
    if not args.json and not semantic_output:
        _print_context("vault_integrate", context)
    _ensure_main(vault_dir, "vault_integrate")
    if _has_worktree_changes(vault_dir):
        raise VaultGitError(
            "vault_integrate: main esta sujo. Rode precommit/commit ou limpe o vault antes de integrar."
        )

    branch = args.branch
    _validate_branch_ref(branch)
    run_id = _parallel_run_id(args.run_id or _run_id_from_branch(branch))
    _sync_main(vault_dir, "vault_integrate")
    merge_ref = _fetch_branch(vault_dir, branch)
    head_before = _git(vault_dir, ["rev-parse", "HEAD"]).stdout.strip()
    identity = _resolve_git_identity(args.agent)
    merge = _git(
        vault_dir,
        [
            "-c",
            f"user.name={identity.name}",
            "-c",
            f"user.email={identity.email}",
            "merge",
            "--no-ff",
            "-m",
            _merge_message(branch, args.agent, args.workflow, run_id),
            merge_ref,
        ],
        check=False,
        extra_env=_git_identity_env(identity),
    )
    if merge.returncode != 0:
        conflicts = [
            line.strip()
            for line in _git(vault_dir, ["diff", "--name-only", "--diff-filter=U"], check=False).stdout.splitlines()
            if line.strip()
        ]
        _git(vault_dir, ["merge", "--abort"], check=False)
        if conflicts:
            if semantic_output:
                message = (
                    "Nada foi alterado. Encontrei conflito entre mudanças paralelas; "
                    "revise os arquivos listados e tente de novo."
                )
                payload = {
                    "schema": "medical-notes-workbench.vault-run-finish.v1",
                    "status": "blocked_conflict",
                    "agent": _agent_slug(args.agent),
                    "workflow": args.workflow,
                    "run_id": run_id,
                    "conflicts": conflicts,
                    "human_message": message,
                    "next_action": "revisar conflitos listados e repetir o fechamento do run",
                    "human_decision_required": True,
                }
                _emit(
                    args,
                    payload,
                    message + "\n" + _format_block("Arquivos que precisam de revisão:", conflicts),
                )
                return 1
            payload: dict[str, object] = {
                "schema": "medical-notes-workbench.vault-integrate.v1",
                "status": "blocked_conflict",
                "branch": branch,
                "agent": _agent_slug(args.agent),
                "workflow": args.workflow,
                "run_id": run_id,
                "conflicts": conflicts,
                "next_action": (
                    "resolver conflito clinico/manualmente ou ajustar a branch e rodar integrate de novo"
                ),
            }
            _emit(
                args,
                payload,
                "vault_integrate: conflito detectado; merge abortado.\n"
                + _format_block("Arquivos conflitados:", conflicts)
                + "\nResolva manualmente ou ajuste a branch e rode integrate novamente.",
            )
            return 1
        detail = (merge.stderr or merge.stdout).strip()
        raise VaultGitError(f"vault_integrate: merge falhou: {detail}")

    _validate_integrated_tree(vault_dir)
    _push_branch(vault_dir, "main", "vault_integrate", required=True)
    head_after = _git(vault_dir, ["rev-parse", "HEAD"]).stdout.strip()
    status = "already_integrated" if head_after == head_before else "merged"
    if semantic_output:
        semantic_status = "already_recorded" if status == "already_integrated" else "integrated"
        label = _restore_point_label(args.workflow, when="after")
        message = "Ponto de restauração salvo com o resultado do workflow."
        payload = {
            "schema": "medical-notes-workbench.vault-run-finish.v1",
            "status": semantic_status,
            "agent": _agent_slug(args.agent),
            "workflow": args.workflow,
            "run_id": run_id,
            "restore_point_id": head_after[:12],
            "restore_point_label": label,
            "human_message": message,
            "pushed": True,
        }
        _add_git_identity_payload(payload, identity)
        _emit(args, payload, message)
        return 0
    payload = {
        "schema": "medical-notes-workbench.vault-integrate.v1",
        "status": status,
        "branch": branch,
        "agent": _agent_slug(args.agent),
        "workflow": args.workflow,
        "run_id": run_id,
        "merge_commit": head_after[:12],
        "pushed": True,
    }
    _add_git_identity_payload(payload, identity)
    _emit(args, payload, f"vault_integrate: {status} {branch} em main ({head_after[:12]})")
    return 0


def _timeline_items(vault_dir: Path, limit: int, *, since: str | None = None, until: str | None = None) -> list[dict[str, str]]:
    args = [
        "log",
        f"--max-count={limit}",
        "--date=iso-strict",
        "--format=%H%x1f%ai%x1f%an%x1f%s%x1f%B%x1e",
    ]
    if since:
        args.append(f"--since={since}")
    if until:
        args.append(f"--until={until}")
    raw = _git(
        vault_dir,
        args,
    ).stdout
    items: list[dict[str, str]] = []
    for record in raw.split("\x1e"):
        record = record.strip()
        if not record:
            continue
        parts = record.split("\x1f", 4)
        if len(parts) != 5:
            continue
        full_sha, created_at, author, subject, body = parts
        workflow = (
            _trailer_value(body, "Workflow")
            or _trailer_value(body, "Integrated-Workflow")
            or _trailer_value(body, "Triggered-By-Workflow")
        )
        run_id = _trailer_value(body, "Run-Id") or _trailer_value(body, "Integrated-Run-Id")
        label = _trailer_value(body, "Restore-Point-Label")
        if not label:
            if subject.startswith("snapshot:"):
                label = _restore_point_label(workflow or "um workflow", when="before")
            elif subject.startswith("restaura("):
                label = f"Restauração aplicada por {workflow or '/mednotes:history'}"
            elif workflow:
                label = _restore_point_label(workflow, when="after")
            else:
                label = "Ponto de restauração do vault"
        items.append(
            {
                "id": full_sha[:12],
                "label": label,
                "workflow": workflow,
                "run_id": run_id,
                "created_at": created_at,
                "author": author,
            }
        )
    return items


def run_timeline(args: argparse.Namespace) -> int:
    vault_dir = resolve_vault_dir(args.vault_dir)
    context = validate_vault(vault_dir)
    limit = max(1, int(args.limit or 10))
    items = _timeline_items(vault_dir, limit, since=args.since, until=args.until)
    backup = _backup_status_payload(vault_dir, context)
    payload: dict[str, object] = {
        "schema": "medical-notes-workbench.vault-timeline.v1",
        "status": "completed",
        "restore_points": items,
        "count": len(items),
        "since": args.since or "",
        "until": args.until or "",
        "backup_online": context.backup_online,
        **backup,
    }
    if context.origin_url:
        payload["origin_url"] = context.origin_url
    if args.json:
        _emit(args, payload, "")
        return 0
    lines = ["Pontos de restauração:"]
    if not items:
        lines.append("- nenhum ponto encontrado")
    for item in items:
        lines.append(f"- {item['id']} — {item['label']} — {item['created_at']}")
    backup_status = str(backup["backup_status"])
    if backup_status == "synced":
        lines.append("Backup online: atualizado.")
    elif backup_status == "local_checkpoints_pending":
        count = backup["local_checkpoints_pending_count"]
        lines.append(f"Backup online: pendente para {count} ponto(s) local(is).")
    elif backup_status == "skipped_no_remote":
        lines.append("Backup online: pendente de configuração.")
    elif backup_status == "unavailable":
        lines.append("Backup online: não conferido agora; proteção local continua válida.")
    elif backup_status == "remote_changes_pending":
        lines.append("Backup online: há mudanças externas para sincronizar antes de continuar.")
    elif backup_status == "diverged":
        lines.append("Backup online: precisa de revisão antes de sincronizar.")
    print("\n".join(lines))
    return 0


def _read_restore_plan(path_value: str) -> dict[str, object]:
    path = Path(path_value).expanduser()
    if not path.is_file():
        raise VaultGitError(f"vault_restore: plano nao encontrado: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VaultGitError(f"vault_restore: plano invalido: {path}") from exc
    if not isinstance(data, dict) or data.get("schema") != "medical-notes-workbench.vault-restore-plan.v1":
        raise VaultGitError(f"vault_restore: schema de plano invalido em {path}")
    return data


def run_restore_preview(args: argparse.Namespace) -> int:
    vault_dir = resolve_vault_dir(args.vault_dir)
    context = validate_vault(vault_dir)
    _ensure_main(vault_dir, "vault_restore_preview")
    restore_to = _git(vault_dir, ["rev-parse", args.to]).stdout.strip()
    current_head = _head(vault_dir)
    paths = list(args.path or [])
    entries = _status_entries(vault_dir, restore_to, current_head, paths)
    affected = _affected_files(entries)
    seed = json.dumps(
        {
            "vault_dir": str(vault_dir),
            "restore_to": restore_to,
            "current_head": current_head,
            "paths": paths,
            "reason": args.reason or "",
            "created_at": _now_iso(),
        },
        sort_keys=True,
    )
    plan_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    plan_dir = _restore_plan_dir()
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_path = plan_dir / f"{plan_id}.json"
    message = "Nada foi alterado ainda. Confirme para aplicar."
    payload: dict[str, object] = {
        "schema": "medical-notes-workbench.vault-restore-plan.v1",
        "status": "preview_ready",
        "plan_id": plan_id,
        "created_at": _now_iso(),
        "vault_dir": str(vault_dir),
        "backup_online": context.backup_online,
        "restore_to": restore_to,
        "current_head": current_head,
        "reason": args.reason or "",
        "entries": entries,
        "affected_files": affected,
        "plan_path": str(plan_path),
        "human_message": message,
    }
    if context.origin_url:
        payload["origin_url"] = context.origin_url
    plan_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _emit(
        args,
        payload,
        "Estas notas seriam restauradas:\n"
        + _format_block("Arquivos afetados:", affected)
        + f"\n{message}",
    )
    return 0


def _apply_restore_entries(vault_dir: Path, restore_to: str, entries: list[dict[str, str]]) -> None:
    for entry in entries:
        status = str(entry.get("status") or "")
        path = str(entry.get("path") or "")
        new_path = str(entry.get("new_path") or "")
        if status.startswith("R"):
            if new_path:
                _git(vault_dir, ["rm", "-f", "--", new_path], check=False)
            if path:
                _git(vault_dir, ["restore", "--source", restore_to, "--", path])
        elif status == "A":
            if path:
                _git(vault_dir, ["rm", "-f", "--", path], check=False)
        elif path:
            _git(vault_dir, ["restore", "--source", restore_to, "--", path])


def run_restore_apply(args: argparse.Namespace) -> int:
    plan = _read_restore_plan(args.plan)
    plan_id = str(plan.get("plan_id") or "")
    if args.confirm != plan_id:
        payload: dict[str, object] = {
            "schema": "medical-notes-workbench.vault-restore-apply.v1",
            "status": "blocked_confirmation_required",
            "plan_id": plan_id,
            "human_message": "Nada foi alterado. Confirme o preview antes de restaurar.",
        }
        _emit(args, payload, "Nada foi alterado. Confirme o preview antes de restaurar.")
        return 1

    vault_dir = resolve_vault_dir(args.vault_dir or str(plan.get("vault_dir") or ""))
    context = validate_vault(vault_dir)
    _ensure_main(vault_dir, "vault_restore_apply")

    current_head = _head(vault_dir)
    expected_head = str(plan.get("current_head") or "")
    if current_head != expected_head:
        payload = {
            "schema": "medical-notes-workbench.vault-restore-apply.v1",
            "status": "blocked_stale_preview",
            "plan_id": plan_id,
            "expected_head": expected_head,
            "current_head": current_head,
            "human_message": "Nada foi alterado. O preview ficou antigo; gere um novo preview de restauração.",
        }
        _emit(args, payload, str(payload["human_message"]))
        return 1

    run_id = _parallel_run_id(args.run_id or f"restore-{plan_id}")
    pre_restore_point_id = ""
    if _has_worktree_changes(vault_dir):
        pre_restore_point_id = _snapshot_dirty_main(
            vault_dir,
            agent=args.agent,
            workflow=args.workflow,
            run_id=run_id,
            restore_point_label="Ponto de restauração antes da restauração",
        ) or ""

    guard_lease = _write_guard_lease(vault_dir, agent=args.agent, workflow=args.workflow, run_id=run_id)
    restore_to = str(plan.get("restore_to") or "")
    entries_raw = plan.get("entries") if isinstance(plan.get("entries"), list) else []
    entries = [entry for entry in entries_raw if isinstance(entry, dict)]
    affected_raw = plan.get("affected_files")
    affected = [str(path) for path in affected_raw] if isinstance(affected_raw, list) else []
    _apply_restore_entries(vault_dir, restore_to, entries)  # type: ignore[arg-type]
    _git(vault_dir, ["add", "-A"])
    if not _has_staged_changes(vault_dir):
        guard_lease = _close_guard_lease(vault_dir, agent=args.agent, run_id=run_id)
        payload = {
            "schema": "medical-notes-workbench.vault-restore-apply.v1",
            "status": "no_changes",
            "plan_id": plan_id,
            "pre_restore_point_id": pre_restore_point_id,
            "guard_lease": guard_lease,
            "human_message": "Nada precisou ser restaurado; o vault já estava igual ao preview.",
        }
        _emit(args, payload, str(payload["human_message"]))
        return 0

    reason = str(plan.get("reason") or "restauração solicitada pelo usuário")
    label = "Ponto de restauração depois da restauração"
    body = (
        f"Restauração aplicada a partir de preview confirmado.\n\n"
        f"{_format_block('Arquivos restaurados:', affected)}\n\n"
        f"Motivo informado: {reason}\n\n"
        f"Agent: {_agent_slug(args.agent)}\n"
        f"Workflow: {args.workflow}\n"
        f"Run-Id: {run_id}\n"
        f"Restore-Plan: {plan_id}\n"
        f"Restore-To: {restore_to[:12]}\n"
        "Restore-Point: restore-apply\n"
        f"Restore-Point-Label: {label}"
    )
    identity = _commit(
        vault_dir,
        title=f"restaura(vault): volta para ponto de restauração {restore_to[:12]}",
        messages=[body],
        identity=_resolve_git_identity(args.agent),
    )
    sync_status = _sync_and_push(vault_dir, "vault_restore_apply")
    restore_point_id = _short_sha(vault_dir)
    guard_lease = _close_guard_lease(vault_dir, agent=args.agent, run_id=run_id)
    message = "Pronto, restaurei o vault e salvei um novo ponto de restauração."
    payload = {
        "schema": "medical-notes-workbench.vault-restore-apply.v1",
        "status": "restored",
        "plan_id": plan_id,
        "pre_restore_point_id": pre_restore_point_id,
        "restore_point_id": restore_point_id,
        "affected_files": affected,
        "backup_online": context.backup_online,
        "sync_status": sync_status,
        "guard_lease": guard_lease,
        "human_message": message,
    }
    _add_git_identity_payload(payload, identity)
    if context.origin_url:
        payload["origin_url"] = context.origin_url
    _emit(args, payload, message)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Registra mutacoes do vault Obsidian conforme a politica de version control."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup = subparsers.add_parser(
        "setup",
        help="Prepara protecao local do vault e guia backup online pelo GitHub.",
    )
    setup.add_argument("--vault-dir")
    setup.add_argument("--agent", required=True)
    setup.add_argument("--workflow", required=True)
    setup.add_argument("--run-id")
    setup.add_argument("--repo-name")
    setup.add_argument("--confirm-create-remote")
    setup.add_argument("--confirm-main-branch")
    setup.add_argument("--start-github-login", action="store_true")
    setup.add_argument("--json", action="store_true")
    setup.set_defaults(func=run_setup)

    precommit = subparsers.add_parser("precommit", help="Cria snapshot pre-agente se o vault estiver sujo.")
    precommit.add_argument("--agent", required=True)
    precommit.add_argument("--workflow", required=True)
    precommit.add_argument("--vault-dir")
    precommit.set_defaults(func=run_precommit)

    commit = subparsers.add_parser("commit", help="Cria commit identificado para mutacoes do agente.")
    commit.add_argument("--agent", required=True)
    commit.add_argument("--workflow", required=True)
    commit.add_argument("--title", required=True)
    commit.add_argument("--body-file")
    commit.add_argument("--tool")
    commit.add_argument("--subagent")
    commit.add_argument("--run-id")
    commit.add_argument("--trigger-context")
    commit.add_argument("--receipt")
    commit.add_argument("--notes-touched")
    commit.add_argument("--vault-dir")
    commit.set_defaults(func=run_commit)

    run_start = subparsers.add_parser(
        "run-start",
        help="Prepara um ponto de restauração invisível antes de mutação real.",
    )
    run_start.add_argument("--agent", required=True)
    run_start.add_argument("--workflow", required=True)
    run_start.add_argument("--run-id")
    run_start.add_argument("--vault-dir")
    run_start.add_argument("--json", action="store_true")
    run_start.add_argument("--public-json", action="store_true", help=argparse.SUPPRESS)
    run_start.set_defaults(func=run_run_start)

    run_finish = subparsers.add_parser(
        "run-finish",
        help="Fecha um run mutante e salva o ponto de restauração resultante.",
    )
    run_finish.add_argument("--agent", required=True)
    run_finish.add_argument("--workflow")
    run_finish.add_argument("--title")
    run_finish.add_argument("--body-file")
    run_finish.add_argument("--tool")
    run_finish.add_argument("--subagent")
    run_finish.set_defaults(run_id_provided=False)
    run_finish.add_argument("--run-id", action=MarkProvidedAction)
    run_finish.add_argument("--trigger-context")
    run_finish.add_argument("--receipt")
    run_finish.add_argument("--notes-touched")
    run_finish.add_argument("--branch")
    run_finish.add_argument("--vault-dir")
    run_finish.add_argument("--json", action="store_true")
    run_finish.add_argument("--public-json", action="store_true")
    run_finish.set_defaults(func=run_run_finish)

    timeline = subparsers.add_parser(
        "timeline",
        help="Lista pontos de restauração em linguagem humana.",
    )
    timeline.add_argument("--limit", type=int, default=10)
    timeline.add_argument("--since")
    timeline.add_argument("--until")
    timeline.add_argument("--vault-dir")
    timeline.add_argument("--json", action="store_true")
    timeline.set_defaults(func=run_timeline)

    restore_preview = subparsers.add_parser(
        "restore-preview",
        help="Mostra o que seria restaurado sem alterar o vault.",
    )
    restore_preview.add_argument("--to", required=True)
    restore_preview.add_argument("--path", action="append")
    restore_preview.add_argument("--reason")
    restore_preview.add_argument("--vault-dir")
    restore_preview.add_argument("--json", action="store_true")
    restore_preview.set_defaults(func=run_restore_preview)

    restore_apply = subparsers.add_parser(
        "restore-apply",
        help="Aplica um preview de restauração confirmado.",
    )
    restore_apply.add_argument("--plan", required=True)
    restore_apply.add_argument("--confirm")
    restore_apply.add_argument("--agent", required=True)
    restore_apply.add_argument("--workflow", required=True)
    restore_apply.add_argument("--run-id")
    restore_apply.add_argument("--vault-dir")
    restore_apply.add_argument("--json", action="store_true")
    restore_apply.set_defaults(func=run_restore_apply)

    guard_status = subparsers.add_parser(
        "guard-status",
        help="Mostra leases ativos da trava de segurança do vault.",
    )
    guard_status.add_argument("--vault-dir")
    guard_status.add_argument("--json", action="store_true")
    guard_status.set_defaults(func=run_guard_status)

    branch_start = subparsers.add_parser(
        "branch-start",
        help="Cria branch/worktree isolado para um agente ou run paralelo.",
    )
    branch_start.add_argument("--agent", required=True)
    branch_start.add_argument("--workflow", required=True)
    branch_start.add_argument("--run-id")
    branch_start.add_argument("--vault-dir")
    branch_start.add_argument("--json", action="store_true")
    branch_start.set_defaults(func=run_branch_start)

    branch_commit = subparsers.add_parser(
        "branch-commit",
        help="Commita e empurra mudancas do worktree paralelo.",
    )
    branch_commit.add_argument("--agent", required=True)
    branch_commit.add_argument("--workflow", required=True)
    branch_commit.add_argument("--title", required=True)
    branch_commit.add_argument("--body-file")
    branch_commit.add_argument("--tool")
    branch_commit.add_argument("--subagent")
    branch_commit.add_argument("--run-id")
    branch_commit.add_argument("--trigger-context")
    branch_commit.add_argument("--receipt")
    branch_commit.add_argument("--notes-touched")
    branch_commit.add_argument("--vault-dir")
    branch_commit.add_argument("--json", action="store_true")
    branch_commit.set_defaults(func=run_branch_commit)

    integrate = subparsers.add_parser(
        "integrate",
        help="Integra branch paralela em main com merge textual limpo do Git.",
    )
    integrate.add_argument("--branch", required=True)
    integrate.add_argument("--agent", required=True)
    integrate.add_argument("--workflow", required=True)
    integrate.add_argument("--run-id")
    integrate.add_argument("--vault-dir")
    integrate.add_argument("--json", action="store_true")
    integrate.set_defaults(func=run_integrate)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except VaultGitError as exc:
        if getattr(args, "json", False):
            print(json.dumps(exc.to_payload(), ensure_ascii=False, sort_keys=True))
            return 1
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

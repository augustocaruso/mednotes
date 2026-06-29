#!/usr/bin/env bash
# Wrapper Bash generico para a politica Git cross-platform do vault.

set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "$script_dir/../.." && pwd)"
if [[ ! -f "$project_root/pyproject.toml" ]]; then
  project_root="$(cd "$script_dir/../../.." && pwd)"
fi
core="$script_dir/vault_git.py"

if ! command -v uv >/dev/null 2>&1; then
  echo "Erro: uv e obrigatorio para executar scripts Python do Workbench. Rode /mednotes:setup ou scripts/bootstrap_windows_python_uv.ps1." >&2
  exit 127
fi

exec uv run --project "$project_root" python "$core" "$@"

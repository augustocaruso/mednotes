"""LLM rewrite prompts emitted by the deterministic Wiki style validator."""
from __future__ import annotations

from typing import Any


def _issue_line(item: dict[str, Any]) -> str:
    details = []
    for key in ("section", "suggested_visual", "reason"):
        value = str(item.get(key) or "").strip()
        if value:
            details.append(f"{key}={value}")
    suffix = f" ({'; '.join(details)})" if details else ""
    return f"- {item['code']}: {item['message']}{suffix}"


def rewrite_prompt(title: str, errors: list[dict[str, Any]], warnings: list[dict[str, Any]]) -> str:
    issue_lines = "\n".join(_issue_line(item) for item in errors + warnings)
    visual_instruction = ""
    if any(item["code"] == "didactic_visual_opportunity" for item in errors + warnings):
        visual_instruction = (
            " Quando houver `didactic_visual_opportunity`, insira Mermaid ou equação "
            "somente na seção clínica correspondente, logo após o texto que justifica "
            "o visual; não crie seção genérica de diagramas/fórmulas e siga o "
            "material-fonte sem inventar relações, etapas, números, limiares ou fórmulas."
        )
    return (
        "Reescreva a nota temporária abaixo para cumprir o Modelo Wiki_Medicina "
        "de estudo para residência, sem inventar fatos novos além do material-fonte. "
        f"Preserve o título '# {title}', use headings ## com emoji semântico, inclua "
        "'## 🏁 Fechamento' com '### Resumo', '### Key Points' e "
        "'### Frase de Prova', inclua '## 🔗 Notas Relacionadas' e finalize com "
        "'## 🧬 Fontes Consolidadas' derivada do YAML chats[]. "
        f"{visual_instruction} "
        "Problemas encontrados:\n"
        f"{issue_lines}"
    )

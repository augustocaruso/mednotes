#!/usr/bin/env python3
"""Gera um .apkg de demonstração com os modelos `Medicina` e `Medicina Cloze`.

Lê os templates HTML/CSS de `bundle/docs/anki-templates/`, monta os dois
note types e adiciona alguns cards exemplares para inspeção visual no Anki
Desktop. Útil sempre que os templates mudarem e você quiser ver o resultado sem
mexer no deck real.

Uso:

    uv run --with genanki python ${extensionPath}/scripts/mednotes/flashcards/build_demo_apkg.py \\
        --output ~/Downloads/medicina-flashcards-demo.apkg
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import genanki  # type: ignore[import-error]  # optional dep, guarded by except below
except ModuleNotFoundError:  # pragma: no cover
    print(
        "Falta a dep `genanki`. Rode com: uv run --with genanki python ...",
        file=sys.stderr,
    )
    raise


TEMPLATES_DIR = Path(__file__).resolve().parents[4] / "docs" / "anki-templates"

# IDs estáveis pra Anki não tratar como modelo novo a cada import.
QA_MODEL_ID = 1726123001
CLOZE_MODEL_ID = 1726123002
DECK_ID = 1726123003

DECK_NAME = "Medicina::_Demo"
DEMO_OBSIDIAN = "obsidian://open?vault=Wiki_Medicina&file=Cardiologia%2FPonte_Miocardica.md"


def _read(name: str) -> str:
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8")


def build_qa_model(css: str) -> genanki.Model:
    return genanki.Model(
        QA_MODEL_ID,
        "Medicina",
        fields=[
            {"name": "Frente"},
            {"name": "Verso"},
            {"name": "Verso Extra"},
            {"name": "Obsidian"},
        ],
        templates=[
            {
                "name": "Card 1",
                "qfmt": _read("qa.front.html"),
                "afmt": _read("qa.back.html"),
            }
        ],
        css=css,
    )


def build_cloze_model(css: str) -> genanki.Model:
    return genanki.Model(
        CLOZE_MODEL_ID,
        "Medicina Cloze",
        fields=[
            {"name": "Texto"},
            {"name": "Verso Extra"},
            {"name": "Obsidian"},
        ],
        templates=[
            {
                "name": "Cloze",
                "qfmt": _read("cloze.front.html"),
                "afmt": _read("cloze.back.html"),
            }
        ],
        css=css,
        model_type=genanki.Model.CLOZE,
    )


def demo_notes(qa_model: genanki.Model, cloze_model: genanki.Model) -> list[genanki.Note]:
    return [
        genanki.Note(
            model=qa_model,
            fields=[
                "O que é ponte miocárdica?",
                "Trajeto intramiocárdico de uma artéria coronária epicárdica.",
                (
                    "<br><br>"
                    "<ul>"
                    "<li>Mais frequente na <strong>artéria descendente anterior</strong>.</li>"
                    "<li>Geralmente <em>assintomática</em>; pode causar isquemia em compressão sistólica intensa.</li>"
                    "<li>Diagnóstico padrão por angio-CT coronariana ou ICA.</li>"
                    "</ul>"
                ),
                DEMO_OBSIDIAN,
            ],
        ),
        genanki.Note(
            model=qa_model,
            fields=[
                "Qual o achado angiográfico clássico de ponte miocárdica?",
                "<em>Milking effect</em> — estreitamento sistólico transitório com normalização diastólica.",
                (
                    "<br><br>"
                    "Procurar segmento comprimido apenas na sístole. "
                    "Use <code>FFR</code> com dobutamina quando dúvida sobre repercussão funcional."
                ),
                DEMO_OBSIDIAN,
            ],
        ),
        genanki.Note(
            model=cloze_model,
            fields=[
                (
                    "A {{c1::ponte miocárdica}} envolve com mais frequência a "
                    "{{c2::artéria descendente anterior}} e tipicamente cursa com "
                    "{{c3::compressão sistólica}} do segmento tunelizado."
                ),
                (
                    "<br><br>"
                    "Cloze múltiplo demonstrando a Twenty Rules #5: três fatos "
                    "atômicos no mesmo contexto, cada um vira um card distinto."
                ),
                DEMO_OBSIDIAN,
            ],
        ),
        genanki.Note(
            model=cloze_model,
            fields=[
                (
                    "O diagnóstico de ponte miocárdica pode ser feito por "
                    "{{c1::angio-CT coronariana}} ou {{c2::angiografia invasiva}}, "
                    "sendo o achado clássico o {{c3::milking effect}}."
                ),
                "",
                DEMO_OBSIDIAN,
            ],
        ),
    ]


def build_package(output: Path) -> Path:
    css = _read("style.css")
    qa_model = build_qa_model(css)
    cloze_model = build_cloze_model(css)
    deck = genanki.Deck(DECK_ID, DECK_NAME)
    for note in demo_notes(qa_model, cloze_model):
        deck.add_note(note)
    package = genanki.Package(deck)
    output.parent.mkdir(parents=True, exist_ok=True)
    package.write_to_file(str(output))
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=str(Path.home() / "Downloads" / "medicina-flashcards-demo.apkg"),
        help="caminho do .apkg de saída (default: ~/Downloads/medicina-flashcards-demo.apkg)",
    )
    args = parser.parse_args(argv)

    output = build_package(Path(args.output).expanduser())
    print(f"OK: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

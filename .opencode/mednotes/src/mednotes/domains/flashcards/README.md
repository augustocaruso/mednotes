# Flashcards Domain

Dominio dos utilitarios determinísticos de `/flashcards`. A lógica real vive
neste pacote; os scripts no diretório pai permanecem como aliases públicos de
CLI.

Módulos reais:

- `sources.py`: resolve arquivos, diretórios, globs, tags e manifests.
- `pipeline.py`: prepara/aplica plano de escrita.
- `index.py`: idempotência local e status de fontes.
- `report.py`: preview e relatório final.
- `model.py`: valida note type/campos do Anki.
- `sync_rules.py`: compara/atualiza a cópia local das Twenty Rules.

Aliases públicos preservados:

- `../flashcard_sources.py`
- `../flashcard_pipeline.py`
- `../flashcard_report.py`
- `../flashcard_index.py`
- `../anki_model_validator.py`
- `../sync_anki_twenty_rules.py`

Novos utilitarios de cards devem entrar neste dominio. Scripts novos no pai
devem ser evitados, salvo quando forem aliases públicos de CLI.

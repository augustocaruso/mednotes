"""Minimal functional Textual review app for the PDF library."""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Footer, Static

from mednotes.domains.wiki.capabilities.pdf.tui import image_backend as image_backend_mod
from mednotes.domains.wiki.capabilities.pdf.tui.state import PdfLibraryState


class RenderableStatic(Static):
    @property
    def renderable(self) -> str:
        return str(self.content)


class PdfLibraryApp(App[None]):
    BINDINGS = [
        ("i", "ingest", "Ingest"),
        ("s", "search", "Search"),
        ("enter", "select_first", "Select"),
        ("p", "preview", "Preview"),
        ("d", "doctor", "Doctor"),
        ("q", "quit", "Quit"),
    ]

    CSS = """
    Screen { layout: vertical; }
    #screen-title { height: 3; padding: 1 2; text-style: bold; }
    #body { padding: 1 2; }
    """

    def __init__(self, *, state: PdfLibraryState | None = None, image_backend: str = "auto") -> None:
        super().__init__()
        self.state = state or PdfLibraryState()
        self.backend = image_backend_mod.detect(preferred=image_backend)

    def compose(self) -> ComposeResult:
        yield RenderableStatic(self._title(), id="screen-title")
        yield Static(self._body(), id="body")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh()

    def action_doctor(self) -> None:
        self.state.active_screen = "doctor"
        self._refresh()

    def action_ingest(self) -> None:
        self.state.active_screen = "ingest"
        self._refresh()

    def action_search(self) -> None:
        self.state.active_screen = "search"
        self._refresh()

    def action_select_first(self) -> None:
        if self.state.search_results:
            self.state.select_figure(self.state.search_results[0].figure_uid)
            self.state.active_screen = "figure_review"
        self._refresh()

    def action_preview(self) -> None:
        self.state.active_screen = "insert_preview"
        self._refresh()

    def _refresh(self) -> None:
        self.query_one("#screen-title", Static).update(self._title())
        self.query_one("#body", Static).update(self._body())

    def _title(self) -> str:
        labels = {
            "doctor": "Doctor / Setup",
            "ingest": "Ingest Queue",
            "search": "Search",
            "figure_review": "Figure Review",
            "insert_preview": "Insert Preview",
        }
        return labels.get(self.state.active_screen, self.state.active_screen)

    def _body(self) -> str:
        return (
            f"Backend: {self.backend.name}\n"
            f"Note: {self.state.selected_note or '-'}\n"
            f"Queued PDFs: {len(self.state.ingest_queue)}\n"
            f"Results: {len(self.state.search_results)}\n"
            f"Selected figure: {self.state.selected_figure_uid or '-'}"
        )

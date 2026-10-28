"""Keyboard-operable scroll regions shared by result and review screens."""

from typing import ClassVar

from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import DataTable


class PreviewTable(DataTable):
    """Publish viewport changes after the table receives its actual geometry."""

    class ViewportChanged(Message):
        pass

    def on_resize(self) -> None:
        self.post_message(self.ViewportChanged())


class ScrollPanel(Vertical):
    """A tab-stop scroll region for SQL, local errors, and reviewed metadata."""

    can_focus = True
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("up", "scroll_up", show=False),
        Binding("down", "scroll_down", show=False),
        Binding("left", "scroll_left", show=False),
        Binding("right", "scroll_right", show=False),
        Binding("pageup", "page_up", show=False),
        Binding("pagedown", "page_down", show=False),
    ]

    def action_page_up(self) -> None:
        self.scroll_page_up()

    def action_page_down(self) -> None:
        self.scroll_page_down()

"""Arrow-key menus; internal action values do not depend on display text."""

from io import StringIO

import questionary
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.layout import HSplit, Layout, ScrollablePane, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output import ColorDepth
from questionary import Choice
from questionary.prompts.common import InquirerControl
from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

INSTRUCTIONS = "↑↓ 选择, Enter 确认, Esc 返回"
_page_header: tuple[str | None, str | None] = (None, None)


MENU_STYLE = questionary.Style(
    [
        ("qmark", "fg:#00afff bold"),
        ("question", "bold"),
        ("pointer", "fg:#00afff bold"),
        ("highlighted", "fg:#ffffff bg:#005f87 bold"),
        ("answer", "fg:#00afff bold"),
        ("disabled", "fg:#808080"),
    ]
)


def render_page(title: str | None = None, note: str | None = None) -> None:
    """Prepare the header for the next full-screen page."""
    global _page_header
    _page_header = (title, note)


def select_menu(
    choices: list[Choice],
    *,
    title: str | None = None,
    note: str | None = None,
    default: str | None = None,
) -> str | None:
    """Use shared styling and return quietly when Esc or Ctrl+C is pressed."""
    render_page(title, note)
    prompt = questionary.select(
        INSTRUCTIONS,
        choices=choices,
        default=default,
        style=MENU_STYLE,
        qmark="",
        pointer=">",
        instruction=" ",
        color_depth=ColorDepth.DEPTH_8_BIT,
        erase_when_done=True,
    )
    # Questionary also marks `default` as a checked item. In a single-choice
    # menu it should only restore the cursor, not leave a second selection.
    for control in prompt.application.layout.find_all_controls():
        if isinstance(control, InquirerControl):
            control.selected_options.clear()
    return ask_prompt(prompt)


def ask_prompt(prompt: questionary.Question):
    """Esc returns locally; Ctrl+C propagates to the application entry point."""
    # All pages own their full-screen lifecycle, including the first main menu.
    title, note = _page_header
    parts: list[Panel | Text] = [
        Panel.fit(
            "GalleryPack 漫画下载",
            border_style="cyan",
            padding=(0, 2),
            style="bold cyan",
        )
    ]
    if title:
        parts.append(Text(title, style="bold"))
    if note:
        parts.append(Text(note, style="yellow"))
    stream = StringIO()
    Console(
        file=stream,
        force_terminal=True,
        color_system="256",
        width=prompt.application.output.get_size().columns,
    ).print(Group(*parts))
    old_layout = prompt.application.layout
    prompt.application.layout = Layout(
        ScrollablePane(
            HSplit(
                [
                    Window(
                        FormattedTextControl(ANSI(stream.getvalue().rstrip("\r\n"))),
                        dont_extend_height=True,
                    ),
                    old_layout.container,
                ]
            ),
            display_arrows=False,
        ),
        focused_element=old_layout.current_control,
    )
    prompt.application.full_screen = True
    prompt.application.renderer.full_screen = True
    bindings = KeyBindings()

    @bindings.add("escape", eager=True)
    def cancel(event):
        event.app.exit(result=None)

    @bindings.add("c-c", eager=True)
    def interrupt(event):
        event.app.exit(exception=KeyboardInterrupt)

    existing = prompt.application.key_bindings
    prompt.application.key_bindings = (
        merge_key_bindings([existing, bindings]) if existing is not None else bindings
    )
    # Esc is also the prefix of terminal escape sequences. Avoid the default
    # wait while still allowing complete arrow-key sequences to be decoded.
    prompt.application.ttimeoutlen = 0.05
    prompt.application.timeoutlen = 0.05
    try:
        return prompt.unsafe_ask()
    except EOFError:
        return None


def main_menu(default: str | None = None) -> str | None:
    """Select a platform or application action; cancellation means exit."""
    return select_menu(
        [
            Choice("E-Hentai", value="eh"),
            Choice("禁漫天堂", value="jm"),
            Choice("设置", value="settings"),
            Choice("退出", value="exit"),
        ],
        default=default,
    )

"""Small same-page forms using the existing menu style and keyboard handling."""

from collections.abc import Callable

import questionary
from prompt_toolkit.application import Application, get_app
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.widgets import TextArea

from gallerypack.ui.menus import MENU_STYLE, ask_prompt


def _navigation() -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("up", eager=True)
    @bindings.add("s-tab", eager=True)
    def previous(event):
        event.app.layout.focus_previous()

    @bindings.add("down", eager=True)
    @bindings.add("tab", eager=True)
    def following(event):
        event.app.layout.focus_next()

    return bindings


def _choice(label: str) -> FormattedTextControl:
    """A focusable action row, highlighted like the selected menu item."""
    control = FormattedTextControl(focusable=True)

    def tokens() -> FormattedText:
        focused = get_app().layout.has_focus(control)
        return FormattedText(
            [
                (
                    "class:highlighted" if focused else "",
                    ("> " if focused else "  ") + label,
                )
            ]
        )

    control.text = tokens
    return control


def _field(value: str, *, label: str = "") -> TextArea:
    def prefix() -> FormattedText:
        focused = get_app().layout.has_focus(field)
        return FormattedText(
            [("class:pointer" if focused else "", ("> " if focused else "  ") + label)]
        )

    field = TextArea(text=value, multiline=False, height=1, prompt=prefix)
    field.buffer.cursor_position = len(value)
    return field


def proxy_form(current: str, validate: Callable[[str], bool | str]) -> str | None:
    """Up/down chooses direct access or the editable proxy on the same page."""
    direct = _choice("直连")
    address = _field(current, label="输入代理地址: ")
    error = {"text": ""}
    bindings = _navigation()

    @bindings.add("enter", eager=True)
    def confirm(event):
        if event.app.layout.has_focus(direct):
            event.app.exit(result="")
            return
        value = address.text.strip()
        if not value:
            error["text"] = "代理地址不能为空."
            return
        result = validate(value)
        if result is True:
            event.app.exit(result=value)
        else:
            error["text"] = str(result)

    application = Application(
        layout=Layout(
            HSplit(
                [
                    Window(FormattedTextControl("代理地址"), height=1),
                    Window(direct, height=1),
                    address,
                    Window(
                        FormattedTextControl(
                            lambda: FormattedText([("fg:red", error["text"])])
                        ),
                        height=1,
                    ),
                ]
            ),
            focused_element=address if current else direct,
        ),
        key_bindings=bindings,
        style=MENU_STYLE,
        color_depth=ColorDepth.DEPTH_8_BIT,
        erase_when_done=True,
    )
    return ask_prompt(questionary.Question(application))

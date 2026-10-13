"""Multiline paste-friendly input and a fixed-header download dashboard."""

import asyncio
import re
from io import StringIO
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.mouse import load_mouse_bindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import (
    ConditionalContainer,
    HSplit,
    Layout,
    ScrollablePane,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import ConditionalMargin, ScrollbarMargin
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.styles import DynamicStyle, Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Frame, TextArea
from rich.console import Console, Group
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from gallerypack.config import Config, resolve_output_dir
from gallerypack.downloaders.common import Cancelled, DownloadError
from gallerypack.downloaders.eh import (
    Downloader as EHDownloader,
)
from gallerypack.downloaders.eh import (
    normalize_links as eh_links,
)
from gallerypack.downloaders.jm import (
    Downloader as JMDownloader,
)
from gallerypack.downloaders.jm import (
    normalize_links as jm_links,
)
from gallerypack.naming import NamingError, name_size, validate_name
from gallerypack.ui.menus import MENU_STYLE


def title_panel():
    return Panel.fit(
        "GalleryPack 漫画下载", border_style="cyan", padding=(0, 2), style="bold cyan"
    )


def render_ansi(renderable, width=80) -> ANSI:
    stream = StringIO()
    Console(file=stream, force_terminal=True, color_system="256", width=width).print(
        renderable
    )
    return ANSI(stream.getvalue().rstrip("\r\n"))


def input_links(platform: str = "eh") -> list[str] | None:
    platform_title = "E-Hentai" if platform == "eh" else "禁漫天堂"
    normalize_links = eh_links if platform == "eh" else jm_links
    field = TextArea(
        multiline=True, wrap_lines=True, height=Dimension(min=1, preferred=10)
    )
    field.window.right_margins = [
        ConditionalMargin(
            ScrollbarMargin(display_arrows=False),
            Condition(
                lambda: bool(
                    field.window.render_info
                    and sum(
                        field.window.render_info.get_height_for_line(line)
                        for line in range(field.window.render_info.content_height)
                    )
                    > field.window.render_info.window_height
                )
            ),
        )
    ]
    error = {"text": ""}
    bindings = KeyBindings()

    @bindings.add("enter")
    def newline(event):
        field.buffer.insert_text("\n")

    @bindings.add("escape", "enter", eager=True)
    def start(event):
        try:
            event.app.exit(result=normalize_links(field.text))
        except DownloadError as exc:
            error["text"] = str(exc)

    @bindings.add("escape")
    def back(event):
        event.app.exit(result=None)

    @bindings.add("c-c")
    def interrupt(event):
        event.app.exit(exception=KeyboardInterrupt)

    app = Application(
        layout=Layout(
            HSplit(
                [
                    Window(
                        FormattedTextControl(
                            render_ansi(
                                Group(
                                    title_panel(),
                                    Text(platform_title, style="bold cyan"),
                                )
                            )
                        ),
                        height=Dimension(min=0, preferred=4, max=4),
                    ),
                    Window(
                        FormattedTextControl(
                            (
                                "每行一个链接. "
                                if platform == "eh"
                                else "每行一个编号或链接. "
                            )
                            + "Enter 换行, Alt+Enter 开始下载, Esc 返回."
                        ),
                        height=1,
                    ),
                    VSplit(
                        [
                            Frame(
                                field,
                                title=lambda: (
                                    (
                                        "画廊链接"
                                        if platform == "eh"
                                        else "漫画编号或链接"
                                    )
                                    if app.output.get_size().columns >= 18
                                    else ""
                                ),
                                width=Dimension(max=58),
                            ),
                            Window(
                                width=Dimension(min=0, preferred=0),
                                dont_extend_height=True,
                            ),
                        ]
                    ),
                    Window(
                        FormattedTextControl(
                            lambda: FormattedText([("fg:red", error["text"])])
                        ),
                        height=1,
                    ),
                ]
            ),
            focused_element=field,
        ),
        key_bindings=bindings,
        style=MENU_STYLE,
        full_screen=True,
        color_depth=ColorDepth.DEPTH_8_BIT,
    )
    app.ttimeoutlen = 0.05
    app.timeoutlen = 0.05
    try:
        return app.run()
    except EOFError:
        return None


def download_dashboard(
    links: list[str], config: Config, output: Path, platform: str = "eh"
):
    platform_title = "E-Hentai" if platform == "eh" else "禁漫天堂"
    downloader_type = EHDownloader if platform == "eh" else JMDownloader
    cancelled, lock = Event(), Lock()
    state: dict[str, Any] = {
        "completed": 0,
        "target_total": len(links),
        "failed": 0,
        "title": "准备下载",
        "done": 0,
        "total": 0,
        "phase": "",
        "finished": False,
        "errors": [],
        "name_request": None,
    }
    bindings = KeyBindings()

    waiting_for_name = Condition(lambda: state["name_request"] is not None)
    name_field = TextArea(
        multiline=True,
        wrap_lines=True,
        height=Dimension(min=1),
        dont_extend_height=True,
    )
    name_error = {"text": ""}

    def name_length():
        request = state["name_request"]
        return (name_size(name_field.text), request["limit"] if request else 0)

    def name_style():
        current, maximum = name_length()
        return "fg:red" if current > maximum else "fg:cyan"

    def length_text():
        current, maximum = name_length()
        return FormattedText([(name_style(), f"长度: {current}/{maximum}")])

    def view():
        with lock:
            data = dict(state)
            errors = list(state["errors"])
        width = max(20, app.output.get_size().columns - 1)
        stats = Table.grid(expand=True)
        stats.add_column(ratio=1)
        stats.add_column(ratio=1)
        stats.add_row(
            Text(f"已完成: {data['completed']}/{data['target_total']}", style="bold"),
            Text(f"失败: {data['failed']}", style="bold"),
        )
        total_done = data["completed"]
        overall = Panel(
            Group(
                stats,
                ProgressBar(
                    total=data["target_total"],
                    completed=total_done,
                    complete_style="cyan",
                ),
            ),
            title="画廊进度",
            border_style="cyan",
        )
        current = Panel(
            Group(
                Text(data["phase"] or "正在下载", style="cyan"),
                Text(data["title"], style="bold", overflow="fold"),
                Text(f"{data['done']}/{data['total']} 页"),
                ProgressBar(
                    total=max(1, data["total"]),
                    completed=data["done"],
                    complete_style="green",
                ),
            ),
            title="当前画廊",
            border_style="green",
        )
        items = [
            Panel(
                Text("\n".join(f"- {url}" for url in links)),
                title="下载链接",
                border_style="dim cyan",
            ),
            overall,
            current,
        ]
        if errors:
            items.append(
                Panel(
                    Text("\n".join(errors), style="red"),
                    title="失败记录",
                    border_style="red",
                )
            )
        return render_ansi(Group(*items), width)

    def footer_text():
        with lock:
            finished = state["finished"]
            waiting = state["name_request"] is not None
        if waiting:
            return "Enter 确认名称, Esc 停止下载, Ctrl+C 退出. 滚轮查看进度, Shift 拖选复制."
        return (
            "下载结束. Enter 或 Esc 返回. ↑↓ / PageUp / PageDown 滚动."
            if finished
            else "↑↓ 滚动. Esc 停止下载, Ctrl+C 退出."
        )

    def footer_height():
        columns = max(1, app.output.get_size().columns)
        return max(1, (get_cwidth(footer_text()) + columns - 1) // columns)

    def scroll(amount):
        size = app.output.get_size()
        height = pane.content.preferred_height(
            max(1, size.columns - 1), 10000
        ).preferred
        maximum = max(0, height - pane.viewport_height)
        pane.vertical_scroll = max(0, min(maximum, pane.vertical_scroll + amount))

    control = FormattedTextControl(view, focusable=True)

    name_dialog = ConditionalContainer(
        Frame(
            HSplit(
                [
                    Window(
                        FormattedTextControl("名称过长. 请修改文件夹名称:"),
                        wrap_lines=True,
                        dont_extend_height=True,
                    ),
                    name_field,
                    Window(FormattedTextControl(length_text), height=1),
                    Window(
                        FormattedTextControl(
                            lambda: FormattedText([("fg:red", name_error["text"])])
                        ),
                        height=1,
                    ),
                ]
            ),
            title=lambda: (
                "请输入较短名称" if app.output.get_size().columns >= 26 else ""
            ),
        ),
        filter=waiting_for_name,
    )

    class ProgressPane(ScrollablePane):
        viewport_height = 1

        def write_to_screen(
            self,
            screen,
            mouse_handlers,
            write_position,
            parent_style,
            erase_bg,
            z_index,
        ):
            self.viewport_height = max(1, write_position.height)
            super().write_to_screen(
                screen, mouse_handlers, write_position, parent_style, erase_bg, z_index
            )

    pane = ProgressPane(
        HSplit(
            [
                Window(control, dont_extend_height=True, always_hide_cursor=True),
                name_dialog,
            ]
        ),
        keep_cursor_visible=False,
        keep_focused_window_visible=False,
        display_arrows=False,
    )

    default_mouse = load_mouse_bindings()

    @bindings.add(Keys.Vt100MouseEvent, filter=waiting_for_name, eager=True)
    def wheel(event):
        match = re.fullmatch(r"\x1b\[<(\d+);\d+;\d+[Mm]", event.data)
        if match and int(match[1]) & 64:
            scroll(3 if int(match[1]) & 1 else -3)
            return
        # Retain normal input-field clicks and selection for non-wheel events.
        for binding in default_mouse.get_bindings_for_keys((Keys.Vt100MouseEvent,)):
            binding.handler(event)

    # VS Code translates wheel movement into arrow keys in the alternate screen
    # when mouse capture is off. Keep these bindings active during name editing.
    @bindings.add("up", eager=True)
    def up(event):
        scroll(-1)

    @bindings.add("down", eager=True)
    def down(event):
        scroll(1)

    @bindings.add("pageup", eager=True)
    def page_up(event):
        scroll(-max(1, pane.viewport_height - 1))

    @bindings.add("pagedown", eager=True)
    def page_down(event):
        scroll(max(1, pane.viewport_height - 1))

    @bindings.add("home", filter=~waiting_for_name)
    def top(event):
        scroll(-10000)

    @bindings.add("end", filter=~waiting_for_name)
    def bottom(event):
        scroll(10000)

    @bindings.add("escape", eager=True)
    def stop(event):
        with lock:
            finished = state["finished"]
            if not finished:
                state["phase"] = "正在停止并保存进度"
        if finished:
            event.app.exit()
        else:
            cancelled.set()
            event.app.invalidate()

    @bindings.add("enter", eager=True)
    def finish(event):
        with lock:
            finished = state["finished"]
            request = state["name_request"]
        if request is not None:
            try:
                name = validate_name(name_field.text.strip(), request["limit"])
                path = output / name
                if path.is_symlink() or (
                    path.exists() and (not path.is_dir() or any(path.iterdir()))
                ):
                    raise NamingError("同名路径已存在, 请更换名称.")
            except (NamingError, OSError) as exc:
                name_error["text"] = (
                    str(exc) if isinstance(exc, NamingError) else "无法检查目录."
                )
                return
            request["result"] = name
            with lock:
                state["name_request"] = None
                state["phase"] = "正在扫描图片页"
            event.app.layout.focus(control)
            request["ready"].set()
            event.app.invalidate()
            return
        if finished:
            event.app.exit()

    @bindings.add("c-c")
    def interrupt(event):
        cancelled.set()
        event.app.exit(exception=KeyboardInterrupt)

    app = Application(
        layout=Layout(
            HSplit(
                [
                    Window(
                        FormattedTextControl(
                            render_ansi(
                                Group(
                                    title_panel(),
                                    Text(platform_title, style="bold cyan"),
                                )
                            )
                        ),
                        height=Dimension(min=0, preferred=4, max=4),
                    ),
                    pane,
                    Window(
                        FormattedTextControl(footer_text),
                        height=lambda: Dimension(
                            min=1, preferred=footer_height(), max=footer_height()
                        ),
                        wrap_lines=True,
                        style="fg:#888888",
                    ),
                ]
            ),
            focused_element=control,
        ),
        key_bindings=bindings,
        full_screen=True,
        mouse_support=waiting_for_name,
        style=DynamicStyle(
            lambda: Style.from_dict(
                {"frame.border": name_style(), "frame.label": name_style()}
            )
        ),
        color_depth=ColorDepth.DEPTH_8_BIT,
    )
    app.ttimeoutlen = 0.05
    app.timeoutlen = 0.05

    def follow_name(buffer):
        if waiting_for_name():
            scroll(10000)
            app.invalidate()

    name_field.buffer.on_text_changed += follow_name

    def start():
        loop = asyncio.get_running_loop()

        def refresh():
            if not app.is_done:
                app.invalidate()

        def update(**values):
            with lock:
                state.update(values)
            try:
                loop.call_soon_threadsafe(refresh)
            except RuntimeError:
                pass

        def choose_name(full_name: str, limit: int) -> str:
            ready = Event()
            request: dict[str, Any] = {
                "name": full_name,
                "limit": limit,
                "ready": ready,
                "result": None,
            }

            def open_form():
                if app.is_done or cancelled.is_set():
                    return
                name_field.text = full_name
                name_field.buffer.cursor_position = 0
                name_error["text"] = ""
                app.layout.focus(name_field)
                scroll(10000)
                app.invalidate()

            update(name_request=request, phase="等待输入短名称")
            try:
                loop.call_soon_threadsafe(open_form)
                while not ready.wait(0.05):
                    if cancelled.is_set():
                        raise Cancelled("已取消.")
                if cancelled.is_set():
                    raise Cancelled("已取消.")
                return request["result"]
            finally:
                with lock:
                    if state["name_request"] is request:
                        state["name_request"] = None

        def worker():
            downloader = downloader_type(
                config, output, cancelled, choose_name=choose_name
            )
            targets, preparation_errors = list(links), {}
            if platform == "jm":
                try:
                    targets, preparation_errors = downloader.prepare_targets(
                        links, update
                    )
                    update(target_total=len(targets))
                except Cancelled:
                    update(finished=True, phase="已结束")
                    loop.call_soon_threadsafe(
                        lambda: app.exit() if not app.is_done else None
                    )
                    return
                except (
                    OSError,
                    ValueError,
                    KeyError,
                    TypeError,
                    AttributeError,
                    IndexError,
                    RuntimeError,
                ):
                    preparation_errors = {
                        link: "无法读取 JM 章节列表." for link in links
                    }
            existing = set()
            update(title="正在校验已有下载", phase="准备下载")
            try:
                for link in targets:
                    if link not in preparation_errors and downloader.is_complete(link):
                        existing.add(link)
                        update(completed=len(existing))
            except Cancelled:
                pass
            except OSError:
                pass  # The regular download path will report directory errors.
            for number, link in enumerate(targets, 1):
                if cancelled.is_set():
                    break
                if link in existing:
                    continue
                update(title=f"画廊 {number}", done=0, total=0, phase="准备下载")
                try:
                    if link in preparation_errors:
                        raise DownloadError(preparation_errors[link])
                    downloader.download(link, update)
                    with lock:
                        state["completed"] += 1
                except Cancelled:
                    break
                except (
                    DownloadError,
                    OSError,
                    ValueError,
                    KeyError,
                    TypeError,
                    AttributeError,
                    IndexError,
                    RuntimeError,
                ) as exc:
                    with lock:
                        state["failed"] += 1
                        state["completed"] += 1
                        state["errors"].append(
                            f"{number}. {link}\n   {str(exc) if isinstance(exc, DownloadError) else '文件处理失败.'}"
                        )
                update()
            update(finished=True, phase="已结束")
            if cancelled.is_set():
                try:
                    loop.call_soon_threadsafe(
                        lambda: app.exit() if not app.is_done else None
                    )
                except RuntimeError:
                    pass

        Thread(target=worker, daemon=True).start()

    app.pre_run_callables.append(start)
    try:
        app.run()
    except EOFError:
        pass
    finally:
        cancelled.set()


def eh_download(config: Config, base_dir: Path):
    links = input_links()
    if links is not None:
        download_dashboard(links, config, resolve_output_dir(base_dir, config))


def jm_download(config: Config, base_dir: Path):
    links = input_links("jm")
    if links is not None:
        download_dashboard(links, config, resolve_output_dir(base_dir, config), "jm")

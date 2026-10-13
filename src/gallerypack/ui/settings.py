"""Edit one setting at a time and save immediately after confirmation."""

from copy import deepcopy
from pathlib import Path

import questionary
from prompt_toolkit.output import ColorDepth
from questionary import Choice

from gallerypack.config import (
    Config,
    ConfigError,
    default_config,
    normalize_proxy,
    save_config,
    validate_config,
)
from gallerypack.ui.forms import proxy_form
from gallerypack.ui.menus import (
    INSTRUCTIONS,
    MENU_STYLE,
    ask_prompt,
    render_page,
    select_menu,
)


def _input(message: str, *, default: str = "", validate=None) -> str | None:
    """Keep the field label above the editable value."""
    return ask_prompt(
        questionary.text(
            message + "\n",
            default=default,
            validate=validate,
            qmark="",
            style=MENU_STYLE,
            instruction="",
            color_depth=ColorDepth.DEPTH_8_BIT,
            erase_when_done=True,
        )
    )


def _save_changes(config: Config, candidate: Config, base_dir: Path) -> str | None:
    """Only update live settings if saving the confirmed candidate succeeds."""
    try:
        save_config(base_dir, candidate)
    except ConfigError as exc:
        return f"保存失败: {exc}"
    except OSError:
        return "保存失败: 请检查运行目录权限和磁盘空间. 原设置未修改."
    config.clear()
    config.update(candidate)
    return None


def _edit_field(
    config: Config,
    section: dict,
    key: str,
    label: str,
    *,
    title: str,
    integer: bool = False,
) -> bool:
    previous = section[key]

    def check(value: str):
        try:
            if integer:
                try:
                    section[key] = int(value)
                except ValueError:
                    # Let the same range validator handle non-numeric input.
                    section[key] = value
            else:
                section[key] = value
            validate_config(config)
            return True
        except ConfigError as exc:
            return str(exc)
        finally:
            section[key] = previous

    render_page(title, INSTRUCTIONS)
    value = _input(label, default=str(previous), validate=check)
    if value is None:
        return False
    section[key] = int(value) if integer else value
    return True


def _edit_proxy(config: Config) -> bool:
    common = config["global"]
    previous = common["proxy"]

    def check(value: str) -> bool | str:
        try:
            common["proxy"] = normalize_proxy(value)
            validate_config(config)
            return True
        except ConfigError as exc:
            return str(exc)
        finally:
            common["proxy"] = previous

    render_page("全局设置", INSTRUCTIONS)
    value = proxy_form(previous, check)
    if value is None:
        return False
    common["proxy"] = normalize_proxy(value)
    return True


def _global_menu(config: Config, base_dir: Path) -> None:
    note = None
    selected = None
    labels = {
        "output_dir": "下载目录",
        "retries": "重试次数",
        "timeout_seconds": "超时时间(秒)",
    }
    while True:
        common = config["global"]
        action = select_menu(
            [
                Choice(f"下载目录: {common['output_dir']}", value="output_dir"),
                Choice(f"重试次数: {common['retries']}", value="retries"),
                Choice(
                    f"超时时间(秒): {common['timeout_seconds']}",
                    value="timeout_seconds",
                ),
                Choice("代理地址: " + (common["proxy"] or "直连"), value="proxy"),
                Choice(
                    "文件名过长处理: "
                    + (
                        "自动截断"
                        if common["long_name_policy"] == "truncate"
                        else "让我决定"
                    ),
                    value="long_name_policy",
                ),
                Choice("重置为默认值", value="reset"),
            ],
            title="全局设置",
            note=note,
            default=selected,
        )
        if action is None:
            return
        selected = action
        candidate = deepcopy(config)
        if action == "reset":
            candidate["global"] = default_config()["global"]
            changed = True
        elif action == "proxy":
            changed = _edit_proxy(candidate)
        elif action == "long_name_policy":
            value = select_menu(
                [Choice("让我决定", value="ask"), Choice("自动截断", value="truncate")],
                title="全局设置",
                default=common["long_name_policy"],
            )
            changed = value is not None
            if value is not None:
                candidate["global"]["long_name_policy"] = value
        else:
            changed = _edit_field(
                candidate,
                candidate["global"],
                action,
                labels[action],
                title="全局设置",
                integer=action != "output_dir",
            )
        note = _save_changes(config, candidate, base_dir) if changed else None


def _platform_menu(config: Config, name: str, base_dir: Path) -> None:
    title = "E-Hentai 设置" if name == "eh" else "禁漫天堂设置"
    note = None
    selected = None
    while True:
        platform = config["platforms"][name]
        choices = [
            Choice(
                f"同时下载的图片数量: {platform['image_workers']}",
                value="image_workers",
            )
        ]
        if name == "eh":
            choices.append(
                Choice(
                    "下载原图: " + ("开启" if platform["original_images"] else "关闭"),
                    value="original_images",
                )
            )
        if name == "jm":
            labels = {
                "merge": "全部章节, 合并保存",
                "chapter": "仅当前章节",
                "split": "全部章节, 分别保存",
            }
            choices.append(
                Choice(
                    f"章节下载模式: {labels[platform['chapter_mode']]}",
                    value="chapter_mode",
                )
            )
        choices.append(Choice("重置为默认值", value="reset"))
        action = select_menu(choices, title=title, note=note, default=selected)
        if action is None:
            return
        selected = action
        candidate = deepcopy(config)
        platform = candidate["platforms"][name]
        changed = False
        if action == "image_workers":
            changed = _edit_field(
                candidate,
                platform,
                action,
                "同时下载的图片数量",
                title=title,
                integer=True,
            )
        elif action == "chapter_mode":
            value = select_menu(
                [
                    Choice("全部章节, 分别保存", value="split"),
                    Choice("全部章节, 合并保存", value="merge"),
                    Choice("仅当前章节", value="chapter"),
                ],
                title="禁漫天堂章节下载模式",
                default=platform["chapter_mode"],
            )
            if value is not None:
                platform["chapter_mode"] = value
                changed = True
        elif action == "original_images":
            value = select_menu(
                [
                    Choice("关闭", value="normal"),
                    Choice("开启", value="original"),
                ],
                title="E-Hentai 下载原图",
            )
            if value is not None:
                platform["original_images"] = value == "original"
                changed = True
        elif action == "reset":
            candidate["platforms"][name] = default_config()["platforms"][name]
            changed = True
        note = _save_changes(config, candidate, base_dir) if changed else None


def settings_menu(config: Config, base_dir: Path) -> Config:
    """Esc navigates back; confirmed edits have already been saved."""
    selected = None
    while True:
        action = select_menu(
            [
                Choice("全局设置", value="global"),
                Choice("E-Hentai 设置", value="eh"),
                Choice("禁漫天堂设置", value="jm"),
            ],
            title="设置",
            default=selected,
        )
        if action is None:
            return config
        selected = action
        if action == "global":
            _global_menu(config, base_dir)
        else:
            _platform_menu(config, action, base_dir)

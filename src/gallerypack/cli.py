"""Application entry point and interactive navigation."""

import sys
from pathlib import Path

from rich.console import Console

from gallerypack.config import ConfigError, load_config
from gallerypack.ui.download import eh_download, jm_download
from gallerypack.ui.menus import main_menu
from gallerypack.ui.settings import settings_menu


def main() -> None:
    """Run the menu loop without starting a download implicitly."""
    console = Console()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        console.print("请在交互式终端中执行 uv run gallerypack.", markup=False)
        raise SystemExit(2)

    base_dir = Path.cwd().resolve()
    try:
        config = load_config(base_dir)
    except ConfigError as exc:
        console.print(f"配置加载失败: {exc}", markup=False)
        raise SystemExit(2) from None
    except OSError:
        console.print("无法读取或创建 config.json, 请检查运行目录权限.", markup=False)
        raise SystemExit(2) from None

    try:
        # Each page uses the alternate screen and restores the shell on exit.
        selected = None
        while True:
            action = main_menu(default=selected)
            if action is None or action == "exit":
                break
            selected = action
            if action == "jm":
                jm_download(config, base_dir)
            elif action == "eh":
                eh_download(config, base_dir)
            elif action == "settings":
                config = settings_menu(config, base_dir)
    except KeyboardInterrupt:
        console.print("已中断 GalleryPack.", markup=False)
        raise SystemExit(130) from None
    except EOFError:
        pass

    console.print("已退出 GalleryPack.", markup=False)


if __name__ == "__main__":
    main()

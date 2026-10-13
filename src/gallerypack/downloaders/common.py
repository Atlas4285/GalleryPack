"""Shared download errors, image validation and atomic metadata writes."""

import json
import re
from collections.abc import Callable
from pathlib import Path

from PIL import Image

from gallerypack.naming import folder_limit, name_size, truncate_name, validate_name


class DownloadError(RuntimeError):
    pass


class Cancelled(DownloadError):
    pass


class AccessError(DownloadError):
    """Do not keep requesting pages after an access or quota rejection."""


def image_extension(path: Path) -> str:
    with Image.open(path) as image:
        fmt = image.format
        image.verify()
    with Image.open(path) as image:
        image.load()
    extensions = {
        "JPEG": ".jpg",
        "PNG": ".png",
        "WEBP": ".webp",
        "GIF": ".gif",
        "AVIF": ".avif",
    }
    if fmt not in extensions:
        raise DownloadError("图片格式不支持.")
    return extensions[fmt]


def write_json(path: Path, value) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def new_directory(
    output: Path,
    identity: str,
    title: str,
    settings: dict,
    choose_name: Callable[[str, int], str] | None,
) -> Path:
    safe_title = re.sub(r'[\\/:*"<>|,?\x00-\x1f]', "_", title).rstrip(" .")
    full_name = f"{identity} - {safe_title}"
    limit = folder_limit(output)
    if limit < len(identity):
        raise DownloadError("下载目录过长, 请缩短下载目录路径.")
    name = full_name
    if name_size(name) > limit:
        if settings.get("long_name_policy", "ask") == "truncate":
            name = truncate_name(full_name, limit)
        elif choose_name is not None:
            name = choose_name(full_name, limit)
        else:
            raise DownloadError("名称过长, 请设置自动截断或输入短名称.")
    validate_name(name, limit)
    directory = output / name
    if (
        directory.is_symlink()
        or directory.exists()
        and (not directory.is_dir() or any(directory.iterdir()))
    ):
        raise DownloadError("同名路径已存在, 请更换名称.")
    return directory

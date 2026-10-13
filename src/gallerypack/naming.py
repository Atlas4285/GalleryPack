"""Filesystem-aware folder limits; original titles stay in metadata."""

import os
import re
from pathlib import Path


class NamingError(ValueError):
    """A short message safe to show in the name input form."""


def name_size(value: str) -> int:
    return (
        len(value.encode("utf-16-le")) // 2
        if os.name == "nt"
        else len(os.fsencode(value))
    )


def folder_limit(parent: Path) -> int:
    ancestor = parent.resolve()
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if os.name == "nt":
        # Leave space for the longest metadata filename and its temporary suffix.
        return min(255, 259 - name_size(str(parent.resolve())) - 1 - 1 - 13)
    try:
        name_max = os.pathconf(ancestor, "PC_NAME_MAX")
        path_max = os.pathconf(ancestor, "PC_PATH_MAX")
    except OSError, ValueError:
        name_max, path_max = 255, 4096
    return min(
        name_max if name_max > 0 else 255,
        (path_max if path_max > 0 else 4096)
        - name_size(str(parent.resolve()))
        - 1
        - 1
        - 13
        - 1,
    )


def validate_name(value: str, limit: int) -> str:
    if not value.strip():
        raise NamingError("名称不能为空.")
    if (
        value in (".", "..")
        or re.search(r'[\\/:*"<>|?\x00-\x1f]', value)
        or value.endswith((".", " "))
    ):
        raise NamingError("名称包含无效字符.")
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if value.split(".", 1)[0].upper() in reserved:
        raise NamingError("名称是系统保留名称.")
    if name_size(value) > limit:
        raise NamingError("名称仍然过长, 请继续缩短.")
    return value


def truncate_name(value: str, limit: int) -> str:
    if limit < 1:
        raise NamingError("下载目录过长, 请缩短下载目录路径.")
    while value and name_size(value) > limit:
        value = value[:-1]
    return validate_name(value.rstrip(" ."), limit)

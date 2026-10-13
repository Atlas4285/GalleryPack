"""Local settings, stored in config.json."""

import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

type Config = dict[str, Any]


class ConfigError(ValueError):
    """A configuration error whose message never includes credential values."""


def default_config() -> Config:
    """Return fresh defaults so editing one platform never changes another."""
    return {
        "global": {
            "output_dir": "./download",
            "retries": 3,
            "timeout_seconds": 90,
            "proxy": "",
            "long_name_policy": "ask",
        },
        "platforms": {
            "eh": {
                "image_workers": 3,
                "original_images": False,
            },
            "jm": {
                "image_workers": 8,
                "chapter_mode": "split",
            },
        },
    }


def _merge_defaults(defaults: Config, values: Config) -> Config:
    result = deepcopy(values)
    for key, default in defaults.items():
        if key not in result:
            result[key] = deepcopy(default)
        elif isinstance(default, dict) and isinstance(result[key], dict):
            result[key] = _merge_defaults(default, result[key])
    return result


def _object(value: Any, field: str) -> Config:
    if not isinstance(value, dict):
        raise ConfigError(f"{field}必须是 JSON 对象.")
    return value


def _string(value: Any, field: str, *, nonempty: bool = False) -> None:
    if not isinstance(value, str) or "\x00" in value:
        raise ConfigError(f"{field}必须是不含空字符的字符串.")
    if nonempty and not value.strip():
        raise ConfigError(f"{field}不能为空.")


def _integer(value: Any, field: str, minimum: int, maximum: int) -> None:
    # bool is a subclass of int, but is not a valid count or timeout.
    if type(value) is not int or not minimum <= value <= maximum:
        raise ConfigError(f"{field}必须是 {minimum}-{maximum} 的整数.")


def normalize_proxy(value: str) -> str:
    """Treat an address without a protocol as an HTTP proxy."""
    value = value.strip()
    if value and "://" not in value:
        value = "http://" + value
    return value


def validate_config(config: Config) -> None:
    """Validate complete settings without printing passwords or sessions."""
    config = _object(config, "配置")
    common = _object(config.get("global"), "全局设置")
    _string(common.get("output_dir"), "下载目录", nonempty=True)
    _integer(common.get("retries"), "重试次数", 0, 10)
    _integer(common.get("timeout_seconds"), "超时时间(秒)", 10, 300)
    if common.get("long_name_policy") not in ("truncate", "ask"):
        raise ConfigError("文件名过长处理必须是自动截断或让我决定.")
    proxy = common.get("proxy")
    _string(proxy, "代理地址")
    if proxy:
        proxy = normalize_proxy(proxy)
        try:
            url = urlsplit(proxy)
            valid = (
                url.scheme in {"http", "https", "socks5", "socks5h"}
                and bool(url.hostname)
                and (url.port is None or 1 <= url.port <= 65535)
                and url.path in {"", "/"}
                and not url.query
                and not url.fragment
                and not any(c.isspace() for c in proxy)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ConfigError(
                "代理地址必须是有效的 http/https/socks5/socks5h 代理 URL."
            )
    platforms = _object(config.get("platforms"), "平台设置")
    for name in ("eh", "jm"):
        prefix = "E-Hentai" if name == "eh" else "禁漫天堂"
        platform = _object(platforms.get(name), prefix)
        _integer(
            platform.get("image_workers"),
            f"{prefix}同时下载的图片数量",
            1,
            16 if name == "eh" else 32,
        )
        if name == "jm" and platform.get("chapter_mode") not in (
            "merge",
            "chapter",
            "split",
        ):
            raise ConfigError(
                "禁漫天堂章节下载模式必须是全部合并、仅当前章节或全部拆分."
            )
        if name == "eh" and type(platform.get("original_images")) is not bool:
            raise ConfigError(f"{prefix}下载原图选项必须是布尔值.")


def _complete_config(config: Config) -> Config:
    config = _merge_defaults(default_config(), _object(config, "配置"))
    validate_config(config)
    for platform in config["platforms"].values():
        if isinstance(platform, dict):
            platform.pop("auth", None)
    config["global"]["proxy"] = normalize_proxy(config["global"]["proxy"])
    return config


def save_config(base_dir: Path, config: Config) -> None:
    """Validate, write a private temporary file, then replace atomically."""
    config = _complete_config(config)
    try:
        content = (
            json.dumps(config, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )
    except TypeError, ValueError:
        raise ConfigError("配置包含无法保存为 JSON 的值.") from None
    path = Path(base_dir).resolve() / "config.json"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix="config.json.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            if os.name == "posix":
                os.chmod(temporary, 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_config(base_dir: Path) -> Config:
    """Create defaults on first run and preserve malformed existing files."""
    path = Path(base_dir).resolve() / "config.json"
    if not path.exists():
        config = default_config()
        save_config(base_dir, config)
        return config
    try:
        original = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"config.json: JSON 格式错误, 第 {exc.lineno} 行, 第 {exc.colno} 列."
        ) from None
    except UnicodeError:
        raise ConfigError("config.json: 请使用 UTF-8 编码.") from None
    config = _complete_config(original)
    if config != original:
        save_config(base_dir, config)
    return config


def resolve_output_dir(base_dir: Path, config: Config) -> Path:
    """Resolve relative output paths without creating a download directory."""
    validate_config(config)
    path = Path(config["global"]["output_dir"]).expanduser()
    if not path.is_absolute():
        path = Path(base_dir) / path
    return path.resolve()

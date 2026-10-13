"""Anonymous E-H gallery downloads with validated, resumable image files."""

import html
import json
import re
import time
from collections.abc import Callable
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
from threading import Event
from urllib.parse import urljoin, urlsplit

import requests
from PIL import Image

from gallerypack.config import Config, normalize_proxy
from gallerypack.downloaders.common import (
    AccessError,
    Cancelled,
    DownloadError,
    image_extension,
    new_directory,
    write_json,
)


def normalize_links(text: str) -> list[str]:
    links = []
    for number, line in enumerate(text.splitlines(), 1):
        value = line.strip()
        if not value:
            continue
        match = re.fullmatch(
            r"(?:https?://)?e-hentai\.org/g/([1-9]\d*)/([a-fA-F0-9]{10})/?(?:[?#].*)?",
            value,
        )
        if not match:
            raise DownloadError(f"第 {number} 行链接无效.")
        url = f"https://e-hentai.org/g/{match[1]}/{match[2].lower()}/"
        if url not in links:
            links.append(url)
    if not links:
        raise DownloadError("请填写画廊链接.")
    return links


class Page(HTMLParser):
    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.links: list[str] = []
        self.image = self.original = self.source = None
        match = re.search(r"showkey\s*=\s*['\"]([^'\"]+)", text, re.IGNORECASE)
        self.showkey = match[1] if match else None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div":
            if self.depth:
                self.depth += 1
            elif attrs.get("id") == "gdt":
                self.depth = 1
        if tag == "a" and attrs.get("href"):
            if self.depth:
                self.links.append(attrs["href"])
            if "fullimg.php" in attrs["href"]:
                self.original = attrs["href"]
        if tag == "img" and attrs.get("id") == "img":
            self.image = attrs.get("src")
        if attrs.get("id") == "loadfail":
            match = re.search(r"nl\(['\"]([^'\"]+)", attrs.get("onclick", ""))
            self.source = match[1] if match else None

    def handle_endtag(self, tag):
        if tag == "div" and self.depth:
            self.depth -= 1


class Downloader:
    def __init__(
        self,
        config: Config,
        output: Path,
        cancelled: Event,
        choose_name: Callable[[str, int], str] | None = None,
    ):
        self.common = config["global"]
        self.options = config["platforms"]["eh"]
        self.output = output
        self.cancelled = cancelled
        self.choose_name = choose_name

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise Cancelled("已取消.")

    def existing_directories(self, gid: int, token: str):
        if not self.output.is_dir():
            return
        for directory in self.output.iterdir():
            self.check_cancelled()
            if not directory.is_dir() or directory.is_symlink():
                continue
            try:
                with (directory / ".info").open(encoding="utf-8") as stream:
                    gallery = json.loads(stream.readline())
                if gallery.get("gid") == gid and gallery.get("token") == token:
                    yield directory
            except OSError, ValueError, AttributeError:
                continue

    def directory_for(self, gid: int, token: str, title: str) -> Path:
        existing = next(self.existing_directories(gid, token), None)
        if existing is not None:
            return existing
        return new_directory(
            self.output, str(gid), title, self.common, self.choose_name
        )

    def is_complete(self, url: str) -> bool:
        """Check a finished local gallery without making any network request."""
        parts = urlsplit(url).path.strip("/").split("/")
        gid, token = int(parts[1]), parts[2]
        for directory in self.existing_directories(gid, token):
            try:
                lines = (directory / ".info").read_text(encoding="utf-8").splitlines()
                gallery, pages = json.loads(lines[0]), json.loads(lines[1])
                count = int(gallery["fileCount"])
                if (
                    gallery["gid"] != gid
                    or gallery["token"] != token
                    or bool(gallery.get("downloadOrigImage"))
                    != self.options["original_images"]
                    or gallery.get("status") != 3
                    or gallery.get("completCount") != count
                    or count < 1
                    or len(pages) != count
                    or {p["ser"] for p in pages} != set(range(1, count + 1))
                ):
                    continue
                metadata = json.loads(
                    (directory / "info.json").read_text(encoding="utf-8")
                )
                if metadata.get("source") != url or not isinstance(
                    metadata.get("tags"), (dict, list)
                ):
                    continue
                names = set()
                for page in pages:
                    self.check_cancelled()
                    filename = page["filePath"]
                    if (
                        page["status"] != 3
                        or not isinstance(filename, str)
                        or Path(filename).name != filename
                    ):
                        raise ValueError("Incomplete page")
                    names.add(filename)
                    image_extension(directory / filename)
                if len(names) == count:
                    return True
            except Cancelled:
                raise
            except (
                OSError,
                ValueError,
                KeyError,
                IndexError,
                TypeError,
                AttributeError,
                DownloadError,
                Image.DecompressionBombError,
            ):
                continue
        return False

    def session(self) -> requests.Session:
        session = requests.Session()
        session.trust_env = False
        session.headers["User-Agent"] = "Mozilla/5.0 GalleryPack/0.1"
        proxy = normalize_proxy(self.common["proxy"])
        if proxy:
            session.proxies.update(http=proxy, https=proxy)
        return session

    def request(self, session, method, url, **kwargs):
        for attempt in range(self.common["retries"] + 1):
            self.check_cancelled()
            try:
                response = session.request(
                    method, url, timeout=(10, self.common["timeout_seconds"]), **kwargs
                )
                if response.status_code in (403, 429, 509):
                    status = response.status_code
                    response.close()
                    raise AccessError(f"访问受限 ({status}).")
                response.raise_for_status()
                if not kwargs.get("stream") and any(
                    marker in response.text.lower()
                    for marker in (
                        "cf-chl-",
                        "just a moment...",
                        "verify you are human",
                    )
                ):
                    response.close()
                    raise AccessError("需要浏览器验证.")
                return response
            except requests.RequestException as exc:
                if exc.response is not None:
                    status = exc.response.status_code
                    exc.response.close()
                    if status < 500:
                        raise DownloadError(f"请求失败 ({status}).") from None
                if attempt == self.common["retries"]:
                    raise DownloadError("网络请求失败.") from None
                if self.cancelled.wait(min(2**attempt, 8)):
                    self.check_cancelled()
        raise DownloadError("请求失败.")

    def download(self, url: str, progress: Callable[..., None]) -> None:
        parts = urlsplit(url).path.strip("/").split("/")
        gid, token = int(parts[1]), parts[2]
        with self.session() as session:
            progress(title="正在获取元数据", done=0, total=0)
            with self.request(
                session,
                "POST",
                "https://api.e-hentai.org/api.php",
                json={"method": "gdata", "gidlist": [[gid, token]], "namespace": 1},
            ) as response:
                try:
                    meta = response.json()["gmetadata"][0]
                    if meta.get("error") or int(meta["gid"]) != gid:
                        raise DownloadError("画廊元数据不可用.")
                    count = int(meta["filecount"])
                    title = html.unescape(meta["title"])
                    if count < 1:
                        raise DownloadError("画廊没有图片.")
                except ValueError, KeyError, IndexError, TypeError:
                    raise DownloadError("元数据响应无效.") from None
            progress(title=title, done=0, total=count, phase="正在扫描图片页")
            directory = self.directory_for(gid, token, title)
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise DownloadError("无法创建目录, 请检查路径长度或权限.") from None
            progress(title=title, done=0, total=count, phase="正在扫描图片页")
            previous, old = [], {}
            if (directory / ".info").exists():
                try:
                    lines = (
                        (directory / ".info").read_text(encoding="utf-8").splitlines()
                    )
                    old, previous = json.loads(lines[0]), json.loads(lines[1])
                    if (
                        not isinstance(old, dict)
                        or not isinstance(previous, list)
                        or not all(
                            isinstance(p, dict) and isinstance(p.get("ser"), int)
                            for p in previous
                        )
                    ):
                        raise DownloadError("已有 .info 无效.")
                    if (
                        old["gid"] != gid
                        or old["token"] != token
                        or bool(old.get("downloadOrigImage"))
                        != self.options["original_images"]
                    ):
                        raise DownloadError("已有下载的身份或原图模式不同.")
                except ValueError, IndexError, KeyError, TypeError:
                    raise DownloadError("已有 .info 无效.") from None
            links = {}
            pagination = 0
            while len(links) < count:
                self.check_cancelled()
                with self.request(
                    session, "GET", url, params={"p": pagination}
                ) as response:
                    parsed = Page(response.text)
                before = len(links)
                for href in parsed.links:
                    href = urljoin(url, href)
                    match = re.fullmatch(
                        r"/s/([a-fA-F0-9]+)/" + str(gid) + r"-(\d+)",
                        urlsplit(href).path,
                    )
                    if match and urlsplit(href).hostname == "e-hentai.org":
                        links[int(match[2])] = (href, match[1])
                if len(links) == before or pagination >= count:
                    raise DownloadError("图片页不完整或画廊无法访问.")
                pagination += 1
                progress(
                    title=title,
                    done=0,
                    total=count,
                    phase=f"正在扫描图片页 ({len(links)}/{count})",
                )
                if len(links) < count and self.cancelled.wait(0.3):
                    self.check_cancelled()
            if set(links) != set(range(1, count + 1)):
                raise DownloadError("图片页编号不完整.")
        gallery = {
            "gid": gid,
            "token": token,
            "url": url,
            "title": title,
            "dirPath": "",
            "fileCount": count,
            "completCount": 0,
            "status": 1,
            "coverImage": None,
            "addTime": old.get("addTime", int(time.time() * 1000)),
            "coverUrl": meta.get("thumb"),
            "rating": float(meta.get("rating", 0)),
            "category": meta.get("category"),
            "uploader": meta.get("uploader"),
            "jsonString": None,
            "tag": None,
            "downloadOrigImage": self.options["original_images"],
            "showKey": old.get("showKey"),
        }
        pages = [
            {
                "gid": gid,
                "token": links[n][1],
                "href": links[n][0],
                "sourceId": None,
                "imageUrl": "",
                "ser": n,
                "filePath": None,
                "status": 1,
            }
            for n in range(1, count + 1)
        ]
        saved = {p["ser"]: p for p in previous}
        for page in pages:
            record = saved.get(page["ser"], {})
            filename = record.get("filePath")
            if (
                filename
                and Path(filename).name == filename
                and record.get("token") == page["token"]
            ):
                try:
                    image_extension(directory / filename)
                    page.update(
                        filePath=filename,
                        sourceId=record.get("sourceId"),
                        imageUrl=record.get("imageUrl", ""),
                        status=3,
                    )
                except OSError, ValueError, DownloadError, Image.DecompressionBombError:
                    pass
        tags = {}
        for tag in meta.get("tags", []):
            namespace, separator, value = tag.partition(":")
            tags.setdefault(namespace if separator else "misc", []).append(
                value if separator else tag
            )
        write_json(
            directory / "info.json",
            {
                **meta,
                "title": title,
                "tags": tags,
                "category": str(meta.get("category", "")).lower(),
                "source": url,
            },
        )

        def save():
            gallery["completCount"] = sum(p["status"] == 3 for p in pages)
            gallery["status"] = 3 if gallery["completCount"] == count else 2
            gallery["coverImage"] = (
                pages[0]["filePath"] if pages[0]["status"] == 3 else None
            )
            temporary = directory / ".info.tmp"
            temporary.write_text(
                "\n".join(
                    json.dumps(x, ensure_ascii=False, separators=(",", ":"))
                    for x in (gallery, pages)
                )
                + "\n",
                encoding="utf-8",
            )
            temporary.replace(directory / ".info")
            progress(
                title=title, done=gallery["completCount"], total=count, phase="正在下载"
            )

        blocked = Event()

        def fetch_once(page):
            if blocked.is_set():
                raise AccessError("当前画廊已停止.")
            temporary = directory / f".{page['ser']:04d}.part"
            try:
                with self.session() as session:
                    with self.request(session, "GET", page["href"]) as response:
                        parsed = Page(response.text)
                    image_url = (
                        parsed.original
                        if self.options["original_images"]
                        else parsed.image
                    )
                    if not image_url:
                        raise DownloadError(
                            "原图无法访问."
                            if self.options["original_images"]
                            else "图片地址不可用."
                        )
                    image_url = urljoin(page["href"], image_url)
                    if re.search(r"/509s?\.gif(?:\?|$)", image_url):
                        raise AccessError("图片配额已耗尽.")
                    with (
                        self.request(
                            session,
                            "GET",
                            image_url,
                            headers={"Referer": page["href"]},
                            stream=True,
                        ) as response,
                        temporary.open("wb") as stream,
                    ):
                        for chunk in response.iter_content(256 * 1024):
                            self.check_cancelled()
                            if blocked.is_set():
                                raise AccessError("当前画廊已停止.")
                            stream.write(chunk)
                    extension = image_extension(temporary)
                    self.check_cancelled()
                    filename = f"{page['ser']:0{max(4, len(str(count)))}d}{extension}"
                    temporary.replace(directory / filename)
                    return {
                        **page,
                        "filePath": filename,
                        "status": 3,
                        "sourceId": parsed.source,
                        "imageUrl": image_url,
                    }, parsed.showkey
            except requests.RequestException:
                raise DownloadError("图片传输失败.") from None
            finally:
                temporary.unlink(missing_ok=True)

        def fetch(page):
            for attempt in range(self.common["retries"] + 1):
                self.check_cancelled()
                try:
                    return fetch_once(page)
                except OSError, ValueError, Image.DecompressionBombError:
                    if attempt == self.common["retries"]:
                        raise DownloadError("图片校验失败.") from None
                    if self.cancelled.wait(min(2**attempt, 8)):
                        self.check_cancelled()
            raise DownloadError("图片下载失败.")

        failures = 0
        access_error = None
        save()
        try:
            with ThreadPoolExecutor(max_workers=self.options["image_workers"]) as pool:
                futures = {
                    pool.submit(fetch, page): page
                    for page in pages
                    if page["status"] != 3
                }
                for future in as_completed(futures):
                    page = futures[future]
                    try:
                        record, showkey = future.result()
                        page.update(record)
                        if showkey:
                            gallery["showKey"] = showkey
                    except Cancelled:
                        self.cancelled.set()
                        for pending in futures:
                            pending.cancel()
                    except CancelledError:
                        pass
                    except AccessError as exc:
                        blocked.set()
                        access_error = str(exc)
                        for pending in futures:
                            pending.cancel()
                    except (
                        DownloadError,
                        OSError,
                        ValueError,
                        Image.DecompressionBombError,
                    ):
                        failures += 1
                    save()
        finally:
            save()
        self.check_cancelled()
        if access_error:
            raise AccessError(access_error)
        if failures:
            raise DownloadError(f"{failures} 页下载失败, 可重新执行续传.")

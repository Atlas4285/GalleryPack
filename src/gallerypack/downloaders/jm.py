"""Download individual JM chapters and retain raw tags in GalleryDL metadata."""

import hashlib
import html
import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Event, local
from urllib.parse import urlsplit

import jmcomic
from PIL import Image

from gallerypack.config import Config
from gallerypack.downloaders.common import (
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
        match = re.fullmatch(r"(?:JM)?([1-9]\d*)", value, re.IGNORECASE)
        if not match:
            match = re.fullmatch(
                r"(?:https?://)?(?:www\.)?(?:18comic\.(?:vip|org)|jmcomic\.(?:me|org)|18commic\.com)/(?:album|photo)/([1-9]\d*)(?:/[^\s?#]*)?(?:[?#][^\s]*)?",
                value,
                re.IGNORECASE,
            )
        if not match:
            raise DownloadError(f"第 {number} 行编号或链接无效.")
        link = f"https://18comic.vip/album/{match[1]}/"
        if link not in links:
            links.append(link)
    if not links:
        raise DownloadError("请填写漫画编号或链接.")
    return links


def build_client(settings: dict):
    proxy = settings["proxy"]
    return jmcomic.JmOption.construct(
        {
            "log": False,
            "client": {
                "impl": "api",
                "retry_times": settings["retries"],
                "postman": {
                    "type": "curl_cffi_session",
                    "meta_data": {
                        "impersonate": "chrome",
                        "trust_env": False,
                        "proxies": {"http": proxy, "https": proxy} if proxy else {},
                        "timeout": settings["timeout_seconds"],
                    },
                },
            },
        }
    ).build_jm_client()


class Downloader:
    def __init__(
        self,
        config: Config,
        output: Path,
        cancelled: Event,
        choose_name: Callable[[str, int], str] | None = None,
    ):
        self.common = config["global"]
        self.options = config["platforms"]["jm"]
        self.output, self.cancelled, self.choose_name = output, cancelled, choose_name
        self.mode = self.options.get("chapter_mode", "split")
        self.photos = {}

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise Cancelled("已取消.")

    def prepare_targets(self, links: list[str], progress):
        """Expand series into deduplicated chapter tasks before counting progress."""
        if self.mode != "split":
            return list(links), {}
        targets, errors = [], {}
        client = None
        try:
            for link in links:
                self.check_cancelled()
                identity = urlsplit(link).path.strip("/").split("/")[1]
                progress(title=f"JM{identity}", phase="正在读取章节列表")
                try:
                    if client is None:
                        client = build_client(self.common)
                    photo = client.get_photo_detail(identity)
                    self.photos[identity] = photo
                    chapters = photo.from_album.episode_list
                    if not chapters:
                        raise DownloadError("没有找到章节列表.")
                    expanded = [
                        f"https://18comic.vip/album/{chapter[0]}/"
                        for chapter in chapters
                    ]
                    for target in expanded:
                        if target not in targets:
                            targets.append(target)
                except (
                    jmcomic.JmcomicException,
                    OSError,
                    ValueError,
                    KeyError,
                    TypeError,
                    RuntimeError,
                    AttributeError,
                    IndexError,
                ):
                    self.check_cancelled()
                    if link not in targets:
                        targets.append(link)
                    errors[link] = "无法读取 JM 章节列表."
            return targets, errors
        finally:
            if client is not None:
                client.postman.session.close()

    def existing_directories(self, identity: str):
        if not self.output.is_dir():
            return
        for directory in self.output.iterdir():
            self.check_cancelled()
            if not directory.is_dir() or directory.is_symlink():
                continue
            try:
                metadata = json.loads(
                    (directory / "info.json").read_text(encoding="utf-8")
                )
                saved = metadata.get("_download", {})
                if saved.get("id") == identity and saved.get("scope") == (
                    "album" if self.mode == "merge" else "chapter"
                ):
                    yield directory, metadata
            except OSError, ValueError, AttributeError:
                continue

    def valid_page(self, directory: Path, page: dict) -> bool:
        name = page.get("filePath")
        if not isinstance(name, str) or not re.fullmatch(
            r"\d+\.(?:jpg|png|webp|gif|avif)", name
        ):
            return False
        path = directory / name
        try:
            self.check_cancelled()
            return (
                not path.is_symlink() and image_extension(path) == path.suffix.lower()
            )
        except OSError, ValueError, Image.DecompressionBombError:
            return False
        except Cancelled:
            raise
        except DownloadError:
            return False

    def is_complete(self, url: str) -> bool:
        identity = urlsplit(url).path.strip("/").split("/")[1]
        for directory, metadata in self.existing_directories(identity):
            saved = metadata["_download"]
            pages = saved.get("pages", [])
            if (
                saved.get("complete") is True
                and isinstance(pages, list)
                and pages
                and len(pages) == saved.get("page_count")
                and metadata.get("source") == url
                and isinstance(metadata.get("tags"), list)
                and all(
                    isinstance(p, dict)
                    and p.get("status") == "completed"
                    and (self.mode == "merge" or p.get("chapter_id") == identity)
                    and (self.mode == "merge" or p.get("chapter_page") == number)
                    and self.valid_page(directory, p)
                    for number, p in enumerate(pages, 1)
                )
                and len({p["filePath"] for p in pages}) == len(pages)
            ):
                return True
        return False

    def download(self, url: str, progress):
        self.check_cancelled()
        identity = urlsplit(url).path.strip("/").split("/")[1]
        clients = []
        try:
            client = build_client(self.common)
            clients.append(client)
            try:
                photo = self.photos.get(identity) or client.get_photo_detail(identity)
            except (
                jmcomic.JmcomicException,
                OSError,
                ValueError,
                KeyError,
                TypeError,
                RuntimeError,
                AttributeError,
                IndexError,
            ):
                self.check_cancelled()
                raise DownloadError("无法获取 JM 漫画信息.") from None
            self.check_cancelled()
            if str(photo.id) != identity:
                raise DownloadError("返回的章节编号与输入不一致.")
            album = photo.from_album
            photos = [photo]
            if self.mode == "merge":
                photos = [
                    photo
                    if str(chapter.id) == identity
                    else client.get_photo_detail(str(chapter.id))
                    for chapter in album
                ]
            images = [image for chapter in photos for image in chapter]
            if not images:
                raise DownloadError("该章节没有图片.")
            title = html.unescape(album.title if self.mode == "merge" else photo.title)
            progress(title=title, total=len(images), done=0, phase="正在扫描图片页")
            existing = next(self.existing_directories(identity), None)
            directory = (
                existing[0]
                if existing
                else new_directory(
                    self.output,
                    f"JM{identity}",
                    title + (" (全部章节)" if self.mode == "merge" else ""),
                    self.common,
                    self.choose_name,
                )
            )
            self.check_cancelled()
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise DownloadError("无法创建目录, 请检查路径长度或权限.") from None
            pages = [
                {
                    "chapter_id": str(chapter.id),
                    "chapter_page": n,
                    "source_image_name": image.filename,
                    "filePath": None,
                    "status": "pending",
                }
                for chapter in photos
                for n, image in enumerate(chapter, 1)
            ]
            fingerprint = hashlib.sha256(
                json.dumps(
                    [
                        [p["chapter_id"], p["chapter_page"], p["source_image_name"]]
                        for p in pages
                    ],
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            previous = existing[1]["_download"] if existing else {}
            if previous and previous.get("fingerprint") != fingerprint:
                raise DownloadError("章节图片列表已变化, 请使用新的下载目录.")
            old_pages = previous.get("pages", [])
            for number, page in enumerate(pages):
                if number < len(old_pages) and isinstance(old_pages[number], dict):
                    old = old_pages[number]
                    if (
                        old.get("chapter_id") == page["chapter_id"]
                        and old.get("chapter_page") == page["chapter_page"]
                        and old.get("source_image_name") == page["source_image_name"]
                        and self.valid_page(directory, old)
                    ):
                        page.update(filePath=old["filePath"], status="completed")
            # Preserve original values, without translating or inferring tags.
            tags = list(album.tags)
            for field, namespace in [
                ("authors", "artist"),
                ("works", "parody"),
                ("actors", "character"),
            ]:
                tags.extend(f"{namespace}:{value}" for value in getattr(album, field))
            tags.extend(["origin:jm", f"jm_id:{identity}"])
            metadata = {
                "id": identity,
                "album_id": str(album.id),
                "title": title,
                "album_title": album.title,
                "source": url,
                "tags": list(dict.fromkeys(tags)),
                "authors": list(album.authors),
                "works": list(album.works),
                "actors": list(album.actors),
                "description": album.description,
                "pub_date": album.pub_date,
                "update_date": album.update_date,
                "_download": {
                    "id": identity,
                    "scope": "album" if self.mode == "merge" else "chapter",
                    "chapters": [str(chapter.id) for chapter in photos],
                    "fingerprint": fingerprint,
                    "complete": False,
                    "page_count": len(pages),
                    "pages": pages,
                },
            }
            failures = 0

            def save():
                done = sum(p["status"] == "completed" for p in pages)
                metadata["_download"]["complete"] = done == len(pages)
                write_json(directory / "info.json", metadata)
                progress(done=done, total=len(pages), phase="正在下载")

            save()
            thread_clients = local()

            def fetch(number, image):
                self.check_cancelled()
                if not hasattr(thread_clients, "client"):
                    thread_clients.client = build_client(self.common)
                    clients.append(thread_clients.client)
                image_client = thread_clients.client
                temporary = directory / f".{number:04d}.part{image.img_file_suffix}"
                try:
                    # jmcomic decodes the website's scrambled images.
                    for attempt in range(self.common["retries"] + 1):
                        self.check_cancelled()
                        try:
                            image_client.download_by_image_detail(
                                image, str(temporary), decode_image=True
                            )
                            suffix = image_extension(temporary)
                            self.check_cancelled()
                            filename = (
                                f"{number:0{max(4, len(str(len(pages))))}d}{suffix}"
                            )
                            temporary.replace(directory / filename)
                            return filename
                        except Cancelled:
                            raise
                        except (
                            jmcomic.JmcomicException,
                            OSError,
                            ValueError,
                            KeyError,
                            TypeError,
                            RuntimeError,
                            AttributeError,
                            IndexError,
                        ):
                            temporary.unlink(missing_ok=True)
                            if attempt == self.common["retries"]:
                                raise DownloadError("图片下载或解码失败.") from None
                            self.cancelled.wait(min(2**attempt, 10))
                finally:
                    temporary.unlink(missing_ok=True)

            try:
                with ThreadPoolExecutor(
                    max_workers=self.options["image_workers"]
                ) as executor:
                    futures = {
                        executor.submit(fetch, n, image): n - 1
                        for n, image in enumerate(images, 1)
                        if pages[n - 1]["status"] != "completed"
                    }
                    for future in as_completed(futures):
                        try:
                            filename = future.result()
                        except Cancelled:
                            continue
                        except (
                            jmcomic.JmcomicException,
                            OSError,
                            ValueError,
                            KeyError,
                            TypeError,
                            RuntimeError,
                            AttributeError,
                            IndexError,
                        ):
                            failures += 1
                        else:
                            pages[futures[future]].update(
                                filePath=filename, status="completed"
                            )
                        save()
            finally:
                save()
            self.check_cancelled()
            if failures:
                raise DownloadError(f"{failures} 页下载失败, 再次输入同一编号可继续.")
        except jmcomic.JmcomicException:
            self.check_cancelled()
            raise DownloadError("JM 请求失败, 请检查网络或稍后重试.") from None
        finally:
            for client in clients:
                try:
                    client.postman.session.close()
                except OSError:
                    pass

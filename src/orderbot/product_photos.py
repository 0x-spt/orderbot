"""The marketplace's main product photo for the order message.

The adapter gives the address (photo_url, read-only API data); this module downloads it once
and keeps it on disk for MAX_AGE_DAYS per marketplace and article. Only https, at most
MAX_BYTES, TIMEOUT_S per request, and the bytes must decode as an image. Whatever fails
gives no photo, logged at info: the order decision never waits on or depends on it.
"""
import asyncio
import hashlib
import io
import logging
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx
from PIL import Image

log = logging.getLogger(__name__)

MAX_BYTES = 5 * 1024 * 1024
TIMEOUT_S = 15.0
MAX_AGE_DAYS = 7
FAILED_RETRY_S = 30 * 60   # an article whose photo failed is not asked again for this long
MAX_REDIRECTS = 3
MAX_SIDE = 1200        # px: plenty for a picture shown next to the 1C tables
MAX_PIXELS = 50_000_000  # a "photo" bigger than this is not decoded

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


class _Refused(Exception):
    pass


def _https(url) -> bool:
    return isinstance(url, str) and url.startswith("https://")


class ProductPhotos:
    def __init__(self, root, *, clock=time.time, transport=None):
        self._root = Path(root)
        self._clock = clock
        self._transport = transport
        self._failed: dict[tuple[str, str], float] = {}   # (marketplace, article) -> when

    def _path(self, marketplace: str, article: str) -> Path:
        digest = hashlib.sha1(f"{marketplace}\0{article}".encode()).hexdigest()[:10]
        return self._root / f"{_UNSAFE.sub('_', marketplace)}-{_UNSAFE.sub('_', article)[:40]}-{digest}.jpg"

    def _fresh(self, path: Path) -> bool:
        try:
            return path.is_file() and path.stat().st_mtime >= self._clock() - MAX_AGE_DAYS * 86400
        except OSError:
            return False

    async def get(self, marketplace: str, article: str, url_fn) -> str | None:
        """Path of the cached photo, or None. `url_fn(article)` is awaited only when the
        cache has no fresh copy."""
        path = self._path(marketplace, article)
        if self._fresh(path):
            return str(path)
        key = (marketplace, article)
        failed = self._failed.get(key)
        if failed is not None and self._clock() - failed < FAILED_RETRY_S:
            return None
        ok = False
        try:
            url = await url_fn(article)
            if not url:
                raise _Refused("no photo address")
            if not _https(url):
                raise _Refused("not an https address")
            # one deadline for the whole download, on top of httpx's per-phase timeouts
            data = await asyncio.wait_for(self._download(url), TIMEOUT_S)
            image = self._decode(data)
            self._root.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            image.save(tmp, "JPEG", quality=90)
            tmp.replace(path)
            ok = True
            self._failed.pop(key, None)
            return str(path)
        except Exception as e:
            log.info("no %s photo for %s: %s: %s", marketplace, article, type(e).__name__, e)
            return None
        finally:
            # also when the caller stopped waiting (its time cap): slow, not asked again soon
            if not ok:
                self._failed[key] = self._clock()

    async def _download(self, url: str) -> bytes:
        async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_S), follow_redirects=False,
                                     transport=self._transport) as client:
            for _ in range(MAX_REDIRECTS + 1):
                async with client.stream("GET", url) as r:
                    if r.is_redirect:
                        url = urljoin(url, r.headers.get("location", ""))
                        if not _https(url):
                            raise _Refused("redirect away from https")
                        continue
                    r.raise_for_status()
                    length = r.headers.get("content-length")
                    if length and length.isdigit() and int(length) > MAX_BYTES:
                        raise _Refused(f"{length} bytes")
                    data = bytearray()
                    async for chunk in r.aiter_bytes():
                        data += chunk
                        if len(data) > MAX_BYTES:
                            raise _Refused("more than the size limit")
                    return bytes(data)
        raise _Refused("too many redirects")

    @staticmethod
    def _decode(data: bytes) -> Image.Image:
        with Image.open(io.BytesIO(data)) as im:
            if im.width * im.height > MAX_PIXELS:
                raise _Refused(f"{im.width}x{im.height} px")
            im.load()
            rgba = im.convert("RGBA")
        image = Image.new("RGB", rgba.size, "white")   # transparent parts become white
        image.paste(rgba, mask=rgba.split()[3])
        image.thumbnail((MAX_SIDE, MAX_SIDE))
        return image

    def purge(self) -> int:
        """Delete cached photos older than MAX_AGE_DAYS; returns how many."""
        if not self._root.is_dir():
            return 0
        cutoff = self._clock() - MAX_AGE_DAYS * 86400
        removed = 0
        for p in self._root.iterdir():
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                log.exception("cannot purge %s", p)
        return removed

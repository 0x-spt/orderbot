"""Marketplace product photos for the order message: download and cache."""
import io
import os
import time

import httpx
import pytest
import respx
from PIL import Image

from orderbot import product_photos
from orderbot.product_photos import ProductPhotos

URL = "https://cdn.example.ru/p/1.jpg"


def image_bytes(fmt="JPEG", size=(60, 80), color="red"):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, fmt)
    return buf.getvalue()


def url_of(value):
    calls = []

    async def fn(article):
        calls.append(article)
        if isinstance(value, Exception):
            raise value
        return value

    fn.calls = calls
    return fn


async def test_download_is_cached_per_marketplace_and_article(tmp_path):
    photos = ProductPhotos(tmp_path / "pp")
    fn = url_of(URL)
    with respx.mock() as r:
        route = r.get(URL).mock(return_value=httpx.Response(200, content=image_bytes()))
        first = await photos.get("ozon", "ART/1 (x)", fn)
        second = await photos.get("ozon", "ART/1 (x)", fn)
        other = await photos.get("wb", "ART/1 (x)", url_of(URL))
    assert first and first == second and other != first
    assert route.call_count == 2 and fn.calls == ["ART/1 (x)"]
    assert os.path.dirname(first) == str(tmp_path / "pp")
    with Image.open(first) as im:
        assert im.size == (60, 80)
        assert im.convert("RGB").getpixel((30, 40))[0] > 200


async def test_cache_older_than_seven_days_is_fetched_again(tmp_path):
    photos = ProductPhotos(tmp_path)
    with respx.mock() as r:
        route = r.get(URL).mock(return_value=httpx.Response(200, content=image_bytes()))
        path = await photos.get("ozon", "A1", url_of(URL))
        old = time.time() - 8 * 86400
        os.utime(path, (old, old))
        await photos.get("ozon", "A1", url_of(URL))
    assert route.call_count == 2


async def test_webp_is_decoded(tmp_path):
    with respx.mock() as r:
        r.get(URL).mock(return_value=httpx.Response(200, content=image_bytes("WEBP")))
        assert await ProductPhotos(tmp_path).get("wb", "A1", url_of(URL))


@pytest.mark.parametrize("url", [None, "", "http://cdn.example.ru/p.jpg", "file:///etc/passwd",
                                 "ftp://x/y.jpg"])
async def test_only_https(tmp_path, url):
    with respx.mock() as r:
        assert await ProductPhotos(tmp_path).get("ozon", "A1", url_of(url)) is None
    assert not r.calls


async def test_redirect_to_http_is_refused(tmp_path):
    with respx.mock(assert_all_called=False) as r:
        r.get(URL).mock(return_value=httpx.Response(302, headers={"Location": "http://x.ru/a.jpg"}))
        plain = r.get("http://x.ru/a.jpg").mock(
            return_value=httpx.Response(200, content=image_bytes()))
        assert await ProductPhotos(tmp_path).get("ozon", "A1", url_of(URL)) is None
    assert not plain.called


async def test_https_redirect_is_followed(tmp_path):
    with respx.mock() as r:
        r.get(URL).mock(return_value=httpx.Response(
            301, headers={"Location": "https://cdn2.example.ru/a.jpg"}))
        r.get("https://cdn2.example.ru/a.jpg").mock(
            return_value=httpx.Response(200, content=image_bytes()))
        assert await ProductPhotos(tmp_path).get("ozon", "A1", url_of(URL))


async def test_more_than_five_megabytes_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(product_photos, "MAX_BYTES", 1000)
    with respx.mock() as r:
        r.get(URL).mock(return_value=httpx.Response(200, content=image_bytes(size=(400, 400))
                                                    + b"\0" * 2000))
        assert await ProductPhotos(tmp_path).get("ozon", "A1", url_of(URL)) is None
    assert list(tmp_path.iterdir()) == []


def test_limits_are_the_agreed_ones():
    assert product_photos.MAX_BYTES == 5 * 1024 * 1024
    assert product_photos.TIMEOUT_S <= 15
    assert product_photos.MAX_AGE_DAYS == 7


@pytest.mark.parametrize("response", [
    httpx.Response(200, content=b"<html>not an image</html>"),
    httpx.Response(404),
    httpx.Response(500),
])
async def test_bad_answers_give_no_photo(tmp_path, response):
    with respx.mock() as r:
        r.get(URL).mock(return_value=response)
        assert await ProductPhotos(tmp_path).get("ozon", "A1", url_of(URL)) is None
    assert list(tmp_path.iterdir()) == []


async def test_network_error_and_api_error_give_no_photo(tmp_path):
    with respx.mock() as r:
        r.get(URL).mock(side_effect=httpx.ConnectTimeout("slow"))
        assert await ProductPhotos(tmp_path).get("ozon", "A1", url_of(URL)) is None
    assert await ProductPhotos(tmp_path).get("ozon", "A1", url_of(RuntimeError("api"))) is None


async def test_big_photo_is_scaled_down_for_the_cache(tmp_path):
    with respx.mock() as r:
        r.get(URL).mock(return_value=httpx.Response(200, content=image_bytes(size=(3000, 1500))))
        path = await ProductPhotos(tmp_path).get("ozon", "A1", url_of(URL))
    with Image.open(path) as im:
        assert max(im.size) == product_photos.MAX_SIDE and im.size[0] == 2 * im.size[1]


def test_purge_keeps_its_own_seven_days(tmp_path):
    old, new = tmp_path / "old.jpg", tmp_path / "new.jpg"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    t = time.time() - 8 * 86400
    os.utime(old, (t, t))
    assert ProductPhotos(tmp_path).purge() == 1
    assert not old.exists() and new.exists()


# --- negative cache and the overall deadline ------------------------


class Now:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.mark.parametrize("value", [None, "http://x.ru/a.jpg", RuntimeError("api")])
async def test_a_failure_is_not_retried_for_thirty_minutes(tmp_path, value):
    now = Now()
    photos = ProductPhotos(tmp_path, clock=now)
    fn = url_of(value)
    assert await photos.get("ozon", "A1", fn) is None
    now.t += 29 * 60
    assert await photos.get("ozon", "A1", fn) is None
    assert len(fn.calls) == 1
    now.t += 2 * 60
    await photos.get("ozon", "A1", fn)
    assert len(fn.calls) == 2
    assert product_photos.FAILED_RETRY_S == 30 * 60


async def test_failed_download_is_remembered_too(tmp_path):
    photos = ProductPhotos(tmp_path)
    with respx.mock() as r:
        route = r.get(URL).mock(return_value=httpx.Response(500))
        await photos.get("ozon", "A1", url_of(URL))
        await photos.get("ozon", "A1", url_of(URL))
    assert route.call_count == 1


async def test_cancelled_fetch_is_remembered_as_failed(tmp_path):
    import asyncio
    photos = ProductPhotos(tmp_path)

    async def slow(article):
        await asyncio.sleep(30)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(photos.get("ozon", "A1", slow), 0.05)
    fn = url_of(URL)
    assert await photos.get("ozon", "A1", fn) is None and fn.calls == []


async def test_a_download_never_takes_longer_than_the_limit(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setattr(product_photos, "TIMEOUT_S", 0.1)

    async def trickle(request):
        await asyncio.sleep(5)
        return httpx.Response(200, content=image_bytes())

    with respx.mock(assert_all_called=False) as r:   # cancelled mid-request
        r.get(URL).mock(side_effect=trickle)
        assert await asyncio.wait_for(
            ProductPhotos(tmp_path).get("ozon", "A1", url_of(URL)), 2) is None

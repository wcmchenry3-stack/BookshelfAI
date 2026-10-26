"""Fakes for the scan evaluation harness tests (#536): an in-memory R2
client and a scripted vision adapter. Nothing here touches the network."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Callable
from typing import Any

import pytest
from PIL import Image

from app.ai.providers.base import ProviderResult
from app.services.book_identifier import BookCandidate
from eval.ai_scan.dataset import R2Config, R2Dataset

ENDPOINT = "https://acct0123.r2.cloudflarestorage.com"
BUCKET = "eval-bucket"


def jpeg(
    color: tuple[int, int, int] = (200, 30, 30), exif_text: str | None = None
) -> bytes:
    img = Image.new("RGB", (64, 48), color)
    out = io.BytesIO()
    kwargs: dict[str, Any] = {}
    if exif_text is not None:
        exif = Image.Exif()
        exif[0x010E] = exif_text  # ImageDescription
        exif[0x013B] = exif_text  # Artist
        kwargs["exif"] = exif.tobytes()
    img.save(out, format="JPEG", **kwargs)
    return out.getvalue()


def manifest(cases: list[dict], version: str = "v1") -> bytes:
    return json.dumps(
        {"schema_version": 1, "dataset_version": version, "cases": cases}
    ).encode()


def case(i: int, books: list[dict] | None = None, image: str | None = None) -> dict:
    return {
        "id": f"case-{i:03d}",
        "image": image or f"images/case-{i:03d}.jpg",
        "books": (
            books
            if books is not None
            else [{"title": "Dune", "author": "Frank Herbert"}]
        ),
    }


class FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, n: int = -1) -> bytes:
        return self._data if n < 0 else self._data[:n]


class FakeS3:
    """``get_object`` from a dict of objects; records every call."""

    def __init__(
        self,
        objects: dict[str, bytes],
        on_get: Callable[[str], None] | None = None,
    ) -> None:
        self.objects = objects
        self.calls: list[tuple[str, str]] = []
        self.on_get = on_get

    def get_object(self, *, Bucket: str, Key: str) -> dict:
        self.calls.append((Bucket, Key))
        if self.on_get is not None:
            self.on_get(Key)
        if Key not in self.objects:
            raise KeyError(f"NoSuchKey: {Bucket}/{Key}")
        return {"Body": FakeBody(self.objects[Key])}


def dataset(
    objects: dict[str, bytes],
    *,
    endpoint: str = ENDPOINT,
    bucket: str = BUCKET,
    prefix: str = "",
    client: FakeS3 | None = None,
) -> tuple[R2Dataset, FakeS3]:
    client = client or FakeS3(objects)
    config = R2Config(endpoint, bucket, "AKIDTEST", "SECRETTEST", prefix)
    return R2Dataset(config, client_factory=lambda _cfg: client), client


def objects_for(
    cases: list[dict], prefix: str = "", image: bytes | None = None
) -> dict:
    data = {f"{prefix}manifest.json": manifest(cases)}
    for c in cases:
        data[prefix + c["image"]] = image if image is not None else jpeg()
    return data


def result(
    books: list[tuple[str, str]] | None = None,
    *,
    outcome: str = "ok",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    isbns: list[str | None] | None = None,
) -> ProviderResult:
    books = books or []
    isbns = isbns or [None] * len(books)
    return ProviderResult(
        candidates=[
            BookCandidate(title=t, author=a, confidence=0.9, isbn_13=i)
            for (t, a), i in zip(books, isbns, strict=True)
        ],
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        outcome=outcome,  # type: ignore[arg-type]
    )


class ScriptedProvider:
    """A vision adapter that replays ``script`` (ProviderResult or an
    exception instance) call by call; records the image bytes it got."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.images: list[bytes] = []

    async def identify(self, image_bytes: bytes) -> ProviderResult:
        self.images.append(image_bytes)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def restore_logger_levels():
    """``run_eval.main`` pins some third-party loggers to WARNING; put them
    back so other tests see the levels they expect."""
    from eval.ai_scan.run_eval import QUIET_LOGGERS

    saved = {name: logging.getLogger(name).level for name in QUIET_LOGGERS}
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)

"""The labelled dataset: where it lives (a private Cloudflare R2 bucket), its
layout (one ``manifest.json``) and how it is read.

Nothing here prints or logs a bucket name, endpoint, object key, credential,
title or author. Errors carry only positions (case number, field name) and
exception type names.

Layout (see ``backend/eval/README.md``)::

    <prefix>manifest.json
    <prefix><each case's "image" key>

``manifest.json``::

    {
      "schema_version": 1,
      "dataset_version": "2026-11-01",
      "cases": [
        {
          "id": "case-001",
          "image": "images/case-001.jpg",
          "books": [
            {"title": "Dune", "author": "Frank Herbert", "isbn": "9780441013593"}
          ]
        }
      ]
    }
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from eval.ai_scan import EvalError

# --- Location and credentials: environment only, never the repo ------------

ENV_ENDPOINT = "AI_EVAL_R2_ENDPOINT"
ENV_BUCKET = "AI_EVAL_R2_BUCKET"
ENV_ACCESS_KEY_ID = "AI_EVAL_R2_ACCESS_KEY_ID"
ENV_SECRET_ACCESS_KEY = "AI_EVAL_R2_SECRET_ACCESS_KEY"
# Optional: a folder inside the bucket ("shelves/v1/"). Empty means the root.
ENV_PREFIX = "AI_EVAL_R2_PREFIX"

REQUIRED_ENV = (ENV_ENDPOINT, ENV_BUCKET, ENV_ACCESS_KEY_ID, ENV_SECRET_ACCESS_KEY)

# R2's S3 API host: https://<account id>.r2.cloudflarestorage.com, or with a
# jurisdiction, https://<account id>.eu.r2.cloudflarestorage.com. Anything
# else is refused, so the credential is never sent to another host.
R2_HOST_SUFFIX = ".r2.cloudflarestorage.com"

MANIFEST_NAME = "manifest.json"
MAX_MANIFEST_BYTES = 5 * 1024 * 1024
# Above /scan's 5 MB upload cap on purpose: the eval downloads the owner's
# original photos; app.ai.images normalises them to the same JPEG either way.
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_CASES = 1_000
MAX_BOOKS_PER_CASE = 100
MAX_TEXT_LENGTH = 500

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif")


@dataclass(frozen=True)
class R2Config:
    endpoint: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    prefix: str = ""

    def __repr__(self) -> str:  # never show the location or the credential
        return "R2Config(<redacted>)"

    __str__ = __repr__


def r2_config_from_env(env: dict[str, str] | None = None) -> R2Config:
    """The dataset location and credential from the environment.

    Raises :class:`EvalError` naming the missing variables (names only) when
    any is unset, and when the endpoint is not an R2 S3 API URL."""
    env = os.environ if env is None else env
    missing = [name for name in REQUIRED_ENV if not (env.get(name) or "").strip()]
    if missing:
        raise EvalError(
            "refusing to run: the dataset location or credential is not set "
            f"({', '.join(missing)})"
        )
    endpoint = env[ENV_ENDPOINT].strip()
    parts = urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or not host.endswith(R2_HOST_SUFFIX)
        or host == R2_HOST_SUFFIX.lstrip(".")
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
    ):
        raise EvalError(
            f"refusing to run: {ENV_ENDPOINT} must be an https "
            f"<account>{R2_HOST_SUFFIX} URL with no path"
        )
    prefix = (env.get(ENV_PREFIX) or "").strip()
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    if prefix and not _is_safe_key(prefix.rstrip("/")):
        raise EvalError(f"refusing to run: {ENV_PREFIX} is not a plain relative path")
    return R2Config(
        endpoint=endpoint,
        bucket=env[ENV_BUCKET].strip(),
        access_key_id=env[ENV_ACCESS_KEY_ID].strip(),
        secret_access_key=env[ENV_SECRET_ACCESS_KEY].strip(),
        prefix=prefix,
    )


# --- Manifest ---------------------------------------------------------------

_SAFE_KEY = re.compile(r"^[A-Za-z0-9._\-/]+$")
_ID = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")
_VERSION = re.compile(r"^[A-Za-z0-9._\-]{1,32}$")


def _is_safe_key(key: str) -> bool:
    """A plain relative object key: no leading slash, no ``.``/``..``
    segment, no empty segment, no backslash, only ``[A-Za-z0-9._-/]``."""
    if not key or not _SAFE_KEY.match(key) or key.startswith("/"):
        return False
    return all(seg not in ("", ".", "..") for seg in key.split("/"))


def _isbn_digits(value: str) -> str | None:
    """``value`` without hyphens or spaces when it is a valid ISBN-10 or
    ISBN-13 (digit count and checksum), else None."""
    chars = value.replace("-", "").replace(" ", "").upper()
    if len(chars) == 13 and chars.isascii() and chars.isdigit():
        total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(chars))
        return chars if total % 10 == 0 else None
    if (
        len(chars) == 10
        and chars.isascii()
        and chars[:9].isdigit()
        and (chars[9].isdigit() or chars[9] == "X")
    ):
        total = sum(
            (10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(chars)
        )
        return chars if total % 11 == 0 else None
    return None


class _Strict(BaseModel):
    # hide_input_in_errors: a validation error never carries a title, author
    # or key; we also only ever report its location and type.
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class TruthBook(_Strict):
    title: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH)
    author: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH)
    isbn: str | None = None

    @field_validator("title", "author")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("blank")
        return value

    @field_validator("isbn")
    @classmethod
    def _valid_isbn(cls, value: str | None) -> str | None:
        if value is None:
            return None
        digits = _isbn_digits(value)
        if digits is None:
            raise ValueError("not a valid ISBN-10 or ISBN-13")
        return digits


class Case(_Strict):
    id: str
    image: str
    books: list[TruthBook] = Field(max_length=MAX_BOOKS_PER_CASE)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not _ID.match(value):
            raise ValueError("id must be 1-64 of [A-Za-z0-9._-]")
        return value

    @field_validator("image")
    @classmethod
    def _valid_image(cls, value: str) -> str:
        if not _is_safe_key(value) or len(value) > 512:
            raise ValueError("image must be a plain relative object key")
        if not value.lower().endswith(IMAGE_EXTENSIONS):
            raise ValueError("image must be a .jpg/.jpeg/.png/.webp/.heic/.heif")
        return value


class Manifest(_Strict):
    schema_version: Literal[1]
    dataset_version: str
    cases: list[Case] = Field(min_length=1, max_length=MAX_CASES)

    @field_validator("dataset_version")
    @classmethod
    def _valid_version(cls, value: str) -> str:
        if not _VERSION.match(value):
            raise ValueError("dataset_version must be 1-32 of [A-Za-z0-9._-]")
        return value

    @model_validator(mode="after")
    def _unique(self) -> Manifest:
        ids = [c.id for c in self.cases]
        if len(set(ids)) != len(ids):
            raise ValueError("case ids must be unique")
        images = [c.image for c in self.cases]
        if len(set(images)) != len(images):
            raise ValueError("case images must be unique")
        return self


def _safe_errors(exc: ValidationError) -> str:
    """Field locations and error types only: never an input value."""
    parts = []
    for err in exc.errors(include_input=False, include_url=False)[:10]:
        loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
        parts.append(f"{loc}: {err.get('type', 'invalid')}")
    return "; ".join(parts)


def parse_manifest(raw: bytes) -> Manifest:
    """Strictly validate ``manifest.json`` bytes. Raises :class:`EvalError`
    whose message holds only field locations and error types."""
    if len(raw) > MAX_MANIFEST_BYTES:
        raise EvalError("manifest is larger than the 5 MB limit")
    try:
        return Manifest.model_validate_json(raw)
    except ValidationError as exc:
        raise EvalError(f"manifest is invalid: {_safe_errors(exc)}") from None


# --- R2 access ---------------------------------------------------------------


def default_client_factory(config: R2Config) -> Any:
    """An S3 client for R2 (boto3, from requirements-eval.txt). Imported
    here, not at module level, so the unit tests (which inject a fake client)
    never need boto3."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=config.endpoint,
        aws_access_key_id=config.access_key_id,
        aws_secret_access_key=config.secret_access_key,
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=10,
            read_timeout=60,
        ),
    )


def _read_capped(body: Any, limit: int, what: str) -> bytes:
    data = body.read(limit + 1)
    if len(data) > limit:
        raise EvalError(f"{what} is larger than the {limit // (1024 * 1024)} MB limit")
    return data


class R2Dataset:
    """Reads the manifest and the images of one dataset in R2.

    Images are downloaded into a private temporary directory (mode 0700)
    under generated names (``00001.img``), never the object key. The
    directory exists only inside :meth:`open` and is removed when it exits,
    on success or on any error."""

    def __init__(
        self,
        config: R2Config,
        client_factory: Callable[[R2Config], Any] = default_client_factory,
    ) -> None:
        self._config = config
        self._client_factory = client_factory
        self._client: Any = None
        self.workdir: Path | None = None

    def __repr__(self) -> str:
        return "R2Dataset(<redacted>)"

    @contextlib.contextmanager
    def open(self) -> Iterator[R2Dataset]:
        workdir = Path(tempfile.mkdtemp(prefix="ai-eval-"))
        try:
            os.chmod(workdir, 0o700)
            self.workdir = workdir
            try:
                self._client = self._client_factory(self._config)
            except Exception as exc:  # noqa: BLE001 - message may name the host
                raise EvalError(
                    f"could not create the R2 client ({type(exc).__name__})"
                ) from None
            yield self
        finally:
            self._client = None
            self.workdir = None
            shutil.rmtree(workdir, ignore_errors=True)

    def _get(self, key: str, limit: int, what: str) -> bytes:
        if self._client is None:
            raise EvalError("dataset is not open")
        try:
            response = self._client.get_object(
                Bucket=self._config.bucket, Key=self._config.prefix + key
            )
            return _read_capped(response["Body"], limit, what)
        except EvalError:
            raise
        except Exception as exc:  # noqa: BLE001 - the message may name the key
            raise EvalError(f"could not read {what} ({type(exc).__name__})") from None

    def manifest(self) -> Manifest:
        return parse_manifest(self._get(MANIFEST_NAME, MAX_MANIFEST_BYTES, "manifest"))

    def image(self, index: int, case: Case) -> bytes:
        """Download case ``index`` (1-based) to the temp dir and return its
        bytes."""
        what = f"image of case #{index}"
        data = self._get(case.image, MAX_IMAGE_BYTES, what)
        if self.workdir is None:  # pragma: no cover - only inside open()
            raise EvalError("dataset is not open")
        path = self.workdir / f"{index:05d}.img"
        path.write_bytes(data)
        os.chmod(path, 0o600)
        return path.read_bytes()

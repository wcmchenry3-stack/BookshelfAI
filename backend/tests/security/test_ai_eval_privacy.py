"""Scan evaluation privacy (#536): the dataset's location, object keys,
titles, authors, images and EXIF never reach the report, the logs or the
console. Sentinel values are planted everywhere and searched for in every
output. R2 and the provider are fakes; no network."""

import json
import logging
from unittest.mock import patch

import pytest

from app.ai import gateway
from app.core.config import settings
from eval.ai_scan import run_eval as cli
from tests import _ai_eval_fakes as f

restore_logger_levels = f.restore_logger_levels

pytestmark = [pytest.mark.security, pytest.mark.usefixtures("restore_logger_levels")]

S_ENDPOINT = "https://sentinelacct7.r2.cloudflarestorage.com"
S_BUCKET = "sentinel-bucket-q7"
S_PREFIX = "sentinel-prefix-q7/"
S_KEY = "sentinel-key-q7/shelf.jpg"
S_TITLE = "Sentinel Title Q7"
S_AUTHOR = "Sentinel Author Q7"
S_PRED_TITLE = "Sentinel Predicted Q7"
S_EXIF = "SentinelExifQ7"
S_CASE_ID = "sentinel-case-q7"
S_ERROR = "sentinel-error-q7"

SENTINELS = [
    S_ENDPOINT,
    "sentinelacct7",
    S_BUCKET,
    S_PREFIX.rstrip("/"),
    S_KEY,
    "sentinel-key-q7",
    S_TITLE,
    S_AUTHOR,
    S_PRED_TITLE,
    S_EXIF,
    S_CASE_ID,
    S_ERROR,
    "AKIDTEST",
    "SECRETTEST",
]


def _objects():
    cases = [
        {
            "id": S_CASE_ID,
            "image": S_KEY,
            "books": [{"title": S_TITLE, "author": S_AUTHOR, "isbn": "9780441013593"}],
        },
        f.case(2, [{"title": S_TITLE, "author": S_AUTHOR}]),
        f.case(3, [{"title": S_TITLE, "author": S_AUTHOR}]),
    ]
    image = f.jpeg(exif_text=S_EXIF)
    assert S_EXIF.encode() in image  # the sentinel really is in the EXIF
    return f.objects_for(cases, prefix=S_PREFIX, image=image)


class SentinelProvider:
    calls = 0

    def __init__(self, spec):
        pass

    async def identify(self, image_bytes):
        SentinelProvider.calls += 1
        assert S_EXIF.encode() not in image_bytes  # EXIF stripped before the call
        if SentinelProvider.calls == 2:
            raise RuntimeError(f"{S_ERROR} {S_BUCKET}/{S_KEY} {S_TITLE}")
        return f.result(
            [(S_PRED_TITLE, S_AUTHOR), (S_TITLE, S_AUTHOR)],
            input_tokens=1_000,
            output_tokens=100,
        )


def _assert_clean(text: str, where: str) -> None:
    for sentinel in SENTINELS:
        assert sentinel not in text, f"{sentinel!r} leaked into {where}"


def test_report_logs_and_console_hold_no_dataset_content(tmp_path, capsys, caplog):
    SentinelProvider.calls = 0
    ds, client = f.dataset(
        _objects(), endpoint=S_ENDPOINT, bucket=S_BUCKET, prefix=S_PREFIX
    )
    out = tmp_path / "report.json"
    caplog.set_level(logging.DEBUG)
    with (
        patch.object(settings, "anthropic_api_key", "test-key"),
        patch.object(gateway, "AnthropicVisionProvider", SentinelProvider),
    ):
        code = cli.main(
            [
                "--feature",
                "scan.standard",
                "--model",
                "claude-haiku-5-5",
                "--spend-cap-usd",
                "1",
                "--report",
                str(out),
            ],
            dataset=ds,
        )
    assert code == 0
    # The run really used the sentinel location and scored the sentinel books.
    assert (S_BUCKET, S_PREFIX + S_KEY) in client.calls
    report_text = out.read_text()
    report = json.loads(report_text)
    assert report["accuracy"]["books_matched"] == 2
    assert report["run"]["error_types"] == {"RuntimeError": 1}

    _assert_clean(report_text, "the report")
    captured = capsys.readouterr()
    _assert_clean(captured.out, "stdout")
    _assert_clean(captured.err, "stderr")
    _assert_clean(caplog.text, "the logs")
    # No image bytes or base64 in the report: it is small, flat JSON.
    assert len(report_text) < 5_000
    assert "/9j/" not in report_text  # base64 JPEG header


@pytest.mark.parametrize("failure", ["manifest", "image"])
def test_failures_name_no_location_or_key(tmp_path, capsys, caplog, failure):
    objects = _objects()
    if failure == "manifest":
        del objects[S_PREFIX + "manifest.json"]
    else:
        del objects[S_PREFIX + S_KEY]
    ds, _ = f.dataset(objects, endpoint=S_ENDPOINT, bucket=S_BUCKET, prefix=S_PREFIX)
    caplog.set_level(logging.DEBUG)
    with (
        patch.object(settings, "anthropic_api_key", "test-key"),
        patch.object(gateway, "AnthropicVisionProvider", SentinelProvider),
    ):
        code = cli.main(
            [
                "--feature",
                "scan.standard",
                "--model",
                "claude-haiku-5-5",
                "--spend-cap-usd",
                "1",
                "--report",
                str(tmp_path / "r.json"),
            ],
            dataset=ds,
        )
    assert code == 2
    captured = capsys.readouterr()
    assert "could not read" in captured.err
    _assert_clean(captured.out + captured.err + caplog.text, "the failure output")
    assert not (tmp_path / "r.json").exists()


def test_invalid_manifest_error_names_no_title(capsys):
    objects = _objects()
    objects[S_PREFIX + "manifest.json"] = json.dumps(
        {
            "schema_version": 1,
            "dataset_version": "v1",
            "cases": [{"id": S_CASE_ID, "image": S_KEY, "books": [{"title": S_TITLE}]}],
        }
    ).encode()
    ds, _ = f.dataset(objects, endpoint=S_ENDPOINT, bucket=S_BUCKET, prefix=S_PREFIX)
    with patch.object(settings, "anthropic_api_key", "test-key"):
        code = cli.main(
            [
                "--feature",
                "scan.standard",
                "--model",
                "claude-haiku-5-5",
                "--spend-cap-usd",
                "1",
            ],
            dataset=ds,
        )
    assert code == 2
    err = capsys.readouterr().err
    assert "manifest is invalid" in err
    _assert_clean(err, "stderr")

"""Scan evaluation dataset (#536): R2 location from the environment only,
strict manifest validation, temp download directory always removed."""

import json
from pathlib import Path

import pytest

from eval.ai_scan import EvalError
from eval.ai_scan.dataset import (
    ENV_ACCESS_KEY_ID,
    ENV_BUCKET,
    ENV_ENDPOINT,
    ENV_PREFIX,
    ENV_SECRET_ACCESS_KEY,
    REQUIRED_ENV,
    R2Config,
    parse_manifest,
    r2_config_from_env,
)
from tests import _ai_eval_fakes as f

GOOD_ENV = {
    ENV_ENDPOINT: f.ENDPOINT,
    ENV_BUCKET: f.BUCKET,
    ENV_ACCESS_KEY_ID: "AKIDTEST",
    ENV_SECRET_ACCESS_KEY: "SECRETTEST",
}


# --- Location and credentials -------------------------------------------------


def test_config_from_env():
    cfg = r2_config_from_env({**GOOD_ENV, ENV_PREFIX: "shelves/v1"})
    assert cfg.endpoint == f.ENDPOINT
    assert cfg.bucket == f.BUCKET
    assert cfg.prefix == "shelves/v1/"


@pytest.mark.parametrize("missing", REQUIRED_ENV)
def test_refuses_without_each_variable(missing):
    env = {k: v for k, v in GOOD_ENV.items() if k != missing}
    with pytest.raises(EvalError, match=missing):
        r2_config_from_env(env)
    with pytest.raises(EvalError, match=missing):
        r2_config_from_env({**env, missing: "   "})


def test_refuses_with_nothing_set():
    with pytest.raises(EvalError, match="refusing to run"):
        r2_config_from_env({})


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://acct.r2.cloudflarestorage.com",
        "https://s3.amazonaws.com",
        "https://acct.r2.cloudflarestorage.com.evil.example",
        "https://r2.cloudflarestorage.com",
        "https://user:pw@acct.r2.cloudflarestorage.com",
        "https://acct.r2.cloudflarestorage.com/bucket",
        "https://acct.r2.cloudflarestorage.com?x=1",
    ],
)
def test_refuses_a_non_r2_endpoint(endpoint):
    with pytest.raises(EvalError, match=ENV_ENDPOINT):
        r2_config_from_env({**GOOD_ENV, ENV_ENDPOINT: endpoint})


def test_eu_jurisdiction_endpoint_is_accepted():
    cfg = r2_config_from_env(
        {**GOOD_ENV, ENV_ENDPOINT: "https://acct.eu.r2.cloudflarestorage.com"}
    )
    assert cfg.endpoint.endswith(".eu.r2.cloudflarestorage.com")


@pytest.mark.parametrize("prefix", ["../x", "a//b", "/abs", "a\\b", "a/./b"])
def test_refuses_an_unsafe_prefix(prefix):
    with pytest.raises(EvalError, match=ENV_PREFIX):
        r2_config_from_env({**GOOD_ENV, ENV_PREFIX: prefix})


def test_config_repr_hides_location_and_credential():
    cfg = R2Config(f.ENDPOINT, f.BUCKET, "AKIDTEST", "SECRETTEST")
    for text in (repr(cfg), str(cfg)):
        for secret in (f.ENDPOINT, f.BUCKET, "AKIDTEST", "SECRETTEST"):
            assert secret not in text


# --- Manifest -------------------------------------------------------------------


def _raw(**overrides):
    doc = {
        "schema_version": 1,
        "dataset_version": "2026-11-01",
        "cases": [
            {
                "id": "case-001",
                "image": "images/case-001.jpg",
                "books": [
                    {
                        "title": "Dune",
                        "author": "Frank Herbert",
                        "isbn": "978-0441013593",
                    },
                    {"title": "Emma", "author": "Jane Austen"},
                ],
            }
        ],
    }
    doc.update(overrides)
    return json.dumps(doc).encode()


def test_valid_manifest():
    m = parse_manifest(_raw())
    assert m.dataset_version == "2026-11-01"
    assert m.cases[0].books[0].isbn == "9780441013593"
    assert m.cases[0].books[1].isbn is None


def _case(**overrides):
    c = {"id": "c1", "image": "a.jpg", "books": []}
    c.update(overrides)
    return c


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        _raw(schema_version=2),
        _raw(dataset_version="has spaces"),
        _raw(cases=[]),
        _raw(extra_field=True),
        _raw(cases=[_case(), _case()]),  # duplicate id
        _raw(cases=[_case(), _case(id="c2")]),  # duplicate image
        _raw(cases=[_case(image="../escape.jpg")]),
        _raw(cases=[_case(image="/abs.jpg")]),
        _raw(cases=[_case(image="notes.txt")]),
        _raw(cases=[_case(id="has space")]),
        _raw(cases=[_case(books=[{"title": "", "author": "A"}])]),
        _raw(cases=[_case(books=[{"title": "   ", "author": "A"}])]),
        _raw(cases=[_case(books=[{"title": "T"}])]),
        _raw(
            cases=[
                _case(books=[{"title": "T", "author": "A", "isbn": "9780441013594"}])
            ]
        ),
        _raw(cases=[_case(books=[{"title": "T", "author": "A", "isbn": "123"}])]),
        _raw(cases=[_case(books=[{"title": "T", "author": "A", "year": 1965}])]),
        _raw(cases=[_case(books=[{"title": 5, "author": "A"}])]),
        _raw(cases=[_case(books=[{"title": "x" * 501, "author": "A"}])]),
    ],
)
def test_invalid_manifests_are_refused(raw):
    with pytest.raises(EvalError, match="manifest"):
        parse_manifest(raw)


def test_manifest_error_never_echoes_a_value():
    raw = _raw(
        cases=[
            _case(
                image="SENTINEL_KEY/../x.jpg",
                books=[{"title": 7, "author": "SENTINEL_AUTHOR", "isbn": "SENTINEL"}],
            )
        ]
    )
    with pytest.raises(EvalError) as info:
        parse_manifest(raw)
    assert "SENTINEL" not in str(info.value)
    assert "cases.0" in str(info.value)


def test_oversized_manifest_is_refused():
    with pytest.raises(EvalError, match="5 MB"):
        parse_manifest(b" " * (5 * 1024 * 1024 + 1))


# --- Download directory -----------------------------------------------------------


def _cases(n=2):
    return [f.case(i) for i in range(1, n + 1)]


def test_reads_manifest_and_images_under_the_prefix():
    cases = _cases()
    ds, client = f.dataset(f.objects_for(cases, prefix="p/"), prefix="p/")
    with ds.open() as opened:
        m = opened.manifest()
        data = opened.image(1, m.cases[0])
        files = sorted(p.name for p in opened.workdir.iterdir())
    assert data[:2] == b"\xff\xd8"
    assert files == ["00001.img"]  # generated name, never the key
    assert client.calls == [
        (f.BUCKET, "p/manifest.json"),
        (f.BUCKET, "p/images/case-001.jpg"),
    ]


def test_temp_dir_removed_after_success():
    ds, _ = f.dataset(f.objects_for(_cases()))
    with ds.open() as opened:
        workdir = opened.workdir
        m = opened.manifest()
        opened.image(1, m.cases[0])
        assert workdir.is_dir()
        assert oct(workdir.stat().st_mode & 0o777) == "0o700"
    assert not workdir.exists()
    assert ds.workdir is None


def test_temp_dir_removed_after_an_error():
    ds, _ = f.dataset(f.objects_for(_cases()))
    seen: list[Path] = []
    with pytest.raises(RuntimeError), ds.open() as opened:
        seen.append(opened.workdir)
        m = opened.manifest()
        opened.image(1, m.cases[0])
        raise RuntimeError("boom")
    assert seen and not seen[0].exists()


def test_temp_dir_removed_when_the_client_cannot_be_built():
    created: list[Path] = []

    def factory(_cfg):
        import tempfile

        created.extend(Path(tempfile.gettempdir()).glob("ai-eval-*"))
        raise ValueError(f"bad endpoint {f.ENDPOINT}")

    ds = f.R2Dataset(R2Config(f.ENDPOINT, f.BUCKET, "a", "b"), client_factory=factory)
    with pytest.raises(EvalError) as info, ds.open():
        pass
    assert f.ENDPOINT not in str(info.value)
    assert all(not p.exists() for p in created)


def test_download_error_names_no_key_or_bucket():
    cases = _cases()
    objects = f.objects_for(cases)
    del objects[cases[1]["image"]]
    ds, _ = f.dataset(objects)
    with ds.open() as opened:
        m = opened.manifest()
        with pytest.raises(EvalError) as info:
            opened.image(2, m.cases[1])
    msg = str(info.value)
    assert "case #2" in msg
    assert f.BUCKET not in msg and "case-002" not in msg


def test_oversized_image_is_refused(monkeypatch):
    from eval.ai_scan import dataset as dataset_mod

    monkeypatch.setattr(dataset_mod, "MAX_IMAGE_BYTES", 10)
    ds, _ = f.dataset(f.objects_for(_cases()))
    with ds.open() as opened:
        m = opened.manifest()
        with pytest.raises(EvalError, match="larger than"):
            opened.image(1, m.cases[0])


def test_not_open_is_refused():
    ds, _ = f.dataset(f.objects_for(_cases()))
    with pytest.raises(EvalError, match="not open"):
        ds.manifest()


def test_default_client_factory_targets_the_r2_endpoint():
    # Only where the eval requirements are installed (boto3 is not an app
    # or CI test dependency). Building a client makes no request.
    pytest.importorskip("boto3")
    from eval.ai_scan.dataset import default_client_factory

    client = default_client_factory(R2Config(f.ENDPOINT, f.BUCKET, "a", "b"))
    assert client.meta.endpoint_url == f.ENDPOINT

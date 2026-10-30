"""WishlistRequest.cover_url validation (#747): no DNS lookup on the event
loop, and one error message for every refusal so nothing leaks about DNS."""

import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.models.user import User
from app.schemas.user_book import COVER_URL_INVALID, WishlistRequest

BAD_URLS = [
    "http://covers.example.com/a.jpg",
    "ftp://covers.example.com/a.jpg",
    "https://localhost/a.jpg",
    "https://app.localhost/a.jpg",
    "https:///a.jpg",
    "https://10.1.2.3/a.jpg",
    "https://127.0.0.1/a.jpg",
    "https://169.254.169.254/latest/meta-data/",
    "https://[::1]/a.jpg",
]


def _build(url):
    return WishlistRequest(title="T", author="A", cover_url=url)


@pytest.fixture
def no_dns():
    with patch("socket.getaddrinfo", side_effect=AssertionError("DNS lookup")) as m:
        yield m


def test_validation_never_resolves_dns(no_dns):
    _build("https://slow-dns.example.com/cover.jpg")
    for url in BAD_URLS:
        with pytest.raises(ValidationError):
            _build(url)
    no_dns.assert_not_called()


def test_public_https_host_accepted_even_if_unresolvable():
    with patch("socket.getaddrinfo", side_effect=socket.gaierror("nxdomain")):
        url = "https://covers.example.com/a.jpg?x=1"
        assert _build(url).cover_url == url


def test_none_cover_url_accepted():
    assert _build(None).cover_url is None


@pytest.mark.parametrize("url", BAD_URLS)
def test_unsafe_urls_rejected(url):
    with pytest.raises(ValidationError):
        _build(url)


def test_every_rejection_has_the_same_message():
    messages = set()
    for url in BAD_URLS:
        with pytest.raises(ValidationError) as exc:
            _build(url)
        (err,) = exc.value.errors()
        messages.add(err["msg"])
    assert messages == {f"Value error, {COVER_URL_INVALID}"}


FAKE_USER = MagicMock(spec=User)


@pytest.fixture
def client():
    from app.auth.dependencies import get_current_user
    from app.core.database import get_db

    async def _fake_db():
        yield AsyncMock()

    app.dependency_overrides[get_current_user] = lambda: FAKE_USER
    app.dependency_overrides[get_db] = _fake_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.mark.parametrize("url", ["http://x.example.com/a.jpg", "https://10.0.0.1/a"])
def test_endpoint_bad_cover_url_is_422(client, no_dns, url):
    resp = client.post(
        "/wishlist", json={"title": "Dune", "author": "F", "cover_url": url}
    )
    assert resp.status_code == 422
    assert COVER_URL_INVALID in resp.json()["detail"][0]["msg"]
    no_dns.assert_not_called()


def test_endpoint_still_requires_auth():
    resp = TestClient(app).post(
        "/wishlist",
        json={"title": "Dune", "author": "F", "cover_url": "https://x.com/a"},
    )
    assert resp.status_code == 401

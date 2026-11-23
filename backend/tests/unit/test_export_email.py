"""The export email's strings and rendering (#694).

- One strings file per app locale (the frontend's 13), each with every key,
  non-empty, and the ``{expires}`` placeholder exactly where it belongs; the
  request schema accepts exactly these locales.
- The HTML body escapes the link; both bodies hold the link and the expiry;
  Arabic and Hebrew are right-to-left; an unknown locale falls back to
  English.
"""

import json
import typing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.schemas.account import ExportLocale
from app.services import export_email

FRONTEND_LOCALES = (
    Path(__file__).resolve().parents[3] / "frontend" / "src" / "i18n" / "locales"
)
EXPIRES = datetime(2026, 11, 30, 9, 5, tzinfo=UTC)
URL = "https://ref.supabase.co/storage/v1/object/sign/b/x.json?token=t&download=a.json"


def test_one_file_per_app_locale():
    files = {p.stem for p in export_email.LOCALES_DIR.glob("*.json")}
    assert files == set(export_email.SUPPORTED_LOCALES)
    app_locales = {
        p.name for p in FRONTEND_LOCALES.iterdir() if p.is_dir() and p.name != "_meta"
    }
    assert set(export_email.SUPPORTED_LOCALES) == app_locales
    assert set(typing.get_args(ExportLocale)) == app_locales


@pytest.mark.parametrize("locale", export_email.SUPPORTED_LOCALES)
def test_every_locale_has_every_string(locale):
    path = export_email.LOCALES_DIR / f"{locale}.json"
    strings = json.loads(path.read_text(encoding="utf-8"))["account_export"]
    assert set(strings) == set(export_email.KEYS)
    assert all(isinstance(v, str) and v.strip() for v in strings.values())
    for key, value in strings.items():
        assert ("{expires}" in value) == (key == "expires"), (locale, key)
        assert "{" not in value.replace("{expires}", ""), (locale, key)


@pytest.mark.parametrize("locale", export_email.SUPPORTED_LOCALES)
def test_render_puts_the_link_and_expiry_in_both_bodies(locale):
    message = export_email.render(locale, "reader@example.com", URL, EXPIRES)
    strings = export_email.strings(locale)
    assert message.to == "reader@example.com"
    assert message.subject == strings["subject"]
    assert URL in message.text
    assert "2026-11-30 09:05 UTC" in message.text
    assert "2026-11-30 09:05 UTC" in message.html
    escaped = URL.replace("&", "&amp;")
    assert f'href="{escaped}"' in message.html
    assert "&download" not in message.html
    direction = "rtl" if locale in ("ar", "he") else "ltr"
    assert f'<html lang="{locale}" dir="{direction}">' in message.html


def test_html_escapes_markup_in_the_link():
    message = export_email.render("en", "a@b.co", 'https://x/"><script>', EXPIRES)
    assert "<script>" not in message.html
    assert "&quot;&gt;&lt;script&gt;" in message.html


def test_an_unknown_locale_falls_back_to_english():
    message = export_email.render("xx", "a@b.co", URL, EXPIRES)
    assert message.subject == export_email.strings("en")["subject"]
    assert '<html lang="en" dir="ltr">' in message.html

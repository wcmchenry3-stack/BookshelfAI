"""The "your data export is ready" email (#694), in the app's languages.

Strings live in ``app/email_locales/<locale>.json`` under ``account_export``,
one file per app locale (the same 13 codes as ``frontend/src/i18n/locales``);
``tests/unit/test_export_email.py`` checks every file has every key and the
``{expires}`` placeholder. A locale without a file falls back to English.

:func:`render` builds the HTML and plain-text bodies. The signed link and
the expiry time are the only values put in; both are HTML-escaped in the
HTML body. The expiry is written ``YYYY-MM-DD HH:MM UTC`` in every language.
"""

import json
from datetime import datetime
from functools import cache
from html import escape
from pathlib import Path

from app.services.email import Message

LOCALES_DIR = Path(__file__).resolve().parent.parent / "email_locales"
DEFAULT_LOCALE = "en"
# The app's locales (frontend/src/i18n/locales); the request picks one.
SUPPORTED_LOCALES = (
    "ar",
    "de",
    "en",
    "es",
    "fr-CA",
    "he",
    "hi",
    "ja",
    "ko",
    "nl",
    "pt",
    "ru",
    "zh",
)
RTL_LOCALES = frozenset({"ar", "he"})
KEYS = ("subject", "heading", "intro", "button", "expires", "fallback", "not_you")


@cache
def strings(locale: str) -> dict[str, str]:
    """The ``account_export`` strings of ``locale`` (English when unknown)."""
    if locale not in SUPPORTED_LOCALES:
        locale = DEFAULT_LOCALE
    path = LOCALES_DIR / f"{locale}.json"
    loaded = json.loads(path.read_text(encoding="utf-8"))["account_export"]
    return {key: loaded[key] for key in KEYS}


def format_expiry(expires_at: datetime) -> str:
    return expires_at.strftime("%Y-%m-%d %H:%M UTC")


def render(locale: str, to: str, url: str, expires_at: datetime) -> Message:
    """The email to ``to`` with the download link ``url``, valid until
    ``expires_at`` (UTC)."""
    s = strings(locale)
    lang = locale if locale in SUPPORTED_LOCALES else DEFAULT_LOCALE
    direction = "rtl" if lang in RTL_LOCALES else "ltr"
    expires = s["expires"].format(expires=format_expiry(expires_at))
    text = "\n\n".join(
        [s["heading"], s["intro"], f"{s['button']}: {url}", expires, s["not_you"]]
    )
    link = escape(url, quote=True)
    html = (
        f'<!doctype html><html lang="{lang}" dir="{direction}"><body '
        'style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;'
        'line-height:1.5;color:#1f2933">'
        f"<h1 style=\"font-size:20px\">{escape(s['heading'])}</h1>"
        f"<p>{escape(s['intro'])}</p>"
        f'<p><a href="{link}" style="display:inline-block;padding:10px 16px;'
        'background:#2f5d50;color:#ffffff;text-decoration:none;border-radius:6px">'
        f"{escape(s['button'])}</a></p>"
        f"<p>{escape(expires)}</p>"
        f"<p style=\"font-size:13px\">{escape(s['fallback'])}<br>"
        f'<a href="{link}">{link}</a></p>'
        f"<p style=\"font-size:13px\">{escape(s['not_you'])}</p>"
        "</body></html>"
    )
    return Message(to=to, subject=s["subject"], html=html, text=text)

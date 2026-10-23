"""RedactingFilter edge modes not covered by test_log_redaction.py."""

import logging

from app.core.logging import RedactingFilter


def _record(msg, args=(), **kw):
    return logging.LogRecord("t", logging.INFO, "", 0, msg, args, None, **kw)


def test_preserve_args_redacts_a_placeholder_free_message():
    rec = _record("login by user@example.com")
    RedactingFilter(preserve_args=True).filter(rec)
    assert rec.msg == "login by [REDACTED_EMAIL]"


def test_preserve_args_leaves_a_format_string_alone_but_redacts_its_args():
    rec = _record("user %s", ("user@example.com",))
    RedactingFilter(preserve_args=True).filter(rec)
    assert rec.msg == "user %s"
    assert rec.getMessage() == "user [REDACTED_EMAIL]"


def test_preserve_args_redacts_dict_args_and_keeps_non_strings():
    rec = _record("%(who)s %(n)d", None)
    rec.args = {"who": "a@b.co", "n": 3}
    RedactingFilter(preserve_args=True).filter(rec)
    assert rec.args == {"who": "[REDACTED_EMAIL]", "n": 3}
    assert rec.getMessage() == "[REDACTED_EMAIL] 3"


def test_stack_info_is_redacted():
    rec = _record("x")
    rec.stack_info = "Stack: Authorization Bearer abc.def.ghi-token-value"
    RedactingFilter().filter(rec)
    assert "abc.def" not in rec.stack_info
    assert "Bearer [REDACTED]" in rec.stack_info


def test_filter_always_lets_the_record_through():
    assert RedactingFilter().filter(_record("hello")) is True

"""Every lint/type suppression in ``backend/app`` must say why (#819).

Reason format: after the code list, a ``-`` or an em dash, then text::

    except Exception:  # noqa: BLE001 - alerting must never break the request
    value = x["k"]  # type: ignore[index] - body is untyped JSON

Applies to ``# noqa`` (with or without codes) and ``# type: ignore[...]``.
A bare ``# noqa`` or ``# type: ignore`` is also rejected (name the code).
"""

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"

_NOQA = re.compile(r"#\s*noqa\b(?P<rest>.*)$", re.IGNORECASE)
_TYPE_IGNORE = re.compile(r"#\s*type:\s*ignore\b(?P<rest>.*)$")
_NOQA_CODES = re.compile(r"^:\s*[A-Z]+[0-9]+(?:\s*,\s*[A-Z]+[0-9]+)*")
_TYPE_IGNORE_CODES = re.compile(r"^\[[a-z0-9-]+(?:\s*,\s*[a-z0-9-]+)*\]")
_REASON = re.compile(r"^\s+[-—]\s+\S")


def _problems(line: str) -> list[str]:
    found = []
    for pattern, codes in ((_NOQA, _NOQA_CODES), (_TYPE_IGNORE, _TYPE_IGNORE_CODES)):
        match = pattern.search(line)
        if not match:
            continue
        rest = match.group("rest")
        code_match = codes.match(rest)
        if not code_match:
            found.append("missing code")
        elif not _REASON.match(rest[code_match.end() :]):
            found.append("missing reason")
    return found


def test_every_suppression_has_a_reason():
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            for problem in _problems(line):
                offenders.append(f"{path.relative_to(APP.parent)}:{number}: {problem}")
    assert not offenders, "suppressions need ' - <reason>':\n" + "\n".join(offenders)


def test_checker_accepts_and_rejects_the_documented_forms():
    assert _problems("x  # noqa: BLE001 - why") == []
    assert _problems("x  # noqa: BLE001, S110 — why") == []
    assert _problems("x  # type: ignore[index] - why") == []
    assert _problems("x  # noqa: BLE001") == ["missing reason"]
    assert _problems("x  # noqa: BLE001 -") == ["missing reason"]
    assert _problems("x  # type: ignore[index]") == ["missing reason"]
    assert _problems("x  # type: ignore") == ["missing code"]
    assert _problems("x  # noqa") == ["missing code"]
    assert _problems("x = 1") == []

"""Every library and account write honours ``Idempotency-Key`` (#685).

Enumerates every ``POST`` / ``PATCH`` / ``PUT`` / ``DELETE`` route on the
real app. Each must either be keyed (its endpoint carries the
``@idempotent`` wrapper, ``is_idempotent``, and its route class is an
``IdempotencyRoute``, which stores the response; without it a claimed key
would stay pending) or be listed in ``NOT_KEYED`` with the reason. A new
write route therefore fails here until someone decides.

pytest -m security -v tests/security/test_idempotency_coverage.py
"""

from collections.abc import Iterator

import pytest
from fastapi.routing import APIRoute

from app.api._idempotency import IdempotencyRoute, is_idempotent
from app.main import app

pytestmark = pytest.mark.security

_WRITES = {"POST", "PATCH", "PUT", "DELETE"}

# "METHOD /path" -> why the route takes no Idempotency-Key.
NOT_KEYED: dict[str, str] = {
    "POST /auth/google": "sign-in: a retry signs in again, issuing a fresh session",
    "POST /auth/refresh": "rotation has its own reuse grace (#690)",
    "POST /auth/logout": "revoking a revoked token changes nothing",
    "POST /auth/logout-all": "bumping the token version again changes nothing",
    "POST /auth/test-login": "dev-only E2E login, not registered when hardened",
    "POST /scan": "the scan queue owns its retries; repeats are scan records (#585)",
    "POST /attestation/nonce": "issues a fresh single-use nonce, nothing to replay",
    "POST /attestation/register": "nonce-bound and single-use, a replay is refused",
}

# The writes the issue names: library, wishlist, custom books, account.
KEYED = {
    "POST /wishlist",
    "POST /wishlist/unverified",
    "POST /purchased",
    "PATCH /user-books/{user_book_id}",
    "DELETE /user-books/{user_book_id}",
    "POST /user-books/{user_book_id}/restore",
    "PATCH /users/me/preferences",
    "POST /users/me/ai-consent",
    "DELETE /users/me/ai-consent",
    "POST /account/export",
}


def _write_routes() -> Iterator[tuple[str, APIRoute, object]]:
    """``("METHOD /path", route, endpoint)`` for every write route, routers
    included (see ``test_rate_limit_coverage.iter_endpoints``)."""
    for route in app.routes:
        if isinstance(route, APIRoute):
            contexts = [(route.path, route.methods, route, route.endpoint)]
        elif hasattr(route, "effective_route_contexts"):
            contexts = [
                (c.path_format, c.methods, c.original_route, c.endpoint)
                for c in route.effective_route_contexts()
                if isinstance(c.original_route, APIRoute)
            ]
        else:
            continue
        for path, methods, original, endpoint in contexts:
            for method in sorted(set(methods or ()) & _WRITES):
                yield f"{method} {path}", original, endpoint


def test_every_write_is_keyed_or_listed():
    unlisted = [
        name
        for name, _, endpoint in _write_routes()
        if not is_idempotent(endpoint) and name not in NOT_KEYED
    ]
    assert unlisted == [], (
        "write routes without @idempotent: add it (under @limiter.limit, on a "
        f"router with an IdempotencyRoute route class) or list them: {unlisted}"
    )


def test_the_issue_writes_are_keyed_on_an_idempotency_route():
    routes = {name: (route, endpoint) for name, route, endpoint in _write_routes()}
    assert KEYED <= set(routes)
    for name in KEYED:
        route, endpoint = routes[name]
        assert is_idempotent(endpoint), name
        assert isinstance(route, IdempotencyRoute), name


def test_every_keyed_route_stores_its_response():
    for name, route, endpoint in _write_routes():
        if is_idempotent(endpoint):
            assert isinstance(route, IdempotencyRoute), name
            assert name in KEYED, f"{name} is keyed: add it to KEYED and docs/api.md"


def test_the_exclusions_are_real_unkeyed_routes():
    names = {
        name for name, _, endpoint in _write_routes() if not is_idempotent(endpoint)
    }
    # test-login exists only outside hardened environments; it is in this app.
    assert set(NOT_KEYED) <= names

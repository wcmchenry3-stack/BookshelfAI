"""Every API route must be registered with the slowapi limiter (#761).

CLAUDE.md hard rule 8: every endpoint carries
``@limiter.limit(settings.rate_limit_*)``. This test enumerates every
``APIRoute`` on the real app (routers mounted in ``app/main.py`` and routes
defined directly in it) and fails when one has no limit.

How "registered" is decided: slowapi's ``@limiter.limit`` records the limit in
``limiter._route_limits`` (static strings) or ``limiter._dynamic_route_limits``
(callables), keyed by ``f"{func.__module__}.{func.__name__}"``. The decorator
returns a ``functools.wraps`` wrapper, so ``route.endpoint`` carries the same
module and name. The check uses that key. It never reloads the ``limiter``
singleton (see ``tests/unit/test_environment_hardening.py``).

"Limit comes from ``settings.rate_limit_*``": checked by parsing the endpoint's
source (AST) for a ``limiter.limit(settings.rate_limit_<name>)`` decorator.
Comparing the resolved value instead would be brittle: a literal "5/minute"
equals ``rate_limit_auth`` today. Routes that deliberately use a literal are
listed in ``LITERAL_LIMIT_ALLOWLIST``.

Decorator order matters: there is no ``SlowAPIMiddleware``, so a limit is
enforced only by the wrapper ``@limiter.limit`` returns. If ``@limiter.limit``
sits ABOVE ``@router.get(...)``, FastAPI registers the original function
before slowapi wraps it, the limit is recorded in ``_route_limits`` (so the
key check passes) but never enforced. Two signals catch this: the registered
``route.endpoint`` must be a wrapper (``__wrapped__`` set by
``functools.wraps``), and, by AST, the route decorator must come before
(above) the ``.limit`` decorator in ``decorator_list``. Both are used because
either alone could miss an unusual decorator stack.

To allow an intentionally unlimited route, add ``"METHOD /path": "<reason>"``
to ``UNLIMITED_ALLOWLIST``. Keep it short and every entry justified.

pytest -m security -v tests/security/test_rate_limit_coverage.py
"""

import ast
import inspect
import textwrap
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute
from slowapi import Limiter

pytestmark = pytest.mark.security

# "METHOD /path" -> reason the route has no rate limit.
# Empty on purpose: FastAPI's /docs, /redoc and /openapi.json are plain
# starlette Routes (not APIRoute), so they are not enumerated here.
UNLIMITED_ALLOWLIST: dict[str, str] = {}

# "METHOD /path" -> reason the limit is a literal rather than settings.rate_limit_*.
LITERAL_LIMIT_ALLOWLIST: dict[str, str] = {
    "GET /debug/sentry-test": "dev-only (not registered when hardened); fixed tiny limit",
    "POST /auth/test-login": "dev-only E2E login (not registered when hardened); fixed tiny limit",
}


@dataclass(frozen=True)
class _Endpoint:
    path: str
    methods: tuple[str, ...]
    endpoint: Callable[..., Any]

    @property
    def keys(self) -> list[str]:
        return [f"{m} {self.path}" for m in self.methods]


def iter_endpoints(routes: Iterable[Any]) -> Iterator[_Endpoint]:
    """Yield every API endpoint, including routers added with ``include_router``.

    FastAPI 0.162 (pinned) no longer copies included routes into ``app.routes``;
    each ``include_router`` call leaves an ``_IncludedRouter`` whose
    ``effective_route_contexts()`` has the prefixed path, methods and endpoint.
    Iterating ``app.routes`` for ``APIRoute`` alone would see only the routes
    defined directly on the app and silently pass. Older FastAPI puts
    ``APIRoute`` objects straight in ``app.routes``; both shapes are handled.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield _Endpoint(
                route.path, tuple(sorted(route.methods or [])), route.endpoint
            )
        elif hasattr(route, "effective_route_contexts"):
            for ctx in route.effective_route_contexts():
                if isinstance(ctx.original_route, APIRoute):
                    yield _Endpoint(
                        ctx.path_format, tuple(sorted(ctx.methods)), ctx.endpoint
                    )


def _limiter_key(ep: _Endpoint) -> str:
    fn = ep.endpoint
    return f"{fn.__module__}.{fn.__name__}"


_ROUTE_DECORATORS = {
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "api_route",
    "head",
    "options",
}


def _decorator_list(ep: _Endpoint) -> list[ast.expr] | None:
    fn = inspect.unwrap(ep.endpoint)
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except (OSError, TypeError, SyntaxError):
        return None
    node = tree.body[0]
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    return node.decorator_list


def _limit_above_route(ep: _Endpoint) -> bool:
    """True when ``.limit`` is listed above (before) the route decorator."""
    decs = _decorator_list(ep) or []
    limit_idx = route_idx = None
    for i, dec in enumerate(decs):
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
            if dec.func.attr == "limit" and limit_idx is None:
                limit_idx = i
            elif dec.func.attr in _ROUTE_DECORATORS and route_idx is None:
                route_idx = i
    return limit_idx is not None and route_idx is not None and limit_idx < route_idx


def _uses_settings_limit(ep: _Endpoint) -> bool:
    """True when a ``limiter.limit(settings.rate_limit_*)`` decorator is on the endpoint."""
    fn = inspect.unwrap(ep.endpoint)
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except (OSError, TypeError, SyntaxError):
        return False
    node = tree.body[0]
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    for dec in node.decorator_list:
        if (
            isinstance(dec, ast.Call)
            and isinstance(dec.func, ast.Attribute)
            and dec.func.attr == "limit"
            and dec.args
            and isinstance(dec.args[0], ast.Attribute)
            and dec.args[0].attr.startswith("rate_limit_")
            and isinstance(dec.args[0].value, ast.Name)
            and dec.args[0].value.id == "settings"
        ):
            return True
    return False


def find_rate_limit_violations(
    app: FastAPI,
    limiter: Limiter,
    unlimited_allowlist: dict[str, str] | None = None,
    literal_allowlist: dict[str, str] | None = None,
) -> list[str]:
    """Return a message per route with no limit or a non-settings limit."""
    unlimited_allowlist = unlimited_allowlist or {}
    literal_allowlist = literal_allowlist or {}
    problems: list[str] = []
    for route in iter_endpoints(app.routes):
        keys = route.keys
        if all(k in unlimited_allowlist for k in keys):
            continue
        name = _limiter_key(route)
        registered = bool(limiter._route_limits.get(name)) or bool(
            limiter._dynamic_route_limits.get(name)
        )
        if not registered:
            problems.append(f"{', '.join(keys)}: no @limiter.limit ({name})")
        elif not hasattr(route.endpoint, "__wrapped__") or _limit_above_route(route):
            problems.append(
                f"{', '.join(keys)}: limit decorator is above the route decorator; "
                f"FastAPI registered the unwrapped function ({name})"
            )
        elif not all(k in literal_allowlist for k in keys) and not _uses_settings_limit(
            route
        ):
            problems.append(
                f"{', '.join(keys)}: limit is not settings.rate_limit_* ({name})"
            )
    return problems


def test_every_route_has_a_rate_limit():
    from app.core.limiter import limiter
    from app.main import app

    paths = {ep.path for ep in iter_endpoints(app.routes)}
    # Guard against the enumeration silently seeing nothing (see iter_endpoints).
    assert {"/health", "/auth/refresh", "/scan", "/user-books"} <= paths
    assert (
        find_rate_limit_violations(
            app, limiter, UNLIMITED_ALLOWLIST, LITERAL_LIMIT_ALLOWLIST
        )
        == []
    )


def test_allowlist_entries_have_reasons():
    for allow in (UNLIMITED_ALLOWLIST, LITERAL_LIMIT_ALLOWLIST):
        for key, reason in allow.items():
            assert reason.strip(), f"{key} has no reason"


def _tiny_app(lim: Limiter) -> FastAPI:
    app = FastAPI()

    class _S:
        rate_limit_reads = "5/minute"

    settings = _S()

    @app.get("/limited")
    @lim.limit(settings.rate_limit_reads)
    async def limited(request: Request):
        return {}

    @app.get("/naked")
    async def naked():
        return {}

    @app.get("/literal")
    @lim.limit("5/minute")
    async def literal(request: Request):
        return {}

    return app


def test_self_check_flags_unlimited_and_literal_routes():
    lim = Limiter(key_func=lambda r: "k")
    app = _tiny_app(lim)
    problems = find_rate_limit_violations(app, lim)
    assert len(problems) == 2
    assert any("GET /naked" in p and "no @limiter.limit" in p for p in problems)
    assert any("GET /literal" in p and "not settings" in p for p in problems)
    assert not any("/limited" in p for p in problems)


def test_self_check_honours_allowlists():
    lim = Limiter(key_func=lambda r: "k")
    app = _tiny_app(lim)
    assert (
        find_rate_limit_violations(
            app,
            lim,
            {"GET /naked": "test"},
            {"GET /literal": "test"},
        )
        == []
    )


def test_self_check_sees_routes_from_included_routers():
    from fastapi import APIRouter

    lim = Limiter(key_func=lambda r: "k")
    router = APIRouter(prefix="/sub")

    @router.post("/naked-in-router")
    async def naked_in_router():
        return {}

    app = FastAPI()
    app.include_router(router)
    problems = find_rate_limit_violations(app, lim)
    assert len(problems) == 1 and "POST /sub/naked-in-router" in problems[0]


def test_self_check_flags_limit_above_route_and_it_is_not_enforced():
    from fastapi.testclient import TestClient

    lim = Limiter(key_func=lambda r: "k")
    app = FastAPI()
    app.state.limiter = lim

    class _S:
        rate_limit_reads = "2/minute"

    settings = _S()

    @lim.limit(settings.rate_limit_reads)  # wrong: above the route decorator
    @app.get("/reversed")
    async def reversed_order(request: Request):
        return {}

    problems = find_rate_limit_violations(app, lim)
    assert len(problems) == 1
    assert "limit decorator is above the route decorator" in problems[0]

    # Why the order matters: the route is genuinely unlimited.
    client = TestClient(app)
    assert [client.get("/reversed").status_code for _ in range(5)] == [200] * 5

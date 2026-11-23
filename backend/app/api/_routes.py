"""A route class base for work on the finished response (#685)."""

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, Response
from fastapi.routing import APIRoute


class AfterResponseRoute(APIRoute):
    """Runs :meth:`after_response` on every response the route's handler
    returns (not on an exception it raises), before the response is sent.

    Subclasses override :meth:`after_response` and call ``super()`` in it, so
    several can be combined as bases: the one listed first runs its own work
    around the later ones (``VersionETagRoute`` sets the ``ETag``, then
    ``IdempotencyRoute`` stores the response with it)."""

    async def after_response(self, request: Request, response: Response) -> None:
        """The last in the chain: nothing to do."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def route_handler(request: Request) -> Response:
            response = await handler(request)
            await self.after_response(request, response)
            return response

        return route_handler

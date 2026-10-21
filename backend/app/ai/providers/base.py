"""Provider interfaces used by the gateway."""

from typing import Protocol

from app.services.book_identifier import BookCandidate


class VisionProvider(Protocol):
    """Identifies books in one image.

    A provider is built for one :class:`~app.ai.registry.FeatureSpec` and takes
    its model, token cap and timeout from that spec only.

    Raises ``ScanUnavailableError`` when the provider times out or returns an
    HTTP error. ``httpx.ConnectError`` (never connected) propagates as-is.
    """

    async def identify(self, image_bytes: bytes) -> list[BookCandidate]: ...

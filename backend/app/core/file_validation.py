"""Magic bytes validation for uploaded image files.

Extension and Content-Type headers are both client-supplied and can be spoofed.
This module verifies that the actual file content matches the declared format,
catching polyglot files and content/extension mismatches before bytes are
forwarded to downstream services (e.g. OpenAI Vision API).

Note on EXIF/metadata: images from mobile cameras may embed GPS coordinates and
other metadata. Uploads are never forwarded as-is: after this check,
app.ai.images re-encodes every image and drops all metadata before any provider
call.
"""

# Maps file extension to one or more recognised byte signatures.
# Only the minimum discriminating prefix is checked — not the full header.
_MAGIC: dict[str, list[bytes]] = {
    ".jpg": [b"\xff\xd8\xff"],
    ".jpeg": [b"\xff\xd8\xff"],
    ".png": [b"\x89PNG\r\n\x1a\n"],
    ".webp": [b"RIFF"],  # "RIFF" at 0; "WEBP" at offset 8 — checked below
}

# HEIC/HEIF use an ISO Base Media File Format (ISOBMFF) "ftyp" box.
# Layout: 4-byte box size | "ftyp" | 4-byte brand (heic/heix/mif1/msf1/…)
_HEIC_BRANDS = {b"heic", b"heix", b"mif1", b"msf1", b"hevc", b"hevx"}
_HEIF_EXTENSIONS = {".heic", ".heif"}


def validate_magic_bytes(ext: str, data: bytes) -> bool:
    """Return True if *data* starts with bytes consistent with *ext*.

    Args:
        ext: Lowercase file extension including the leading dot (e.g. ".jpg").
        data: Raw file bytes (at least the first 12 bytes are needed).

    Returns:
        True if the magic bytes match, False otherwise.
    """
    if len(data) < 4:
        return False

    if ext in _HEIF_EXTENSIONS:
        # ftyp box: bytes [4:8] == b"ftyp", bytes [8:12] == brand
        if len(data) < 12:
            return False
        return data[4:8] == b"ftyp" and data[8:12] in _HEIC_BRANDS

    if ext == ".webp":
        # Must start with RIFF and have WEBP at offset 8
        return data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP"

    signatures = _MAGIC.get(ext, [])
    return any(data[: len(sig)] == sig for sig in signatures)

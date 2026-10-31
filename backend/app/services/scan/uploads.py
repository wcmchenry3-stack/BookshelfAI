"""The scan upload's read and file checks, in the order the route runs them:
extension and Content-Type (before any byte is read), a capped streaming read,
magic bytes, then the antivirus scan. Normalisation (decode, pixel cap, EXIF
strip, re-encode) happens afterwards in the route."""

from fastapi import HTTPException, UploadFile, status

from app.core.file_security import scan_for_malware
from app.core.file_validation import validate_magic_bytes

MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5MB
# Upload read size; the handler never holds more than MAX_IMAGE_BYTES + 1 bytes.
READ_CHUNK_BYTES = 64 * 1024
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}


async def read_upload_capped(file: UploadFile, limit: int) -> bytes:
    """Read ``file`` in chunks; 413 as soon as more than ``limit`` bytes arrive.

    Each read asks for at most the bytes still allowed plus one, so no more
    than ``limit + 1`` bytes are ever read into memory.
    """
    buf = bytearray()
    while True:
        chunk = await file.read(min(READ_CHUNK_BYTES, limit + 1 - len(buf)))
        if not chunk:
            return bytes(buf)
        buf += chunk
        if len(buf) > limit:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Image must be 5MB or smaller",
            )


async def read_validated_upload(file: UploadFile) -> bytes:
    """The upload's bytes once its extension, Content-Type, size (5MB), magic
    bytes and antivirus scan have passed; 415 / 413 otherwise. Runs before the
    quota is reserved, so a refusal here takes nothing."""
    # --- Validate extension and Content-Type BEFORE reading the body ---
    filename = file.filename or ""
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="File must be a JPEG, PNG, WebP, or HEIC image",
        )

    # Content-Type is in the multipart header — check it before reading bytes
    # so an invalid MIME type is rejected without consuming the upload body.
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="File must be an image",
        )

    # Streaming cap: stop reading as soon as the upload passes 5MB.
    image_bytes = await read_upload_capped(file, MAX_IMAGE_BYTES)

    # Verify actual file content matches the declared extension.
    # Extension and Content-Type are both client-supplied and can be spoofed;
    # magic bytes confirm the payload looks like the declared format before
    # anything tries to decode it.
    if not validate_magic_bytes(ext, image_bytes):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="File content does not match declared type",
        )

    # Antivirus scan (optional, controlled by CLAMAV_ENABLED setting).
    # Runs on the original bytes before any re-encoding.
    scan_for_malware(image_bytes)
    return image_bytes

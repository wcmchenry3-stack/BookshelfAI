"""The areas ``python -m app.ai.settings list`` groups the keys by (#581)."""

from app.ai import settings as ai_settings

# Areas for ``list`` (#581), in display order. The first matching key prefix
# wins; a key that matches none is shown under OTHER_AREA (a test fails then,
# so a new key gets an area here and a row in docs/scan-limits.md).
AREA_RULES: tuple[tuple[str, str], ...] = (
    ("scan.standard.enabled", "Switches"),
    ("scan.enhanced.enabled", "Switches"),
    ("scan.standard.", "Providers and models"),
    ("scan.enhanced.zero_result_", "Enhanced scans"),
    ("scan.enhanced.", "Providers and models"),
    ("scan.requests_per_user_daily", "Request and global limits"),
    ("scan.global_daily_limit", "Request and global limits"),
    ("scan.max_image_pixels", "Uploads and images"),
    ("quota.free.", "Allowances: Free"),
    ("quota.premium.", "Allowances: Premium"),
    ("breaker.", "Spend breaker"),
    ("scan_records.", "Scan records (repeat photos)"),
)
OTHER_AREA = "Other"


def area_of(key: str) -> str:
    """The ``list`` group of ``key``."""
    for prefix, area in ai_settings.AREA_RULES:
        if key.startswith(prefix):
            return area
    return ai_settings.OTHER_AREA


def keys_by_area() -> dict[str, tuple[str, ...]]:
    """Every known key grouped by area, areas in :data:`AREA_RULES` order and
    keys in registry order within an area."""
    order = [
        *dict.fromkeys(area for _, area in ai_settings.AREA_RULES),
        ai_settings.OTHER_AREA,
    ]
    grouped: dict[str, list[str]] = {area: [] for area in order}
    for key in ai_settings.KNOWN_KEYS:
        grouped[ai_settings.area_of(key)].append(key)
    return {area: tuple(keys) for area, keys in grouped.items() if keys}

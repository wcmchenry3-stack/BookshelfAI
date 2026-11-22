"""The ``POST /scan`` pipeline's helpers (#808), used by ``app/api/scan.py``.

The route module keeps the routers, the request/response wiring, the rate-limit
decorators and the step functions that make up ``scan()``; these modules hold
the helpers those steps call:

- ``features``: the gateway feature names a scan runs under;
- ``uploads``: the upload read and file checks (size, type, magic bytes,
  antivirus);
- ``quota_steps``: the quota reserve / refund / lease release mapped to HTTP
  refusals, and the settle-time decisions;
- ``records``: the per-user scan records (#585);
- ``settlement``: the cost settle and usage-ledger row builders;
- ``books``: the post-enrichment dedupe.

They log under ``SCAN_LOGGER``, the name the scan route has always logged
under, so log lines and the Sentry ``logger`` field are unchanged by the move.
"""

SCAN_LOGGER = "app.api.scan"

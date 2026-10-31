"""The named steps of ``POST /scan`` (#808): each step on its own, the order
``scan()`` runs them in, and where the reservation is refunded (and where it
is not) on every failure after it was taken.

Collaborators are patched on ``app.api.scan``, the targets the rest of the
suite uses, so these tests also pin that those patches reach the steps."""

import asyncio
import io
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image
from starlette.datastructures import Headers

from app.ai import quota
from app.ai.entitlements import Tier
from app.ai.gateway import AIResult, FeatureDisabledError
from app.ai.providers.base import ProviderTimeoutError
from app.api import scan as scan_api
from app.models.ai_usage import (
    STATUS_ERROR,
    STATUS_MALFORMED,
    STATUS_OK,
    STATUS_REFUNDED,
    STATUS_TIMEOUT,
)
from app.services.book_identifier import BookCandidate, ScanUnavailableError
from app.services.scan import quota_steps, uploads

_USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
_CALL_ID = uuid.UUID("00000000-0000-0000-0000-0000000000cc")


def _result(*, outcome=STATUS_OK, books=1, feature="scan.standard") -> AIResult:
    return AIResult(
        feature=feature,
        provider="p",
        model="m",
        candidates=[
            BookCandidate(title=f"T{i}", author="A", confidence=0.9)
            for i in range(books)
        ],
        tier=Tier.FREE,
        outcome=outcome,
    )


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buf, format="JPEG")
    return buf.getvalue()


def _upload(data: bytes, *, name="s.jpg", content_type="image/jpeg") -> UploadFile:
    return UploadFile(
        file=io.BytesIO(data),
        filename=name,
        headers=Headers({"content-type": content_type} if content_type else {}),
    )


# ---------------------------------------------------------------------------
# _run_provider: the provider call, refund and settle
# ---------------------------------------------------------------------------


class _Money:
    """Spies for everything _run_provider touches, recording call order."""

    def __init__(self, provider):
        self.calls: list[str] = []
        self.released: dict = {}
        self.provider = provider

        async def refund(db, reservation):
            self.calls.append("refund")

        async def release(db, reservation, ledger_row, settlement, zr, nm):
            self.calls.append("release")
            self.released = {
                "row": ledger_row,
                "settlement": settlement,
                "zero_result": zr,
                "no_match": nm,
            }
            return "moved"

        def row(*, status=None, result=None, **_kw):
            return ("row", status if result is None else result.outcome)

        def settle(*, status=None, result=None, **_kw):
            return ("spend", status if result is None else result.outcome)

        self.refund, self.release = AsyncMock(side_effect=refund), release
        self.row, self.settle = row, settle

    def patches(self):
        return (
            patch.object(scan_api, "run_feature", self.provider),
            patch.object(scan_api, "_refund_scan_quota", self.refund),
            patch.object(scan_api, "_release_scan_lease", self.release),
            patch.object(scan_api, "_usage_row", self.row),
            patch.object(scan_api, "_settlement", self.settle),
        )

    async def run(self, *, enhanced=False):
        p = self.patches()
        with p[0], p[1], p[2], p[3], p[4]:
            return await scan_api._run_provider(
                AsyncMock(),
                MagicMock(),
                user_id=_USER_ID,
                reservation=MagicMock(),
                call_id=_CALL_ID,
                feature="scan.enhanced" if enhanced else "scan.standard",
                enhanced=enhanced,
                image_bytes=b"img",
                ai=MagicMock(),
                attempt=1,
            )


class TestRunProviderRefunds:
    """Every exception path of the provider call: refunded or kept, the
    ledger row and cost settled, and the lease always released after any
    refund."""

    @pytest.mark.parametrize(
        ("exc", "row", "spend"),
        [
            (ScanUnavailableError("down"), ("row", STATUS_REFUNDED), None),
            (httpx.ConnectError("no route"), ("row", STATUS_REFUNDED), None),
            (
                ProviderTimeoutError("slow"),
                ("row", STATUS_TIMEOUT),
                ("spend", STATUS_TIMEOUT),
            ),
            # A kill switch hit never reached a provider: no row, no spend.
            (FeatureDisabledError("off"), None, None),
        ],
        ids=["unavailable", "connect_error", "timeout", "feature_disabled"],
    )
    async def test_unavailable_is_refunded_then_settled_as_503(self, exc, row, spend):
        money = _Money(AsyncMock(side_effect=exc))
        with pytest.raises(HTTPException) as err:
            await money.run()
        assert (err.value.status_code, err.value.detail) == (503, "scan_unavailable")
        assert money.calls == ["refund", "release"]
        assert money.released["row"] == row
        assert money.released["settlement"] == spend
        assert money.released["zero_result"] is None
        assert money.released["no_match"] is None

    async def test_other_failure_keeps_the_reservation_and_reraises(self):
        money = _Money(AsyncMock(side_effect=RuntimeError("boom")))
        with pytest.raises(RuntimeError, match="boom"):
            await money.run()
        assert money.calls == ["release"]
        assert money.released["row"] == ("row", STATUS_ERROR)
        assert money.released["settlement"] == ("spend", STATUS_ERROR)

    async def test_cancellation_keeps_the_reservation_and_settles_an_error(self):
        money = _Money(AsyncMock(side_effect=asyncio.CancelledError()))
        with pytest.raises(asyncio.CancelledError):
            await money.run()
        assert money.calls == ["release"]
        assert money.released["row"] == ("row", STATUS_ERROR)
        assert money.released["settlement"] == ("spend", STATUS_ERROR)

    async def test_malformed_reply_is_refunded_and_settles_its_priced_cost(self):
        money = _Money(AsyncMock(return_value=_result(outcome=STATUS_MALFORMED)))
        with pytest.raises(HTTPException) as err:
            await money.run()
        assert (err.value.status_code, err.value.detail) == (503, "scan_unavailable")
        assert money.calls == ["refund", "release"]
        assert money.released["row"] == ("row", STATUS_MALFORMED)
        assert money.released["settlement"] == ("spend", STATUS_MALFORMED)
        assert money.released["zero_result"] is None

    async def test_answer_is_kept_and_settled(self):
        answer = _result(books=2)
        money = _Money(AsyncMock(return_value=answer))
        assert await money.run() == (answer, "moved")
        assert money.calls == ["release"]
        assert money.released["row"] == ("row", STATUS_OK)
        assert money.released["settlement"] == ("spend", STATUS_OK)
        assert money.released["zero_result"] is None
        assert money.released["no_match"] is None

    async def test_standard_no_books_passes_a_no_match_to_the_settle(self):
        money = _Money(AsyncMock(return_value=_result(books=0)))
        await money.run()
        assert money.calls == ["release"]
        assert money.released["no_match"] == quota.NoMatch()
        assert money.released["zero_result"] is None

    async def test_enhanced_no_books_passes_a_zero_result_to_the_settle(self):
        money = _Money(AsyncMock(return_value=_result(books=0)))
        await money.run(enhanced=True)
        assert money.calls == ["release"]
        assert money.released["zero_result"] == quota.ZeroResult(ledger_id=_CALL_ID)
        assert money.released["no_match"] is None

    async def test_refund_failure_still_releases_the_lease(self):
        money = _Money(AsyncMock(side_effect=ScanUnavailableError("down")))
        money.refund.side_effect = RuntimeError("refund blew up")
        with pytest.raises(RuntimeError, match="refund blew up"):
            await money.run()
        assert money.calls == ["release"]
        assert money.released["row"] == ("row", STATUS_REFUNDED)


class TestSettleDecisions:
    @pytest.mark.parametrize(
        ("enhanced", "outcome", "books", "row", "zero", "no_match"),
        [
            (False, STATUS_OK, 0, "r", None, quota.NoMatch()),
            (False, STATUS_OK, 2, "r", None, None),
            (False, "refused", 0, "r", None, None),
            (True, STATUS_OK, 0, "r", quota.ZeroResult(ledger_id=_CALL_ID), None),
            (True, "refused", 0, "r", quota.ZeroResult(ledger_id=_CALL_ID), None),
            (True, STATUS_OK, 0, None, quota.ZeroResult(ledger_id=None), None),
            (True, STATUS_OK, 1, "r", None, None),
        ],
    )
    def test_decisions(self, enhanced, outcome, books, row, zero, no_match):
        got = quota_steps.settle_decisions(
            _result(outcome=outcome, books=books),
            enhanced=enhanced,
            call_id=_CALL_ID,
            ledger_row=row,
        )
        assert got == (zero, no_match)


# ---------------------------------------------------------------------------
# _record_and_enrich: after the settle; never refunds
# ---------------------------------------------------------------------------


class TestRecordAndEnrich:
    @staticmethod
    def _patches(order, *, saved_at="t0", complete=True, enrich_exc=None):
        def step(name, value):
            async def fn(*_a, **_kw):
                order.append(name)
                if name == "enrich" and enrich_exc is not None:
                    raise enrich_exc
                return value

            return fn

        books = [SimpleNamespace(already_in_library=True)]
        return [
            patch.object(scan_api, "_save_ai_step", step("save_ai", saved_at)),
            patch.object(scan_api, "_enrich", step("enrich", (books, complete))),
            patch.object(scan_api, "_save_enrichment_step", step("save_enr", True)),
            patch.object(scan_api, "_saved_record_id", step("record_id", "rid")),
            patch.object(scan_api, "_mark_library", step("mark", books)),
            patch.object(scan_api.scan_records, "log_event"),
            patch.object(scan_api, "_refund_scan_quota", AsyncMock()),
        ]

    async def _run(self, patches):
        for p in patches:
            p.start()
        try:
            return await scan_api._record_and_enrich(
                AsyncMock(),
                fingerprint="f" * 64,
                user_id=_USER_ID,
                reservation=MagicMock(),
                result=_result(),
                ai=MagicMock(),
                attempt=3,
            )
        finally:
            for p in reversed(patches):
                p.stop()

    async def test_saved_answer_order_and_log(self):
        order: list[str] = []
        patches = self._patches(order)
        log, refund = patches[5], patches[6]
        with log as log_event, refund as refund_spy:
            books, record_id = await self._run(patches[:5])
        assert order == ["save_ai", "enrich", "save_enr", "record_id", "mark"]
        assert record_id == "rid" and len(books) == 1
        log_event.assert_called_once_with(
            "saved", ai_done=True, enrich_done=True, books=1, in_library=1, attempt=3
        )
        refund_spy.assert_not_awaited()

    async def test_unsaved_answer_skips_the_enrichment_save_and_the_log(self):
        order: list[str] = []
        patches = self._patches(order, saved_at=None)
        with patches[5] as log_event:
            await self._run(patches[:5])
        assert order == ["save_ai", "enrich", "record_id", "mark"]
        log_event.assert_not_called()

    async def test_incomplete_enrichment_is_not_saved(self):
        order: list[str] = []
        patches = self._patches(order, complete=False)
        with patches[5] as log_event:
            await self._run(patches[:5])
        assert "save_enr" not in order
        assert log_event.call_args.kwargs["enrich_done"] is False

    async def test_enrichment_failure_propagates_without_a_refund(self):
        """After the settle the unit stays charged: an enrichment crash is
        a 500 with the reservation kept, as before the split."""
        order: list[str] = []
        patches = self._patches(order, enrich_exc=RuntimeError("ol down"))
        with patches[6] as refund_spy, pytest.raises(RuntimeError, match="ol down"):
            await self._run(patches[:6])
        assert order == ["save_ai", "enrich"]
        refund_spy.assert_not_awaited()


# ---------------------------------------------------------------------------
# The steps before the reservation
# ---------------------------------------------------------------------------


class TestLoadScanSettings:
    @staticmethod
    def _ai(**kw):
        return SimpleNamespace(
            scan_enabled=kw.get("scan", True),
            enhanced_scan_enabled=kw.get("enhanced", True),
        )

    async def test_returns_the_settings(self):
        ai = self._ai()
        with patch.object(scan_api, "get_ai_settings", AsyncMock(return_value=ai)):
            assert await scan_api._load_scan_settings(AsyncMock(), True) is ai

    @pytest.mark.parametrize(
        ("ai_kw", "enhanced", "detail"),
        [
            ({"scan": False}, False, "scan_disabled"),
            ({"enhanced": False}, True, "enhanced_scan_disabled"),
        ],
    )
    async def test_kill_switches(self, ai_kw, enhanced, detail):
        ai = self._ai(**ai_kw)
        with (
            patch.object(scan_api, "get_ai_settings", AsyncMock(return_value=ai)),
            pytest.raises(HTTPException) as err,
        ):
            await scan_api._load_scan_settings(AsyncMock(), enhanced)
        assert (err.value.status_code, err.value.detail) == (503, detail)

    async def test_standard_scan_ignores_the_enhanced_switch(self):
        ai = self._ai(enhanced=False)
        with patch.object(scan_api, "get_ai_settings", AsyncMock(return_value=ai)):
            assert await scan_api._load_scan_settings(AsyncMock(), False) is ai

    async def test_unreadable_settings_fail_closed(self):
        from app.ai.settings import AISettingsUnavailableError

        with (
            patch.object(
                scan_api,
                "get_ai_settings",
                AsyncMock(side_effect=AISettingsUnavailableError()),
            ),
            pytest.raises(HTTPException) as err,
        ):
            await scan_api._load_scan_settings(AsyncMock(), False)
        assert (err.value.status_code, err.value.detail) == (503, "ai_unavailable")


class TestCheckTurnstile:
    async def test_skipped_when_not_required(self):
        verify = AsyncMock()
        with (
            patch.object(scan_api.settings, "turnstile_required", False),
            patch.object(scan_api, "_verify_turnstile", verify),
        ):
            await scan_api._check_turnstile(None)
        verify.assert_not_awaited()

    @pytest.mark.parametrize(
        ("token", "valid", "code"),
        [(None, True, 400), ("", True, 400), ("bad", False, 403)],
    )
    async def test_refusals(self, token, valid, code):
        with (
            patch.object(scan_api.settings, "turnstile_required", True),
            patch.object(scan_api, "_verify_turnstile", AsyncMock(return_value=valid)),
            pytest.raises(HTTPException) as err,
        ):
            await scan_api._check_turnstile(token)
        assert err.value.status_code == code

    async def test_valid_token_passes(self):
        verify = AsyncMock(return_value=True)
        with (
            patch.object(scan_api.settings, "turnstile_required", True),
            patch.object(scan_api, "_verify_turnstile", verify),
        ):
            await scan_api._check_turnstile("tok")
        verify.assert_awaited_once_with("tok")


class TestReadValidatedUpload:
    async def test_valid_upload_is_read_and_malware_scanned(self):
        data = _jpeg()
        with patch.object(uploads, "scan_for_malware") as av:
            assert await uploads.read_validated_upload(_upload(data)) == data
        av.assert_called_once_with(data)

    @pytest.mark.parametrize(
        ("name", "ctype", "detail"),
        [
            ("s.gif", "image/gif", "File must be a JPEG, PNG, WebP, or HEIC image"),
            ("noext", "image/jpeg", "File must be a JPEG, PNG, WebP, or HEIC image"),
            ("s.jpg", "text/plain", "File must be an image"),
            ("s.jpg", None, "File must be an image"),
        ],
    )
    async def test_header_checks_refuse_before_reading(self, name, ctype, detail):
        upload = _upload(_jpeg(), name=name, content_type=ctype)
        upload.read = AsyncMock()
        with pytest.raises(HTTPException) as err:
            await uploads.read_validated_upload(upload)
        assert (err.value.status_code, err.value.detail) == (415, detail)
        upload.read.assert_not_awaited()

    async def test_spoofed_content_is_refused_before_the_malware_scan(self):
        with (
            patch.object(uploads, "scan_for_malware") as av,
            pytest.raises(HTTPException) as err,
        ):
            await uploads.read_validated_upload(_upload(b"not a jpeg at all"))
        assert err.value.detail == "File content does not match declared type"
        av.assert_not_called()

    async def test_oversize_is_413_without_reading_past_the_cap(self):
        big = b"\xff\xd8\xff" + b"0" * uploads.MAX_IMAGE_BYTES
        upload = _upload(big)
        with pytest.raises(HTTPException) as err:
            await uploads.read_validated_upload(upload)
        assert err.value.status_code == 413
        assert upload.file.tell() <= uploads.MAX_IMAGE_BYTES + 1


class TestServeSavedAnswer:
    async def _call(self, *, record, enhanced=False, fingerprint="f" * 64):
        serve = AsyncMock(return_value="repeat-response")
        entitle = AsyncMock(return_value=Tier.FREE)
        with (
            patch.object(scan_api, "_record_lookup", AsyncMock(return_value=record)),
            patch.object(scan_api, "_require_enhanced_entitlement", entitle),
            patch.object(scan_api, "_serve_repeat", serve),
        ):
            got = await scan_api._serve_saved_answer(
                AsyncMock(),
                MagicMock(),
                user_id=_USER_ID,
                feature="scan.enhanced" if enhanced else "scan.standard",
                enhanced=enhanced,
                fingerprint=fingerprint,
                ai=MagicMock(),
                attempt=1,
            )
        return got, entitle, serve

    async def test_no_record_takes_the_normal_path(self):
        got, entitle, serve = await self._call(record=None)
        assert got is None
        entitle.assert_not_awaited()
        serve.assert_not_awaited()

    async def test_no_fingerprint_takes_the_normal_path(self):
        got, _, serve = await self._call(
            record=SimpleNamespace(feature="scan.standard"), fingerprint=None
        )
        assert got is None
        serve.assert_not_awaited()

    async def test_standard_record_does_not_answer_an_enhanced_scan(self):
        got, entitle, _ = await self._call(
            record=SimpleNamespace(feature="scan.standard"), enhanced=True
        )
        assert got is None
        entitle.assert_not_awaited()

    async def test_matching_record_is_served_after_the_entitlement_check(self):
        got, entitle, serve = await self._call(
            record=SimpleNamespace(feature="scan.standard")
        )
        assert got == "repeat-response"
        entitle.assert_awaited_once()
        assert serve.await_args.kwargs["tier"] is Tier.FREE

    async def test_entitlement_refusal_propagates(self):
        with (
            patch.object(
                scan_api,
                "_record_lookup",
                AsyncMock(return_value=SimpleNamespace(feature="scan.enhanced")),
            ),
            patch.object(
                quota,
                "require_enhanced_entitlement",
                AsyncMock(side_effect=quota.EnhancedRequiresPremium()),
            ),
            pytest.raises(HTTPException) as err,
        ):
            await scan_api._serve_saved_answer(
                AsyncMock(),
                MagicMock(),
                user_id=_USER_ID,
                feature="scan.enhanced",
                enhanced=True,
                fingerprint="f" * 64,
                ai=MagicMock(),
                attempt=1,
            )
        assert (err.value.status_code, err.value.detail) == (
            403,
            "enhanced_requires_premium",
        )


class TestReserveScanQuotaFailsClosed:
    async def test_unexpected_error_is_not_mapped_to_a_refusal(self):
        """Only the refusal classes become 4xx/503; anything else propagates
        (a 500 before any provider call)."""
        with (
            patch.object(quota, "reserve", AsyncMock(side_effect=KeyError("x"))),
            pytest.raises(KeyError),
        ):
            await scan_api._reserve_scan_quota(
                AsyncMock(), MagicMock(), "scan.standard", MagicMock(), False
            )


# ---------------------------------------------------------------------------
# scan(): the steps in order, and what runs after each failure
# ---------------------------------------------------------------------------


class _Pipeline:
    """Every step of scan() replaced by a recorder on app.api.scan."""

    STEPS = (
        "_load_scan_settings",
        "_count_scan_request",
        "_check_turnstile",
        "_read_validated_upload",
        "_normalise_image",
        "_serve_saved_answer",
        "_reserve_scan_quota",
        "_run_provider",
        "_record_and_enrich",
        "_enhanced_remaining",
    )

    def __init__(self, *, fail: str | None = None, repeat=None):
        self.order: list[str] = []
        self.refund = AsyncMock()
        returns = {
            "_load_scan_settings": MagicMock(),
            "_read_validated_upload": b"raw",
            "_normalise_image": b"norm",
            "_serve_saved_answer": repeat,
            "_reserve_scan_quota": MagicMock(),
            "_run_provider": (_result(), None),
            "_record_and_enrich": ([], None),
            "_enhanced_remaining": None,
        }
        self.mocks = {}
        for name in self.STEPS:

            async def fn(*_a, _name=name, **_kw):
                self.order.append(_name)
                if _name == fail:
                    raise HTTPException(status_code=599, detail=_name)
                return returns.get(_name)

            self.mocks[name] = fn

    async def run(self):
        patches = [patch.object(scan_api, n, f) for n, f in self.mocks.items()]
        patches += [
            patch.object(scan_api, "_fingerprint", MagicMock(return_value="fp")),
            patch.object(scan_api, "_refund_scan_quota", self.refund),
            patch.object(scan_api.catalog_token, "sign_books", return_value=[]),
        ]
        for p in patches:
            p.start()
        try:
            return await scan_api.scan.__wrapped__(
                request=MagicMock(),
                file=MagicMock(),
                current_user=SimpleNamespace(id=_USER_ID),
                db=AsyncMock(),
                cf_turnstile_response="tok",
                enhanced=False,
                attempt=1,
            )
        finally:
            for p in reversed(patches):
                p.stop()


class TestScanRunsTheStepsInOrder:
    async def test_full_path(self):
        pipe = _Pipeline()
        resp = await pipe.run()
        assert pipe.order == list(_Pipeline.STEPS)
        assert resp.repeat is False

    async def test_a_repeat_returns_before_the_reservation(self):
        pipe = _Pipeline(repeat="served")
        assert await pipe.run() == "served"
        assert pipe.order == list(_Pipeline.STEPS[:6])

    @pytest.mark.parametrize("failing", _Pipeline.STEPS[:-1])
    async def test_a_failing_step_stops_the_pipeline_and_never_refunds_here(
        self, failing
    ):
        """scan() itself never refunds: the only refunds are inside
        _run_provider (unavailable / malformed). A failure in any step
        stops every later one."""
        pipe = _Pipeline(fail=failing)
        with pytest.raises(HTTPException) as err:
            await pipe.run()
        assert err.value.detail == failing
        idx = _Pipeline.STEPS.index(failing)
        assert pipe.order == list(_Pipeline.STEPS[: idx + 1])
        pipe.refund.assert_not_awaited()


class TestLookupRecord:
    async def test_lookup_failure_recovers_the_session_and_takes_the_normal_path(
        self,
    ):
        db, user = AsyncMock(), MagicMock()
        with (
            patch.object(
                scan_api.scan_records, "lookup", AsyncMock(side_effect=OSError("db"))
            ),
            patch.object(scan_api.scan_records, "log_event") as log_event,
        ):
            got = await scan_api._record_lookup(
                db, user, _USER_ID, "f" * 64, MagicMock(), attempt=2
            )
        assert got is None
        db.rollback.assert_awaited_once()
        db.refresh.assert_awaited_once_with(user)
        log_event.assert_called_once_with(
            "lookup_failed", ai_done=False, enrich_done=False, attempt=2
        )

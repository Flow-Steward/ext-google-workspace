from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from urllib.error import HTTPError, URLError

import pytest
from gmail.errors import GmailExtensionError
from gmail.transport import (
    ATTACHMENT_RESPONSE_LIMIT,
    FIXED_JSON_OVERHEAD,
    MAX_ATTACHMENT_BASE64_BYTES,
    GmailTransport,
    _http_error,
    _read_bounded,
    _retry_after_seconds,
)


def test_gmail_retry_after_accepts_http_date() -> None:
    value = format_datetime(datetime.now(UTC) + timedelta(minutes=10), usegmt=True)

    assert 590 <= (_retry_after_seconds({"Retry-After": value}) or 0) <= 600


def test_attachment_provider_response_limit_uses_explicit_base64_bound() -> None:
    assert MAX_ATTACHMENT_BASE64_BYTES == 27_962_028
    assert ATTACHMENT_RESPONSE_LIMIT == 27_962_028 + FIXED_JSON_OVERHEAD


def test_bounded_reader_accumulates_chunks_and_rejects_overflow() -> None:
    assert _read_bounded(io.BytesIO(b"1234"), 4) == b"1234"
    _raises_input_35_1 = io.BytesIO(b"12345")
    with pytest.raises(GmailExtensionError) as exc_info:
        _read_bounded(_raises_input_35_1, 4)
    assert exc_info.value.code == "response_too_large"


def test_post_network_failure_is_ambiguous_but_get_failure_is_not() -> None:
    def failed(*_args, **_kwargs):
        raise URLError("private provider detail")

    transport = GmailTransport("token", opener=failed)
    with pytest.raises(GmailExtensionError) as get_error:
        transport.profile()
    assert get_error.value.code == "network_failure"
    assert get_error.value.ambiguous is False

    with pytest.raises(GmailExtensionError) as post_error:
        transport.batch_modify(["a"], ["STARRED"], [])
    assert post_error.value.code == "timeout_unknown"
    assert post_error.value.ambiguous is True
    assert post_error.value.retry_facts()["retryable"] is False


def test_rate_limited_read_exposes_retry_facts() -> None:
    def failed(request, **_kwargs):
        raise HTTPError(
            request.full_url,
            429,
            "private message",
            {"Retry-After": "6"},
            io.BytesIO(b"{}"),
        )

    _raises_callable_67_1 = GmailTransport("token", opener=failed).profile
    with pytest.raises(GmailExtensionError) as exc_info:
        _raises_callable_67_1()

    assert exc_info.value.retry_facts() == {
        "failure_class": "provider",
        "retryable": True,
        "retry_after_seconds": 6.0,
        "definitely_no_external_effect": True,
        "external_effect_status": "failed",
        "provider_error_code": "rate_limited",
        "http_status": 429,
    }


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        (403, "insufficientPermissions", "authorization_required"),
        (403, "accessNotConfigured", "gmail_api_disabled"),
        (400, "failedPrecondition", "mailbox_unusable"),
    ],
)
def test_provider_errors_are_classified_from_bounded_safe_reason(
    status: int,
    reason: str,
    expected: str,
) -> None:
    body = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode()

    def failed(request, **_kwargs):
        raise HTTPError(request.full_url, status, "private message", {}, io.BytesIO(body))

    _raises_callable_99_1 = GmailTransport("token", opener=failed).profile
    with pytest.raises(GmailExtensionError) as exc_info:
        _raises_callable_99_1()
    assert exc_info.value.code == expected
    assert "private message" not in str(exc_info.value)


def test_access_not_configured_guides_operator_to_enable_gmail_api() -> None:
    body = json.dumps({"error": {"errors": [{"reason": "accessNotConfigured"}]}}).encode()

    def failed(request, **_kwargs):
        raise HTTPError(request.full_url, 403, "private message", {}, io.BytesIO(body))

    _raises_callable_111_1 = GmailTransport("token", opener=failed).profile
    with pytest.raises(GmailExtensionError) as exc_info:
        _raises_callable_111_1()

    assert exc_info.value.code == "gmail_api_disabled"
    assert exc_info.value.message == (
        "Enable the Gmail API in the Google Cloud project that owns this OAuth Client ID, "
        "wait several minutes, then retry."
    )
    assert "private message" not in str(exc_info.value)


def test_provider_5xx_after_post_is_ambiguous_for_every_submitted_id() -> None:
    def failed(request, **_kwargs):
        raise HTTPError(request.full_url, 503, "private message", {}, io.BytesIO(b"{}"))

    _raises_callable_126_1 = GmailTransport("token", opener=failed).batch_modify
    with pytest.raises(GmailExtensionError) as exc_info:
        _raises_callable_126_1(["a", "b"], [], ["UNREAD"])
    assert exc_info.value.code == "timeout_unknown"
    assert exc_info.value.ambiguous is True


def test_http_408_after_post_is_an_ambiguous_mutation_timeout() -> None:
    error = _http_error(408, method="POST", purpose="Gmail batch modify")

    assert error.code == "timeout_unknown"
    assert error.ambiguous is True

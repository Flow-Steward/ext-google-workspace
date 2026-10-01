from __future__ import annotations

SAFE_ERROR_CODES = frozenset(
    {
        "artifact_output_unavailable",
        "attachment_not_found",
        "attachment_too_large",
        "attachment_type_disallowed",
        "authorization_required",
        "gmail_api_disabled",
        "internal_error",
        "invalid_connection",
        "invalid_cursor",
        "invalid_payload",
        "label_ineligible",
        "label_list_too_large",
        "label_not_found",
        "mailbox_unusable",
        "message_not_found",
        "message_too_complex",
        "message_too_large",
        "network_failure",
        "provider_unavailable",
        "rate_limited",
        "response_too_large",
        "timeout_unknown",
    }
)


class GmailExtensionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        ambiguous: bool = False,
        failure_class: str | None = None,
        retryable: bool | None = None,
        retry_after_seconds: float | None = None,
        definitely_no_external_effect: bool | None = None,
        external_effect_status: str | None = None,
        http_status: int | None = None,
    ) -> None:
        self.code = code if code in SAFE_ERROR_CODES else "internal_error"
        self.message = message
        self.ambiguous = ambiguous
        self.failure_class = failure_class
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.definitely_no_external_effect = definitely_no_external_effect
        self.external_effect_status = external_effect_status
        self.http_status = http_status
        super().__init__(f"{self.code}: {message}")

    def retry_facts(self) -> dict[str, object]:
        facts: dict[str, object] = {}
        for key in (
            "failure_class",
            "retryable",
            "retry_after_seconds",
            "definitely_no_external_effect",
            "external_effect_status",
            "http_status",
        ):
            value = getattr(self, key)
            if value is not None:
                facts[key] = value
        if self.failure_class in {"provider", "transient"}:
            facts["provider_error_code"] = self.code
        return facts

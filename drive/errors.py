from __future__ import annotations

SAFE_ERROR_CODES = frozenset(
    {
        "google_api_disabled",
        "google_file_not_found",
        "google_quota_exceeded",
        "google_rate_limited",
        "google_reauthorization_required",
        "google_resource_not_available_to_connection",
        "google_scope_required",
        "google_service_not_enabled",
        "google_unsupported_export",
        "google_write_incomplete",
        "google_workspace_admin_approval_required",
        "artifact_input_unavailable",
        "artifact_output_unavailable",
        "drive_file_too_large",
        "internal_error",
        "invalid_connection",
        "invalid_payload",
        "network_failure",
        "provider_unavailable",
        "response_too_large",
        "timeout_unknown",
    }
)


class DriveExtensionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        failure_class: str = "",
        retryable: bool | None = None,
        retry_after_seconds: float | None = None,
        definitely_no_external_effect: bool | None = None,
        external_effect_status: str = "",
        http_status: int | None = None,
    ) -> None:
        self.code = code if code in SAFE_ERROR_CODES else "internal_error"
        self.message = message
        if self.code == "timeout_unknown":
            failure_class = failure_class or "transient"
            retryable = False if retryable is None else retryable
            definitely_no_external_effect = (
                False if definitely_no_external_effect is None else definitely_no_external_effect
            )
            external_effect_status = external_effect_status or "timeout_unknown"
        self.failure_class = failure_class
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.definitely_no_external_effect = definitely_no_external_effect
        self.external_effect_status = external_effect_status
        self.http_status = http_status
        super().__init__(f"{self.code}: {message}")

    def retry_facts(self) -> dict[str, object]:
        facts: dict[str, object] = {"provider_error_code": self.code}
        for key in (
            "failure_class",
            "retryable",
            "retry_after_seconds",
            "definitely_no_external_effect",
            "external_effect_status",
            "http_status",
        ):
            value = getattr(self, key)
            if value not in (None, ""):
                facts[key] = value
        return facts

from __future__ import annotations


class ModelConfigurationRequired(RuntimeError):
    provider_attempted = False

    def __init__(
        self,
        message: str,
        *,
        safe_error_code: str = "MODEL_ROUTE_CONFIGURATION_REQUIRED",
    ) -> None:
        super().__init__(message)
        self.safe_error_code = (
            safe_error_code
            if safe_error_code in {
                "AGENT_SAFETY_CONFIGURATION_REQUIRED",
                "MODEL_ROUTE_CONFIGURATION_INVALID",
                "MODEL_ROUTE_CONFIGURATION_REQUIRED",
            }
            else "MODEL_ROUTE_CONFIGURATION_REQUIRED"
        )


class ModelCallFailed(RuntimeError):
    provider_attempted = True


class ModelRefused(RuntimeError):
    provider_attempted = True


class GroundingViolation(RuntimeError):
    provider_attempted = True


def safe_model_error_code(error: Exception) -> str:
    """Return a bounded code without retaining an exception message or provider payload."""

    value = str(error).strip().upper()
    known_codes = {
        "MODEL_OUTPUT_CONTRACT_INVALID",
        "MODEL_OUTPUT_MISSING",
        "MODEL_PROVIDER_UNAVAILABLE",
        "MODEL_RESPONSE_INVALID_JSON",
    }
    provider_status_code = (
        value.startswith("MODEL_PROVIDER_HTTP_")
        and value.removeprefix("MODEL_PROVIDER_HTTP_").isdigit()
        and len(value.removeprefix("MODEL_PROVIDER_HTTP_")) == 3
    )
    if value in known_codes or (
        provider_status_code
        and len(value) == len("MODEL_PROVIDER_HTTP_000")
    ):
        return value
    return "MODEL_CALL_FAILED"

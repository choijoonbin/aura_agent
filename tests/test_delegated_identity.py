import base64
import hashlib
import hmac
import json
from uuid import UUID

import pytest

from dwp_agent.delegated_identity import (
    DelegatedIdentityError,
    verify_delegated_identity,
)


SECRET = "test-delegated-identity-secret-at-least-32-characters"
NOW = 1_800_000_000
GOLDEN_ASSERTION = (
    "eyJhbGciOiJIUzI1NiIsImtpZCI6ImdhdGV3YXktYWdlbnQtdjEiLCJ0eXAiOiJkd3AtaWRlbnRpdHkrand0In0."
    "eyJhdWQiOiJkd3AtYWdlbnQiLCJjaWQiOiJjb3JyLTEiLCJkbiI6bnVsbCwiZXhwIjoxODAwMDAwMDE1LCJodG0iOiJQT1NUIiwiaHR1IjoiL3YxL2FzayIsImlhdCI6MTgwMDAwMDAwMCwiaXAiOm51bGwsImlzcyI6ImR3cC1nYXRld2F5IiwianRpIjoiMDAwMDAwMDAtMDAwMC0wMDAwLTAwMDAtMDAwMDAwMDAwMDk5IiwibmJmIjoxNzk5OTk5OTk5LCJwZXJtaXNzaW9ucyI6WyJBUFAuQVNLOlZJRVciLCJBUFAuV09SSzpWSUVXIl0sInBpZCI6IjAwMDAwMDAwLTAwMDAtMDAwMC0wMDAwLTAwMDAwMDAwMDAwNyIsInJvbGVzIjpbIkVNUExPWUVFIiwiUkVWSUVXRVIiXSwic2lkIjpudWxsLCJzdWIiOiJ1c2VyLTciLCJ0aWQiOiJ0ZW5hbnQtMSJ9."
    "Epk2HcyQcgjnLz0XvV71kH4UiwveMBxSK3RXX5uNIL8"
)
HEADERS = {
    "X-DWP-User-ID": "user-7",
    "X-DWP-Tenant-ID": "tenant-1",
    "X-Correlation-ID": "corr-1",
    "X-DWP-Roles": "EMPLOYEE,REVIEWER",
    "X-DWP-Permissions": "APP.WORK:VIEW,APP.ASK:VIEW",
    "X-DWP-Person-Public-ID": str(UUID("00000000-0000-0000-0000-000000000007")),
}


def test_signed_identity_binds_verified_claims_method_and_path() -> None:
    assertion = assertion_for()

    verify_delegated_identity(
        assertion=assertion,
        secret=SECRET,
        method="POST",
        path="/v1/ask",
        headers=HEADERS,
        now=NOW,
    )

    with pytest.raises(DelegatedIdentityError, match="does not match"):
        verify_delegated_identity(
            assertion=assertion,
            secret=SECRET,
            method="POST",
            path="/v1/actions/MAIL.DRAFT.CREATE/preview",
            headers=HEADERS,
            now=NOW,
        )


def test_signed_identity_rejects_tampering_and_expired_assertions() -> None:
    assertion = assertion_for()
    tampered = assertion[:-1] + ("A" if assertion[-1] != "A" else "B")

    with pytest.raises(DelegatedIdentityError, match="invalid"):
        verify_delegated_identity(
            assertion=tampered,
            secret=SECRET,
            method="POST",
            path="/v1/ask",
            headers=HEADERS,
            now=NOW,
        )
    with pytest.raises(DelegatedIdentityError, match="expired"):
        verify_delegated_identity(
            assertion=assertion,
            secret=SECRET,
            method="POST",
            path="/v1/ask",
            headers=HEADERS,
            now=NOW + 31,
        )


def test_signed_identity_binds_exact_scope_resource_roles_when_forwarded() -> None:
    scoped_headers = {
        **HEADERS,
        "X-DWP-Resource-Roles": "APP_OWNER@RS_MAIL,APP_ACCESS_APPROVER@RS_HRIS",
    }

    with pytest.raises(DelegatedIdentityError, match="does not match"):
        verify_delegated_identity(
            assertion=assertion_for(),
            secret=SECRET,
            method="POST",
            path="/v1/ask",
            headers=scoped_headers,
            now=NOW,
        )

    verify_delegated_identity(
        assertion=assertion_with_resource_roles(
            ["APP_ACCESS_APPROVER@RS_HRIS", "APP_OWNER@RS_MAIL"]
        ),
        secret=SECRET,
        method="POST",
        path="/v1/ask",
        headers=scoped_headers,
        now=NOW,
    )


def test_signed_identity_rejects_malformed_base64_without_leaking_decoder_errors() -> None:
    signed = "a.a"
    signature = encode(hmac.new(SECRET.encode(), signed.encode(), hashlib.sha256).digest())
    malformed = f"{signed}.{signature}"

    with pytest.raises(DelegatedIdentityError, match="invalid"):
        verify_delegated_identity(
            assertion=malformed,
            secret=SECRET,
            method="POST",
            path="/v1/ask",
            headers=HEADERS,
            now=NOW,
        )


def assertion_for() -> str:
    return GOLDEN_ASSERTION


def assertion_with_resource_roles(resource_roles: list[str]) -> str:
    encoded_header, encoded_payload, _ = GOLDEN_ASSERTION.split(".")
    payload = json.loads(
        base64.urlsafe_b64decode(encoded_payload + "=" * (-len(encoded_payload) % 4))
    )
    payload["resourceRoles"] = resource_roles
    rewritten_payload = encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    )
    signed = f"{encoded_header}.{rewritten_payload}"
    signature = encode(hmac.new(SECRET.encode(), signed.encode(), hashlib.sha256).digest())
    return f"{signed}.{signature}"


def encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")

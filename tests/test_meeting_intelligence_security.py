from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from dwp_agent.meeting_intelligence_security import (
    InMemoryMeetingAssertionReplayStore,
    MeetingWorkloadAssertionVerifier,
    MeetingWorkloadIdentityConfiguration,
    MeetingWorkloadIdentityError,
)


GOLDEN_ASSERTION = (
    "dwp1.eyJ2IjoxLCJraWQiOiJtZWV0aW5nLXdvcmtsb2FkLXYxIiwibWV0aG9kIjoiR0VUIiw"
    "icGF0aCI6Ii9pbnRlcm5hbC92MS9tZWV0aW5nLWludGVsbGlnZW5jZS9jYXBhYmlsaXRpZXMiL"
    "CJ0ZW5hbnRJZCI6NzcsIm1lZXRpbmdJZCI6IjI5ZjE0NzM5LTBmOTItNDY5ZS04NTI4LWQzNzM"
    "xZTgwOWY1NSIsInJ1bklkIjoiNzc2ZDBkOGUtOTZlMi00YzI5LThkZjUtOWY1ZTNiZWFmNzJmI"
    "iwiaWF0IjoxNzg3ODc4ODAwLCJleHAiOjE3ODc4Nzg4MzAsImp0aSI6ImUwYWE2YzYyLTZjZDE"
    "tNDVkMC1iZjk0LTU0NzkzNDM2OWViOCIsImJvZHlTaGEyNTYiOiJlM2IwYzQ0Mjk4ZmMxYzE0O"
    "WFmYmY0Yzg5OTZmYjkyNDI3YWU0MWU0NjQ5YjkzNGNhNDk1OTkxYjc4NTJiODU1In0.kJN5DXq"
    "OTSVfKwlPSgLfQHMjQoTzvET9TFO4uXJTZws"
)
MEETING_ID = UUID("29f14739-0f92-469e-8528-d3731e809f55")
RUN_ID = UUID("776d0d8e-96e2-4c29-8df5-9f5e3beaf72f")


def test_java_golden_assertion_is_consumed_once_and_binds_empty_get_body() -> None:
    verifier = MeetingWorkloadAssertionVerifier(
        MeetingWorkloadIdentityConfiguration(
            service_token="meeting-service-secret-2026-at-least-32",
            signing_secret=b"0123456789abcdef0123456789abcdef",
            key_id="meeting-workload-v1",
        ),
        InMemoryMeetingAssertionReplayStore(
            now=lambda: datetime.fromtimestamp(1_787_878_801, tz=UTC)
        ),
        now=lambda: 1_787_878_801,
    )
    parameters = {
        "service_token": "meeting-service-secret-2026-at-least-32",
        "assertion": GOLDEN_ASSERTION,
        "method": "GET",
        "path": "/internal/v1/meeting-intelligence/capabilities",
        "tenant_id": 77,
        "meeting_id": MEETING_ID,
        "run_id": RUN_ID,
        "body": b"",
    }

    verifier.verify(**parameters)

    with pytest.raises(MeetingWorkloadIdentityError, match="already used"):
        verifier.verify(**parameters)


def test_golden_capability_assertion_rejects_nonempty_body() -> None:
    verifier = MeetingWorkloadAssertionVerifier(
        MeetingWorkloadIdentityConfiguration(
            service_token="meeting-service-secret-2026-at-least-32",
            signing_secret=b"0123456789abcdef0123456789abcdef",
            key_id="meeting-workload-v1",
        ),
        InMemoryMeetingAssertionReplayStore(),
        now=lambda: 1_787_878_801,
    )

    with pytest.raises(MeetingWorkloadIdentityError, match="Invalid"):
        verifier.verify(
            service_token="meeting-service-secret-2026-at-least-32",
            assertion=GOLDEN_ASSERTION,
            method="GET",
            path="/internal/v1/meeting-intelligence/capabilities",
            tenant_id=77,
            meeting_id=MEETING_ID,
            run_id=RUN_ID,
            body=b"{}",
        )

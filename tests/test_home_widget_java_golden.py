from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest

from dwp_agent.home_widget_identity import (
    HomeDelegatedIdentityError,
    InMemoryHomeIdentityReplayStore,
    verify_home_delegated_identity,
)


# This is an exact copy of the Java signer fixture at the pinned backend commit.
BACKEND_SOURCE_COMMIT = "f23d9bb039711cfb34e033d46917df8a46b24754"
BACKEND_SOURCE_PATH = "contracts/home-runtime/dwaion-signed-workload.v1.json"
FIXTURE_SHA256 = "dd2ab8edc36e7d52db73d3f6ae954111819ad763742e23f24eb12986faae0482"
FIXTURE = Path(__file__).parent / "fixtures" / "dwaion-signed-workload.v1.json"


def _fixture() -> dict[str, object]:
    raw = FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FIXTURE_SHA256, (
        "The pinned Java-to-Python fixture changed; copy it from "
        f"backend {BACKEND_SOURCE_COMMIT}:{BACKEND_SOURCE_PATH} and review the protocol diff."
    )
    fixture = json.loads(raw)
    assert isinstance(fixture, dict)
    return fixture


def _verify(fixture: dict[str, object], body: bytes) -> None:
    issued_at = datetime.fromisoformat(str(fixture["issuedAt"]).replace("Z", "+00:00"))
    verify_home_delegated_identity(
        assertion=str(fixture["assertion"]),
        secret=str(fixture["testOnlySigningSecret"]),
        method=str(fixture["method"]),
        path=str(fixture["path"]),
        body=body,
        headers=cast(Mapping[str, str], fixture["headers"]),
        replay_store=InMemoryHomeIdentityReplayStore(now=lambda: issued_at),
        now=int(issued_at.timestamp()),
        key_id=str(fixture["keyId"]),
    )


def test_accepts_exact_backend_java_signed_workload_fixture() -> None:
    fixture = _fixture()
    body = str(fixture["requestBodyUtf8"]).encode("utf-8")

    assert hashlib.sha256(body).hexdigest() == fixture["requestBodySha256"]
    assert hashlib.sha256(str(fixture["assertion"]).encode("ascii")).hexdigest() == (
        fixture["assertionSha256"]
    )

    _verify(fixture, body)


def test_rejects_body_tampering_against_backend_java_signed_workload_fixture() -> None:
    fixture = _fixture()
    tampered_body = str(fixture["requestBodyUtf8"]).encode("utf-8") + b" "

    with pytest.raises(HomeDelegatedIdentityError, match="does not match"):
        _verify(fixture, tampered_body)

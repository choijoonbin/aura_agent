from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from dwp_agent import artifact_home_projection
from dwp_agent.artifact_home_projection import ArtifactHomeProjectionQueries
from dwp_agent.personal_domain_security import PersonalDomainIdentity


class _Result:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def fetchall(self) -> list[dict[str, object]]:
        return self.rows


class _Connection:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.sql = ""
        self.parameters: tuple[object, ...] = ()

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, parameters: tuple[object, ...]) -> _Result:
        self.sql = sql
        self.parameters = parameters
        return _Result(self.rows)


class _Codec:
    def decrypt_json(self, envelope: object, **_: object) -> dict[str, object]:
        if envelope is None:
            raise TypeError("missing projection")
        return {"title": envelope}


def test_home_projection_is_recipient_bounded_and_reads_title_projection_only(
    monkeypatch,
) -> None:
    now = datetime.now(timezone.utc)
    rows = [
        {
            "artifact_id": uuid4(),
            "tenant_id": 71,
            "artifact_type": "DOCUMENT",
            "artifact_state": "DRAFT",
            "revision": 2,
            "updated_at": now,
            "home_title_envelope": "projected title",
            "visible_count": 3,
        },
        {
            "artifact_id": uuid4(),
            "tenant_id": 71,
            "artifact_type": "WORK_PLAN",
            "artifact_state": "REVIEW_REQUIRED",
            "revision": 4,
            "updated_at": now,
            "home_title_envelope": None,
            "visible_count": 3,
        },
    ]
    connection = _Connection(rows)
    monkeypatch.setattr(
        artifact_home_projection,
        "connect",
        lambda *_args, **_kwargs: connection,
    )
    queries = ArtifactHomeProjectionQueries()
    queries.database_url = "postgresql://unused"
    queries.codec = _Codec()
    identity = PersonalDomainIdentity(
        tenant_id=71,
        user_id="82",
        correlation_id="corr",
        auth_session_id="session",
        roles=frozenset(),
        permissions=frozenset(),
    )

    projection = queries.home_projection(identity, limit=2)

    assert connection.parameters == (71, "82", 2)
    assert "content_envelope" not in connection.sql
    assert "artifact_state IN ('DRAFT', 'REVIEW_REQUIRED')" in connection.sql
    assert "ORDER BY a.updated_at DESC, a.artifact_id" in connection.sql
    assert projection.visible_count == 3
    assert projection.slots[0].title == "projected title"
    assert projection.slots[1] is None

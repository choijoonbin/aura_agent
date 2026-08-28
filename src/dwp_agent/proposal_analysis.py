from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from .context_broker import (
    ContextBrokerUnavailable,
    GroundedContext,
    GroundedSource,
    WorkspaceContextBroker,
)
from .contracts import CitationSourceType
from .policy import AskIdentity
from .proposal_analysis_commands import (
    ProposalAnalysisCommandStore,
    ProposalAnalysisCommandUnavailable,
    ProposalAnalysisLease,
    get_proposal_analysis_command_store,
)
from .proposal_analysis_contracts import ProposalAnalysisReceipt
from .proposal_analysis_control import (
    ProposalAnalysisControl,
    ProposalAnalysisDisabled,
    get_proposal_analysis_control,
)
from .proposal_analysis_fingerprints import ProposalAnalysisFingerprints
from .proposal_contracts import (
    AgentProposal,
    CreateAgentProposalRequest,
    ProposalContent,
    ProposalEvidence,
    ProposalKind,
    ProposalPriority,
    ProposalState,
)
from .proposal_store import ProposalConflict, get_proposal_store


MAX_ANALYSIS_PROPOSALS = 6
_ANALYSIS_SCOPES = (
    CitationSourceType.WORK_ITEM,
    CitationSourceType.MAIL,
    CitationSourceType.CALENDAR,
)
_PRIORITY_ORDER = {
    ProposalPriority.URGENT: 0,
    ProposalPriority.HIGH: 1,
    ProposalPriority.MEDIUM: 2,
    ProposalPriority.LOW: 3,
}


class ProposalContextBroker(Protocol):
    def collect(
        self,
        query: str,
        *,
        identity: AskIdentity,
        locale: str,
        agent_key: str,
        source_scopes: tuple[CitationSourceType, ...],
    ) -> GroundedContext: ...


@dataclass(frozen=True)
class DetectedProposal:
    kind: ProposalKind
    priority: ProposalPriority
    title: str
    summary: str
    rationale: str
    source: GroundedSource
    expires_at: datetime


class ProposalAnalysisService:
    def __init__(
        self,
        broker: ProposalContextBroker | None = None,
        fingerprints: ProposalAnalysisFingerprints | None = None,
        control: ProposalAnalysisControl | None = None,
        commands: ProposalAnalysisCommandStore | None = None,
    ) -> None:
        self._broker = broker or WorkspaceContextBroker()
        self._fingerprints = fingerprints or ProposalAnalysisFingerprints.load()
        self._control = control or get_proposal_analysis_control()
        self._commands = commands or get_proposal_analysis_command_store()

    def analyze(
        self,
        *,
        identity: AskIdentity,
        command_id: UUID,
        locale: str,
        auth_session_id: str,
    ) -> ProposalAnalysisReceipt:
        preference = self._control.preference(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
        )
        if not preference.proactive_analysis_enabled:
            raise ProposalAnalysisDisabled("Proactive analysis is disabled.")
        start = self._commands.begin(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            auth_session_id=auth_session_id,
            command_id=command_id,
            locale=locale,
        )
        if start.replay is not None:
            return start.replay
        lease = start.lease
        if lease is None:
            raise ProposalAnalysisCommandUnavailable(
                "Proposal analysis command lease is unavailable."
            )
        try:
            now = datetime.now(timezone.utc)
            context = self._broker.collect(
                _analysis_query(locale),
                identity=identity,
                locale=locale,
                agent_key="DWP_ASSISTANT",
                source_scopes=_ANALYSIS_SCOPES,
            )
            detected = detect_proactive_signals(context, locale=locale, now=now)
            proposals: list[AgentProposal] = []
            for signal in detected:
                try:
                    proposals.append(
                        self._commands.run_under_lease(
                            tenant_id=identity.tenant_id,
                            user_id=identity.user_id,
                            lease=lease,
                            operation=lambda signal=signal: self._persist(
                                signal,
                                identity=identity,
                                command_id=command_id,
                                locale=locale,
                            ),
                        )
                    )
                except ProposalConflict:
                    # A privacy tombstone or a concurrent canonical producer wins.
                    continue
            actionable = [
                proposal
                for proposal in proposals
                if proposal.state in {ProposalState.PENDING, ProposalState.SNOOZED}
            ]
            receipt = ProposalAnalysisReceipt(
                analyzed_at=now,
                sources_analyzed=len(context.sources),
                actionable_proposals=len(actionable),
                attempted_sources=list(context.attempted_sources),
                unavailable_sources=list(context.unavailable_sources),
                proposals=actionable,
            )
            return self._commands.complete(
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                lease=lease,
                receipt=receipt,
            )
        except Exception:
            self._mark_failed(identity, lease)
            raise

    def _mark_failed(
        self, identity: AskIdentity, lease: ProposalAnalysisLease
    ) -> None:
        try:
            self._commands.fail(
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                lease=lease,
            )
        except ProposalAnalysisCommandUnavailable:
            pass

    def _persist(
        self,
        signal: DetectedProposal,
        *,
        identity: AskIdentity,
        command_id: UUID,
        locale: str,
    ) -> AgentProposal:
        route = _internal_route(signal.source.citation.route)
        content = ProposalContent(
            title=signal.title,
            summary=signal.summary,
            rationale=signal.rationale,
            evidence=[
                ProposalEvidence(
                    source_type=signal.source.citation.source_type.value,
                    reference_id=self._reference_id(identity, signal.source),
                    label=_evidence_label(locale, signal.source.citation.source_type),
                    occurred_at=signal.source.citation.occurred_at,
                    route=route,
                )
            ],
        )
        source_event_id = self._source_event_id(identity, signal)
        request = CreateAgentProposalRequest(
            command_id=uuid5(
                NAMESPACE_URL,
                f"urn:dwp:proposal-analysis-command:{command_id}:{source_event_id}",
            ),
            target_user_id=identity.user_id,
            source_event_id=source_event_id,
            kind=signal.kind,
            priority=signal.priority,
            agent_key="DWP_ASSISTANT",
            content=content,
            expires_at=signal.expires_at,
            change_reason="User requested a governed analysis of current workspace signals.",
        )
        return get_proposal_store().create(
            tenant_id=identity.tenant_id,
            actor_user_id=identity.user_id,
            correlation_id=identity.correlation_id,
            request=request,
        )

    def _reference_id(self, identity: AskIdentity, source: GroundedSource) -> str:
        citation = source.citation
        digest = self._fingerprints.source_revision(
            identity.tenant_id,
            {
                "purpose": "workspace-evidence-reference-v1",
                "userId": identity.user_id,
                "sourceType": citation.source_type.value,
                "route": citation.route or "",
                "occurredAt": (
                    citation.occurred_at.isoformat() if citation.occurred_at else ""
                ),
            },
        )
        return str(uuid5(NAMESPACE_URL, f"urn:dwp:proposal-evidence:{digest}"))

    def _source_event_id(
        self,
        identity: AskIdentity,
        signal: DetectedProposal,
    ) -> str:
        source = signal.source
        digest = self._fingerprints.source_revision(
            identity.tenant_id,
            {
                "purpose": "proactive-work-analysis-v1",
                "userId": identity.user_id,
                "sourceType": source.citation.source_type.value,
                "route": source.citation.route or "",
                "occurredAt": (
                    source.citation.occurred_at.isoformat()
                    if source.citation.occurred_at
                    else ""
                ),
                "sourceRevision": source.evidence,
                "kind": signal.kind.value,
                "priority": signal.priority.value,
            },
        )
        return f"workspace-signal:{digest}"


def detect_proactive_signals(
    context: GroundedContext,
    *,
    locale: str,
    now: datetime,
) -> list[DetectedProposal]:
    detected = [
        signal
        for source in context.sources
        if (signal := _detect(source, locale=locale, now=now)) is not None
    ]
    detected.sort(
        key=lambda signal: (
            _PRIORITY_ORDER[signal.priority],
            signal.source.rank,
            signal.title,
        )
    )
    return detected[:MAX_ANALYSIS_PROPOSALS]


def _detect(
    source: GroundedSource,
    *,
    locale: str,
    now: datetime,
) -> DetectedProposal | None:
    source_type = source.citation.source_type
    fields = _evidence_fields(source.evidence)
    korean = locale.lower().startswith("ko")
    if source_type == CitationSourceType.CALENDAR and _truthy(fields.get("conflict")):
        return DetectedProposal(
            kind=ProposalKind.SCHEDULE,
            priority=ProposalPriority.HIGH,
            title=(
                "겹치는 일정을 확인하세요"
                if korean
                else "Review a schedule conflict"
            ),
            summary=(
                "겹치는 일정이 감지되었습니다. 참석 우선순위와 이동 시간을 확인하세요."
                if korean
                else "An overlapping event was detected. Review attendance priority and travel time."
            ),
            rationale=(
                "캘린더가 이 일정을 충돌 상태로 반환했습니다. DWAI-ON은 일정을 변경하지 않고 검토만 제안합니다."
                if korean
                else "Calendar returned this event as conflicting. DWAI-ON proposes review without changing the schedule."
            ),
            source=source,
            expires_at=_expiry(source, now, fallback_days=3),
        )
    if source_type == CitationSourceType.WORK_ITEM:
        priority = (fields.get("priority") or "").upper()
        status = (fields.get("status") or "").upper()
        due_at = _parse_datetime(fields.get("dueat"))
        overdue = due_at is not None and now - timedelta(days=7) <= due_at < now
        due_soon = due_at is not None and now <= due_at <= now + timedelta(hours=48)
        if status not in {"DONE", "CLOSED", "COMPLETED", "CANCELLED"} and (
            priority in {"HIGH", "URGENT", "CRITICAL"} or due_soon or overdue
        ):
            urgent = overdue or priority in {"URGENT", "CRITICAL"}
            return DetectedProposal(
                kind=ProposalKind.RISK if overdue else ProposalKind.WORK_SIGNAL,
                priority=ProposalPriority.URGENT if urgent else ProposalPriority.HIGH,
                title=(
                    "우선 확인할 업무가 있습니다"
                    if korean
                    else "A work item needs priority review"
                ),
                summary=(
                    "마감과 우선순위 신호를 기준으로 지금 확인할 업무로 분류했습니다."
                    if korean
                    else "Deadline and priority signals place this work item among your immediate reviews."
                ),
                rationale=(
                    "업무 상태·우선순위·마감 시각만 사용해 결정적으로 선별했으며, DWAI-ON은 원본 업무를 변경하지 않습니다."
                    if korean
                    else "Deterministic status, priority, and deadline signals were used; DWAI-ON does not modify the source item."
                ),
                source=source,
                expires_at=_bounded_expiry(due_at, now, fallback_days=5),
            )
    if source_type == CitationSourceType.MAIL:
        importance = (fields.get("importance") or "").upper()
        occurred_at = source.citation.occurred_at
        fresh = occurred_at is None or occurred_at >= now - timedelta(days=7)
        if (
            fresh
            and _truthy(fields.get("unread"))
            and importance in {"HIGH", "URGENT"}
        ):
            return DetectedProposal(
                kind=ProposalKind.WORK_SIGNAL,
                priority=ProposalPriority.HIGH,
                title=(
                    "읽지 않은 중요 메일을 확인하세요"
                    if korean
                    else "Review an unread important message"
                ),
                summary=(
                    "아직 읽지 않은 중요 메일입니다. 응답이나 후속 업무가 필요한지 확인하세요."
                    if korean
                    else "This important message is still unread. Check whether a response or follow-up is required."
                ),
                rationale=(
                    "메일의 중요도와 읽음 상태만 사용했으며, 본문을 수정하거나 자동 회신하지 않습니다."
                    if korean
                    else "Only importance and read state were used; no message is changed or answered automatically."
                ),
                source=source,
                expires_at=_expiry(source, now, fallback_days=5),
            )
    return None


def _analysis_query(locale: str) -> str:
    if locale.lower().startswith("ko"):
        return "긴급 중요 마감 임박 일정 충돌 읽지 않은 메일 우선 업무"
    return "urgent important due soon schedule conflict unread mail priority work"


def _evidence_fields(value: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for segment in value.split("|"):
        key, separator, raw_value = segment.strip().partition("=")
        if separator and key:
            fields[key.strip().lower()] = raw_value.strip()
    return fields


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"true", "1", "yes", "y"}


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _expiry(source: GroundedSource, now: datetime, *, fallback_days: int) -> datetime:
    occurred_at = source.citation.occurred_at
    return _bounded_expiry(occurred_at, now, fallback_days=fallback_days)


def _bounded_expiry(
    candidate: datetime | None, now: datetime, *, fallback_days: int
) -> datetime:
    fallback = now + timedelta(days=fallback_days)
    if candidate is None or candidate <= now + timedelta(minutes=15):
        return fallback
    return min(candidate + timedelta(hours=4), fallback)


def _evidence_label(locale: str, source_type: CitationSourceType) -> str:
    korean = locale.lower().startswith("ko")
    labels = {
        CitationSourceType.WORK_ITEM: ("업무 원문", "Source work item"),
        CitationSourceType.MAIL: ("메일 원문", "Source message"),
        CitationSourceType.CALENDAR: ("일정 원문", "Source event"),
    }
    label = labels.get(source_type, ("업무 근거", "Workspace evidence"))
    return label[0] if korean else label[1]


def _internal_route(value: str | None) -> str | None:
    if (
        value
        and value.startswith("/")
        and not value.startswith("//")
        and not re.search(r"[\r\n\x00]", value)
    ):
        return value
    return None


_ANALYSIS_SERVICE_OVERRIDE: ProposalAnalysisService | None = None


def get_proposal_analysis_service() -> ProposalAnalysisService:
    return _ANALYSIS_SERVICE_OVERRIDE or ProposalAnalysisService()


def set_proposal_analysis_service_for_tests(
    service: ProposalAnalysisService | None,
) -> None:
    global _ANALYSIS_SERVICE_OVERRIDE
    _ANALYSIS_SERVICE_OVERRIDE = service


__all__ = [
    "ContextBrokerUnavailable",
    "DetectedProposal",
    "ProposalAnalysisService",
    "detect_proactive_signals",
    "get_proposal_analysis_service",
    "set_proposal_analysis_service_for_tests",
]

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _camel(value: str) -> str:
    first, *rest = value.split("_")
    return first + "".join(part.capitalize() for part in rest)


class _Contract(BaseModel):
    model_config = ConfigDict(
        alias_generator=_camel,
        populate_by_name=True,
        extra="forbid",
        str_strip_whitespace=True,
    )


class TranscriptSegment(_Contract):
    segment_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
    start_millis: int = Field(ge=0)
    end_millis: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=4_000)

    @model_validator(mode="after")
    def validate_range(self) -> "TranscriptSegment":
        if self.end_millis <= self.start_millis:
            raise ValueError("Transcript segment end must follow its start.")
        return self


class MeetingIntelligenceRequest(_Contract):
    analysis_profile: str = Field(pattern=r"^STANDARD_RECAP_V1$")
    output_language: str = Field(pattern=r"^[a-z]{2}(?:-[A-Z]{2})?$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    transcript: list[TranscriptSegment] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_transcript(self) -> "MeetingIntelligenceRequest":
        ids = [segment.segment_id for segment in self.transcript]
        if len(ids) != len(set(ids)):
            raise ValueError("Transcript segment identifiers must be unique.")
        if sum(len(segment.text) for segment in self.transcript) > 300_000:
            raise ValueError("Transcript text exceeds the governed analysis limit.")
        if any(
            current.start_millis < previous.start_millis
            for previous, current in zip(self.transcript, self.transcript[1:])
        ):
            raise ValueError("Transcript segments must be time ordered.")
        return self


class Citation(_Contract):
    segment_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
    start_millis: int = Field(ge=0)
    end_millis: int = Field(gt=0)


class CitedText(_Contract):
    text: str = Field(min_length=1, max_length=2_000)
    citations: list[Citation] = Field(min_length=1, max_length=20)


class ClimateLabel(StrEnum):
    ALIGNED = "ALIGNED"
    MIXED = "MIXED"
    CONTESTED = "CONTESTED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class ClimateSignal(StrEnum):
    BALANCED_TURN_TAKING = "BALANCED_TURN_TAKING"
    CONSTRUCTIVE_DISAGREEMENT = "CONSTRUCTIVE_DISAGREEMENT"
    UNRESOLVED_DISAGREEMENT = "UNRESOLVED_DISAGREEMENT"
    DOMINANT_MONOLOGUE_PATTERN = "DOMINANT_MONOLOGUE_PATTERN"
    LOW_TRANSCRIPT_EVIDENCE = "LOW_TRANSCRIPT_EVIDENCE"


class ConversationClimate(_Contract):
    label: ClimateLabel
    signals: list[ClimateSignal] = Field(max_length=5)
    citations: list[Citation] = Field(max_length=20)


class MeetingIntelligenceAnalysis(_Contract):
    executive_summary: CitedText
    topics: list[CitedText] = Field(max_length=20)
    decisions: list[CitedText] = Field(max_length=30)
    action_items: list[CitedText] = Field(max_length=30)
    open_questions: list[CitedText] = Field(max_length=30)
    risks: list[CitedText] = Field(max_length=30)
    conversation_climate: ConversationClimate


class MeetingIntelligenceCapability(_Contract):
    available: bool
    provider_code: str
    model: str
    processing_region: str
    customer_data_training_disabled: bool
    provider_retention_disabled: bool
    schema_versions: list[str]


class MeetingIntelligenceCapabilityEnvelope(_Contract):
    data: MeetingIntelligenceCapability


class MeetingIntelligenceAnalysisEnvelope(_Contract):
    data: MeetingIntelligenceAnalysis

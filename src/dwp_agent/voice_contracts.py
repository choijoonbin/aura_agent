from __future__ import annotations

from pydantic import Field, model_validator

from .contracts import ContractModel


class VoiceTranscription(ContractModel):
    text: str = Field(min_length=1, max_length=4_000)
    language: str = Field(pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


class VoiceTranscriptionEnvelope(ContractModel):
    status: str = "SUCCESS"
    message: str = "Voice transcription completed."
    success: bool = True
    data: VoiceTranscription


class VoiceSpeechRequest(ContractModel):
    text: str = Field(min_length=1, max_length=4_000)
    locale: str = Field(default="en", pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")

    @model_validator(mode="after")
    def normalize(self) -> "VoiceSpeechRequest":
        self.text = self.text.strip()
        self.locale = self.locale.strip()
        if not self.text:
            raise ValueError("Speech text must contain a non-whitespace character.")
        return self

from pydantic import BaseModel, ConfigDict, model_validator


class EvidenceSpan(BaseModel):
    model_config = ConfigDict(frozen=True)

    evidence_id: str
    document_version_id: str
    chunk_id: str

    page_number: int | None = None
    start_offset: int | None = None
    end_offset: int | None = None

    source_text: str
    content_hash: str

    @model_validator(mode="after")
    def validate_location(self) -> "EvidenceSpan":
        if self.page_number is not None and self.page_number <= 0:
            raise ValueError("page_number must be positive")

        if self.start_offset is not None and self.start_offset < 0:
            raise ValueError("start_offset must be nonnegative")

        if self.end_offset is not None and self.end_offset < 0:
            raise ValueError("end_offset must be nonnegative")

        if (
            self.start_offset is not None
            and self.end_offset is not None
            and self.end_offset < self.start_offset
        ):
            raise ValueError("end_offset must not precede start_offset")

        return self

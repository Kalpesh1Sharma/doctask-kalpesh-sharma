from pydantic import BaseModel, ConfigDict


class EvidenceSpan(BaseModel):
    model_config = ConfigDict(frozen=True)

    evidence_id: str
    document_id: str
    document_version_id: str

    start_offset: int
    end_offset: int

    source_text: str
    content_hash: str
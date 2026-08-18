"""Pure deterministic renderer for a frozen :class:`FinalReportInput`."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256

from domain.fact import FactStatus
from domain.final_report import FinalReportInput
from domain.finding import FindingStatus
from domain.run_membership import RunMembershipRelation
from domain.update_preparation import UpdateDocumentChangeKind
from domain.update_review import UpdateFactDeltaKind


class FinalReportRenderIntegrityError(ValueError):
    """Frozen final-report input cannot produce a traceable artifact."""


@dataclass(frozen=True)
class PreservedReportPart:
    """One exact byte range copied from the baseline report into an UPDATE."""

    part_id: str
    baseline_start: int
    baseline_end: int
    updated_start: int
    updated_end: int
    content_hash: str

    @property
    def size_bytes(self) -> int:
        return self.updated_end - self.updated_start

    def to_payload(self) -> dict[str, object]:
        return {
            "part_id": self.part_id,
            "baseline_start": self.baseline_start,
            "baseline_end": self.baseline_end,
            "updated_start": self.updated_start,
            "updated_end": self.updated_end,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class FocusedUpdateProof:
    """Deterministic proof that unchanged UPDATE ranges came from the baseline."""

    baseline_content_hash: str
    preserved_parts: tuple[PreservedReportPart, ...]

    @property
    def preserved_bytes(self) -> int:
        return sum(part.size_bytes for part in self.preserved_parts)

    def to_payload(self) -> dict[str, object]:
        return {
            "baseline_content_hash": self.baseline_content_hash,
            "preserved_bytes": self.preserved_bytes,
            "preserved_parts": [part.to_payload() for part in self.preserved_parts],
        }


@dataclass(frozen=True)
class _SemanticLine:
    kind: str
    identity: str
    state: str | None
    start: int
    end: int
    content: bytes

    @property
    def key(self) -> tuple[str, str, str | None]:
        return (self.kind, self.identity, self.state)


_DOCUMENT_LINE = re.compile(r'^- document_id: "([0-9a-f-]+)";')
_FACT_LINE = re.compile(r'^- fact_id: "([0-9a-f-]+)";')
_FINDING_LINE = re.compile(r'^- finding_id: "([0-9a-f-]+)";')
_EVIDENCE_LINE = re.compile(r'^- evidence_span_id: "([0-9a-f-]+)";')


def _json(value: object) -> str:
    return json.dumps(
        _json_compatible(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_compatible(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_compatible(item) for item in value]
    return value


def render_final_report(value: FinalReportInput) -> bytes:
    """Render canonical UTF-8 Markdown with LF endings and one final newline."""

    return _encode_lines(_render_lines(value))


def render_focused_update(
    value: FinalReportInput,
    baseline_bytes: bytes,
    baseline_content_hash: str,
) -> tuple[bytes, FocusedUpdateProof]:
    """Patch an UPDATE report from its baseline while copying unaffected bytes.

    The current frozen input still defines the canonical final artifact.  For
    records that the durable UPDATE change set says are retained/carried, the
    implementation copies the exact baseline bytes instead of merely rendering
    equivalent text.  A carried Fact's run-local ``provenance`` field is the
    only permitted in-line edit; the rest of that claim line is copied exactly.
    Existing immutable citation lines are also copied byte-for-byte.
    """

    change_set = value.update_change_set
    if change_set is None:
        raise FinalReportRenderIntegrityError("focused UPDATE rendering requires an UPDATE change set")
    _validate_baseline_bytes(baseline_bytes, baseline_content_hash)

    current_bytes = _encode_lines(_render_lines(value))
    baseline_lines = _semantic_lines(baseline_bytes)
    current_lines = _semantic_lines(current_bytes)
    baseline_by_key = _unique_semantic_lines(baseline_lines, "baseline")
    baseline_by_identity = _unique_semantic_identity_lines(baseline_lines, "baseline")
    current_by_start = {line.start: line for line in current_lines}

    retained_documents = {
        change.document_id
        for change in change_set.document_delta
        if change.kind is UpdateDocumentChangeKind.RETAINED
    }
    carried_facts = {
        fact.fact_id
        for fact in value.render_input.source.facts
        if fact.relation is RunMembershipRelation.CARRIED_FORWARD
    }
    carried_findings = {
        membership.finding_id
        for membership in value.render_input.review_state.memberships
        if membership.relation is RunMembershipRelation.CARRIED_FORWARD
    }

    pieces: list[bytes] = []
    proof_parts: list[PreservedReportPart] = []
    updated_offset = 0
    current_offset = 0
    for line in current_bytes.splitlines(keepends=True):
        semantic = current_by_start.get(current_offset)
        replacement = line
        parts: tuple[PreservedReportPart, ...] = ()
        if semantic is not None:
            baseline = baseline_by_key.get(semantic.key)
            if semantic.kind == "document" and semantic.identity in retained_documents:
                if baseline is None:
                    raise FinalReportRenderIntegrityError(
                        f"retained document {semantic.identity} is absent from the baseline report",
                    )
                replacement, parts = _preserve_exact_line(
                    semantic,
                    baseline,
                    updated_offset,
                    f"document:{semantic.identity}",
                )
            elif semantic.kind == "fact" and semantic.identity in carried_facts:
                if baseline is None:
                    baseline = baseline_by_identity.get((semantic.kind, semantic.identity))
                if baseline is None:
                    raise FinalReportRenderIntegrityError(
                        f"carried Fact {semantic.identity} is absent from the baseline report",
                    )
                replacement, parts = _preserve_carried_fact_line(
                    semantic,
                    baseline,
                    updated_offset,
                )
            elif semantic.kind == "finding" and semantic.identity in carried_findings:
                if baseline is None:
                    raise FinalReportRenderIntegrityError(
                        f"carried Finding {semantic.identity} is absent from the baseline report",
                    )
                replacement, parts = _preserve_exact_line(
                    semantic,
                    baseline,
                    updated_offset,
                    f"finding:{semantic.identity}",
                )
            elif semantic.kind == "evidence":
                if baseline is None:
                    baseline = baseline_by_identity.get((semantic.kind, semantic.identity))
                if baseline is not None:
                    replacement, parts = _preserve_exact_line(
                        semantic,
                        baseline,
                        updated_offset,
                        f"evidence:{semantic.identity}",
                    )
        pieces.append(replacement)
        proof_parts.extend(parts)
        current_offset += len(line)
        updated_offset += len(replacement)

    updated = b"".join(pieces)
    if updated != current_bytes:
        raise FinalReportRenderIntegrityError(
            "focused UPDATE patch does not equal the canonical report for frozen input",
        )
    proof = FocusedUpdateProof(
        baseline_content_hash=baseline_content_hash,
        preserved_parts=tuple(proof_parts),
    )
    return updated, proof


def final_report_content_hash(value: FinalReportInput) -> str:
    return f"sha256:{sha256(render_final_report(value)).hexdigest()}"


def _render_lines(value: FinalReportInput) -> tuple[str, ...]:
    source = value.render_input.source
    review = value.render_input.review_state
    evidence = {item.evidence_id: item for item in source.evidence_spans}
    fact_evidence = {item.fact_id: item.evidence_span_ids for item in source.fact_evidence}
    finding_evidence = {item.finding_id: item.evidence_span_ids for item in source.finding_evidence}
    lines: list[str] = ["# Final Report", "", "## Metadata"]
    lines.extend((
        f"- run_id: {_json(value.run_id)}",
        f"- renderer_version: {_json(value.renderer_version)}",
        f"- render_input_format: {_json(value.render_input.format)}",
    ))

    lines.extend(("", "## Source Versions"))
    for document in sorted(
        source.documents,
        key=lambda item: (item.document_id, item.version_number, item.document_version_id),
    ):
        lines.append(
            f"- document_id: {_json(document.document_id)}; version: {document.version_number}; document_version_id: {_json(document.document_version_id)}; "
            f"content_hash: {_json(document.content_hash)}",
        )

    if value.update_change_set is not None:
        lines.extend(("", "## Update Delta"))
        for change in value.update_change_set.document_delta:
            lines.append(f"- document_change: {_json(change.model_dump(mode='json'))}")
        final_fact_ids = {fact.fact_id for fact in source.facts}
        for change in value.update_change_set.fact_delta:
            applied = (
                change.kind is UpdateFactDeltaKind.CREATED
                and change.fact_id in final_fact_ids
            ) or (
                change.kind is UpdateFactDeltaKind.REMOVED_FROM_CURRENT_STATE
                and change.fact_id not in final_fact_ids
            )
            if applied:
                lines.append(f"- fact_change: {_json(change.model_dump(mode='json'))}")

    lines.extend(("", "## Confirmed Facts"))
    facts = sorted(
        source.facts,
        key=lambda item: (
            item.subject,
            item.predicate,
            _json(item.object_value),
            item.fact_id,
        ),
    )
    for fact in facts:
        if fact.status is FactStatus.CONFIRMED:
            evidence_span_ids = _claim_evidence_ids(
                claim_kind="fact",
                claim_id=fact.fact_id,
                references=fact_evidence,
                available_evidence=evidence,
            )
            lines.append(_fact_line(fact, evidence_span_ids))

    lines.extend(("", "## Disputed Facts"))
    for fact in facts:
        if fact.status is FactStatus.DISPUTED:
            evidence_span_ids = _claim_evidence_ids(
                claim_kind="fact",
                claim_id=fact.fact_id,
                references=fact_evidence,
                available_evidence=evidence,
            )
            lines.append(_fact_line(fact, evidence_span_ids))

    approved = sorted(
        (item for item in review.memberships if item.status is FindingStatus.APPROVED),
        key=lambda item: (item.finding_type.value, item.severity, item.title, item.finding_id),
    )
    rejected = sorted(
        (item for item in review.memberships if item.status is FindingStatus.REJECTED),
        key=lambda item: (item.finding_type.value, item.severity, item.title, item.finding_id),
    )
    lines.extend(("", "## Approved Findings"))
    for finding in approved:
        evidence_span_ids = _claim_evidence_ids(
            claim_kind="finding",
            claim_id=finding.finding_id,
            references=finding_evidence,
            available_evidence=evidence,
        )
        lines.append(_finding_line(finding, evidence_span_ids))
    lines.extend(("", "## Rejected Review Records"))
    for finding in rejected:
        # This audit section deliberately never states a rejected item as a conclusion.
        evidence_span_ids = _claim_evidence_ids(
            claim_kind="finding",
            claim_id=finding.finding_id,
            references=finding_evidence,
            available_evidence=evidence,
        )
        lines.append(_finding_line(finding, evidence_span_ids))

    referenced = {
        evidence_id
        for identifiers in (*fact_evidence.values(), *finding_evidence.values())
        for evidence_id in identifiers
    }
    missing_referenced_evidence = tuple(sorted(referenced.difference(evidence)))
    if missing_referenced_evidence:
        raise FinalReportRenderIntegrityError(
            "final report references evidence absent from frozen render input: "
            f"{missing_referenced_evidence}",
        )

    lines.extend(("", "## Citations"))
    for item in sorted(
        (evidence[item_id] for item_id in referenced),
        key=lambda item: (
            item.document_version_id,
            item.page_number is None,
            item.page_number if item.page_number is not None else 0,
            item.chunk_id,
            item.start_offset is None,
            item.start_offset if item.start_offset is not None else 0,
            item.end_offset is None,
            item.end_offset if item.end_offset is not None else 0,
            item.evidence_id,
        ),
    ):
        lines.append(
            f"- evidence_span_id: {_json(item.evidence_id)}; document_version_id: {_json(item.document_version_id)}; chunk_id: {_json(item.chunk_id)}; "
            f"page_number: {_json(item.page_number)}; start_offset: {_json(item.start_offset)}; end_offset: {_json(item.end_offset)}; "
            f"quoted_text: {_json(item.source_text)}; content_hash: {_json(item.content_hash)}",
        )
    return tuple(lines)


def _encode_lines(lines: tuple[str, ...]) -> bytes:
    return ("\n".join(lines).replace("\r\n", "\n").replace("\r", "\n") + "\n").encode()


def _validate_baseline_bytes(content: bytes, content_hash: str) -> None:
    if b"\r" in content:
        raise FinalReportRenderIntegrityError("baseline final report is not canonical LF-only UTF-8")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise FinalReportRenderIntegrityError("baseline final report is not valid UTF-8") from error
    if not content.endswith(b"\n"):
        raise FinalReportRenderIntegrityError("baseline final report is missing its final newline")
    actual = f"sha256:{sha256(content).hexdigest()}"
    if content_hash != actual:
        raise FinalReportRenderIntegrityError("baseline final report hash does not match its bytes")


def _semantic_lines(content: bytes) -> tuple[_SemanticLine, ...]:
    try:
        text_lines = content.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError as error:
        raise FinalReportRenderIntegrityError("final report bytes are not valid UTF-8") from error
    result: list[_SemanticLine] = []
    section: str | None = None
    offset = 0
    for text_line in text_lines:
        raw = text_line.encode()
        stripped = text_line.removesuffix("\n")
        if stripped.startswith("## "):
            section = stripped
        semantic = _semantic_line(stripped, section, offset, offset + len(raw), raw)
        if semantic is not None:
            result.append(semantic)
        offset += len(raw)
    return tuple(result)


def _semantic_line(
    line: str,
    section: str | None,
    start: int,
    end: int,
    content: bytes,
) -> _SemanticLine | None:
    match = _DOCUMENT_LINE.match(line)
    if section == "## Source Versions" and match is not None:
        return _SemanticLine("document", match.group(1), None, start, end, content)
    match = _FACT_LINE.match(line)
    if section in {"## Confirmed Facts", "## Disputed Facts"} and match is not None:
        state = "CONFIRMED" if section == "## Confirmed Facts" else "DISPUTED"
        return _SemanticLine("fact", match.group(1), state, start, end, content)
    match = _FINDING_LINE.match(line)
    if section in {"## Approved Findings", "## Rejected Review Records"} and match is not None:
        state = "APPROVED" if section == "## Approved Findings" else "REJECTED"
        return _SemanticLine("finding", match.group(1), state, start, end, content)
    match = _EVIDENCE_LINE.match(line)
    if section == "## Citations" and match is not None:
        return _SemanticLine("evidence", match.group(1), None, start, end, content)
    return None


def _unique_semantic_lines(
    lines: tuple[_SemanticLine, ...],
    label: str,
) -> dict[tuple[str, str, str | None], _SemanticLine]:
    result: dict[tuple[str, str, str | None], _SemanticLine] = {}
    for line in lines:
        if line.key in result:
            raise FinalReportRenderIntegrityError(
                f"{label} report contains duplicate semantic part {line.key}",
            )
        result[line.key] = line
    return result


def _unique_semantic_identity_lines(
    lines: tuple[_SemanticLine, ...],
    label: str,
) -> dict[tuple[str, str], _SemanticLine]:
    result: dict[tuple[str, str], _SemanticLine] = {}
    for line in lines:
        key = (line.kind, line.identity)
        if key in result:
            raise FinalReportRenderIntegrityError(
                f"{label} report contains duplicate semantic identity {key}",
            )
        result[key] = line
    return result


def _preserve_exact_line(
    current: _SemanticLine,
    baseline: _SemanticLine,
    updated_start: int,
    part_id: str,
) -> tuple[bytes, tuple[PreservedReportPart, ...]]:
    if current.content != baseline.content:
        raise FinalReportRenderIntegrityError(
            f"unaffected report part {part_id} differs from the baseline bytes",
        )
    part = _proof_part(
        part_id,
        baseline.start,
        baseline.end,
        updated_start,
        updated_start + len(baseline.content),
        baseline.content,
    )
    return baseline.content, (part,)


def _preserve_carried_fact_line(
    current: _SemanticLine,
    baseline: _SemanticLine,
    updated_start: int,
) -> tuple[bytes, tuple[PreservedReportPart, ...]]:
    if current.state != baseline.state:
        raise FinalReportRenderIntegrityError(
            f"carried Fact {current.identity} changed status relative to the baseline report",
        )
    if current.content == baseline.content:
        return _preserve_exact_line(
            current,
            baseline,
            updated_start,
            f"fact:{current.identity}",
        )

    marker = b"; provenance: "
    suffix_marker = b"; subject: "
    baseline_marker = baseline.content.find(marker)
    current_marker = current.content.find(marker)
    if baseline_marker < 0 or current_marker < 0:
        raise FinalReportRenderIntegrityError(
            f"carried Fact {current.identity} has a malformed provenance field",
        )
    baseline_value_start = baseline_marker + len(marker)
    current_value_start = current_marker + len(marker)
    baseline_suffix_start = baseline.content.find(suffix_marker, baseline_value_start)
    current_suffix_start = current.content.find(suffix_marker, current_value_start)
    if baseline_suffix_start < 0 or current_suffix_start < 0:
        raise FinalReportRenderIntegrityError(
            f"carried Fact {current.identity} has a malformed claim body",
        )
    baseline_prefix = baseline.content[:baseline_value_start]
    current_prefix = current.content[:current_value_start]
    baseline_suffix = baseline.content[baseline_suffix_start:]
    current_suffix = current.content[current_suffix_start:]
    if baseline_prefix != current_prefix or baseline_suffix != current_suffix:
        raise FinalReportRenderIntegrityError(
            f"carried Fact {current.identity} changed outside its run-local provenance field",
        )

    current_provenance = current.content[current_value_start:current_suffix_start]
    replacement = baseline_prefix + current_provenance + baseline_suffix
    if replacement != current.content:
        raise FinalReportRenderIntegrityError(
            f"carried Fact {current.identity} cannot be patched to canonical bytes",
        )
    prefix_end = updated_start + len(baseline_prefix)
    suffix_updated_start = prefix_end + len(current_provenance)
    parts = (
        _proof_part(
            f"fact:{current.identity}:identity",
            baseline.start,
            baseline.start + len(baseline_prefix),
            updated_start,
            prefix_end,
            baseline_prefix,
        ),
        _proof_part(
            f"fact:{current.identity}:claim-body",
            baseline.start + baseline_suffix_start,
            baseline.end,
            suffix_updated_start,
            suffix_updated_start + len(baseline_suffix),
            baseline_suffix,
        ),
    )
    return replacement, parts


def _proof_part(
    part_id: str,
    baseline_start: int,
    baseline_end: int,
    updated_start: int,
    updated_end: int,
    content: bytes,
) -> PreservedReportPart:
    return PreservedReportPart(
        part_id=part_id,
        baseline_start=baseline_start,
        baseline_end=baseline_end,
        updated_start=updated_start,
        updated_end=updated_end,
        content_hash=f"sha256:{sha256(content).hexdigest()}",
    )


def _claim_evidence_ids(
    *,
    claim_kind: str,
    claim_id: str,
    references: dict[str, tuple[str, ...]],
    available_evidence: dict[str, object],
) -> tuple[str, ...]:
    evidence_span_ids = references.get(claim_id)
    if evidence_span_ids is None:
        raise FinalReportRenderIntegrityError(
            f"{claim_kind} {claim_id} has no evidence-reference record",
        )

    missing = tuple(
        evidence_id
        for evidence_id in evidence_span_ids
        if evidence_id not in available_evidence
    )
    if missing:
        raise FinalReportRenderIntegrityError(
            f"{claim_kind} {claim_id} references evidence absent from frozen render input: {missing}",
        )

    return evidence_span_ids


def _fact_line(fact: object, evidence_span_ids: tuple[str, ...]) -> str:
    return (
        f"- fact_id: {_json(fact.fact_id)}; provenance: {_json(fact.relation.value)}; subject: {_json(fact.subject)}; predicate: {_json(fact.predicate)}; "
        f"object: {_json(fact.object_value)}; origin_run_id: {_json(fact.origin_run_id)}; evidence_span_ids: {_json(evidence_span_ids)}"
    )


def _finding_line(finding: object, evidence_span_ids: tuple[str, ...]) -> str:
    return (
        f"- finding_id: {_json(finding.finding_id)}; type: {_json(finding.finding_type.value)}; rule_id: {_json(finding.rule_id)}; severity: {_json(finding.severity)}; "
        f"title: {_json(finding.title)}; description: {_json(finding.description)}; status: {_json(finding.status.value)}; "
        f"evidence_span_ids: {_json(evidence_span_ids)}"
    )

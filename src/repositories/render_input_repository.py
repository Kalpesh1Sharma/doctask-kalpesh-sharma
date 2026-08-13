from dataclasses import dataclass
from typing import cast
from uuid import UUID

from psycopg import Connection
from pydantic import JsonValue

from domain.fact import FactStatus
from domain.render_input import (
    FactEvidenceReferences,
    FindingEvidenceReferences,
    FindingOriginRule,
    FindingOriginRuleSet,
    RenderInputDocument,
    RenderInputEvidenceSpan,
    RenderInputFact,
    RenderInputRule,
    RenderInputRuleSet,
    RenderInputSource,
)
from domain.review_state import ReviewStateSnapshot
from domain.run_membership import RunMembershipRelation
from repositories.run_rule_scope_repository import (
    RunRuleScopeRecord,
    RunRuleScopeRepository,
)


@dataclass(frozen=True)
class RenderInputSourceRecord:
    rule_scope: RunRuleScopeRecord | None
    documents: tuple[RenderInputDocument, ...]
    facts: tuple[RenderInputFact, ...]
    evidence_spans: tuple[RenderInputEvidenceSpan, ...]
    fact_evidence: tuple[FactEvidenceReferences, ...]
    finding_evidence: tuple[FindingEvidenceReferences, ...]
    evaluated_rule_sets: tuple[RenderInputRuleSet, ...]
    evaluated_rules: tuple[RenderInputRule, ...]
    finding_origin_rule_sets: tuple[FindingOriginRuleSet, ...]
    finding_origin_rules: tuple[FindingOriginRule, ...]

    def to_domain(self) -> RenderInputSource:
        return RenderInputSource(
            documents=self.documents,
            facts=self.facts,
            evidence_spans=self.evidence_spans,
            fact_evidence=self.fact_evidence,
            finding_evidence=self.finding_evidence,
            evaluated_rule_sets=self.evaluated_rule_sets,
            evaluated_rules=self.evaluated_rules,
            finding_origin_rule_sets=self.finding_origin_rule_sets,
            finding_origin_rules=self.finding_origin_rules,
        )


class RenderInputRepository:
    """Connection-scoped reads for complete RenderInput source material."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def load_for_run(
        self,
        run_id: str,
        review_state: ReviewStateSnapshot,
    ) -> RenderInputSourceRecord:
        normalized_run_id = str(UUID(run_id))
        documents = tuple(self._load_documents(normalized_run_id))
        facts = tuple(self._load_facts(normalized_run_id))
        fact_evidence = tuple(self._load_fact_evidence(normalized_run_id, facts))
        finding_evidence = tuple(self._load_finding_evidence(review_state))
        evidence_ids = {
            evidence_id
            for references in (*fact_evidence, *finding_evidence)
            for evidence_id in references.evidence_span_ids
        }
        evidence_spans = tuple(self._load_evidence_spans(evidence_ids))
        scope_repository = RunRuleScopeRepository(self._connection)
        rule_scope = scope_repository.get_scope(normalized_run_id)
        evaluated_rule_sets = tuple(
            RenderInputRuleSet(
                source_rule_set_id=record.source_rule_set_id,
                name=record.name,
                description=record.description,
                version=record.version,
            )
            for record in scope_repository.list_rule_sets_for_run(normalized_run_id)
        )
        evaluated_rules = tuple(
            RenderInputRule(
                source_rule_id=record.source_rule_id,
                source_rule_set_id=record.source_rule_set_id,
                rule_key=record.rule_key,
                title=record.title,
                description=record.description,
                severity=record.severity,
                definition=record.definition,
            )
            for record in scope_repository.list_rules_for_run(normalized_run_id)
        )
        origin_rule_sets, origin_rules = self._load_origin_rule_context(normalized_run_id, review_state)

        return RenderInputSourceRecord(
            rule_scope=rule_scope,
            documents=documents,
            facts=facts,
            evidence_spans=evidence_spans,
            fact_evidence=fact_evidence,
            finding_evidence=finding_evidence,
            evaluated_rule_sets=evaluated_rule_sets,
            evaluated_rules=evaluated_rules,
            finding_origin_rule_sets=origin_rule_sets,
            finding_origin_rules=origin_rules,
        )

    def _load_documents(self, run_id: str) -> list[RenderInputDocument]:
        rows = self._connection.execute(
            """
            SELECT document_versions.id, documents.id, document_versions.version_number,
                   document_versions.content_hash
            FROM run_document_versions
            JOIN document_versions ON document_versions.id = run_document_versions.document_version_id
            JOIN documents ON documents.id = document_versions.document_id
            WHERE run_document_versions.run_id = %s::uuid
            ORDER BY document_versions.id ASC
            """,
            (run_id,),
        ).fetchall()
        return [
            RenderInputDocument(
                document_version_id=str(row[0]),
                document_id=str(row[1]),
                version_number=cast(int, row[2]),
                content_hash=cast(str, row[3]),
            )
            for row in rows
        ]

    def _load_facts(self, run_id: str) -> list[RenderInputFact]:
        rows = self._connection.execute(
            """
            SELECT facts.id, facts.origin_run_id, run_facts.relation, facts.subject,
                   facts.predicate, facts.object_value, facts.status
            FROM run_facts
            JOIN facts ON facts.id = run_facts.fact_id
            WHERE run_facts.run_id = %s::uuid
            ORDER BY facts.id ASC
            """,
            (run_id,),
        ).fetchall()
        return [
            RenderInputFact(
                fact_id=str(row[0]),
                origin_run_id=str(row[1]),
                relation=RunMembershipRelation(str(row[2])),
                subject=cast(str, row[3]),
                predicate=cast(str, row[4]),
                object_value=cast(JsonValue, row[5]),
                status=FactStatus(str(row[6])),
            )
            for row in rows
        ]

    def _load_fact_evidence(
        self,
        run_id: str,
        facts: tuple[RenderInputFact, ...],
    ) -> list[FactEvidenceReferences]:
        evidence_by_fact = {fact.fact_id: [] for fact in facts}
        rows = self._connection.execute(
            """
            SELECT fact_evidence.fact_id, fact_evidence.evidence_span_id
            FROM run_facts
            JOIN fact_evidence ON fact_evidence.fact_id = run_facts.fact_id
            WHERE run_facts.run_id = %s::uuid
            ORDER BY fact_evidence.fact_id ASC, fact_evidence.evidence_span_id ASC
            """,
            (run_id,),
        ).fetchall()
        for row in rows:
            evidence_by_fact[str(row[0])].append(str(row[1]))
        return [
            FactEvidenceReferences(fact_id=fact_id, evidence_span_ids=tuple(evidence_ids))
            for fact_id, evidence_ids in evidence_by_fact.items()
        ]

    def _load_finding_evidence(
        self,
        review_state: ReviewStateSnapshot,
    ) -> list[FindingEvidenceReferences]:
        evidence_by_finding = {membership.finding_id: [] for membership in review_state.memberships}
        finding_ids = list(evidence_by_finding)
        if finding_ids:
            rows = self._connection.execute(
                """
                SELECT finding_id, evidence_span_id
                FROM finding_evidence
                WHERE finding_id = ANY(%s::uuid[])
                ORDER BY finding_id ASC, evidence_span_id ASC
                """,
                (finding_ids,),
            ).fetchall()
            for row in rows:
                evidence_by_finding[str(row[0])].append(str(row[1]))
        return [
            FindingEvidenceReferences(finding_id=finding_id, evidence_span_ids=tuple(evidence_ids))
            for finding_id, evidence_ids in evidence_by_finding.items()
        ]

    def _load_evidence_spans(self, evidence_ids: set[str]) -> list[RenderInputEvidenceSpan]:
        if not evidence_ids:
            return []
        rows = self._connection.execute(
            """
            SELECT id, document_version_id, chunk_id, page_number, start_offset, end_offset,
                   quoted_text, content_hash
            FROM evidence_spans
            WHERE id = ANY(%s::uuid[])
            ORDER BY id ASC
            """,
            (sorted(evidence_ids),),
        ).fetchall()
        return [
            RenderInputEvidenceSpan(
                evidence_id=str(row[0]),
                document_version_id=str(row[1]),
                chunk_id=str(row[2]),
                page_number=cast(int | None, row[3]),
                start_offset=cast(int | None, row[4]),
                end_offset=cast(int | None, row[5]),
                source_text=cast(str, row[6]),
                content_hash=cast(str, row[7]),
            )
            for row in rows
        ]

    def _load_origin_rule_context(
        self,
        run_id: str,
        review_state: ReviewStateSnapshot,
    ) -> tuple[tuple[FindingOriginRuleSet, ...], tuple[FindingOriginRule, ...]]:
        requested_pairs = sorted(
            {
                (membership.origin_run_id, membership.rule_id)
                for membership in review_state.memberships
                if membership.finding_type.value == "RULE_VIOLATION"
                and membership.origin_run_id != run_id
                and membership.rule_id is not None
            },
        )
        if not requested_pairs:
            return (), ()

        rows = self._connection.execute(
            """
            WITH requested(origin_run_id, source_rule_id) AS (
                SELECT * FROM unnest(%s::uuid[], %s::uuid[])
            )
            SELECT
                run_rule_snapshots.run_id,
                run_rule_set_snapshots.source_rule_set_id,
                run_rule_set_snapshots.name,
                run_rule_set_snapshots.description,
                run_rule_set_snapshots.version,
                run_rule_snapshots.source_rule_id,
                run_rule_snapshots.rule_key,
                run_rule_snapshots.title,
                run_rule_snapshots.description,
                run_rule_snapshots.severity,
                run_rule_snapshots.definition
            FROM requested
            JOIN run_rule_snapshots
              ON run_rule_snapshots.run_id = requested.origin_run_id
             AND run_rule_snapshots.source_rule_id = requested.source_rule_id
            JOIN run_rule_set_snapshots
              ON run_rule_set_snapshots.run_id = run_rule_snapshots.run_id
             AND run_rule_set_snapshots.source_rule_set_id = run_rule_snapshots.source_rule_set_id
            ORDER BY run_rule_snapshots.run_id ASC, run_rule_snapshots.source_rule_id ASC
            """,
            ([pair[0] for pair in requested_pairs], [pair[1] for pair in requested_pairs]),
        ).fetchall()
        rule_sets: dict[tuple[str, str], FindingOriginRuleSet] = {}
        rules: list[FindingOriginRule] = []
        for row in rows:
            origin_run_id = str(row[0])
            source_rule_set_id = str(row[1])
            rule_sets[(origin_run_id, source_rule_set_id)] = FindingOriginRuleSet(
                origin_run_id=origin_run_id,
                source_rule_set_id=source_rule_set_id,
                name=cast(str, row[2]),
                description=cast(str | None, row[3]),
                version=cast(int, row[4]),
            )
            rules.append(
                FindingOriginRule(
                    origin_run_id=origin_run_id,
                    source_rule_id=str(row[5]),
                    source_rule_set_id=source_rule_set_id,
                    rule_key=cast(str, row[6]),
                    title=cast(str, row[7]),
                    description=cast(str | None, row[8]),
                    severity=cast(str, row[9]),
                    definition=cast(JsonValue, row[10]),
                ),
            )
        return tuple(rule_sets.values()), tuple(rules)

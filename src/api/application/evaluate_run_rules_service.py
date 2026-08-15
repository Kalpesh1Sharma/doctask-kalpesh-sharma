"""Application use case for grounded immutable Run-rule evaluation."""

import re
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from psycopg import Connection
from psycopg.errors import DeadlockDetected, SerializationFailure
from psycopg_pool import ConnectionPool

from api.application.idempotency_claim_takeover import (
    DEFAULT_STALE_CLAIM_AFTER,
    classify_in_progress,
    reclaim_if_stale,
    validate_stale_claim_after,
)
from domain.fact import FactStatus
from domain.fact_extraction import normalize_uuid
from domain.finding import Finding, FindingStatus, FindingType
from domain.rule_evaluation import (
    RuleEvaluationRequest,
    RuleEvaluationResponse,
    RuleEvaluationRule,
    RuleEvaluationRuleSet,
    RuleEvaluationSourceEvidence,
    RuleEvaluationSourceFact,
    RuleOutcomeType,
    canonical_json_value,
    rule_evaluation_input_fingerprint,
)
from domain.run import RunStatus
from domain.run_membership import RunMembershipRelation
from repositories.finding_evidence_repository import FindingEvidenceRepository
from repositories.finding_repository import FindingRepository
from repositories.idempotency_repository import (
    IdempotencyRecord,
    IdempotencyRepository,
    IdempotencyStatus,
    InProgressClaimState,
)
from repositories.run_fact_repository import RunFactReconciliationRecord, RunFactRepository
from repositories.run_finding_repository import RunFindingRepository
from repositories.run_repository import RunRecord, RunRepository
from repositories.run_rule_scope_repository import RunRuleScopeRepository
from services.rule_evaluation_provider import RuleEvaluationProvider, RuleEvaluationProviderError

_SCOPE = "rule-evaluation-v1"
_MAX_ATTEMPTS = 3
_REFERENCE = re.compile(
    r"^rule-evaluation:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{12}:sha256:[0-9a-f]{64}$",
)


class RuleEvaluationApplicationError(Exception):
    pass


class InvalidRuleEvaluationCommandError(RuleEvaluationApplicationError):
    pass


class RuleEvaluationRunNotFoundError(RuleEvaluationApplicationError):
    pass


class RuleEvaluationRunNotEligibleError(RuleEvaluationApplicationError):
    pass


class RuleScopeNotCapturedError(RuleEvaluationApplicationError):
    pass


class RuleEvaluationInputIntegrityError(RuleEvaluationApplicationError):
    pass


class RuleEvaluationInputDriftError(RuleEvaluationApplicationError):
    pass


class RuleEvaluationInputConflictError(RuleEvaluationApplicationError):
    pass


class RuleEvaluationIdempotencyIntegrityError(RuleEvaluationApplicationError):
    pass


class RuleEvaluationInProgressError(RuleEvaluationApplicationError):
    pass


class RuleEvaluationPreviouslyFailedError(RuleEvaluationApplicationError):
    pass


class InvalidRuleEvaluationResponseError(RuleEvaluationApplicationError):
    pass


class UnsupportedRuleEvaluationError(RuleEvaluationApplicationError):
    pass


class RuleEvaluationProviderFailure(RuleEvaluationApplicationError):
    pass


class RuleEvaluationClaimLostError(RuleEvaluationApplicationError):
    pass


@dataclass(frozen=True)
class EvaluateRunRulesCommand:
    run_id: str


@dataclass(frozen=True)
class EvaluateRunRulesResult:
    run_id: str
    input_fingerprint: str
    replayed: bool


@dataclass(frozen=True)
class _Claim:
    request: RuleEvaluationRequest
    fingerprint: str
    token: int


@dataclass(frozen=True)
class _FindingPlan:
    finding: Finding
    evidence_ids: tuple[str, ...]


class EvaluateRunRulesService:
    def __init__(
        self,
        connection_pool: ConnectionPool,
        provider: RuleEvaluationProvider,
        trusted_instructions: str,
        instruction_version: str,
        *,
        stale_claim_after: timedelta = DEFAULT_STALE_CLAIM_AFTER,
    ) -> None:
        self._connection_pool = connection_pool
        self._provider = provider
        self._trusted_instructions = trusted_instructions
        self._instruction_version = instruction_version
        self._stale_claim_after = validate_stale_claim_after(stale_claim_after)

    def evaluate(self, command: EvaluateRunRulesCommand) -> EvaluateRunRulesResult:
        run_id = self._normalize_run_id(command.run_id)
        claimed = self._claim_or_replay(run_id)
        if isinstance(claimed, EvaluateRunRulesResult):
            return claimed

        if not claimed.request.rules:
            self._persist(claimed, ())
            return EvaluateRunRulesResult(run_id, claimed.fingerprint, replayed=False)

        try:
            response = self._provider.evaluate(claimed.request)
            plans = self._build_finding_plans(claimed.request, response)
        except RuleEvaluationApplicationError:
            self._mark_failed(claimed)
            raise
        except RuleEvaluationProviderError as error:
            self._mark_failed(claimed)
            raise RuleEvaluationProviderFailure("rule evaluation provider failed") from error
        except Exception as error:
            self._mark_failed(claimed)
            raise RuleEvaluationProviderFailure("rule evaluation provider failed") from error

        self._persist(claimed, plans)
        return EvaluateRunRulesResult(run_id, claimed.fingerprint, replayed=False)

    def _claim_or_replay(self, run_id: str) -> _Claim | EvaluateRunRulesResult:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                with self._connection_pool.connection() as connection:
                    result = self._claim_once(connection, run_id)
                    connection.commit()
                    return result
            except (DeadlockDetected, SerializationFailure):
                if attempt == _MAX_ATTEMPTS - 1:
                    raise
        raise AssertionError("unreachable")

    def _claim_once(self, connection: Connection, run_id: str) -> _Claim | EvaluateRunRulesResult:
        run = RunRepository(connection).get_for_update(run_id)
        if run is None:
            raise RuleEvaluationRunNotFoundError(f"run {run_id} does not exist")
        repository = IdempotencyRepository(connection)
        existing = repository.get(_SCOPE, self._key(run_id))
        if existing is None:
            if run.status is not RunStatus.RUNNING:
                raise RuleEvaluationRunNotEligibleError(f"run {run_id} is not running rule evaluation")
            request = self._load_request(connection, run_id)
            fingerprint = rule_evaluation_input_fingerprint(request)
            record = repository.claim(_SCOPE, self._key(run_id))
            if record is not None:
                return _Claim(request, fingerprint, record.claim_generation)
            return self._resolve_authoritative_existing(connection, run, repository)
        if existing.status is IdempotencyStatus.FAILED:
            raise RuleEvaluationPreviouslyFailedError("rule evaluation previously failed")
        if existing.status is IdempotencyStatus.COMPLETED:
            return self._resolve_completed_existing(connection, run, existing)
        state = classify_in_progress(repository, existing, self._stale_claim_after)
        if state is InProgressClaimState.MATCHING_FRESH:
            raise RuleEvaluationInProgressError("rule evaluation is already in progress")
        if state is InProgressClaimState.CHANGED_OR_MISSING:
            return self._resolve_authoritative_existing(connection, run, repository)
        if run.status is not RunStatus.RUNNING:
            raise RuleEvaluationRunNotEligibleError(f"run {run_id} is not running rule evaluation")
        request = self._load_request(connection, run_id)
        fingerprint = rule_evaluation_input_fingerprint(request)
        record = reclaim_if_stale(repository, existing, self._stale_claim_after)
        if record is not None:
            return _Claim(request, fingerprint, record.claim_generation)
        return self._resolve_authoritative_existing(connection, run, repository)

    def _resolve_authoritative_existing(
        self, connection: Connection, run: RunRecord, repository: IdempotencyRepository,
    ) -> EvaluateRunRulesResult:
        current = repository.get(_SCOPE, self._key(run.run_id))
        if current is None:
            raise RuleEvaluationIdempotencyIntegrityError("claimed operation disappeared")
        if current.status is IdempotencyStatus.FAILED:
            raise RuleEvaluationPreviouslyFailedError("rule evaluation previously failed")
        if current.status is IdempotencyStatus.IN_PROGRESS:
            raise RuleEvaluationInProgressError("rule evaluation is already in progress")
        return self._resolve_completed_existing(connection, run, current)

    def _resolve_completed_existing(
        self, connection: Connection, run: RunRecord, existing: IdempotencyRecord,
    ) -> EvaluateRunRulesResult:
        request = self._load_request(connection, run.run_id)
        fingerprint = rule_evaluation_input_fingerprint(request)
        return self._resolve_existing(existing, request, fingerprint, self._reference(run.run_id, fingerprint))

    def _resolve_existing(
        self,
        existing: IdempotencyRecord,
        request: RuleEvaluationRequest,
        fingerprint: str,
        reference: str,
    ) -> EvaluateRunRulesResult:
        if existing.status is IdempotencyStatus.COMPLETED:
            if existing.result_reference is None or not _REFERENCE.fullmatch(existing.result_reference):
                raise RuleEvaluationIdempotencyIntegrityError("completed evaluation has malformed reference")
            if existing.result_reference != reference:
                raise RuleEvaluationInputConflictError("rule evaluation completed for different input")
            return EvaluateRunRulesResult(request.run_id, fingerprint, replayed=True)
        if existing.status is IdempotencyStatus.IN_PROGRESS:
            raise RuleEvaluationInProgressError("rule evaluation is already in progress")
        raise RuleEvaluationPreviouslyFailedError("rule evaluation previously failed")

    def _persist(self, claim: _Claim, plans: tuple[_FindingPlan, ...]) -> None:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                with self._connection_pool.connection() as connection:
                    self._persist_once(connection, claim, plans)
                    connection.commit()
                    return
            except (DeadlockDetected, SerializationFailure):
                if attempt == _MAX_ATTEMPTS - 1:
                    raise
        raise AssertionError("unreachable")

    def _persist_once(self, connection: Connection, claim: _Claim, plans: tuple[_FindingPlan, ...]) -> None:
        run = RunRepository(connection).get_for_update(claim.request.run_id)
        if run is None:
            raise RuleEvaluationRunNotFoundError(f"run {claim.request.run_id} does not exist")
        if run.status is not RunStatus.RUNNING:
            raise RuleEvaluationRunNotEligibleError("run is no longer running rule evaluation")
        current = self._load_request(connection, claim.request.run_id)
        if rule_evaluation_input_fingerprint(current) != claim.fingerprint:
            raise RuleEvaluationInputDriftError("rule-evaluation input changed during provider work")

        findings = FindingRepository(connection)
        memberships = RunFindingRepository(connection)
        evidence = FindingEvidenceRepository(connection)
        for plan in plans:
            stored = findings.create(plan.finding)
            memberships.add(claim.request.run_id, stored.finding_id, RunMembershipRelation.CREATED)
            for evidence_id in plan.evidence_ids:
                evidence.add(stored.finding_id, evidence_id)
        completed = IdempotencyRepository(connection).mark_completed(
            _SCOPE,
            self._key(claim.request.run_id),
            claim.token,
            self._reference(claim.request.run_id, claim.fingerprint),
        )
        if completed is None:
            raise RuleEvaluationClaimLostError("rule-evaluation claim is no longer owned")

    def _mark_failed(self, claim: _Claim) -> None:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                with self._connection_pool.connection() as connection:
                    failed = IdempotencyRepository(connection).mark_failed(
                        _SCOPE, self._key(claim.request.run_id), claim.token,
                    )
                    if failed is None:
                        raise RuleEvaluationClaimLostError("rule-evaluation claim is no longer owned")
                    connection.commit()
                    return
            except (DeadlockDetected, SerializationFailure):
                if attempt == _MAX_ATTEMPTS - 1:
                    raise
        raise AssertionError("unreachable")

    def _load_request(self, connection: Connection, run_id: str) -> RuleEvaluationRequest:
        scopes = RunRuleScopeRepository(connection)
        if scopes.get_scope(run_id) is None:
            raise RuleScopeNotCapturedError(f"run {run_id} has no closed rule scope")
        rule_sets = tuple(
            RuleEvaluationRuleSet(
                source_rule_set_id=item.source_rule_set_id,
                name=item.name,
                description=item.description,
                version=item.version,
            )
            for item in scopes.list_rule_sets_for_run(run_id)
        )
        rules = tuple(
            RuleEvaluationRule(
                source_rule_id=item.source_rule_id,
                source_rule_set_id=item.source_rule_set_id,
                rule_key=item.rule_key,
                title=item.title,
                description=item.description,
                severity=item.severity,
                definition=item.definition,
            )
            for item in scopes.list_rules_for_run(run_id)
        )
        source_facts = () if not rules else self._load_facts(connection, run_id)
        return RuleEvaluationRequest(
            run_id=run_id,
            instruction_version=self._instruction_version,
            trusted_instructions=self._trusted_instructions,
            rule_sets=rule_sets,
            rules=rules,
            source_facts=source_facts,
        )

    def _load_facts(self, connection: Connection, run_id: str) -> tuple[RuleEvaluationSourceFact, ...]:
        records = RunFactRepository(connection).list_reconciliation_input_for_run(run_id)
        facts = tuple(
            self._source_fact(record)
            for record in records
            if record.status in {FactStatus.CONFIRMED, FactStatus.DISPUTED}
        )
        if any(not fact.evidence for fact in facts):
            raise RuleEvaluationInputIntegrityError("participating Facts require durable evidence")
        return facts

    def _source_fact(self, record: RunFactReconciliationRecord) -> RuleEvaluationSourceFact:
        return RuleEvaluationSourceFact(
            fact_id=record.fact_id,
            origin_run_id=record.origin_run_id,
            relation=record.relation,
            subject=record.subject,
            predicate=record.predicate,
            object_value=record.object_value,
            status=record.status,
            evidence=tuple(
                RuleEvaluationSourceEvidence(
                    evidence_id=item.evidence_id,
                    document_version_id=item.document_version_id,
                    chunk_id=item.chunk_id,
                    page_number=item.page_number,
                    start_offset=item.start_offset,
                    end_offset=item.end_offset,
                    source_text=item.source_text,
                    content_hash=item.content_hash,
                )
                for item in record.evidence
            ),
        )

    def _build_finding_plans(
        self,
        request: RuleEvaluationRequest,
        response: RuleEvaluationResponse,
    ) -> tuple[_FindingPlan, ...]:
        if not isinstance(response, RuleEvaluationResponse):
            raise InvalidRuleEvaluationResponseError("provider returned an invalid response type")
        rules = {rule.source_rule_id: rule for rule in request.rules}
        facts = {fact.fact_id: fact for fact in request.source_facts}
        if {item.source_rule_id for item in response.outcomes} != set(rules):
            raise InvalidRuleEvaluationResponseError("response must contain every supplied Rule exactly once")
        plans: list[_FindingPlan] = []
        for outcome in response.outcomes:
            if outcome.source_rule_id not in rules:
                raise InvalidRuleEvaluationResponseError("provider referenced Rule outside immutable scope")
            if outcome.outcome is RuleOutcomeType.UNSUPPORTED:
                raise UnsupportedRuleEvaluationError("provider cannot ground one or more captured Rules")
            if outcome.outcome is not RuleOutcomeType.VIOLATIONS:
                continue
            rule = rules[outcome.source_rule_id]
            for candidate in outcome.violations:
                if any(fact_id not in facts for fact_id in candidate.supporting_fact_ids):
                    raise InvalidRuleEvaluationResponseError("provider referenced Fact outside evaluation input")
                support = tuple(facts[fact_id] for fact_id in candidate.supporting_fact_ids)
                evidence_ids = tuple(sorted({
                    *(item.evidence_id for fact in support for item in fact.evidence),
                }))
                if not evidence_ids:
                    raise RuleEvaluationInputIntegrityError("violations require durable evidence")
                values = ", ".join(canonical_json_value(fact.object_value) for fact in support)
                plans.append(_FindingPlan(
                    Finding(
                        finding_id=str(uuid4()),
                        origin_run_id=request.run_id,
                        finding_type=FindingType.RULE_VIOLATION,
                        rule_id=rule.source_rule_id,
                        title=f"Rule violation: {rule.title}",
                        description=(
                            f"Rule {rule.rule_key} ({rule.source_rule_id}) violated by Fact IDs: "
                            f"{', '.join(candidate.supporting_fact_ids)}; values: {values}."
                        ),
                        severity=rule.severity,
                        status=FindingStatus.PENDING,
                    ),
                    evidence_ids,
                ))
        return tuple(plans)

    def _normalize_run_id(self, value: str) -> str:
        try:
            return normalize_uuid(value, "run_id")
        except ValueError as error:
            raise InvalidRuleEvaluationCommandError("run_id must be a valid UUID") from error

    @staticmethod
    def _key(run_id: str) -> str:
        return f"examine-rules:{run_id}"

    @staticmethod
    def _reference(run_id: str, fingerprint: str) -> str:
        return f"rule-evaluation:{run_id}:{fingerprint}"

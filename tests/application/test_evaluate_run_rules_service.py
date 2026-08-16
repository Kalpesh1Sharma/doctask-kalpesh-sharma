import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

import pytest
from psycopg.errors import DeadlockDetected, DivisionByZero, LockNotAvailable, SerializationFailure

from api.application import evaluate_run_rules_service as evaluation_module
from api.application.evaluate_run_rules_service import (
    EvaluateRunRulesCommand,
    EvaluateRunRulesService,
    InvalidRuleEvaluationResponseError,
    RuleEvaluationClaimLostError,
    RuleEvaluationIdempotencyIntegrityError,
    RuleEvaluationInProgressError,
    RuleEvaluationInputConflictError,
    RuleEvaluationInputDriftError,
    RuleEvaluationPreviouslyFailedError,
    RuleEvaluationProviderFailure,
    RuleEvaluationRunNotEligibleError,
    RuleScopeNotCapturedError,
    UnsupportedRuleEvaluationError,
)
from db.pool import pool
from domain.fact import Fact, FactStatus
from domain.rule_evaluation import (
    RuleEvaluationOutcome,
    RuleEvaluationRequest,
    RuleEvaluationResponse,
    RuleOutcomeType,
    RuleViolationCandidate,
)
from domain.run_membership import RunMembershipRelation
from domain.run_rule_scope import RunRuleSetSnapshot, RunRuleSnapshot
from repositories.fact_evidence_repository import FactEvidenceRepository
from repositories.fact_repository import FactRepository
from repositories.idempotency_repository import (
    IdempotencyFailureKind,
    IdempotencyRepository,
    IdempotencyStatus,
)
from repositories.run_fact_repository import RunFactRepository
from repositories.run_repository import RunRepository
from repositories.run_rule_scope_repository import RunRuleScopeRepository
from services.rule_evaluation_provider import RuleEvaluationProviderError


@dataclass(frozen=True)
class Resources:
    pile_id: str
    run_id: str
    rule_set_id: str
    rule_id: str
    document_version_id: str
    chunk_id: str
    evidence_id: str
    fact_id: str


class FakeProvider:
    def __init__(self, response: RuleEvaluationResponse) -> None:
        self.response = response
        self.calls: list[RuleEvaluationRequest] = []
        self.before_return = None

    def evaluate(self, request: RuleEvaluationRequest) -> RuleEvaluationResponse:
        self.calls.append(request)
        if self.before_return is not None:
            self.before_return(request)
        return self.response


class FailingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, _: RuleEvaluationRequest) -> RuleEvaluationResponse:
        self.calls += 1
        raise RuleEvaluationProviderError("provider unavailable")


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


def create_resources(
    *,
    scope: str = "normal",
    status: str = "RUNNING",
) -> Resources:
    with pool.connection() as connection:
        pile = connection.execute(
            "INSERT INTO piles (name) VALUES (%s) RETURNING id",
            (f"Rule evaluation {uuid4()}",),
        ).fetchone()
        assert pile is not None
        pile_id = str(pile[0])
        run = connection.execute(
            "INSERT INTO runs (pile_id, run_type, status) VALUES (%s::uuid, 'MANUAL', %s::run_status) RETURNING id",
            (pile_id, status),
        ).fetchone()
        assert run is not None
        run_id = str(run[0])
        rule_set = connection.execute(
            "INSERT INTO rule_sets (pile_id, name, description, version) VALUES (%s::uuid, 'Immutable rules', 'Captured policy', 1) RETURNING id",
            (pile_id,),
        ).fetchone()
        assert rule_set is not None
        rule_set_id = str(rule_set[0])
        rule_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO rules (id, rule_set_id, rule_key, title, description, severity, definition)
            VALUES (%s::uuid, %s::uuid, 'amount-max', 'Amount maximum', 'No amount over 100', 'HIGH', %s::jsonb)
            """,
            (rule_id, rule_set_id, '{"maximum":100}'),
        )
        version_id, chunk_id, evidence_id = create_document_evidence(
            connection, pile_id, "Amount 110", 7, 10, "110"
        )
        fact_id = create_fact(connection, run_id, 110, evidence_id)
        if scope != "none":
            snapshots = RunRuleScopeRepository(connection)
            if scope != "empty":
                snapshots.insert_rule_set_snapshot(RunRuleSetSnapshot(
                    run_id=run_id,
                    source_rule_set_id=rule_set_id,
                    name="Immutable rules",
                    description="Captured policy",
                    version=1,
                ))
            if scope == "normal":
                snapshots.insert_rule_snapshot(RunRuleSnapshot(
                    run_id=run_id,
                    source_rule_id=rule_id,
                    source_rule_set_id=rule_set_id,
                    rule_key="amount-max",
                    title="Amount maximum",
                    description="No amount over 100",
                    severity="HIGH",
                    definition={"maximum": 100},
                ))
            snapshots.complete_scope(run_id)
        connection.commit()
    return Resources(pile_id, run_id, rule_set_id, rule_id, version_id, chunk_id, evidence_id, fact_id)


def create_document_evidence(
    connection,
    pile_id: str,
    content: str,
    start: int,
    end: int,
    quote: str,
) -> tuple[str, str, str]:
    document = connection.execute(
        "INSERT INTO documents (pile_id, logical_key) VALUES (%s::uuid, %s) RETURNING id",
        (pile_id, f"rule-{uuid4()}"),
    ).fetchone()
    assert document is not None
    version = connection.execute(
        """
        INSERT INTO document_versions (document_id, version_number, content_hash, storage_uri)
        VALUES (%s::uuid, 1, %s, %s) RETURNING id
        """,
        (str(document[0]), f"sha256:{uuid4().hex}", f"storage://{uuid4()}"),
    ).fetchone()
    assert version is not None
    chunk = connection.execute(
        """
        INSERT INTO document_chunks (document_version_id, chunk_index, content, content_hash)
        VALUES (%s::uuid, 0, %s, %s) RETURNING id
        """,
        (str(version[0]), content, f"sha256:{uuid4().hex}"),
    ).fetchone()
    assert chunk is not None
    evidence = connection.execute(
        """
        INSERT INTO evidence_spans (document_version_id, chunk_id, start_offset, end_offset, quoted_text, content_hash)
        VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s) RETURNING id
        """,
        (str(version[0]), str(chunk[0]), start, end, quote, f"sha256:{uuid4().hex}"),
    ).fetchone()
    assert evidence is not None
    return str(version[0]), str(chunk[0]), str(evidence[0])


def create_fact(
    connection,
    run_id: str,
    value: object,
    evidence_id: str,
    *,
    origin_run_id: str | None = None,
    relation: RunMembershipRelation = RunMembershipRelation.CREATED,
    status: FactStatus = FactStatus.CONFIRMED,
) -> str:
    record = FactRepository(connection).create(Fact(
        fact_id=str(uuid4()),
        origin_run_id=origin_run_id or run_id,
        subject="amount",
        predicate="equals",
        object_value=value,
        status=status,
    ))
    RunFactRepository(connection).add(run_id, record.fact_id, relation)
    FactEvidenceRepository(connection).add(record.fact_id, evidence_id)
    return record.fact_id


def response_for(resources: Resources, *fact_ids: str) -> RuleEvaluationResponse:
    return RuleEvaluationResponse(format="rule-evaluation-v1", outcomes=(RuleEvaluationOutcome(
        source_rule_id=resources.rule_id,
        outcome=RuleOutcomeType.VIOLATIONS,
        violations=(RuleViolationCandidate(supporting_fact_ids=fact_ids or (resources.fact_id,)),),
    ),))


def no_violation(resources: Resources) -> RuleEvaluationResponse:
    return RuleEvaluationResponse(format="rule-evaluation-v1", outcomes=(RuleEvaluationOutcome(
        source_rule_id=resources.rule_id,
        outcome=RuleOutcomeType.NO_VIOLATION,
    ),))


def service(provider) -> EvaluateRunRulesService:
    return EvaluateRunRulesService(pool, provider, "Evaluate immutable policy only.", "rules-v1")


def record(resources: Resources):
    with pool.connection() as connection:
        return IdempotencyRepository(connection).get(
            "rule-evaluation-v1", f"examine-rules:{resources.run_id}"
        )


def finding_count(resources: Resources) -> int:
    with pool.connection() as connection:
        row = connection.execute(
            "SELECT count(*) FROM findings WHERE origin_run_id = %s::uuid", (resources.run_id,)
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_persists_rule_violation_from_immutable_snapshot_and_origin_backstop() -> None:
    resources = create_resources()
    with pool.connection() as connection:
        connection.execute(
            "UPDATE rules SET title = 'Mutable rewrite', description = 'changed', severity = 'LOW', definition = %s::jsonb WHERE id = %s::uuid",
            ('{"maximum":999}', resources.rule_id),
        )
        connection.execute(
            "UPDATE rule_sets SET name = 'Mutable set', description = 'changed set', version = 2 WHERE id = %s::uuid",
            (resources.rule_set_id,),
        )
        connection.commit()
    provider = FakeProvider(response_for(resources))
    result = service(provider).evaluate(EvaluateRunRulesCommand(resources.run_id.upper()))
    assert result.replayed is False
    assert provider.calls[0].rule_sets[0].model_dump() == {
        "source_rule_set_id": resources.rule_set_id, "name": "Immutable rules",
        "description": "Captured policy", "version": 1,
    }
    assert provider.calls[0].rules[0].title == "Amount maximum"
    assert provider.calls[0].rules[0].severity == "HIGH"
    assert provider.calls[0].rules[0].definition == {"maximum": 100}
    with pool.connection() as connection:
        finding = connection.execute(
            "SELECT id, rule_id, origin_run_id, status, severity, title, description FROM findings WHERE origin_run_id = %s::uuid",
            (resources.run_id,),
        ).fetchone()
        assert finding is not None
        membership = connection.execute(
            "SELECT relation FROM run_findings WHERE run_id = %s::uuid AND finding_id = %s::uuid",
            (resources.run_id, str(finding[0])),
        ).fetchone()
        evidence = connection.execute(
            "SELECT evidence_span_id FROM finding_evidence WHERE finding_id = %s::uuid ORDER BY evidence_span_id",
            (str(finding[0]),),
        ).fetchall()
        snapshot = connection.execute(
            "SELECT 1 FROM run_rule_snapshots WHERE run_id = %s::uuid AND source_rule_id = %s::uuid",
            (resources.run_id, resources.rule_id),
        ).fetchone()
    assert tuple(map(str, finding[1:3])) == (resources.rule_id, resources.run_id)
    assert finding[3:6] == ("PENDING", "HIGH", "Rule violation: Amount maximum")
    assert finding[6] == (
        f"Rule amount-max ({resources.rule_id}) violated by Fact IDs: {resources.fact_id}; values: 110."
    )
    assert membership == ("CREATED",)
    assert [str(row[0]) for row in evidence] == [resources.evidence_id]
    assert snapshot == (1,)
    completed = record(resources)
    assert completed is not None and completed.status is IdempotencyStatus.COMPLETED


def test_multiple_supporting_facts_cross_document_evidence_and_membership_statuses() -> None:
    resources = create_resources()
    with pool.connection() as connection:
        version, _, evidence = create_document_evidence(connection, resources.pile_id, "Amount 120", 7, 10, "120")
        origin = connection.execute(
            "INSERT INTO runs (pile_id, run_type, status) VALUES (%s::uuid, 'MANUAL', 'RUNNING') RETURNING id",
            (resources.pile_id,),
        ).fetchone()
        assert origin is not None
        carried = create_fact(
            connection, resources.run_id, 120, evidence, origin_run_id=str(origin[0]),
            relation=RunMembershipRelation.CARRIED_FORWARD, status=FactStatus.DISPUTED,
        )
        superseded = create_fact(connection, resources.run_id, 130, evidence, status=FactStatus.SUPERSEDED)
        connection.commit()
    provider = FakeProvider(response_for(resources, carried, resources.fact_id, carried))
    service(provider).evaluate(EvaluateRunRulesCommand(resources.run_id))
    supplied = {fact.fact_id: fact for fact in provider.calls[0].source_facts}
    assert carried in supplied and supplied[carried].status is FactStatus.DISPUTED
    assert supplied[carried].relation is RunMembershipRelation.CARRIED_FORWARD
    assert resources.fact_id in supplied and superseded not in supplied
    with pool.connection() as connection:
        finding = connection.execute("SELECT id FROM findings WHERE origin_run_id = %s::uuid", (resources.run_id,)).fetchone()
        assert finding is not None
        rows = connection.execute(
            "SELECT evidence_span_id FROM finding_evidence WHERE finding_id = %s::uuid ORDER BY evidence_span_id",
            (str(finding[0]),),
        ).fetchall()
    assert [str(row[0]) for row in rows] == sorted([resources.evidence_id, evidence])
    assert version != resources.document_version_id


def test_nonempty_scope_no_violation_completes_with_zero_findings() -> None:
    resources = create_resources()
    provider = FakeProvider(no_violation(resources))
    service(provider).evaluate(EvaluateRunRulesCommand(resources.run_id))
    assert len(provider.calls) == 1 and finding_count(resources) == 0
    assert record(resources).status is IdempotencyStatus.COMPLETED  # type: ignore[union-attr]


@pytest.mark.parametrize("scope", ["empty", "selected_empty"])
def test_empty_captured_scope_variants_complete_without_provider(scope: str) -> None:
    resources = create_resources(scope=scope)
    provider = FakeProvider(RuleEvaluationResponse(format="rule-evaluation-v1", outcomes=()))
    service(provider).evaluate(EvaluateRunRulesCommand(resources.run_id))
    assert provider.calls == []
    assert finding_count(resources) == 0
    assert record(resources).status is IdempotencyStatus.COMPLETED  # type: ignore[union-attr]


def test_uncaptured_scope_rejects_before_provider() -> None:
    resources = create_resources(scope="none")
    provider = FakeProvider(RuleEvaluationResponse(format="rule-evaluation-v1", outcomes=()))
    with pytest.raises(RuleScopeNotCapturedError):
        service(provider).evaluate(EvaluateRunRulesCommand(resources.run_id))
    assert provider.calls == [] and record(resources) is None


def test_fresh_non_running_status_rejects_without_writes() -> None:
    resources = create_resources(status="REVIEW_REQUIRED")
    with pytest.raises(RuleEvaluationRunNotEligibleError):
        service(FakeProvider(no_violation(resources))).evaluate(EvaluateRunRulesCommand(resources.run_id))
    assert finding_count(resources) == 0 and record(resources) is None


def test_provider_and_invalid_response_failures_mark_claim_failed_without_findings() -> None:
    resources = create_resources()
    with pytest.raises(RuleEvaluationProviderFailure):
        service(FailingProvider()).evaluate(EvaluateRunRulesCommand(resources.run_id))
    assert record(resources).status is IdempotencyStatus.FAILED  # type: ignore[union-attr]
    assert record(resources).failure_kind is IdempotencyFailureKind.RETRYABLE_PROVIDER_FAILURE  # type: ignore[union-attr]
    assert finding_count(resources) == 0

    invalid = create_resources()
    missing = FakeProvider(RuleEvaluationResponse(format="rule-evaluation-v1", outcomes=()))
    with pytest.raises(InvalidRuleEvaluationResponseError):
        service(missing).evaluate(EvaluateRunRulesCommand(invalid.run_id))
    assert record(invalid).status is IdempotencyStatus.FAILED  # type: ignore[union-attr]
    assert record(invalid).failure_kind is IdempotencyFailureKind.RETRYABLE_INVALID_RESPONSE  # type: ignore[union-attr]
    assert finding_count(invalid) == 0


def test_normal_failed_retryable_rules_never_reopen_without_workflow() -> None:
    resources = create_resources()
    with pool.connection() as connection:
        claimed = IdempotencyRepository(connection).claim(
            "rule-evaluation-v1", f"examine-rules:{resources.run_id}",
        )
        assert claimed is not None
        IdempotencyRepository(connection).mark_failed(
            claimed.scope, claimed.key, claimed.claim_generation,
            IdempotencyFailureKind.RETRYABLE_PROVIDER_FAILURE,
        )
        connection.commit()
    provider = FakeProvider(no_violation(resources))
    with pytest.raises(RuleEvaluationPreviouslyFailedError):
        service(provider).evaluate(EvaluateRunRulesCommand(resources.run_id))
    current = record(resources)
    assert provider.calls == []
    assert current is not None and current.claim_generation == claimed.claim_generation
    assert current.failure_kind is IdempotencyFailureKind.RETRYABLE_PROVIDER_FAILURE


def test_unsupported_rule_marks_claim_failed_without_findings() -> None:
    resources = create_resources()
    provider = FakeProvider(RuleEvaluationResponse(format="rule-evaluation-v1", outcomes=(
        RuleEvaluationOutcome(source_rule_id=resources.rule_id, outcome=RuleOutcomeType.UNSUPPORTED),
    )))
    with pytest.raises(UnsupportedRuleEvaluationError):
        service(provider).evaluate(EvaluateRunRulesCommand(resources.run_id))
    assert finding_count(resources) == 0
    assert record(resources).status is IdempotencyStatus.FAILED  # type: ignore[union-attr]
    assert record(resources).failure_kind is IdempotencyFailureKind.RETRYABLE_UNSUPPORTED_EVALUATION  # type: ignore[union-attr]


@pytest.mark.parametrize("kind", ["unknown_rule", "unknown_fact"])
def test_out_of_scope_provider_response_is_rejected_before_insertion(kind: str) -> None:
    resources = create_resources()
    if kind == "unknown_rule":
        response = RuleEvaluationResponse(format="rule-evaluation-v1", outcomes=(RuleEvaluationOutcome(
            source_rule_id=str(uuid4()), outcome=RuleOutcomeType.NO_VIOLATION,
        ),))
    else:
        response = RuleEvaluationResponse(format="rule-evaluation-v1", outcomes=(RuleEvaluationOutcome(
            source_rule_id=resources.rule_id, outcome=RuleOutcomeType.VIOLATIONS,
            violations=(RuleViolationCandidate(supporting_fact_ids=(str(uuid4()),)),),
        ),))
    with pytest.raises(InvalidRuleEvaluationResponseError):
        service(FakeProvider(response)).evaluate(EvaluateRunRulesCommand(resources.run_id))
    assert finding_count(resources) == 0
    assert record(resources).status is IdempotencyStatus.FAILED  # type: ignore[union-attr]


def test_run_progression_and_input_drift_during_provider_work_write_nothing() -> None:
    progressed = create_resources()
    provider = FakeProvider(no_violation(progressed))

    def progress(_: RuleEvaluationRequest) -> None:
        with pool.connection() as connection:
            connection.execute("UPDATE runs SET status = 'REVIEW_REQUIRED'::run_status WHERE id = %s::uuid", (progressed.run_id,))
            connection.commit()

    provider.before_return = progress
    with pytest.raises(RuleEvaluationRunNotEligibleError):
        service(provider).evaluate(EvaluateRunRulesCommand(progressed.run_id))
    assert finding_count(progressed) == 0
    assert record(progressed).status is IdempotencyStatus.IN_PROGRESS  # type: ignore[union-attr]

    drifted = create_resources()
    provider = FakeProvider(no_violation(drifted))

    def drift(_: RuleEvaluationRequest) -> None:
        with pool.connection() as connection:
            connection.execute("UPDATE evidence_spans SET quoted_text = 'changed' WHERE id = %s::uuid", (drifted.evidence_id,))
            connection.commit()

    provider.before_return = drift
    with pytest.raises(RuleEvaluationInputDriftError):
        service(provider).evaluate(EvaluateRunRulesCommand(drifted.run_id))
    assert finding_count(drifted) == 0
    assert record(drifted).status is IdempotencyStatus.IN_PROGRESS  # type: ignore[union-attr]


def test_provider_runs_after_preflight_lock_releases_and_fixed_key_contends() -> None:
    resources = create_resources()
    provider = FakeProvider(no_violation(resources))

    def acquire(_: RuleEvaluationRequest) -> None:
        with pool.connection() as connection:
            connection.execute("SET LOCAL lock_timeout = '100ms'")
            assert RunRepository(connection).get_for_update(resources.run_id) is not None
            connection.commit()

    provider.before_return = acquire
    service(provider).evaluate(EvaluateRunRulesCommand(resources.run_id))

    contested = create_resources()
    provider = FakeProvider(no_violation(contested))
    entered, release = threading.Event(), threading.Event()

    def pause(_: RuleEvaluationRequest) -> None:
        entered.set()
        assert release.wait(timeout=5)

    provider.before_return = pause
    evaluator = service(provider)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(evaluator.evaluate, EvaluateRunRulesCommand(contested.run_id))
        assert entered.wait(timeout=5)
        second = executor.submit(evaluator.evaluate, EvaluateRunRulesCommand(contested.run_id))
        with pytest.raises(RuleEvaluationInProgressError):
            second.result(timeout=5)
        release.set()
        first.result(timeout=5)
    assert len(provider.calls) == 1


def test_completed_replay_conflict_integrity_and_prior_claim_outcomes() -> None:
    resources = create_resources()
    provider = FakeProvider(no_violation(resources))
    first = service(provider).evaluate(EvaluateRunRulesCommand(resources.run_id))
    with pool.connection() as connection:
        connection.execute("UPDATE runs SET status = 'REVIEW_REQUIRED'::run_status WHERE id = %s::uuid", (resources.run_id,))
        connection.commit()
    replay = service(provider).evaluate(EvaluateRunRulesCommand(resources.run_id))
    assert replay.replayed and replay.input_fingerprint == first.input_fingerprint and len(provider.calls) == 1

    for reference, error in (("malformed", RuleEvaluationIdempotencyIntegrityError), (
        f"rule-evaluation:{uuid4()}:sha256:{'0' * 64}", RuleEvaluationInputConflictError,
    )):
        candidate = create_resources()
        with pool.connection() as connection:
            connection.execute(
                "INSERT INTO idempotency_records (scope, key, status, result_reference, claimed_at, completed_at) VALUES (%s, %s, 'COMPLETED', %s, clock_timestamp(), now())",
                ("rule-evaluation-v1", f"examine-rules:{candidate.run_id}", reference),
            )
            connection.commit()
        silent = FakeProvider(no_violation(candidate))
        with pytest.raises(error):
            service(silent).evaluate(EvaluateRunRulesCommand(candidate.run_id))
        assert silent.calls == []

    candidate = create_resources()
    with pool.connection() as connection:
        claimed = IdempotencyRepository(connection).claim("rule-evaluation-v1", f"examine-rules:{candidate.run_id}")
        assert claimed is not None and claimed.claimed_at is not None
        connection.commit()
    silent = FakeProvider(no_violation(candidate))
    with pytest.raises(RuleEvaluationInProgressError):
        service(silent).evaluate(EvaluateRunRulesCommand(candidate.run_id))
    with pool.connection() as connection:
        current = IdempotencyRepository(connection).get("rule-evaluation-v1", f"examine-rules:{candidate.run_id}")
        assert current is not None and current.claimed_at is not None
        IdempotencyRepository(connection).mark_failed("rule-evaluation-v1", f"examine-rules:{candidate.run_id}", current.claim_generation)
        connection.commit()
    with pytest.raises(RuleEvaluationPreviouslyFailedError):
        service(silent).evaluate(EvaluateRunRulesCommand(candidate.run_id))
    assert silent.calls == []


def test_fresh_owner_precedes_scope_and_fact_reconstruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = create_resources()
    scope = "rule-evaluation-v1"
    key = f"examine-rules:{resources.run_id}"
    with pool.connection() as connection:
        record = IdempotencyRepository(connection).claim(scope, key)
        assert record is not None
        connection.commit()
    provider = FakeProvider(no_violation(resources))

    def invalid_request(*_args):
        raise RuleScopeNotCapturedError("current scope is missing")

    monkeypatch.setattr(EvaluateRunRulesService, "_load_request", invalid_request)
    with pytest.raises(RuleEvaluationInProgressError):
        service(provider).evaluate(EvaluateRunRulesCommand(resources.run_id))
    assert provider.calls == []


def test_stale_claim_with_ineligible_run_does_not_reclaim() -> None:
    resources = create_resources()
    scope = "rule-evaluation-v1"
    key = f"examine-rules:{resources.run_id}"
    provider = FakeProvider(no_violation(resources))
    with pool.connection() as connection:
        record = IdempotencyRepository(connection).claim(scope, key)
        assert record is not None
        connection.execute(
            "UPDATE idempotency_records SET claimed_at = clock_timestamp() - interval '6 minutes' WHERE scope = %s AND key = %s",
            (scope, key),
        )
        connection.execute(
            "UPDATE runs SET status = 'REVIEW_REQUIRED'::run_status WHERE id = %s::uuid",
            (resources.run_id,),
        )
        connection.commit()
    with pool.connection() as connection:
        aged = IdempotencyRepository(connection).get(scope, key)
    assert aged is not None and aged.claimed_at is not None
    with pytest.raises(RuleEvaluationRunNotEligibleError):
        service(provider).evaluate(EvaluateRunRulesCommand(resources.run_id))
    assert provider.calls == []
    with pool.connection() as connection:
        current = IdempotencyRepository(connection).get(scope, key)
    assert current is not None
    assert current.status is IdempotencyStatus.IN_PROGRESS
    assert current.claim_generation == aged.claim_generation
    assert current.claimed_at == aged.claimed_at


def test_reclaimed_stale_claim_and_post_staging_database_failure_roll_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = create_resources()
    provider = FakeProvider(response_for(resources))
    stale_generation: list[int] = []

    def reclaim(_: RuleEvaluationRequest) -> None:
        with pool.connection() as connection:
            repository = IdempotencyRepository(connection)
            current = repository.get("rule-evaluation-v1", f"examine-rules:{resources.run_id}")
            assert current is not None and current.claimed_at is not None
            stale_generation.append(current.claim_generation)
            connection.execute("UPDATE idempotency_records SET claimed_at = clock_timestamp() - interval '6 minutes' WHERE scope = %s AND key = %s", ("rule-evaluation-v1", f"examine-rules:{resources.run_id}"))
            replacement = repository.reclaim_stale(
                "rule-evaluation-v1",
                f"examine-rules:{resources.run_id}",
                current.claim_generation,
                timedelta(minutes=5),
            )
            assert replacement is not None
            assert replacement.claim_generation == current.claim_generation + 1
            connection.commit()

    provider.before_return = reclaim
    with pytest.raises(RuleEvaluationClaimLostError):
        service(provider).evaluate(EvaluateRunRulesCommand(resources.run_id))

    assert stale_generation
    assert finding_count(resources) == 0
    replacement = record(resources)
    assert replacement is not None
    assert replacement.status is IdempotencyStatus.IN_PROGRESS
    assert replacement.claim_generation == stale_generation[0] + 1
    with pool.connection() as connection:
        run_findings = connection.execute(
            "SELECT count(*) FROM run_findings WHERE run_id = %s::uuid",
            (resources.run_id,),
        ).fetchone()
        finding_evidence = connection.execute(
            "SELECT count(*) FROM finding_evidence WHERE finding_id IN (SELECT id FROM findings WHERE origin_run_id = %s::uuid)",
            (resources.run_id,),
        ).fetchone()
        connection.execute(
            "UPDATE idempotency_records SET claimed_at = clock_timestamp() - interval '6 minutes' WHERE scope = %s AND key = %s",
            ("rule-evaluation-v1", f"examine-rules:{resources.run_id}"),
        )
        connection.commit()
    assert run_findings == (0,)
    assert finding_evidence == (0,)

    recovery_provider = FakeProvider(response_for(resources))
    recovered = service(recovery_provider).evaluate(EvaluateRunRulesCommand(resources.run_id))
    assert recovered.replayed is False
    assert len(recovery_provider.calls) == 1

    final = record(resources)
    assert final is not None
    assert final.status is IdempotencyStatus.COMPLETED
    assert final.claim_generation > stale_generation[0]
    assert finding_count(resources) == 1
    with pool.connection() as connection:
        findings = connection.execute(
            "SELECT id, finding_type, rule_id, status FROM findings WHERE origin_run_id = %s::uuid",
            (resources.run_id,),
        ).fetchall()
        memberships = connection.execute(
            "SELECT finding_id, relation FROM run_findings WHERE run_id = %s::uuid",
            (resources.run_id,),
        ).fetchall()
        evidence = connection.execute(
            "SELECT evidence_span_id FROM finding_evidence WHERE finding_id = %s::uuid ORDER BY evidence_span_id",
            (str(findings[0][0]),),
        ).fetchall()
    assert len(findings) == 1
    assert findings[0][1] == "RULE_VIOLATION"
    assert str(findings[0][2]) == resources.rule_id
    assert findings[0][3] == "PENDING"
    assert memberships == [(findings[0][0], "CREATED")]
    assert [str(row[0]) for row in evidence] == [resources.evidence_id]

    broken = create_resources()
    original = evaluation_module.IdempotencyRepository.mark_completed

    def fail_after_completion(repository, *args):
        original(repository, *args)
        repository._connection.execute("SELECT 1 / 0")

    monkeypatch.setattr(evaluation_module.IdempotencyRepository, "mark_completed", fail_after_completion)
    with pytest.raises(DivisionByZero):
        service(FakeProvider(response_for(broken))).evaluate(EvaluateRunRulesCommand(broken.run_id))
    assert finding_count(broken) == 0
    assert record(broken).status is IdempotencyStatus.IN_PROGRESS  # type: ignore[union-attr]


@pytest.mark.parametrize("retryable", [DeadlockDetected, SerializationFailure])
def test_retryable_persistence_failures_reuse_provider_and_exhaust_cleanly(
    monkeypatch: pytest.MonkeyPatch, retryable: type[Exception]
) -> None:
    resources = create_resources()
    original = evaluation_module.IdempotencyRepository.mark_completed
    attempts = 0

    def fail_once(repository, *args):
        nonlocal attempts
        attempts += 1
        result = original(repository, *args)
        if attempts == 1:
            raise retryable("retry")
        return result

    monkeypatch.setattr(evaluation_module.IdempotencyRepository, "mark_completed", fail_once)
    provider = FakeProvider(response_for(resources))
    service(provider).evaluate(EvaluateRunRulesCommand(resources.run_id))
    assert attempts == 2 and len(provider.calls) == 1 and finding_count(resources) == 1

    exhausted = create_resources()
    attempts = 0

    def fail_every(repository, *args):
        nonlocal attempts
        attempts += 1
        original(repository, *args)
        raise retryable("retry")

    monkeypatch.setattr(evaluation_module.IdempotencyRepository, "mark_completed", fail_every)
    provider = FakeProvider(response_for(exhausted))
    with pytest.raises(retryable):
        service(provider).evaluate(EvaluateRunRulesCommand(exhausted.run_id))
    assert attempts == 3 and len(provider.calls) == 1 and finding_count(exhausted) == 0
    assert record(exhausted).status is IdempotencyStatus.IN_PROGRESS  # type: ignore[union-attr]


def test_persistence_holds_run_lock_until_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    resources = create_resources()
    staged, release = threading.Event(), threading.Event()
    original = evaluation_module.FindingRepository.create

    def pause_after_staging(repository, finding):
        stored = original(repository, finding)
        staged.set()
        assert release.wait(timeout=5)
        return stored

    monkeypatch.setattr(evaluation_module.FindingRepository, "create", pause_after_staging)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(service(FakeProvider(response_for(resources))).evaluate, EvaluateRunRulesCommand(resources.run_id))
        assert staged.wait(timeout=5)
        with pool.connection() as connection:
            connection.execute("SET LOCAL lock_timeout = '100ms'")
            with pytest.raises(LockNotAvailable):
                RunRepository(connection).get_for_update(resources.run_id)
            connection.rollback()
        release.set()
        future.result(timeout=5)

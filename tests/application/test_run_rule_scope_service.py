import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from uuid import uuid4

import pytest
from psycopg.errors import DeadlockDetected, DivisionByZero, LockNotAvailable, SerializationFailure
from psycopg.types.json import Jsonb

from api.application import run_rule_scope_service as scope_module
from api.application.run_rule_scope_service import (
    CaptureRunRuleScopeCommand,
    InvalidRunRuleScopeSelectionError,
    RuleNotFoundError,
    RuleSelectionMismatchError,
    RuleSetNotFoundError,
    RuleSetPileMismatchError,
    RunRuleScopeConflictError,
    RunRuleScopeRunNotEligibleError,
    RunRuleScopeRunNotFoundError,
    RunRuleScopeService,
)
from db.pool import pool
from domain.run import RunStatus
from repositories.run_repository import RunRepository
from repositories.run_rule_scope_repository import RunRuleScopeRepository


@dataclass(frozen=True)
class CaptureResources:
    pile_id: str
    other_pile_id: str
    run_id: str
    second_run_id: str
    running_run_id: str
    rule_set_id: str
    second_rule_set_id: str
    other_rule_set_id: str
    rule_id: str
    second_rule_id: str
    other_rule_id: str


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


@pytest.fixture
def resources() -> CaptureResources:
    with pool.connection() as connection:
        pile_id = create_pile(connection, "Run scope service")
        other_pile_id = create_pile(connection, "Other run scope service")
        run_id = create_manual_run(connection, pile_id, "PENDING")
        second_run_id = create_manual_run(connection, pile_id, "PENDING")
        running_run_id = create_manual_run(connection, pile_id, "RUNNING")
        rule_set_id = create_rule_set(connection, pile_id, "Primary", 1)
        second_rule_set_id = create_rule_set(connection, pile_id, "Secondary", 3)
        other_rule_set_id = create_rule_set(connection, other_pile_id, "Other", 1)
        rule_id = create_rule(connection, rule_set_id, "first", {"first": True})
        second_rule_id = create_rule(connection, second_rule_set_id, "second", {"second": [1, 2]})
        other_rule_id = create_rule(connection, other_rule_set_id, "other", {"other": True})
        connection.commit()

    return CaptureResources(
        pile_id,
        other_pile_id,
        run_id,
        second_run_id,
        running_run_id,
        rule_set_id,
        second_rule_set_id,
        other_rule_set_id,
        rule_id,
        second_rule_id,
        other_rule_id,
    )


def create_pile(connection, prefix: str) -> str:
    row = connection.execute(
        "INSERT INTO piles (name) VALUES (%s) RETURNING id",
        (f"{prefix} {uuid4()}",),
    ).fetchone()
    assert row is not None
    return str(row[0])


def create_manual_run(connection, pile_id: str, status: str) -> str:
    row = connection.execute(
        """
        INSERT INTO runs (pile_id, run_type, status)
        VALUES (%s::uuid, 'MANUAL'::run_type, %s::run_status)
        RETURNING id
        """,
        (pile_id, status),
    ).fetchone()
    assert row is not None
    return str(row[0])


def create_rule_set(connection, pile_id: str, name: str, version: int) -> str:
    row = connection.execute(
        """
        INSERT INTO rule_sets (pile_id, name, description, version)
        VALUES (%s::uuid, %s, %s, %s)
        RETURNING id
        """,
        (pile_id, name, f"{name} description", version),
    ).fetchone()
    assert row is not None
    return str(row[0])


def create_rule(connection, rule_set_id: str, rule_key: str, definition: object) -> str:
    row = connection.execute(
        """
        INSERT INTO rules (rule_set_id, rule_key, title, severity, definition)
        VALUES (%s::uuid, %s, %s, 'HIGH', %s::jsonb)
        RETURNING id
        """,
        (rule_set_id, rule_key, f"{rule_key} title", Jsonb(definition)),
    ).fetchone()
    assert row is not None
    return str(row[0])


def command(
    run_id: str,
    rule_set_ids: tuple[str, ...] = (),
    rule_ids: tuple[str, ...] = (),
) -> CaptureRunRuleScopeCommand:
    return CaptureRunRuleScopeCommand(run_id, rule_set_ids, rule_ids)


def test_captures_snapshots_in_canonical_order_and_replays(resources: CaptureResources) -> None:
    service = RunRuleScopeService(pool)
    request = command(
        resources.run_id.upper(),
        (resources.second_rule_set_id, resources.rule_set_id.upper()),
        (resources.second_rule_id, resources.rule_id.upper()),
    )

    captured = service.capture(request)
    replay = service.capture(request)

    assert captured.replayed is False
    assert [record.source_rule_set_id for record in captured.rule_set_snapshots] == sorted(
        [resources.rule_set_id, resources.second_rule_set_id],
    )
    assert [record.source_rule_id for record in captured.rule_snapshots] == sorted(
        [resources.rule_id, resources.second_rule_id],
    )
    assert replay.replayed is True
    assert replay.scope == captured.scope
    assert replay.rule_set_snapshots == captured.rule_set_snapshots
    assert replay.rule_snapshots == captured.rule_snapshots


def test_selected_empty_rule_set_and_fully_empty_scope_are_closed(resources: CaptureResources) -> None:
    selected_empty_run_id = create_pending_run(resources.pile_id)
    empty_run_id = create_pending_run(resources.pile_id)
    service = RunRuleScopeService(pool)

    selected_empty = service.capture(command(selected_empty_run_id, (resources.rule_set_id,)))
    empty = service.capture(command(empty_run_id))

    assert [record.source_rule_set_id for record in selected_empty.rule_set_snapshots] == [
        resources.rule_set_id,
    ]
    assert selected_empty.rule_snapshots == ()
    assert empty.rule_set_snapshots == ()
    assert empty.rule_snapshots == ()


def test_fresh_capture_rejects_invalid_requests_before_writes(resources: CaptureResources) -> None:
    service = RunRuleScopeService(pool)

    with pytest.raises(InvalidRunRuleScopeSelectionError):
        service.capture(command("not-a-uuid", (resources.rule_set_id,)))
    with pytest.raises(InvalidRunRuleScopeSelectionError):
        service.capture(command(resources.second_run_id, (resources.rule_set_id, resources.rule_set_id.upper())))
    with pytest.raises(RunRuleScopeRunNotFoundError):
        service.capture(command(str(uuid4())))
    with pytest.raises(RunRuleScopeRunNotEligibleError):
        service.capture(command(resources.running_run_id))
    with pytest.raises(RuleSetNotFoundError):
        service.capture(command(resources.second_run_id, (str(uuid4()),)))
    with pytest.raises(RuleNotFoundError):
        service.capture(command(resources.second_run_id, (), (str(uuid4()),)))
    with pytest.raises(RuleSetPileMismatchError):
        service.capture(command(resources.second_run_id, (resources.other_rule_set_id,)))
    with pytest.raises(RuleSelectionMismatchError):
        service.capture(command(resources.second_run_id, (), (resources.rule_id,)))

    with pool.connection() as connection:
        assert RunRuleScopeRepository(connection).get_scope(resources.second_run_id) is None


def test_closed_scope_with_different_selection_conflicts_without_writes(resources: CaptureResources) -> None:
    service = RunRuleScopeService(pool)
    service.capture(command(resources.run_id, (resources.rule_set_id,), (resources.rule_id,)))

    with pytest.raises(RunRuleScopeConflictError):
        service.capture(command(resources.run_id, (resources.second_rule_set_id,), (resources.second_rule_id,)))

    with pool.connection() as connection:
        records = RunRuleScopeRepository(connection).list_rules_for_run(resources.run_id)
    assert [record.source_rule_id for record in records] == [resources.rule_id]


def test_exact_replay_survives_run_status_progression(resources: CaptureResources) -> None:
    service = RunRuleScopeService(pool)
    request = command(resources.run_id, (resources.rule_set_id,), (resources.rule_id,))
    service.capture(request)

    with pool.connection() as connection:
        updated = RunRepository(connection).update_status(resources.run_id, RunStatus.RUNNING)
        assert updated is not None
        connection.commit()

    assert service.capture(request).replayed is True


def test_update_run_captures_its_explicit_scope(resources: CaptureResources) -> None:
    update_run_id = create_update_run(resources.pile_id)

    result = RunRuleScopeService(pool).capture(
        command(update_run_id, (resources.second_rule_set_id,), (resources.second_rule_id,)),
    )

    assert result.replayed is False
    assert [record.source_rule_set_id for record in result.rule_set_snapshots] == [
        resources.second_rule_set_id,
    ]
    assert [record.source_rule_id for record in result.rule_snapshots] == [resources.second_rule_id]


def test_capture_rolls_back_all_staged_rows_after_database_failure(
    resources: CaptureResources,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = create_pending_run(resources.pile_id)
    original_complete = scope_module.RunRuleScopeRepository.complete_scope

    def fail_after_completion(repository, completed_run_id: str):
        original_complete(repository, completed_run_id)
        repository._connection.execute("SELECT 1 / 0")
        raise AssertionError("unreachable")

    monkeypatch.setattr(scope_module.RunRuleScopeRepository, "complete_scope", fail_after_completion)

    with pytest.raises(DivisionByZero):
        RunRuleScopeService(pool).capture(command(run_id, (resources.rule_set_id,), (resources.rule_id,)))

    with pool.connection() as connection:
        repository = RunRuleScopeRepository(connection)
        assert repository.get_scope(run_id) is None
        assert repository.list_rule_sets_for_run(run_id) == []
        assert repository.list_rules_for_run(run_id) == []


def test_identical_capture_requests_serialize_to_capture_then_replay(
    resources: CaptureResources,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = create_pending_run(resources.pile_id)
    first_inserted = threading.Event()
    release_first = threading.Event()
    original_complete = scope_module.RunRuleScopeRepository.complete_scope

    def pause_completion(repository, completed_run_id: str):
        result = original_complete(repository, completed_run_id)
        first_inserted.set()
        assert release_first.wait(timeout=5)
        return result

    monkeypatch.setattr(scope_module.RunRuleScopeRepository, "complete_scope", pause_completion)
    service = RunRuleScopeService(pool)
    request = command(run_id, (resources.rule_set_id,), (resources.rule_id,))

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(service.capture, request)
        assert first_inserted.wait(timeout=5)
        second = executor.submit(service.capture, request)
        time.sleep(0.1)
        assert not second.done()
        release_first.set()
        results = [first.result(timeout=5), second.result(timeout=5)]

    assert sorted(result.replayed for result in results) == [False, True]


def test_different_capture_requests_serialize_to_conflict(
    resources: CaptureResources,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = create_pending_run(resources.pile_id)
    first_inserted = threading.Event()
    release_first = threading.Event()
    original_complete = scope_module.RunRuleScopeRepository.complete_scope

    def pause_completion(repository, completed_run_id: str):
        result = original_complete(repository, completed_run_id)
        first_inserted.set()
        assert release_first.wait(timeout=5)
        return result

    monkeypatch.setattr(scope_module.RunRuleScopeRepository, "complete_scope", pause_completion)
    service = RunRuleScopeService(pool)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            service.capture,
            command(run_id, (resources.rule_set_id,), (resources.rule_id,)),
        )
        assert first_inserted.wait(timeout=5)
        second = executor.submit(
            service.capture,
            command(run_id, (resources.second_rule_set_id,), (resources.second_rule_id,)),
        )
        time.sleep(0.1)
        assert not second.done()
        release_first.set()
        first.result(timeout=5)
        with pytest.raises(RunRuleScopeConflictError):
            second.result(timeout=5)


def test_source_and_run_locks_hold_until_capture_commits(
    resources: CaptureResources,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = create_pending_run(resources.pile_id)
    first_inserted = threading.Event()
    release_first = threading.Event()
    original_insert = scope_module.RunRuleScopeRepository.insert_rule_snapshot

    def pause_after_source_read(repository, snapshot):
        result = original_insert(repository, snapshot)
        first_inserted.set()
        assert release_first.wait(timeout=5)
        return result

    monkeypatch.setattr(scope_module.RunRuleScopeRepository, "insert_rule_snapshot", pause_after_source_read)
    service = RunRuleScopeService(pool)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            service.capture,
            command(run_id, (resources.rule_set_id,), (resources.rule_id,)),
        )
        assert first_inserted.wait(timeout=5)
        with pool.connection() as connection:
            connection.execute("SET LOCAL lock_timeout = '100ms'")
            with pytest.raises(LockNotAvailable):
                connection.execute(
                    "UPDATE rules SET title = 'changed' WHERE id = %s::uuid",
                    (resources.rule_id,),
                )
            connection.rollback()
            connection.execute("SET LOCAL lock_timeout = '100ms'")
            with pytest.raises(LockNotAvailable):
                RunRepository(connection).get_for_update(run_id)
            connection.rollback()
        release_first.set()
        future.result(timeout=5)

    with pool.connection() as connection:
        connection.execute(
            "UPDATE rules SET title = 'changed after capture' WHERE id = %s::uuid",
            (resources.rule_id,),
        )
        connection.commit()
        snapshot = RunRuleScopeRepository(connection).list_rules_for_run(run_id)[0]

    assert snapshot.title == "first title"


@pytest.mark.parametrize("retryable_error", [DeadlockDetected, SerializationFailure])
def test_retryable_failures_restart_capture_once(
    resources: CaptureResources,
    monkeypatch: pytest.MonkeyPatch,
    retryable_error: type[Exception],
) -> None:
    run_id = create_pending_run(resources.pile_id)
    service = RunRuleScopeService(pool)
    original_capture_once = service._capture_once
    attempts = 0

    def fail_after_staging(connection, request):
        nonlocal attempts
        attempts += 1
        result = original_capture_once(connection, request)
        if attempts == 1:
            raise retryable_error("retry complete transaction")
        return result

    monkeypatch.setattr(service, "_capture_once", fail_after_staging)
    result = service.capture(command(run_id, (resources.rule_set_id,), (resources.rule_id,)))

    assert attempts == 2
    assert result.replayed is False


def test_application_errors_are_not_retried(resources: CaptureResources, monkeypatch: pytest.MonkeyPatch) -> None:
    service = RunRuleScopeService(pool)
    attempts = 0
    original_capture_once = service._capture_once

    def count_attempts(connection, request):
        nonlocal attempts
        attempts += 1
        return original_capture_once(connection, request)

    monkeypatch.setattr(service, "_capture_once", count_attempts)

    with pytest.raises(RunRuleScopeRunNotEligibleError):
        service.capture(command(resources.running_run_id))

    assert attempts == 1


@pytest.mark.parametrize("retryable_error", [DeadlockDetected, SerializationFailure])
def test_retryable_failures_stop_after_three_total_attempts(
    resources: CaptureResources,
    monkeypatch: pytest.MonkeyPatch,
    retryable_error: type[Exception],
) -> None:
    service = RunRuleScopeService(pool)
    attempts = 0

    def fail_every_attempt(connection, request):
        nonlocal attempts
        attempts += 1
        raise retryable_error("retry complete transaction")

    monkeypatch.setattr(service, "_capture_once", fail_every_attempt)

    with pytest.raises(retryable_error):
        service.capture(command(resources.second_run_id))

    assert attempts == 3


def create_pending_run(pile_id: str) -> str:
    with pool.connection() as connection:
        run_id = create_manual_run(connection, pile_id, "PENDING")
        connection.commit()
    return run_id


def create_update_run(pile_id: str) -> str:
    with pool.connection() as connection:
        document = connection.execute(
            "INSERT INTO documents (pile_id, logical_key) VALUES (%s::uuid, %s) RETURNING id",
            (pile_id, f"trigger-{uuid4()}"),
        ).fetchone()
        assert document is not None
        version = connection.execute(
            """
            INSERT INTO document_versions (
                document_id, version_number, content_hash, storage_uri, processing_status
            )
            VALUES (%s::uuid, 1, %s, %s, 'READY'::document_version_status)
            RETURNING id
            """,
            (str(document[0]), f"sha256:{uuid4().hex}", f"artifact://{uuid4()}"),
        ).fetchone()
        assert version is not None
        run = connection.execute(
            """
            INSERT INTO runs (pile_id, run_type, status, trigger_document_version_id)
            VALUES (%s::uuid, 'UPDATE'::run_type, 'PENDING'::run_status, %s::uuid)
            RETURNING id
            """,
            (pile_id, str(version[0])),
        ).fetchone()
        assert run is not None
        connection.commit()
    return str(run[0])

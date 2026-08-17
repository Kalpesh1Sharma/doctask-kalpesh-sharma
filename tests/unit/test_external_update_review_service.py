from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.application.external_update_review_service import (
    ExternalUpdateReviewCommand,
    ExternalUpdateReviewService,
    ExternalUpdateReviewStaleError,
    InvalidExternalUpdateReviewCommandError,
)
from domain.update_review import UpdateReviewDecisionType

_RUN_ID = "11111111-1111-1111-1111-111111111111"
_FINGERPRINT = "sha256:" + "a" * 64


class _Builder:
    calls = 0

    def build(self, command):
        self.calls += 1
        assert command.run_id == _RUN_ID
        return SimpleNamespace(
            manifest=SimpleNamespace(),
        )


class _Reviewer:
    calls = 0

    def record(self, command):
        self.calls += 1
        return SimpleNamespace(command=command)


def _service_with_fingerprint(monkeypatch, actual: str):
    builder = _Builder()
    reviewer = _Reviewer()
    monkeypatch.setattr(
        "api.application.external_update_review_service.update_change_set_fingerprint",
        lambda _manifest: actual,
    )
    return ExternalUpdateReviewService(builder, reviewer), builder, reviewer


def _command(expected: str) -> ExternalUpdateReviewCommand:
    return ExternalUpdateReviewCommand(
        run_id=_RUN_ID,
        decision=UpdateReviewDecisionType.APPROVED,
        actor_type="HUMAN",
        actor_id="reviewer@example.test",
        idempotency_key=f"update-review:{_RUN_ID}:approve",
        expected_change_set_fingerprint=expected,
    )


def test_exact_preview_fingerprint_allows_durable_update_decision(monkeypatch) -> None:
    service, builder, reviewer = _service_with_fingerprint(monkeypatch, _FINGERPRINT)

    result = service.record(_command(_FINGERPRINT))

    assert builder.calls == 1
    assert reviewer.calls == 1
    assert result.review.command.decision is UpdateReviewDecisionType.APPROVED


def test_changed_preview_fingerprint_rejects_before_decision_write(monkeypatch) -> None:
    service, builder, reviewer = _service_with_fingerprint(
        monkeypatch,
        "sha256:" + "b" * 64,
    )

    with pytest.raises(ExternalUpdateReviewStaleError):
        service.record(_command(_FINGERPRINT))

    assert builder.calls == 1
    assert reviewer.calls == 0


def test_invalid_expected_fingerprint_is_rejected_before_build(monkeypatch) -> None:
    service, builder, reviewer = _service_with_fingerprint(monkeypatch, _FINGERPRINT)

    with pytest.raises(InvalidExternalUpdateReviewCommandError):
        service.record(_command("not-a-fingerprint"))

    assert builder.calls == 0
    assert reviewer.calls == 0

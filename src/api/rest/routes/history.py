"""REST v1 routes for durable committed UPDATE change history."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from api.application.change_history_service import ChangeHistoryService
from api.change_history_presenter import change_history_response
from api.rest.dependencies import get_change_history_service
from api.rest.schemas.history import ChangeHistoryRunResponse

router = APIRouter()

ChangeHistoryDependency = Annotated[ChangeHistoryService, Depends(get_change_history_service)]
Limit = Annotated[int, Query(ge=1, le=200)]


@router.get(
    "/piles/{pile_id}/change-history",
    response_model=list[ChangeHistoryRunResponse],
)
def list_pile_change_history(
    pile_id: str,
    service: ChangeHistoryDependency,
    limit: Limit = 100,
) -> list[ChangeHistoryRunResponse]:
    """List newest committed UPDATE histories for one Pile."""

    return [
        change_history_response(item)
        for item in service.list_for_pile(pile_id, limit=limit)
    ]


@router.get(
    "/runs/{run_id}/change-history",
    response_model=ChangeHistoryRunResponse,
)
def get_run_change_history(
    run_id: str,
    service: ChangeHistoryDependency,
) -> ChangeHistoryRunResponse:
    """Explain one completed UPDATE Run from immutable committed state."""

    return change_history_response(service.get_for_run(run_id))

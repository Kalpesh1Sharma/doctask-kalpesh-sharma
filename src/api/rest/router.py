"""Versioned REST router root."""

from fastapi import APIRouter

from api.rest.routes.core import router as core_router
from api.rest.routes.review import router as review_router

API_V1_PREFIX = "/api/v1"

api_v1_router = APIRouter(prefix=API_V1_PREFIX)
api_v1_router.include_router(core_router)
api_v1_router.include_router(review_router)

from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.rest.errors import install_error_handlers
from api.rest.router import api_v1_router
from db.health import check_database
from db.pool import pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.open()

    try:
        yield
    finally:
        pool.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Task 1",
        lifespan=lifespan,
    )
    install_error_handlers(app)
    app.include_router(api_v1_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        if not check_database():
            return {"status": "unhealthy"}

        return {"status": "ok"}

    return app


app = create_app()
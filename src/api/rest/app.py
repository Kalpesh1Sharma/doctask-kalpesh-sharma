from contextlib import asynccontextmanager

from fastapi import FastAPI

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

    @app.get("/health")
    def health() -> dict[str, str]:
        if not check_database():
            return {"status": "unhealthy"}

        return {"status": "ok"}

    return app


app = create_app()
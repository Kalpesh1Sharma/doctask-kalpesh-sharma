from fastapi import FastAPI

from db.health import check_database


def create_app() -> FastAPI:
    app = FastAPI(title="Task 1")

    @app.get("/health")
    def health() -> dict[str, str]:
        if not check_database():
            return {"status": "unhealthy"}

        return {"status": "ok"}

    return app


app = create_app()
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_compose_starts_full_operational_runtime_with_migration_gate() -> None:
    compose = _read("docker-compose.yml")

    for service in ("postgres:", "migrate:", "api:", "worker:", "watcher:", "frontend:"):
        assert service in compose
    assert "condition: service_completed_successfully" in compose
    assert 'command: ["task1-worker"]' in compose
    assert 'command: ["task1-watch"]' in compose
    assert '${TASK1_UI_PORT:-5173}:80' in compose
    assert '${TASK1_API_PORT:-8000}:8000' in compose
    assert "GROQ_MODEL_PRICING: ${GROQ_MODEL_PRICING:-{}}" in compose


def test_compose_shares_source_and_artifact_storage_and_mounts_host_watch_folder() -> None:
    compose = _read("docker-compose.yml")

    assert "source_data:/data/sources" in compose
    assert "artifact_data:/data/artifacts" in compose
    assert "./var/watch:/data/watch" in compose
    assert "SOURCE_ROOT: /data/sources" in compose
    assert "ARTIFACT_ROOT: /data/artifacts" in compose


def test_migration_runner_covers_every_executable_migration_without_public_metadata_table() -> None:
    script = _read("docker/migrate.sh")
    migrations = sorted(path.name for path in (ROOT / "sql" / "migrations").glob("*.sql"))

    assert migrations
    for migration in migrations:
        assert migration in script
    assert "task1_meta.schema_migrations" in script
    assert "CREATE SCHEMA IF NOT EXISTS task1_meta" in script
    assert "sha256sum" in script
    assert "Migration checksum mismatch" in script


def test_frontend_container_proxies_rest_and_serves_spa() -> None:
    nginx = _read("frontend/nginx.conf")
    dockerfile = _read("frontend/Dockerfile")

    assert "proxy_pass http://api:8000" in nginx
    assert "try_files $uri $uri/ /index.html;" in nginx
    assert "npm ci" in dockerfile
    assert "npm run build" in dockerfile
    assert "nginx:1.27-alpine" in dockerfile


def test_readme_documents_single_command_operational_startup() -> None:
    readme = _read("README.md")

    assert "docker compose up --build" in readme
    assert "http://127.0.0.1:5173" in readme
    assert "/data/watch" in readme
    assert "phase6c4_operational_verify.ps1" in readme


def test_runtime_data_and_secrets_are_excluded_from_build_and_git() -> None:
    dockerignore = _read(".dockerignore")
    gitignore = _read(".gitignore")

    assert ".env" in dockerignore
    assert ".venv" in dockerignore
    assert "frontend/node_modules/" in dockerignore
    assert "var/" in dockerignore
    assert "var/sources/*" in gitignore
    assert "var/artifacts/*" in gitignore
    assert "var/watch/*" in gitignore
    assert "frontend/*.tsbuildinfo" in gitignore
    assert "frontend/vite.config.js" in gitignore
    assert "frontend/vite.config.d.ts" in gitignore
    assert "*.sh text eol=lf" in _read(".gitattributes")

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

RUN mkdir -p /data/sources /data/artifacts /data/watch

EXPOSE 8000

CMD ["uvicorn", "api.rest.app:app", "--host", "0.0.0.0", "--port", "8000"]

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY migrator ./migrator
COPY rules ./rules

RUN pip install --no-cache-dir .

ENTRYPOINT ["agentic-migrator"]
CMD ["--help"]

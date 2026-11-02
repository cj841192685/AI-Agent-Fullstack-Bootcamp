FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    GATEWAY_CONFIG=/app/gateway.yaml

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install .

COPY gateway.example.yaml ./gateway.yaml
RUN mkdir -p /app/data && chown -R 10001:10001 /app

USER 10001
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]

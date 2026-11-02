.PHONY: install run test lint docker

install:
	python3 -m pip install -e '.[dev]'

run:
	GATEWAY_CONFIG=gateway.yaml uvicorn app.main:app --reload --port 8000

test:
	pytest -q

lint:
	ruff check .

docker:
	docker compose up --build

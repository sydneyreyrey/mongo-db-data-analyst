.PHONY: api smoke test lint build-data report baseline rounds verify-data

smoke:  ## Phase 0: ping Atlas and make one dummy LLM call
	uv run python -m scripts.smoke

test:
	uv run pytest -q

lint:
	uv run ruff check .
	uv run ruff format --check .

baseline:
	uv run python -m scripts.baseline

build-data:  ## Rebuild co_retail in Atlas and write bench/co_retail/planted.json
	uv run python -m scripts.build_company

report:
	uv run python -m scripts.report --company $(COMPANY) $(if $(AS_OF),--as-of $(AS_OF))

rounds:
	uv run python -m scripts.rounds --company $(COMPANY) --n $(N)

verify-data:
	uv run python -m scripts.verify_company

api:  ## Serve the API at http://localhost:8000 (docs at /docs)
	uv run uvicorn api.main:app --reload --port 8000

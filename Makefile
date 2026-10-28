PY ?= .venv/bin/python

.PHONY: install test lint eval redteam chat web doctor

install:            ## create venv + install (uses uv if present, else pip)
	@command -v uv >/dev/null && (uv venv --python 3.11 .venv && uv pip install -e '.[dev]') \
	  || (python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pip install -e . --no-deps)

test:               ## offline unit + flow tests (no cloud access needed)
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check src tests web evals

eval:               ## live: accuracy (LLM judge) + red-team suites
	$(PY) evals/run_evals.py

redteam:            ## live: safety gate only (must be 100%)
	$(PY) evals/run_evals.py --suite redteam

chat:               ## make chat AS=alice
	.venv/bin/retail-agent chat --user $(or $(AS),ceo)

web:                ## web app on http://localhost:8501
	.venv/bin/streamlit run web/app.py

doctor:             ## check credentials and connectivity
	.venv/bin/retail-agent doctor

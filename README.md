# Fordefi Bot

Slack bot for monitoring customer queries and creating support tickets.

## Run Tests

Run tests from the project root:

```bash
pytest tests/test_app.py -v
pytest tests/test_llm_integration.py -v
```

Or run all tests:

```bash
pytest tests/ -v
```

Note: The project is configured in `pyproject.toml` to automatically include the project root in the Python path, so you don't need to activate the virtual environment or set PYTHONPATH manually.

## Run Server

```bash
uvicorn app:app --reload --port 8800
```

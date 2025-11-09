# Fordefi Bot

Slack bot for monitoring customer queries and creating support tickets.

## Run Tests

```bash
source .venv/bin/activate
pytest test_app.py -v
pytest test_llm_integration.py -v
```

## Run Server

```bash
uvicorn app:app --reload --port 8800
```

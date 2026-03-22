# Tools

This directory contains Python scripts for deterministic execution.

## Conventions

- Each script should do one thing well
- Accept inputs via command-line arguments or environment variables
- Print clear success/error messages
- Read credentials from `.env` (never hardcode secrets)
- Exit with code 0 on success, non-zero on failure

## Dependencies

Install dependencies with:
```
pip install -r requirements.txt
```

## Adding a New Tool

1. Create `tools/your_tool.py`
2. Reference it in the relevant workflow under `workflows/`
3. Add any new packages to `requirements.txt`

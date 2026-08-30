"""Environment loading.

Import this before reading any os.getenv value. It loads `.env` from the repo root
regardless of the working directory, so `uv run python -m deliver.telegram` behaves the
same whether you run it from the project root or anywhere else.

Real environment variables always win over `.env`, which is what makes GitHub Actions
work unchanged: the workflow injects secrets directly and there is no `.env` file there.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent

# override=False: a variable already set in the real environment is not replaced.
load_dotenv(REPO_ROOT / ".env", override=False)


def get(name: str, default: str | None = None) -> str | None:
    return os.getenv(name, default)


def require(name: str) -> str:
    """Fetch a variable that the caller cannot proceed without."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and fill it in, "
            f"or export {name} in your shell."
        )
    return value

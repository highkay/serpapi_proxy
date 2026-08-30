"""Entry point: python -m serpapi_proxy."""

from __future__ import annotations

import os
import sys

import uvicorn

from .app import create_app
from .store import KeyStore


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def main() -> None:
    master_key = os.environ.get("MASTER_KEY", "")
    if not master_key:
        sys.exit("MASTER_KEY not set")
    store = KeyStore(os.environ.get("POOL_DB_PATH", "./pool.db"))
    app = create_app(
        master_key=master_key,
        store=store,
        upstream_base=os.environ.get("UPSTREAM_BASE", "https://serpapi.com"),
        refresh_interval=_env_int("REFRESH_INTERVAL_SECONDS", 600),
    )
    uvicorn.run(app, host="0.0.0.0", port=_env_int("PORT", 8001))


if __name__ == "__main__":
    main()
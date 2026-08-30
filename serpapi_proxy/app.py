"""SerpApi key pool HTTP app: Bearer-authed admin API + transparent proxy.

Contract alignment with the harvester's ``web/serpapi_push.py`` _push_key:
- POST /api/keys body ``{"key", "alias"}``; 200 = added, 400 with error
  ``create_failed`` / ``invalid_key_format`` = ignored, 401 = fail-fast,
  429/5xx = client retries.
Everything except ``/healthz`` requires ``Authorization: Bearer MASTER_KEY``.
Raw keys never appear in responses (masked) — never log response bodies.
"""

import hmac
import html
import logging
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

import requests
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .store import KeyStore
from .upstream import KEY_RE, check_account, forward

logger = logging.getLogger("serpapi_proxy")

_MAX_ATTEMPTS = 3

_MASK_PREFIX = 6
_MASK_SUFFIX = 4


def _mask(key: str) -> str:
    return f"{key[:_MASK_PREFIX]}\u2026{key[-_MASK_SUFFIX:]}"


def _row_view(row: dict) -> dict:
    """Serialize a store row for responses — raw key replaced by mask."""
    return {
        "id": row["id"],
        "key_masked": _mask(row["key"]),
        "alias": row["alias"],
        "status": row["status"],
        "plan_name": row["plan_name"],
        "searches_left": row["searches_left"],
        "renewal_date": row["renewal_date"],
        "added_at": row["added_at"],
        "last_used_at": row["last_used_at"],
    }


def _refresher_loop(
    store: KeyStore,
    upstream_base: str,
    timeout: int,
    refresh_interval: int,
) -> None:
    """Periodically re-check every non-invalid key; never dies."""
    while True:
        try:
            for key_id in store.ids_checkable():
                row = store.get(key_id)
                if row is None:
                    continue
                verdict = check_account(row["key"], upstream_base, timeout)
                if verdict.status != "unknown":
                    store.update_account(
                        key_id,
                        status=verdict.status,
                        plan_name=verdict.plan_name,
                        plan_id=verdict.plan_id,
                        searches_left=verdict.searches_left,
                        renewal_date=verdict.renewal_date,
                        now=time.time(),
                    )
        except Exception:
            logger.exception("refresher cycle failed")
        time.sleep(refresh_interval)


def create_app(
    master_key: str,
    store: KeyStore,
    upstream_base: str = "https://serpapi.com",
    timeout: int = 25,
    refresh_interval: int = 600,
    start_refresher: bool = True,
) -> FastAPI:
    lifespan = None
    if start_refresher:

        @asynccontextmanager
        async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
            thread = threading.Thread(
                target=_refresher_loop,
                kwargs={
                    "store": store,
                    "upstream_base": upstream_base,
                    "timeout": timeout,
                    "refresh_interval": refresh_interval,
                },
                daemon=True,
                name="serpapi-refresher",
            )
            thread.start()
            yield

        lifespan = _lifespan

    # docs/redoc/openapi disabled: everything else than /healthz is authed.
    app = FastAPI(
        title="SerpApi key pool",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def _require_bearer(request: Request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        header = request.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        if not token or not hmac.compare_digest(token, master_key):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    # --- No-auth health -------------------------------------------------
    @app.get("/healthz")
    def healthz() -> dict:
        counts = store.counts()
        return {"status": "ok", "keys": counts["keys"], "active": counts["active"]}

    # --- Admin API ------------------------------------------------------
    @app.post("/api/keys")
    async def add_key(request: Request):
        try:
            payload = await request.json()
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            return JSONResponse({"error": "invalid_key_format"}, status_code=400)
        key = payload.get("key")
        if not isinstance(key, str) or KEY_RE.fullmatch(key) is None:
            return JSONResponse({"error": "invalid_key_format"}, status_code=400)
        alias = payload.get("alias")
        if not isinstance(alias, str):
            alias = ""
        if store.find_by_key(key) is not None:
            return JSONResponse({"error": "create_failed"}, status_code=400)
        try:
            key_id = store.add(key, alias)
        except sqlite3.IntegrityError:
            # find-then-add race: another POST inserted the same key first
            return JSONResponse({"error": "create_failed"}, status_code=400)
        verdict = await run_in_threadpool(
            check_account, key.lower(), upstream_base, timeout
        )
        if verdict.status != "unknown":
            store.update_account(
                key_id,
                status=verdict.status,
                plan_name=verdict.plan_name,
                plan_id=verdict.plan_id,
                searches_left=verdict.searches_left,
                renewal_date=verdict.renewal_date,
                now=time.time(),
            )
        row = store.get(key_id) or {}
        return JSONResponse({"id": key_id, "status": row.get("status", "unverified")})

    @app.get("/api/keys")
    def list_keys() -> JSONResponse:
        return JSONResponse([_row_view(row) for row in store.list()])

    @app.delete("/api/keys/{key_id:int}")
    def delete_key(key_id: int) -> JSONResponse:
        if store.delete(key_id):
            return JSONResponse({"deleted": key_id})
        return JSONResponse({"error": "not_found"}, status_code=404)

    @app.post("/api/keys/{key_id:int}/refresh")
    async def refresh_key(key_id: int):
        row = store.get(key_id)
        if row is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        verdict = await run_in_threadpool(
            check_account, row["key"], upstream_base, timeout
        )
        if verdict.status != "unknown":
            store.update_account(
                key_id,
                status=verdict.status,
                plan_name=verdict.plan_name,
                plan_id=verdict.plan_id,
                searches_left=verdict.searches_left,
                renewal_date=verdict.renewal_date,
                now=time.time(),
            )
        return JSONResponse(_row_view(store.get(key_id) or {}))

    # --- Status page ----------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        parts: list[str] = [
            "<html><head><meta charset='utf-8'><title>SerpApi key pool</title>",
            "<style>td,th{font-family:monospace;padding:2px 8px;text-align:left}",
            "</style></head><body>",
            "<h1>SerpApi key pool</h1>",
            "<table border='1' cellspacing='0'><tr><th>key</th><th>alias</th>",
            "<th>status</th><th>plan</th><th>searches left</th><th>renewal</th></tr>",
        ]
        for row in store.list():
            left = "" if row["searches_left"] is None else str(row["searches_left"])
            cells = [
                _mask(row["key"]),
                row["alias"],
                row["status"],
                row["plan_name"] or "",
                left,
                row["renewal_date"] or "",
            ]
            parts.append(
                "<tr>"
                + "".join(f"<td>{html.escape(str(c))}</td>" for c in cells)
                + "</tr>"
            )
        parts.append(f"</table><p>{len(store.list())} keys</p></body></html>")
        return "".join(parts)

    # --- Transparent rotating proxy (catch-all, GET only) ---------------
    @app.get("/{path:path}")
    def proxy(path: str, request: Request) -> Response:
        params = [
            (k, v)
            for k, v in request.query_params.multi_items()
            if k != "api_key"
        ]
        for _ in range(_MAX_ATTEMPTS):
            now = time.time()
            row = store.pick(now)
            if row is None:
                break
            try:
                resp = forward(row["key"], upstream_base, path, params, timeout)
            except requests.RequestException:
                store.set_cooldown(row["id"], now + 10)
                continue
            if resp.status_code == 200:
                store.mark_used(row["id"], now)
                return Response(
                    content=resp.content,
                    status_code=200,
                    media_type=resp.headers.get("content-type"),
                )
            if resp.status_code == 401:
                store.set_status(row["id"], "invalid")
                continue
            if resp.status_code == 429:
                store.set_cooldown(row["id"], now + 60)
                continue
            # Real upstream error (e.g. bad params) — passthrough, no rotation.
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type"),
            )
        return JSONResponse({"error": "no_available_keys"}, status_code=503)

    return app
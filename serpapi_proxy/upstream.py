"""SerpApi upstream client: account validation + passthrough forwarding.

Self-contained: ``requests`` only, no harvester imports. The account.json
payload echoes the submitted ``api_key`` — never log or store response
bodies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import requests

# Mirrors web/serpapi_push.py::_KEY_RE — real keys are 64 hex chars, the
# harvester accepts 20–64.
KEY_RE = re.compile(r"[0-9a-fA-F]{20,64}")


@dataclass
class AccountVerdict:
    status: str  # active | exhausted | invalid | unknown
    plan_name: str | None
    plan_id: str | None
    searches_left: int | None
    renewal_date: str | None


def check_account(key: str, base_url: str, timeout: int) -> AccountVerdict:
    """Validate a key against serpapi.com/account.json (quota-exempt).

    401 → invalid. 200 + JSON dict → exhausted when total_searches_left <= 0
    else active. Anything else → unknown (caller keeps prior state).
    """
    try:
        resp = requests.get(
            f"{base_url}/account.json", params={"api_key": key}, timeout=timeout
        )
    except requests.RequestException:
        return AccountVerdict("unknown", None, None, None, None)

    if resp.status_code == 401:
        return AccountVerdict("invalid", None, None, None, None)

    if resp.status_code != 200:
        return AccountVerdict("unknown", None, None, None, None)

    try:
        data = resp.json()
    except Exception:
        return AccountVerdict("unknown", None, None, None, None)
    if not isinstance(data, dict):
        return AccountVerdict("unknown", None, None, None, None)

    try:
        left = int(data.get("total_searches_left", 0))
    except (TypeError, ValueError):
        left = 0
    status = "exhausted" if left <= 0 else "active"
    return AccountVerdict(
        status,
        data.get("plan_name"),
        data.get("plan_id"),
        left,
        data.get("plan_renewal_date"),
    )


def forward(
    key: str,
    base_url: str,
    path: str,
    params: list[tuple[str, str]],
    timeout: int,
) -> requests.Response:
    """Proxy one SerpApi request, injecting the chosen key as api_key."""
    # Starlette's {path:path} converter strips the leading slash — re-add it.
    url = f"{base_url.rstrip('/')}/{path}"
    return requests.get(url, params=params + [("api_key", key)], timeout=timeout)
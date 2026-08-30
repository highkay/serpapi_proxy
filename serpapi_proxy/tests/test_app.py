"""Tests for the SerpApi pool HTTP app."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock


import requests
from fastapi.testclient import TestClient

from serpapi_proxy.app import create_app
from serpapi_proxy.store import KeyStore

MASTER_KEY = "test-master-key"
AUTH = {"Authorization": f"Bearer {MASTER_KEY}"}

K64 = "ab" * 32  # 64 hex chars, the real-world key shape
K5 = "cd" * 32


def response(status: int, body: dict | None = None) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp._content = json.dumps(body).encode() if body is not None else b"{}"
    resp.headers["content-type"] = "application/json"
    return resp


def account_body(searches_left: int) -> dict:
    return {
        "total_searches_left": searches_left,
        "plan_name": "Free Plan",
        "plan_id": "free",
        "plan_renewal_date": "2026-09-02",
    }



def make_client() -> tuple[TestClient, KeyStore]:
    store = KeyStore(os.path.join(tempfile.mkdtemp(), "pool.db"))
    app = create_app(
        master_key=MASTER_KEY,
        store=store,
        upstream_base="https://serpapi.test",
        start_refresher=False,
    )
    return TestClient(app, raise_server_exceptions=False), store


class PoolAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client, self.store = make_client()

    # --- auth + healthz --------------------------------------------------
    def test_healthz_no_auth_required(self) -> None:
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertIsInstance(data["keys"], int)
        self.assertIsInstance(data["active"], int)
        self.assertEqual(data["keys"], 0)

    def test_protected_routes_require_bearer(self) -> None:
        cases = [
            ("POST", "/api/keys", {"key": K64}),
            ("GET", "/api/keys", None),
            ("DELETE", "/api/keys/1", None),
            ("GET", "/something", None),
        ]
        for method, path, body in cases:
            no_auth = self.client.request(method, path, json=body)
            self.assertEqual(no_auth.status_code, 401, path)
            self.assertEqual(no_auth.json(), {"error": "unauthorized"}, path)
        wrong = self.client.get(
            "/api/keys", headers={"Authorization": "Bearer nope"}
        )
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(wrong.json(), {"error": "unauthorized"})

    # --- add validation --------------------------------------------------
    def test_add_rejects_bad_key_format(self) -> None:
        for bad in ("xyz", "ab" * 5, "ab" * 50, "gg" * 32, "KEY"):
            resp = self.client.post(
                "/api/keys", json={"key": bad, "alias": "harvester"}, headers=AUTH
            )
            self.assertEqual(resp.status_code, 400, bad)
            self.assertEqual(resp.json(), {"error": "invalid_key_format"}, bad)
        # non-str key
        resp = self.client.post("/api/keys", json={"key": 123}, headers=AUTH)
        self.assertEqual(resp.status_code, 400)

    def test_add_rejects_duplicate_key_any_case(self) -> None:
        with mock.patch(
            "serpapi_proxy.upstream.requests.get",
            return_value=response(200, account_body(100)),
        ) as mocked:
            first = self.client.post(
                "/api/keys", json={"key": K64, "alias": "harvester"}, headers=AUTH
            )
            self.assertEqual(first.status_code, 200)
            self.assertIsInstance(first.json()["id"], int)
            dup = self.client.post(
                "/api/keys", json={"key": K64.upper()}, headers=AUTH
            )
            self.assertEqual(dup.status_code, 400)
            self.assertEqual(dup.json(), {"error": "create_failed"})
            self.assertEqual(mocked.call_count, 1)  # no re-check on dup

    def test_add_race_window_maps_to_create_failed(self) -> None:
        # find_by_key passes but add hits the UNIQUE constraint first
        with mock.patch.object(
            self.store,
            "add",
            side_effect=sqlite3.IntegrityError("UNIQUE constraint failed: keys.key"),
        ):
            resp = self.client.post(
                "/api/keys", json={"key": K64, "alias": "harvester"}, headers=AUTH
            )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json(), {"error": "create_failed"})
    # --- add + account verdicts ------------------------------------------
    def test_add_with_quota_marks_active_and_masks_key(self) -> None:
        with mock.patch(
            "serpapi_proxy.upstream.requests.get",
            return_value=response(200, account_body(100)),
        ):
            resp = self.client.post(
                "/api/keys", json={"key": K64, "alias": "harvester"}, headers=AUTH
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "active")
        listing = self.client.get("/api/keys", headers=AUTH)
        self.assertEqual(listing.status_code, 200)
        rows = listing.json()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        expected_mask = K64[:6] + "\u2026" + K64[-4:]
        self.assertEqual(row["key_masked"], expected_mask)
        self.assertEqual(row["alias"], "harvester")
        self.assertEqual(row["searches_left"], 100)
        self.assertNotIn(K64, listing.text)
        self.assertNotIn(K64.upper(), listing.text)

    def test_add_with_zero_quota_marks_exhausted(self) -> None:
        with mock.patch(
            "serpapi_proxy.upstream.requests.get",
            return_value=response(200, account_body(0)),
        ):
            resp = self.client.post(
                "/api/keys", json={"key": K64}, headers=AUTH
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "exhausted")

    def test_add_with_401_marks_invalid_but_stored(self) -> None:
        with mock.patch(
            "serpapi_proxy.upstream.requests.get",
            return_value=response(401, {"error": "Invalid API key"}),
        ):
            resp = self.client.post(
                "/api/keys", json={"key": K64}, headers=AUTH
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "invalid")
        rows = self.client.get("/api/keys", headers=AUTH).json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "invalid")

    # --- rotation ---------------------------------------------------------
    def _row(self, key_id: int) -> dict:
        row = self.store.get(key_id)
        assert row is not None
        return row

    def _seed_active(self, pairs: list[tuple[str, int]]) -> dict[str, int]:
        # keys are unique lowercased; supplied left values are distinctive
        for key, left in pairs:
            key_id = self.store.add(key, "harvester")
            self.store.update_account(
                key_id,
                status="active",
                plan_name=None,
                plan_id=None,
                searches_left=left,
                renewal_date=None,
                now=time.time(),
            )
        ids: dict[str, int] = {}
        for key, _ in pairs:
            ids[key.lower()] = self._row_by_key(key)["id"]
        return ids

    def _row_by_key(self, key: str) -> dict:
        row = self.store.find_by_key(key)
        assert row is not None
        return row

    def _upstream_params(self, mocked: mock.Mock) -> list[tuple[str, str]]:
        return mocked.call_args.kwargs["params"]
    def test_rotation_prefers_most_quota(self) -> None:
        ids = self._seed_active([(K5, 5), (K64, 100)])
        with mock.patch(
            "serpapi_proxy.upstream.requests.get",
            return_value=response(200, {"status": "ok"}),
        ) as mocked:
            resp = self.client.get(
                "/serp/search.json?q=x", headers=AUTH
            )
        self.assertEqual(resp.status_code, 200)
        params = self._upstream_params(mocked)
        self.assertIn(("api_key", K64), params)
        self.assertNotIn(("api_key", K5), params)
        self.assertIsInstance(self._row(ids[K64])["last_used_at"], float)
        self.assertIsNone(self._row(ids[K5])["last_used_at"])

    def test_rotation_429_then_success(self) -> None:
        ids = self._seed_active([(K5, 5), (K64, 100)])
        get = mock.patch(
            "serpapi_proxy.upstream.requests.get",
            side_effect=[response(429), response(200, {"status": "ok"})],
        )
        before = time.time()
        with get:
            resp = self.client.get("/serp/search.json?q=x", headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        cooldown = self._row(ids[K64])["cooldown_until"]
        self.assertGreater(cooldown, before)  # 429 key cooled down
        self.assertIsNotNone(self._row(ids[K5])["last_used_at"])

    def test_rotation_401_then_success(self) -> None:
        ids = self._seed_active([(K5, 5), (K64, 100)])
        with mock.patch(
            "serpapi_proxy.upstream.requests.get",
            side_effect=[response(401), response(200, {"status": "ok"})],
        ):
            resp = self.client.get("/serp/search.json?q=x", headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._row(ids[K64])["status"], "invalid")
        self.assertEqual(self._row(ids[K5])["status"], "active")

    def test_rotation_passthrough_upstream_error(self) -> None:
        self._seed_active([(K64, 100)])
        with mock.patch(
            "serpapi_proxy.upstream.requests.get",
            return_value=response(422, {"error": "Invalid value for engine"}),
        ) as mocked:
            resp = self.client.get(
                "/serp/search.json?engine=nope", headers=AUTH
            )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(mocked.call_count, 1)  # no rotation on 4xx

    def test_no_usable_keys_returns_503(self) -> None:
        resp = self.client.get("/search.json?q=x", headers=AUTH)
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json(), {"error": "no_available_keys"})

    def test_inbound_api_key_replaced(self) -> None:
        self._seed_active([(K64, 100)])
        with mock.patch(
            "serpapi_proxy.upstream.requests.get",
            return_value=response(200, {"status": "ok"}),
        ) as mocked:
            resp = self.client.get(
                "/search.json?q=x&api_key=deadbeef", headers=AUTH
            )
        self.assertEqual(resp.status_code, 200)
        params = self._upstream_params(mocked)
        self.assertIn(("api_key", K64), params)
        self.assertNotIn(("api_key", "deadbeef"), params)

    # --- delete / refresh -----------------------------------------------
    def test_delete_existing_and_unknown(self) -> None:
        key_id = self.store.add(K64, "harvester")
        resp = self.client.delete(f"/api/keys/{key_id}", headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"deleted": key_id})
        resp = self.client.delete("/api/keys/999", headers=AUTH)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json(), {"error": "not_found"})

    def test_refresh_marks_invalid(self) -> None:
        key_id = self.store.add(K64, "harvester")
        with mock.patch(
            "serpapi_proxy.upstream.requests.get",
            return_value=response(401, {"error": "Invalid API key"}),
        ):
            resp = self.client.post(
                f"/api/keys/{key_id}/refresh", headers=AUTH
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "invalid")
        self.assertEqual(resp.json()["id"], key_id)
        self.assertNotIn(K64, resp.text)
        miss = self.client.post("/api/keys/999/refresh", headers=AUTH)
        self.assertEqual(miss.status_code, 404)


if __name__ == "__main__":
    unittest.main()
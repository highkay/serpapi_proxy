"""Tests for the SerpApi pool SQLite store."""

from __future__ import annotations

import os
import tempfile
import unittest

from serpapi_proxy.store import KeyStore

K_A = "aa" * 32
K_B = "bb" * 32
K_C = "cc" * 32

NOW = 2000.0


def make_store() -> KeyStore:
    return KeyStore(os.path.join(tempfile.mkdtemp(), "pool.db"))


def stored(store: KeyStore, key: str) -> dict:
    row = store.find_by_key(key)
    assert row is not None
    return row


def picked(store: KeyStore, now: float = NOW) -> dict:
    row = store.pick(now)
    assert row is not None
    return row


def picked_id(store: KeyStore, now: float = NOW) -> int | None:
    row = store.pick(now)
    return None if row is None else row["id"]


def seed_active(store: KeyStore, key: str, searches_left: int | None) -> int:
    key_id = store.add(key, "t")
    store.update_account(
        key_id,
        status="active",
        plan_name=None,
        plan_id=None,
        searches_left=searches_left,
        renewal_date=None,
        now=1000.0,
    )
    return key_id


class PickOrderingTests(unittest.TestCase):
    def test_unknown_quota_preferred_over_known(self) -> None:
        store = make_store()
        known = seed_active(store, K_A, 10)
        unknown = seed_active(store, K_B, None)
        self.assertEqual(picked_id(store), unknown)
        self.assertNotEqual(picked_id(store), known)

    def test_higher_searches_left_wins(self) -> None:
        store = make_store()
        low = seed_active(store, K_A, 5)
        high = seed_active(store, K_B, 100)
        self.assertEqual(picked_id(store), high)
        self.assertNotEqual(picked_id(store), low)

    def test_equal_quota_lru_tiebreak(self) -> None:
        store = make_store()
        a = seed_active(store, K_A, 100)
        b = seed_active(store, K_B, 100)
        store.mark_used(a, 1500.0)
        store.mark_used(b, 1200.0)
        self.assertEqual(picked_id(store), b)  # older use wins

    def test_exhausted_excluded(self) -> None:
        store = make_store()
        a = seed_active(store, K_A, 100)
        store.set_status(a, "exhausted")
        self.assertIsNone(store.pick(NOW))

    def test_invalid_and_cooldown_excluded(self) -> None:
        store = make_store()
        a = seed_active(store, K_A, 100)
        store.set_status(a, "invalid")
        self.assertIsNone(store.pick(NOW))
        store.set_status(a, "active")
        store.set_cooldown(a, 3000.0)
        self.assertIsNone(store.pick(2500.0))
        self.assertEqual(picked_id(store, 3000.0), a)  # boundary inclusive

    def test_unverified_participates(self) -> None:
        store = make_store()
        fresh = store.add(K_A, "t")  # default status unverified
        self.assertEqual(picked_id(store), fresh)

    def test_zero_searches_left_not_picked(self) -> None:
        store = make_store()
        a = seed_active(store, K_A, 100)
        store.update_account(
            a,
            status="active",
            plan_name=None,
            plan_id=None,
            searches_left=0,
            renewal_date=None,
            now=NOW,
        )
        self.assertIsNone(store.pick(NOW))


class StoreCrudTests(unittest.TestCase):
    def test_add_stores_lowercase_and_find_any_case(self) -> None:
        store = make_store()
        key_id = store.add(K_A.upper(), "x")
        self.assertEqual(store.get(key_id), stored(store, K_A))
        self.assertEqual(stored(store, K_A)["key"], K_A.lower())
        self.assertEqual(stored(store, K_A.upper())["id"], key_id)

    def test_delete_reports_presence(self) -> None:
        store = make_store()
        key_id = store.add(K_A, "x")
        self.assertTrue(store.delete(key_id))
        self.assertFalse(store.delete(key_id))
        self.assertIsNone(store.get(key_id))

    def test_counts(self) -> None:
        store = make_store()
        self.assertEqual(store.counts(), {"keys": 0, "active": 0})
        a = store.add(K_A, "x")
        store.set_status(a, "active")
        store.add(K_B, "x")
        self.assertEqual(store.counts(), {"keys": 2, "active": 1})

    def test_ids_checkable_excludes_invalid(self) -> None:
        store = make_store()
        a = store.add(K_A, "x")
        b = store.add(K_B, "x")
        store.set_status(b, "invalid")
        self.assertEqual(store.ids_checkable(), [a])


if __name__ == "__main__":
    unittest.main()
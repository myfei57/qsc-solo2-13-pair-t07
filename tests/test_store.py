"""持久化层：原子写、回读校验、流水与整库校验。"""

from __future__ import annotations

import json
import unittest

from flashsmelter.errors import IntegrityError, NotFoundError, ValidationError
from flashsmelter.runtime import ManualClock
from flashsmelter.store import DurableStore

from .helpers import make_root


class DurableStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock()
        self.store = DurableStore(make_root(), clock=self.clock)

    def test_put_increments_version_and_verifies_readback(self) -> None:
        first = self.store.put("a/b/doc", {"value": 1})
        second = self.store.put("a/b/doc", {"value": 2})
        self.assertEqual(1, first.version)
        self.assertEqual(2, second.version)
        self.assertTrue(second.durable)
        self.assertEqual({"value": 2}, dict(self.store.require("a/b/doc").payload))
        self.assertEqual({"a/b/doc": 2}, self.store.versions())

    def test_commit_intent_rejects_content_drift(self) -> None:
        record = self.store.commit_intent("intent/arm", {"action": "arm", "heat": "H1"})
        self.assertEqual("intent/arm", record.key)
        self.assertEqual("H1", record.payload["heat"])

    def test_checksum_mismatch_is_detected(self) -> None:
        self.store.put("plant/state", {"state": "smelting"})
        path = self.store.data_root / "plant" / "state.json"
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["payload"]["state"] = "tampered"
        path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(IntegrityError):
            self.store.get("plant/state")
        report = self.store.verify()
        self.assertFalse(report.ok)
        self.assertTrue(any("plant/state" in problem for problem in report.problems))

    def test_missing_record_raises_not_found(self) -> None:
        with self.assertRaises(NotFoundError):
            self.store.require("missing/key")
        self.assertIsNone(self.store.get("missing/key"))

    def test_invalid_keys_are_rejected(self) -> None:
        for key in ("", "../escape", "a/../b", "a//b", "x" * 80):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                self.store.put(key, {"value": 1})

    def test_journal_is_monotonic_and_verifiable(self) -> None:
        first = self.store.append("audit/events", {"action": "one"})
        self.clock.advance(5)
        second = self.store.append("audit/events", {"action": "two"})
        self.assertEqual(1, first.seq)
        self.assertEqual(2, second.seq)
        entries = self.store.read_stream("audit/events", limit=10)
        self.assertEqual(["one", "two"], [entry.payload["action"] for entry in entries])
        self.assertEqual(2, self.store.stream_length("audit/events"))
        self.assertEqual(["two"], [e.payload["action"] for e in self.store.read_stream("audit/events", since_seq=1)])

    def test_tampered_journal_line_is_reported(self) -> None:
        self.store.append("conv/batches", {"batch": "B1", "tons": 10})
        path = self.store.journal_root / "conv" / "batches.jsonl"
        entry = json.loads(path.read_text(encoding="utf-8").strip())
        entry["payload"]["tons"] = 99
        path.write_text(json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")
        with self.assertRaises(IntegrityError):
            self.store.read_stream("conv/batches")

    def test_orphan_temp_files_are_cleaned_on_start(self) -> None:
        orphan = self.store.data_root / "plant" / "state.json.tmp"
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_text("partial", encoding="utf-8")
        reopened = DurableStore(self.store.root, clock=self.clock)
        self.assertEqual(1, reopened.orphans_removed)
        self.assertFalse(orphan.exists())

    def test_snapshot_and_listing(self) -> None:
        self.store.put("plant/a", {"v": 1})
        self.store.put("plant/b", {"v": 2})
        self.store.put("other/c", {"v": 3})
        self.assertEqual(["plant/a", "plant/b"], self.store.list_keys("plant"))
        snapshot = self.store.snapshot("plant")
        self.assertEqual({"v": 1}, dict(snapshot["plant/a"]))
        report = self.store.verify()
        self.assertTrue(report.ok)
        self.assertEqual(3, report.records_checked)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

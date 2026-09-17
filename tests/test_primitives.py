"""命名空间、参数解析、审计与配置校验。"""

from __future__ import annotations

import unittest

from flashsmelter.audit import AuditLog
from flashsmelter.config import Settings
from flashsmelter.errors import ConfigurationError, ValidationError
from flashsmelter.ns import Namespace
from flashsmelter.params import Params
from flashsmelter.runtime import ManualClock
from flashsmelter.store import DurableStore

from .helpers import make_root


class NamespaceTest(unittest.TestCase):
    def test_parse_and_key(self) -> None:
        namespace = Namespace.parse(" smelter/line1 ")
        self.assertEqual("smelter/line1", namespace.prefix)
        self.assertEqual("smelter/line1/conc/state", namespace.key("conc", "state"))
        self.assertTrue(namespace.matches("smelter/line1/conc/state"))
        self.assertEqual("conc/state", namespace.strip("smelter/line1/conc/state"))
        self.assertEqual("reactor", namespace.zone("conc"))
        self.assertEqual("offgas", namespace.zone("waste"))
        self.assertEqual("smelter", namespace.as_dict()["site"])

    def test_invalid_forms(self) -> None:
        for text in ("", "only-one", "a/b/c", "bad site/line1", "smelter/li ne"):
            with self.subTest(text=text), self.assertRaises(ValidationError):
                Namespace.parse(text)

    def test_key_length_and_segments(self) -> None:
        namespace = Namespace.parse("site/unit")
        with self.assertRaises(ValidationError):
            namespace.key()
        with self.assertRaises(ValidationError):
            namespace.key("x" * 90)
        with self.assertRaises(ValidationError):
            namespace.zone("unknown-component")
        with self.assertRaises(ValidationError):
            namespace.strip("other/unit/key")


class ParamsTest(unittest.TestCase):
    def test_coercion(self) -> None:
        params = Params({"actor": "ops", "rate": "12.5", "count": 3, "flag": "yes"})
        self.assertEqual("ops", params.text("actor"))
        self.assertAlmostEqual(12.5, params.number("rate", minimum=0.0))
        self.assertEqual(3, params.integer("count", minimum=0))
        self.assertTrue(params.boolean("flag"))
        self.assertIsNone(params.optional_number("missing"))
        self.assertEqual({}, params.mapping("nested"))

    def test_rejects_bad_values(self) -> None:
        with self.assertRaises(ValidationError):
            Params({}).text("actor")
        with self.assertRaises(ValidationError):
            Params({"rate": "fast"}).number("rate")
        with self.assertRaises(ValidationError):
            Params({"rate": 5}).number("rate", maximum=1)
        with self.assertRaises(ValidationError):
            Params({"rate": 1.5}).integer("rate")
        with self.assertRaises(ValidationError):
            Params({"flag": "maybe"}).boolean("flag")
        with self.assertRaises(ValidationError):
            Params({"extra": 1}).reject_unknown(["known"])
        with self.assertRaises(ValidationError):
            Params("not-a-mapping")  # type: ignore[arg-type]


class SettingsTest(unittest.TestCase):
    def test_defaults_are_valid(self) -> None:
        settings = Settings(root=make_root())
        settings.validate()
        self.assertAlmostEqual(0.45, settings.oxygen_enrichment_min)
        self.assertAlmostEqual(1200.0, settings.heat_feed_budget_tons)
        self.assertEqual("smelter/line1", settings.namespace)
        self.assertIn(str(settings.root), settings.as_dict()["root"])

    def test_env_override_and_validation(self) -> None:
        env = {"FLASHSMELTER_PORT": "9100", "FLASHSMELTER_OXYGEN_ENRICHMENT_MIN": "0.5"}
        settings = Settings.from_env(env, root=make_root())
        self.assertEqual(9100, settings.port)
        self.assertAlmostEqual(0.5, settings.oxygen_enrichment_min)
        with self.assertRaises(ConfigurationError):
            Settings.from_env({"FLASHSMELTER_PORT": "not-a-number"}, root=make_root())
        with self.assertRaises(ValidationError):
            Settings.from_env({"FLASHSMELTER_PORT": "0"}, root=make_root())
        with self.assertRaises(ValidationError):
            Settings(root=make_root(), oxygen_enrichment_min=0.9, oxygen_enrichment_max=0.5).validate()
        with self.assertRaises(ValidationError):
            Settings(root=make_root(), slag_min_thickness_m=0.9).validate()

    def test_ensure_directories(self) -> None:
        root = make_root() / "nested" / "state"
        settings = Settings(root=root)
        settings.ensure_directories()
        self.assertTrue((root / "data").is_dir())
        self.assertTrue((root / "journal").is_dir())


class AuditLogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock()
        self.namespace = Namespace.parse("smelter/line1")
        self.store = DurableStore(make_root(), clock=self.clock)
        self.audit = AuditLog(self.store, self.namespace, self.clock)

    def test_record_and_query(self) -> None:
        self.audit.record(
            actor="ops",
            action="furnace.start",
            target="furnace",
            outcome="ok",
            correlation_id="c1",
            details={"step": "purge"},
        )
        self.audit.record(
            actor="ops",
            action="conc.inject",
            target="conc/H1",
            outcome="rejected",
            correlation_id="c2",
            details={"reason": "stale-baseline"},
        )
        self.clock.advance(3)
        self.audit.record(
            actor="analyzer",
            action="oxygen.record_reading",
            target="oxygen",
            outcome="ok",
            correlation_id="c3",
        )
        self.assertEqual(3, self.audit.length())
        rejected = self.audit.query(outcome="rejected")
        self.assertEqual(["conc.inject"], [event.action for event in rejected])
        by_actor = self.audit.query(actor="analyzer")
        self.assertEqual(1, len(by_actor))
        stats = self.audit.stats()
        self.assertEqual(3, stats["total"])
        self.assertEqual(1, stats["by_outcome"]["rejected"])
        self.assertEqual(2, stats["by_actor"]["ops"])
        self.assertIsNotNone(stats["last_event_at"])
        with self.assertRaises(ValidationError):
            self.audit.record(
                actor="ops", action="x", target="y", outcome="weird", correlation_id="c4"
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

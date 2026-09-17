"""控制台端到端：路由、错误映射、请求体限制与审计查询。"""

from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.request

from flashsmelter.console import ConsoleApp, ConsoleServer

from .helpers import make_app


class ConsoleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = make_app()
        cls.console = ConsoleApp(cls.app)
        cls.server = ConsoleServer(cls.console, host="127.0.0.1", port=0)
        cls.host, cls.port = cls.server.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    # ------------------------------------------------------------------ 工具
    def _request(self, method: str, path: str, body: dict | None = None, raw: bytes | None = None):
        url = f"http://{self.host}:{self.port}{path}"
        data = raw if raw is not None else (json.dumps(body or {}).encode("utf-8") if body is not None else None)
        request = urllib.request.Request(url, data=data, method=method)
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    # ------------------------------------------------------------------ 用例
    def test_health_and_root_endpoints(self) -> None:
        status, payload = self._request("GET", "/api/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])
        self.assertEqual("smelter/line1", payload["namespace"])
        status, root = self._request("GET", "/")
        self.assertEqual(200, status)
        self.assertTrue(any(route["path"] == "/api/actions" for route in root["endpoints"]))

    def test_start_furnace_via_http(self) -> None:
        status, payload = self._request(
            "POST",
            "/api/furnace/start",
            {
                "actor": "http-test",
                "drum_level": 0.6,
                "fuel_pressure_kpa": 200.0,
                "air_flow_nm3h": 5200.0,
                "oxygen_baseline": 0.62,
                "oxygen_baseline_source": "analyzer-a",
                "oxygen_target": 0.62,
                "oxygen_flow_nm3h": 9000.0,
            },
        )
        self.assertEqual(200, status)
        self.assertEqual("oxygen_ready", payload["result"]["state"])
        status, component = self._request("GET", "/api/components/furnace")
        self.assertEqual(200, status)
        self.assertEqual("furnace", component["name"])
        self.assertEqual("oxygen_ready", component["status"]["state"])

    def test_guard_failure_maps_to_conflict(self) -> None:
        status, payload = self._request("POST", "/api/conc/inject", {"rate_tph": 100.0, "tons": 10.0})
        self.assertEqual(409, status)
        self.assertIn(payload["error"], {"state-transition-rejected", "guard-violation", "latch-engaged"})
        self.assertIn("details", payload)

    def test_unknown_routes_and_methods(self) -> None:
        status, payload = self._request("GET", "/api/nope")
        self.assertEqual(404, status)
        self.assertEqual("not-found", payload["error"])
        status, payload = self._request("GET", "/api/conc/inject")
        self.assertEqual(405, status)
        self.assertEqual("method-not-allowed", payload["error"])
        status, payload = self._request("GET", "/api/components/unknown")
        self.assertEqual(400, status)
        self.assertEqual("validation-error", payload["error"])

    def test_invalid_body_and_size_limit(self) -> None:
        status, payload = self._request("POST", "/api/furnace/start", raw=b"{not-json")
        self.assertEqual(400, status)
        self.assertEqual("validation-error", payload["error"])
        big = json.dumps({"actor": "x" * (self.app.settings.max_body_bytes + 10)}).encode("utf-8")
        status, payload = self._request("POST", "/api/furnace/start", raw=big)
        self.assertEqual(413, status)
        self.assertEqual("payload-too-large", payload["error"])

    def test_missing_required_param_is_reported(self) -> None:
        status, payload = self._request("POST", "/api/waste/start", {"actor": "ops"})
        self.assertEqual(400, status)
        self.assertEqual("validation-error", payload["error"])
        self.assertEqual("drum_level", payload["details"]["param"])

    def test_actions_listing_and_audit_query(self) -> None:
        status, listing = self._request("GET", "/api/actions")
        self.assertEqual(200, status)
        names = {item["action"] for item in listing["actions"]}
        self.assertIn("furnace.start", names)
        self.assertIn("waste.update", names)
        status, audit = self._request("GET", "/api/audit?limit=5&outcome=ok")
        self.assertEqual(200, status)
        self.assertLessEqual(audit["count"], 5)
        self.assertTrue(all(event["outcome"] == "ok" for event in audit["events"]))

    def test_state_and_metrics_views(self) -> None:
        status, state = self._request("GET", "/api/state")
        self.assertEqual(200, status)
        self.assertIn("furnace", state["components"])
        self.assertIn("heat", state)
        self.assertEqual("smelter/line1", state["service"]["namespace"]["prefix"])
        status, metrics = self._request("GET", "/api/metrics")
        self.assertEqual(200, status)
        self.assertIn("counters", metrics)
        self.assertIn("actions_ok", metrics["selected"])
        status, heats = self._request("GET", "/api/heats?limit=3")
        self.assertEqual(200, status)
        self.assertIn("current", heats)

    def test_zone_grouping(self) -> None:
        status, payload = self._request("GET", "/api/zones")
        self.assertEqual(200, status)
        self.assertEqual("smelter/line1", payload["namespace"])
        self.assertIn("conc", payload["zones"]["reactor"])
        self.assertIn("matte", payload["zones"]["settler"])
        self.assertIn("conv", payload["zones"]["converter"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

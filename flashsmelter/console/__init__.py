"""纯 JSON 控制台。

控制台只做三件事：把 HTTP 请求翻成动作注册表里的调用、把结果序列化成 JSON、
把错误码映射成 HTTP 状态。它不持有工艺逻辑，工艺逻辑全在组件里。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlparse

from ..application import Application
from ..errors import FlashSmelterError, NotFoundError, ValidationError

LOGGER = logging.getLogger("flashsmelter.console")

_PLACEHOLDER = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    payload: Mapping[str, Any]

    def to_json(self) -> bytes:
        return json.dumps(self.payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


@dataclass(slots=True)
class Route:
    method: str
    pattern: str
    handler: Callable[[Mapping[str, str], Mapping[str, Any]], Mapping[str, Any]]
    segments: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        self.segments = tuple(part for part in self.pattern.strip("/").split("/") if part)

    def match(self, segments: tuple[str, ...]) -> Mapping[str, str] | None:
        if len(segments) != len(self.segments):
            return None
        captured: dict[str, str] = {}
        for expected, actual in zip(self.segments, segments):
            placeholder = _PLACEHOLDER.match(expected)
            if placeholder:
                captured[placeholder.group(1)] = actual
            elif expected != actual:
                return None
        return captured


class Router:
    def __init__(self) -> None:
        self._routes: list[Route] = []

    def add(
        self,
        method: str,
        pattern: str,
        handler: Callable[[Mapping[str, str], Mapping[str, Any]], Mapping[str, Any]],
    ) -> None:
        self._routes.append(Route(method.upper(), pattern, handler))

    def resolve(
        self, method: str, path: str
    ) -> tuple[Callable[[Mapping[str, str], Mapping[str, Any]], Mapping[str, Any]], Mapping[str, str]]:
        segments = tuple(part for part in path.strip("/").split("/") if part)
        method = method.upper()
        allowed: set[str] = set()
        for route in self._routes:
            captured = route.match(segments)
            if captured is None:
                continue
            if route.method == method:
                return route.handler, captured
            allowed.add(route.method)
        if allowed:
            raise FlashSmelterError(
                "该路径不支持此 HTTP 方法",
                code="method-not-allowed",
                status=405,
                details={"path": path, "method": method, "allowed": sorted(allowed)},
            )
        raise NotFoundError("接口不存在", details={"path": path})

    def endpoints(self) -> list[Mapping[str, Any]]:
        return [{"method": route.method, "path": route.pattern} for route in self._routes]


class ConsoleApp:
    """把应用装配成一组 HTTP 路由。"""

    def __init__(self, application: Application) -> None:
        self.application = application
        self.router = Router()
        self._started = time.monotonic()
        self._register_routes()

    # ------------------------------------------------------------------ 路由
    def _register_routes(self) -> None:
        self.router.add("GET", "/", self._root)
        self.router.add("GET", "/api/health", self._health)
        self.router.add("GET", "/api/state", self._state)
        self.router.add("GET", "/api/metrics", self._metrics)
        self.router.add("GET", "/api/actions", self._actions)
        self.router.add("GET", "/api/audit", self._audit)
        self.router.add("GET", "/api/heats", self._heats)
        self.router.add("GET", "/api/components", self._components)
        self.router.add("GET", "/api/components/{component}", self._component)
        self.router.add("GET", "/api/zones", self._zones)
        for name in self.application.actions:
            component, verb = name.split(".", 1)
            self.router.add("POST", f"/api/{component}/{verb}", self._action_handler(name))

    def _action_handler(
        self, action: str
    ) -> Callable[[Mapping[str, str], Mapping[str, Any]], Mapping[str, Any]]:
        def handler(_path: Mapping[str, str], params: Mapping[str, Any]) -> Mapping[str, Any]:
            result = self.application.invoke(action, params, source=f"http:{action}")
            return {"action": action, "result": dict(result)}

        return handler

    # ------------------------------------------------------------------ 视图
    def _root(self, _path: Mapping[str, str], _params: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "service": "flashsmelter-console",
            "version": self.application.state()["service"]["version"],
            "namespace": self.application.namespace.prefix,
            "endpoints": self.router.endpoints(),
        }

    def _health(self, _path: Mapping[str, str], _params: Mapping[str, Any]) -> Mapping[str, Any]:
        furnace = self.application.furnace.status()
        return {
            "status": "ok",
            "uptime_seconds": round(time.monotonic() - self._started, 3),
            "namespace": self.application.namespace.prefix,
            "furnace_state": furnace["state"],
            "generation": self.application.generation.value,
        }

    def _state(self, _path: Mapping[str, str], _params: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.application.state()

    def _metrics(self, _path: Mapping[str, str], _params: Mapping[str, Any]) -> Mapping[str, Any]:
        metrics = self.application.metrics
        snapshot = dict(metrics.snapshot())
        snapshot["selected"] = {
            "actions_ok": metrics.counter("actions.ok"),
            "actions_rejected": metrics.counter("actions.rejected"),
            "actions_failed": metrics.counter("actions.failed"),
            "furnace_state_code": metrics.gauge("furnace.state_code"),
        }
        return snapshot

    def _actions(self, _path: Mapping[str, str], _params: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"actions": self.application.describe_actions()}

    def _audit(self, _path: Mapping[str, str], params: Mapping[str, Any]) -> Mapping[str, Any]:
        from ..params import Params

        parsed = Params(params, source="http:audit")
        limit = parsed.integer("limit", required=False, default=50, minimum=1, maximum=self.application.settings.audit_page_limit)
        since = parsed.integer("since", required=False, default=0, minimum=0)
        events = self.application.audit_events(
            limit=limit,
            since_seq=since,
            action=parsed.optional_text("action"),
            target=parsed.optional_text("target"),
            outcome=parsed.optional_text("outcome"),
            actor=parsed.optional_text("actor"),
        )
        return {"count": len(events), "events": events}

    def _heats(self, _path: Mapping[str, str], params: Mapping[str, Any]) -> Mapping[str, Any]:
        from ..params import Params

        parsed = Params(params, source="http:heats")
        limit = parsed.integer("limit", required=False, default=10, minimum=1, maximum=200)
        return {
            "current": dict(self.application.furnace.heat_report()),
            "heats": [dict(item) for item in self.application.furnace.heats(limit=limit)],
        }

    def _components(self, _path: Mapping[str, str], _params: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "components": [
                {"name": component.name, "zone": self.application.namespace.zone(component.name)}
                for component in self.application.components
            ]
        }

    def _component(self, path: Mapping[str, str], _params: Mapping[str, Any]) -> Mapping[str, Any]:
        name = path["component"]
        component = self.application.component(name)
        return {"name": name, "snapshot": dict(component.snapshot()), "status": dict(component.status())}

    def _zones(self, _path: Mapping[str, str], _params: Mapping[str, Any]) -> Mapping[str, Any]:
        zones: dict[str, list[str]] = {}
        for component, zone in self.application.namespace.iter_zones():
            zones.setdefault(zone, []).append(component)
        return {
            "namespace": self.application.namespace.prefix,
            "zones": {zone: sorted(names) for zone, names in sorted(zones.items())},
        }

    # ------------------------------------------------------------------ 分发
    def handle(
        self,
        method: str,
        path: str,
        query: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Response:
        started = time.monotonic()
        combined: dict[str, Any] = {}
        if query:
            combined.update(query)
        if body:
            combined.update(body)
        try:
            handler, captured = self.router.resolve(method, path)
            payload = handler(captured, combined)
            response = Response(status=200, payload=dict(payload))
        except FlashSmelterError as exc:
            response = Response(status=exc.status, payload=exc.with_detail("path", path).to_dict())
        except Exception:  # pragma: no cover - 兜底，避免控制台线程崩掉
            LOGGER.exception("控制台处理请求时发生未捕获异常", extra={"path": path, "method": method})
            response = Response(
                status=500,
                payload={
                    "error": "internal-error",
                    "message": "控制台内部错误",
                    "status": 500,
                    "details": {"path": path},
                },
            )
        LOGGER.info(
            "%s %s -> %s (%.1f ms)",
            method.upper(),
            path,
            response.status,
            (time.monotonic() - started) * 1000,
        )
        return response


def _build_handler(console: ConsoleApp) -> type[BaseHTTPRequestHandler]:
    settings = console.application.settings

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "FlashSmelterConsole/1.0"
        timeout = settings.request_timeout_seconds

        def do_GET(self) -> None:  # noqa: N802 - http.server 规定的接口名
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            LOGGER.debug("%s - %s", self.address_string(), format % args)

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
            try:
                body = self._read_body()
            except FlashSmelterError as exc:
                self._write(Response(status=exc.status, payload=exc.to_dict()))
                return
            response = console.handle(method, parsed.path, query=query, body=body)
            self._write(response)

        def _read_body(self) -> Mapping[str, Any]:
            length_header = self.headers.get("Content-Length")
            if length_header is None:
                return {}
            try:
                length = int(length_header)
            except ValueError as exc:
                raise ValidationError("Content-Length 不是整数", details={"value": length_header}) from exc
            if length <= 0:
                return {}
            if length > settings.max_body_bytes:
                raise ValidationError(
                    "请求体超过上限",
                    code="payload-too-large",
                    status=413,
                    details={"length": length, "max": settings.max_body_bytes},
                )
            raw = self.rfile.read(length)
            if not raw.strip():
                return {}
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体不是合法 JSON") from exc
            if not isinstance(decoded, dict):
                raise ValidationError("请求体必须是 JSON 对象", details={"type": type(decoded).__name__})
            return decoded

        def _write(self, response: Response) -> None:
            blob = response.to_json()
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(blob)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(blob)

    return Handler


class ConsoleServer:
    """把控制台挂到线程化的 HTTP 服务上。"""

    def __init__(self, console: ConsoleApp, *, host: str, port: int) -> None:
        self.console = console
        self.host = host
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        if self._httpd is None:
            return self.host, self.port
        host, port = self._httpd.server_address[:2]
        return str(host), int(port)

    def start(self) -> tuple[str, int]:
        handler = _build_handler(self.console)
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="flashsmelter-console", daemon=True)
        self._thread.start()
        return self.address

    def serve_forever(self) -> None:
        self.start()
        assert self._httpd is not None
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:  # pragma: no cover - 人工中断
            LOGGER.info("收到中断信号，准备停止控制台")
        finally:
            self.stop()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


__all__ = ["ConsoleApp", "ConsoleServer", "Response", "Router", "Route"]

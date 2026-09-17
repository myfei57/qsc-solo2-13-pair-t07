"""组件共用的状态机。

状态机的职责只有两件：判断跃迁是否被允许，以及把跃迁历史留下来供控制台与审计
回溯。所有组件都用它，保证「非法跃迁必须报错」这条口径完全一致。
"""

from __future__ import annotations

from collections import deque
from typing import Any, Mapping

from .errors import StateTransitionError
from .runtime import Clock


class StateMachine:
    def __init__(
        self,
        name: str,
        initial: str,
        transitions: Mapping[str, tuple[str, ...]],
        clock: Clock,
        *,
        history_limit: int = 12,
    ) -> None:
        self.name = name
        self._state = initial
        self._transitions = transitions
        self._clock = clock
        self._history: list[dict[str, Any]] = []
        self._history_limit = history_limit

    @property
    def state(self) -> str:
        return self._state

    @property
    def history(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self._history[-self._history_limit :])

    def allowed(self) -> tuple[str, ...]:
        return self._transitions.get(self._state, ())

    def can(self, target: str) -> bool:
        return target in self.allowed()

    def require(self, expected: str, action: str) -> None:
        if self._state != expected:
            raise StateTransitionError(
                f"{action} 需要处于 {expected} 状态",
                details={"component": self.name, "state": self._state, "expected": expected},
            )

    def require_one_of(self, expected: tuple[str, ...], action: str) -> None:
        if self._state not in expected:
            raise StateTransitionError(
                f"{action} 需要处于 {'/'.join(expected)} 之一",
                details={"component": self.name, "state": self._state, "expected": list(expected)},
            )

    def to(self, target: str, actor: str, reason: str) -> str:
        if target not in self._transitions:
            raise StateTransitionError(
                "目标状态未定义",
                details={"component": self.name, "to": target, "known": list(self._transitions)},
            )
        if not self.can(target):
            raise StateTransitionError(
                "状态跃迁被拒绝",
                details={
                    "component": self.name,
                    "from": self._state,
                    "to": target,
                    "allowed": list(self.allowed()),
                },
            )
        previous = self._state
        self._state = target
        self._history.append(
            {
                "from": previous,
                "to": target,
                "actor": actor,
                "reason": reason,
                "at": self._clock.timestamp_iso(),
            }
        )
        return previous

    def restore(self, payload: Mapping[str, Any]) -> bool:
        """从落盘状态恢复；返回是否真的恢复了状态。"""

        state = payload.get("state")
        restored = False
        if isinstance(state, str) and state in self._transitions:
            self._state = state
            restored = True
        history = payload.get("history")
        if isinstance(history, list):
            self._history = [entry for entry in history if isinstance(entry, dict)]
        return restored

    def path_to(self, target: str) -> list[str] | None:
        """最短跃迁路径；返回 ``None`` 表示不可达。"""

        if target == self._state:
            return []
        if target not in self._transitions:
            return None
        queue: deque[tuple[str, list[str]]] = deque([(self._state, [])])
        seen = {self._state}
        while queue:
            state, path = queue.popleft()
            for following in self._transitions.get(state, ()):
                if following == target:
                    return [*path, following]
                if following not in seen:
                    seen.add(following)
                    queue.append((following, [*path, following]))
        return None

    def advance_to(self, target: str, actor: str, reason: str, *, max_steps: int = 4) -> list[str]:
        """按最短路径推进到目标状态，途经的中间态逐条记账。

        分层、放料这类流程天生要跨好几个状态（空池 → 进料 → 分层 → 可放料），
        调用方只关心目标态，中间态由状态机自己补齐。
        """

        if target == self._state:
            return []
        path = self.path_to(target)
        if path is None:
            raise StateTransitionError(
                "目标状态不可达",
                details={"component": self.name, "from": self._state, "to": target},
            )
        if len(path) > max_steps:
            raise StateTransitionError(
                "跃迁路径过长，拒绝自动推进",
                details={"component": self.name, "from": self._state, "to": target, "path": path},
            )
        for step in path:
            self.to(step, actor, reason)
        return path

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "allowed": list(self.allowed()),
            "history": list(self.history),
        }


__all__ = ["StateMachine"]

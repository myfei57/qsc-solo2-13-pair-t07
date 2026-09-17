"""控制平台错误类型。

每个错误都带一个稳定的 ``code`` 与对应的 HTTP 状态码，控制台据此把组件内部
的门控失败翻译成可读的响应；审计日志同样按 ``code`` 记录拒绝原因。
"""

from __future__ import annotations

from typing import Any, Mapping


class FlashSmelterError(Exception):
    """平台错误基类。"""

    code = "internal-error"
    status = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = dict(details or {})
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status

    def with_detail(self, key: str, value: Any) -> "FlashSmelterError":
        """补充一条上下文，用于把失败原因随响应一起返回。"""

        self.details[key] = value
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "message": self.message,
            "status": self.status,
            "details": self.details,
        }


class ValidationError(FlashSmelterError):
    """请求参数或配置取值不合法。"""

    code = "validation-error"
    status = 400


class ConfigurationError(FlashSmelterError):
    """启动配置本身不自洽。"""

    code = "configuration-error"
    status = 500


class NotFoundError(FlashSmelterError):
    """请求的资源（记录、批次、路由）不存在。"""

    code = "not-found"
    status = 404


class StateTransitionError(FlashSmelterError):
    """状态机不接受的跃迁。"""

    code = "state-transition-rejected"
    status = 409


class GuardViolation(FlashSmelterError):
    """门控条件不满足：顺序错位、前置状态缺失、越限。"""

    code = "guard-violation"
    status = 409


class StaleBaselineError(GuardViolation):
    """分析仪基线过期，基于该基线的指令必须作废。"""

    code = "stale-baseline"


class LatchEngagedError(GuardViolation):
    """联锁闩锁未复位，禁止推进相关动作。"""

    code = "latch-engaged"


class ConflictError(FlashSmelterError):
    """带代际的指令与当前状态冲突（命令冲突）。"""

    code = "generation-conflict"
    status = 409


class PersistenceError(FlashSmelterError):
    """落盘或回读校验失败。"""

    code = "persistence-error"
    status = 500


class IntegrityError(PersistenceError):
    """已落盘数据校验和不匹配或被截断。"""

    code = "store-integrity-error"


__all__ = [
    "FlashSmelterError",
    "ValidationError",
    "ConfigurationError",
    "NotFoundError",
    "StateTransitionError",
    "GuardViolation",
    "StaleBaselineError",
    "LatchEngagedError",
    "ConflictError",
    "PersistenceError",
    "IntegrityError",
]

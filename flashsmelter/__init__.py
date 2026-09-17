"""FlashSmelter 铜闪速熔炼炉精矿喷吹与放铜控制平台。

设计文档：FlashSmelter 铜闪速熔炼炉精矿喷吹与放铜控制平台（zxy-111）设计。

平台按「精矿 → 富氧喷吹 → 反应塔 → 沉淀池 → 放铜 → 转炉」的控制链运行，
所有状态在进程内计算并落地到本地文件，不依赖任何外部服务。
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = ["__version__"]

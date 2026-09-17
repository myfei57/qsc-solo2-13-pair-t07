"""测试公共夹具：临时状态目录、可推进时钟与常用工艺脚本。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from flashsmelter.application import Application
from flashsmelter.config import Settings
from flashsmelter.runtime import ManualClock

DEFAULT_START = dict(
    drum_level=0.62,
    fuel_pressure_kpa=200.0,
    air_flow_nm3h=5200.0,
    oxygen_baseline=0.62,
    oxygen_baseline_source="analyzer-a",
    oxygen_target=0.62,
    oxygen_flow_nm3h=9000.0,
)


def make_root(prefix: str = "flashsmelter-test-") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def make_app(**overrides) -> Application:
    clock = overrides.pop("clock", None) or ManualClock()
    settings = Settings(root=overrides.pop("root", None) or make_root(), **overrides)
    return Application(settings, clock=clock)


def start_furnace(app: Application, **overrides):
    params = dict(DEFAULT_START)
    params.update(overrides)
    return app.furnace.start("tester", **params)


def settle_pool(app: Application, *, bath: float = 0.68, slag: float = 0.15, matte: float = 0.45):
    app.settler.update("tester", bath_level_m=bath, slag_thickness_m=slag, matte_level_m=matte)
    app.clock.advance(app.settings.settler_layering_dwell_seconds + 1)
    return app.settler.requirements()


def feed_heat(app: Application, heat_id: str = "H-1", *, tons: float = 500.0, rate: float = 150.0):
    settle_pool(app)
    if app.burner.state == "stable":
        app.burner.attest("control-system")  # 每轮扫描刷新燃烧器落盘凭证
    app.furnace.feed("tester", heat_id=heat_id, rate_tph=rate, tons=tons)
    app.clock.advance(app.settings.furnace_min_smelt_dwell_seconds + 1)
    return app.furnace.status()


def run_heat(
    app: Application,
    heat_id: str = "H-1",
    ladle_id: str = "L-1",
    *,
    charge_only: bool = False,
):
    """跑完一炉：喷吹 → 放渣放铜 → 转炉吹炼 → 结束批次。"""

    feed_heat(app, heat_id)
    app.furnace.tap("tester", heat_id=heat_id, ladle_id=ladle_id, slag_tons=8.0, matte_tons=40.0)
    app.conv.charge("tester", ladle_id=ladle_id)
    if charge_only:
        return app.conv.status()
    app.conv.blow("tester", seconds=120.0)
    app.conv.skim("tester", tons=3.0)
    app.conv.discharge("tester", tons=35.0)
    return app.conv.finish_batch("tester")

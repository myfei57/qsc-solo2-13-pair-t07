# FlashSmelter 铜闪速熔炼炉精矿喷吹与放铜控制平台

按「精矿 → 富氧喷吹 → 反应塔 → 沉淀池 → 放铜 → 转炉」的控制链实现的闪速熔炼炉控
制平台。所有状态在进程内计算并落地到本地文件，不依赖 MySQL、Redis 或任何外部服务；
控制台是纯 JSON API，无前端页面。

控制链上的顺序、落盘与联锁是硬约束：任何顺序错位、基线过期或闩锁未复位都会造成
喷溅、炉结或烟气超标，因此每条指令在动作之前都要过门控。

## 目录结构

```text
flashsmelter/
  __main__.py        python -m flashsmelter 入口
  cli.py             命令行：serve / status / actions / call / audit / heat / verify
  application.py     组装根：依赖注入 + 动作注册表
  config.py          工艺量程与环境变量
  errors.py          错误码与 HTTP 状态映射
  params.py          控制台与 CLI 共用参数解析
  machine.py         共享状态机（含跨中间态推进）
  component.py       组件基类：代际校验 + 审计 + 指标 + 先落盘后动作
  runtime.py         时钟、指标、代际、运行上下文
  ns/                冶炼命名空间（site/unit，工位分区）
  store/             文件型持久化：原子写、回读校验、JSONL 流水
  audit/             审计流水（追加型，拒绝动作同样入库）
  burner/            燃烧器：点火 → 火焰确认 → 稳定保持 → 冷却
  oxygen/            富氧系统：分析仪基线、建立、调档、回滚、降档
  conc/              精矿喷吹：门控、热料预算、喷吹意图落盘
  settler/           沉淀池：液位与分层，放料就绪判定
  slag/              放渣：先于放铜，吨位与时长回执
  matte/             放铜：冰铜批记录，供转炉校验
  conv/              转炉：入炉 → 吹炼 → 扒渣 → 出铜 → 结束批次
  waste/             余热锅炉：汽包水位/排烟温度/管束泄漏联锁与保持型闩锁
  furnace/           闪速炉总编排：启动、喷吹、放料、停机、联锁复位
  console/           纯 JSON HTTP 控制台
tests/               70 个用例：存储、门控、工艺链、HTTP 端到端、CLI
tools/dead_code_review.py   符号级死代码审查
```

## 运行

```bash
# 启动控制台（默认 127.0.0.1:8080，状态目录 var/）
python -m flashsmelter serve --root var --port 8080

# 另开一个终端：查看状态、下发指令、查审计
python -m flashsmelter status --root var
python -m flashsmelter actions --root var
python -m flashsmelter call furnace.start --root var --params-json '{"drum_level":0.62,"fuel_pressure_kpa":200,"air_flow_nm3h":5200,"oxygen_baseline":0.62,"oxygen_baseline_source":"analyzer-a","oxygen_target":0.62,"oxygen_flow_nm3h":9000}'
python -m flashsmelter audit --root var --limit 20
python -m flashsmelter heat --root var
python -m flashsmelter verify --root var
```

```bash
# 跑测试
python -m unittest discover -s tests -t .

# 死代码审查（有候选时返回 1，可用于 CI 卡口）
python tools/dead_code_review.py --strict
```

运行环境：Python 3.11+，仅使用标准库，无第三方依赖，离线可跑。

## HTTP 控制台

控制台把动作注册表直接映射成 REST 路径：`<component>.<verb>` → `POST /api/<component>/<verb>`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 服务信息与全部路由 |
| GET | `/api/health` | 存活、命名空间、代际、炉体状态 |
| GET | `/api/state` | 全量状态：服务、炉次、各组件快照、指标 |
| GET | `/api/metrics` | 计数与瞬时量 |
| GET | `/api/actions` | 全部可用动作及其 REST 路径 |
| GET | `/api/audit` | 审计查询（`limit` / `since` / `action` / `target` / `outcome` / `actor`） |
| GET | `/api/heats` | 当前炉次汇总与历史炉次 |
| GET | `/api/components` | 组件清单 |
| GET | `/api/components/{component}` | 单组件状态与落盘版本 |
| GET | `/api/zones` | 按工位分区（reactor / gas / settler / converter / offgas / control）聚合 |
| POST | `/api/<component>/<verb>` | 下发控制指令，JSON 体即动作参数 |

错误按类型映射：参数问题 400、路径不存在 404、方法不支持 405、请求体超限 413、
门控/顺序/闩锁/代际冲突 409、落盘故障 500。响应体统一为
`{"error": "...", "message": "...", "status": ..., "details": {...}}`。

## 工艺门控与安全约束

平台实现的是**正确行为**，每条约束都对应一个可复现的失效机制：

1. **先落盘后动作**：喷吹、开渣口/放铜口、闩锁复位、建立富氧都会先写一份意图记录并
   回读校验（`store.commit_intent`），未落盘绝不驱动执行机构。
2. **燃烧器凭证时效**：精矿喷吹要求燃烧器处于 `stable` 且状态记录在
   `burner_record_max_age_seconds` 内落过盘；控制系统每轮扫描用 `burner.attest` 刷新。
3. **富氧先于喷吹**：`oxygen.ensure_established_for_feed` 要求富氧已建立且分析仪基线新鲜。
4. **基线滞后判定**：基线过期（超过 `oxygen_baseline_window_seconds`）或读数早于基线
   都会被拒绝；读数偏离设定值超过 `oxygen_analyzer_tolerance` 会把富氧降级为 `degraded`。
5. **放渣先于放铜**：`matte.tap` 先查本炉次的放渣回执，未放渣直接拒绝。
6. **沉淀池分层就绪**：液位、渣层厚度、冰铜液位与静置时长四项齐备才进入 `tap_ready`；
   放渣与放铜各自有独立的就绪判据。
7. **余热锅炉保持型闩锁**：汽包水位低、排烟超温或管束泄漏触发闩锁；工况恢复后仍保持，
   必须满足最短保持时长并由人工填写说明后才能复位。
8. **转炉顺序与容量**：入炉必须是已放铜且未被吹炼的批记录，单批不超过容量上限。
9. **停机顺序**：先停精矿喷吹 → 再降富氧 → 停燃烧器 → 余热锅炉冷却；富氧降档会校验
   喷吹确实已停止。
10. **命令代际**：指令可携带 `expected_generation`，与当前代际不一致即判定命令冲突并作废。

## 环境变量

全部配置项都可用 `FLASHSMELTER_<字段名大写>` 覆盖，例如：

```bash
export FLASHSMELTER_PORT=9100
export FLASHSMELTER_ROOT=/srv/flashsmelter
export FLASHSMELTER_NAMESPACE=smelter/line2
export FLASHSMELTER_OXYGEN_BASELINE_WINDOW_SECONDS=120
export FLASHSMELTER_BURNER_RECORD_MAX_AGE_SECONDS=60
```

取值在启动阶段校验：量程不自洽（富氧上下限颠倒、渣层下限大于沉淀池最低液位等）会直接
拒绝启动，而不是留到运行时。

## 数据与恢复

```text
var/
  data/<site>/<unit>/<component>/<key>.json   原子写 + 校验和 + 版本号
  journal/<site>/<unit>/<stream>.jsonl        追加流水（审计、冰铜批、炉次、转炉批次）
```

写入流程是「临时文件 → fsync → 原子替换 → 目录 fsync → 回读校验」；启动时清理残留
临时文件，`verify` 会逐条校验校验和并报告被篡改或截断的记录。组件在构造时从落盘状态
恢复，进程重启后炉次、喷吹吨位与闩锁状态都还在。

## 质量控制

* 无第三方依赖，离线可构建、可测试。
* 无脚手架代码：每个包、每个符号都挂在真实调用链上，`tools/dead_code_review.py` 覆盖
  378 个定义符号与全部配置字段，当前报告为「未被引用的符号：无」。
* 生产代码 6259 行（26 个模块），测试 71 个用例（约 1120 行）。

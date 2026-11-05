# 智关 Agent · 9610 制单/报关 Agent 护城河落地方案

> 依据：《智关Agent_9610特化护城河分析》（四层护城河：① 法规 Know-how 库 / ② 差错案例语料库 / ③ 用户修正反馈飞轮 / ④ 海关-税务对接位）。
> 目标：把"沉淀物"接进**已经跑通的报关链路**，不改顶层 Coordinator、不改已冻结契约。
> 版本：2026-10-19 | 性质：**只做方案，不含代码改动**；确认后按「第 9 节 实施顺序」推进。
> 范围：制单/报关链路 = `CustomsOrchestrator` + 6 个能力槽（A 归类 / B 三单预检 / C 清单合规 / D 风险预判 / E 归并 / F 节点监控）。

---

## 0. 一句话结论

链路的"骨架"已经答完（状态机 + 6 槽 + 契约），文档说的"真壁垒"（②③）在代码里几乎为零。
本方案不是重写链路，而是**在链路两侧加两层**：左侧「沉淀层」（静态资产 ①②④）+ 右侧「回流层」（动态 ③），并给它们定义**唯一的接入点**（`orchestrator.register` / `state` 增量字段 / `receipt_hook`）。

---

## 1. 现状盘点（代码事实，先对齐）

### 1.1 已有链路组件

| 组件 | 文件 | 现状 |
|---|---|---|
| 状态对象 + 状态机 | [customs/state.py](../customs/state.py) | `SLOT_ORDER`、`AGENT_IO_CONTRACT`、`ShipmentState` 已冻结 |
| 编排器 | [agents/customs_orchestrator.py](../agents/customs_orchestrator.py) | 6 槽全部 `register`；降级链 `retry_once → degrade_mode(human_queue)`；`_finalize` 按 `risk_flags[]` 定 tier |
| A 归类 + 制单 | [agents/customs.py](../agents/customs.py) | 归类推理链 + 三级分流 + `_render_9610` 渲染 `declaration_9610.j2`（= 制单产物）+ `_audit_trail` 留痕 |
| B 三单预检 | [agents/three_doc.py](../agents/three_doc.py) | `COMPARE_FIELDS` 字段比对 + 品名 `difflib` 相似度（阈值 0.6），零 LLM |
| C 清单合规 | [agents/decl_compliance.py](../agents/decl_compliance.py) | CEB603 `FIELD_SPEC` 格式校验 + `GENERIC_NAME_BLACKLIST` + 可选 LLM 语义 |
| D 风险预判 | [agents/risk_screen.py](../agents/risk_screen.py) | IP / 禁限 / 货值 / 制裁 四维，`risk_flags[]` 统一出口 |
| E 归并 | [agents/merge.py](../agents/merge.py) | 八个同一分组 + HS 冲突检测 |
| F 节点监控 | [agents/risk_monitor.py](../agents/risk_monitor.py) | 汇总申报（次月15日前）/ 退运（6个月）时间窗 |
| 法规库 / HS 库 | [customs/data/rules.json](../customs/data/rules.json) · [hs_codes.json](../customs/data/hs_codes.json) | rules.json 16 条（部分带 `policy_source` / `policy_validity`） |

### 1.2 四层护城河 vs 现状

| 层 | 文档要求 | 代码现状 | 差距 |
|---|---|---|---|
| ① 法规 Know-how 库 | 八个同一 / 清单核放 / 无票免税 / T+2 / 167 号 / 12 月清单当月汇总 | `rules.json` 16 条 + HS 库 + F 时间窗（散落） | **无统一时效追踪**：有效期字段不成结构、无换版/过期告警；以上 3 条专项未入规则库 |
| ② 差错案例语料库 | 「差错→特征→修正」三元组，带来源 | **无**。[hs_kb.py](../customs/hs_kb.py#L53) `KNOWN_BRANDS` 硬编码；AGENT_TASKS 提的 `brands.json` 未建 | 缺 `error_cases.json` / `brands.json` 与特征匹配引擎 |
| ③ 用户修正反馈飞轮 | AI 建议 → 人工确认 → 回执回流 | **无闭环**。`_audit_trail` 只记 AI 动作，不记人工修正；交互闸门只用于订舱 | 缺 `corrections[]` 捕获、落盘、聚合回流 |
| ④ 海关-税务对接位 | 标准报文进出 + 外部回执挂接点（架构预留） | `state.py` 状态机 + `agent_slots` + `audit_log` 是"可挂接"底子 | 缺 CEB603 报文 schema 与 `external_receipts` 挂接点 |

### 1.3 三条"非护城河"红线（文档要求主动澄清，落地时也必须遵守）

- 大模型本身 ≠ 壁垒 → 本方案**不新增 LLM 决策点**，只把资产接进确定性链路。
- 归类知识库本身 ≠ 深壁垒 → 本方案不以"扩 HS 库"为目标，只做 9610 特有链路知识。
- 单点功能易被抄 → 本方案的重心是 ②③ 的**数据沉淀**，不是某个功能页面。

---

## 2. 总体设计

### 2.1 三层结构（链路层已存在，本方案只加两侧）

```
                         ┌─ 沉淀层（静态资产，本方案新增/增强）──────────────┐
                         │ ① rules.json（+ 结构化 validity）                │
                         │ ② brands.json · error_cases.json                 │
                         │ ④ messages/ceb603_request.json · _receipt.json   │
                         └───────────────────┬─────────────────────────────┘
                                             │ 只读消费
  用户输入 → Coordinator → CustomsOrchestrator（状态机，LLM 不参与路由）
                                             │
   classify → three_doc → decl_compliance → risk_screen → merge → risk_monitor
                                             │
                          制单产物 = declaration_9610.j2 渲染的清单
                                             │
                                    交互闸门（人工确认 / 修正）
                                             │ 捕获
                         ┌─ 回流层（动态，本方案新增）─────────────────────┐
                         │ ③ ShipmentState.corrections[]                     │
                         │    → correction_store.jsonl（append-only）        │
                         │    → 聚合 TopN → 人工审核 → 回灌 ①②            │
                         └──────────────────────────────────────────────────┘
```

### 2.2 新增文件与目录

```
transport_agent/
├── customs/
│   ├── data/
│   │   ├── rules.json              ← 增强：结构化 policy_validity（改）
│   │   ├── brands.json             ← 新增：品牌词库（②）
│   │   ├── error_cases.json        ← 新增：差错案例三元组（②）
│   │   └── messages/               ← 新增（④）
│   │       ├── ceb603_request.json    标准报文请求占位 schema
│   │       └── ceb603_receipt.json    标准回执占位 schema
│   ├── error_cases.py              ← 新增：特征匹配引擎（② 的消费实现）
│   └── correction_store.py         ← 新增：修正落盘 + 聚合（③）
└── agents/
    ├── customs_orchestrator.py     ← 改：加 receipt_hook + 规则有效期自检 + corrections 读写
    ├── risk_screen.py              ← 改：IP 检测改读 brands.json（保留 hs_kb 兜底）
    └── decl_compliance.py          ← 改：接入 ② 的品名/归类特征
```

> 约定：所有消费点改造都必须**保留原兜底路径**——数据文件缺失时，回退到当前硬编码/默认行为，链路不中断（呼应 [AI浓度清单.md](../AI浓度清单.md) 的降级策略）。

### 2.3 编排器接入点（唯一，不改顶层）

| 接入方式 | 用途 | 位置 |
|---|---|---|
| `orchestrator.register(key, agent)` | 能力槽（已存在，本方案不新增槽） | [customs_orchestrator.py](../agents/customs_orchestrator.py#L64) |
| `ShipmentState` **增量字段** | `corrections[]`（③）、`external_receipts[]` + `local_tax_restore`（④） | [state.py](../customs/state.py) |
| `AGENT_IO_CONTRACT` **增量 reads** | C 新增读 `error_cases` 命中结果等 | 同上 |
| `receipt_hook(state, key)`（新增，默认 no-op） | 状态推进点挂外部回执（④） | 编排器 `_run_slot` 之后 |
| `extra_context["corrections"]` | 交互闸门回传人工修正（③） | [web/api/server.py](../web/api/server.py#L116) `_build_extra_context` |

**红线**：只做"增量字段 + 增量 reads"，**不改** 已有契约中的写字段名，**不新增** 槽位 key，**不动** `base.py` / `coordinator.py`。

---

## 3. 卡片 ①：法规库时效化

- **类型**：数据治理 + 校验 | **LLM：否**
- **文件**：`customs/data/rules.json`（增强）；消费点 `agents/risk_monitor.py`、`agents/three_doc.py`、`agents/customs_orchestrator.py`
- **字段设计**（`rules.json` 每条新增/规整）：
  ```json
  {
    "rule_id": "R008",
    "condition": { "cargo_contains": "退运" },
    "risk": "…",
    "advice": "…",
    "severity": "critical",
    "source": { "policy": "财政部 海关总署 税务总局公告 2026年第16号" },
    "policy_validity": {
      "valid_from": "2026-01-01",
      "valid_to": "2027-12-31",
      "status": "active"          // active / expiring_soon / expired / superseded
    }
  }
  ```
- **功能清单**：
  - [ ] F1 把现存的 `policy_validity` 字符串统一为结构化对象（**不新增编造政策**，只规整已有来源）。
  - [ ] F2 新增 3 条 9610 专项（各自注明来源，**职责边界不越界**）：
    - 12 月清单当月汇总的特殊规则 → 入 `rules.json`，由 **F 节点监控** 消费（报关域）。
    - 167 号公告收款单豁免 → 入 `rules.json`，由 **B 三单预检** 消费（报关域）。
    - 深圳无票免税 T+2 登记窗口 → **归顶层 tracking**（生命周期监控），只在本库登记，不在报关域消费。
  - [ ] F3 有效期自检：编排器启动 / 每次 `ask()` 开头，扫描 `status ∈ {expiring_soon, expired, superseded}` 的规则 → 写 `state.audit_log` 一条 `rule_validity_warning`，并在 `summary` 附一行"⚠️ N 条规则已过期/将过期，需复核"。
- **接入点**：`ComplianceRulesEngine`（[declaration.py](../customs/declaration.py#L56)）暴露 `expired_rules()`；`CustomsOrchestrator.ask()` 入口调用一次。
- **边界**：只做"登记 + 告警"，**不自动改写规则内容**；政策更新仍需人工确认。
- **验收**：
  - 造 1 条 `valid_to` 已过期的规则 → 跑 `build_orchestrator().ask("…")`，`summary` 出现过期告警、`audit_log` 有 `rule_validity_warning`。
  - 打印 `expired_rules()` 输出。

---

## 4. 卡片 ②：差错案例语料库（真壁垒，优先做）

- **类型**：数据资产 + 规则匹配 | **LLM：否**（匹配确定性）
- **文件**：`customs/data/error_cases.json`、`customs/data/brands.json`（新增）；引擎 `customs/error_cases.py`（新增）；消费点 `risk_screen.py`、`decl_compliance.py`
- **字段设计**：
  ```json
  {
    "_meta": {
      "version": "1.0",
      "description": "9610 差错案例语料库：差错类型 → 识别特征 → 修正动作 三元组",
      "note": "每条必须带 source_case 与 verified；禁止凭空编造"
    },
    "cases": [
      {
        "id": "EC-0001",
        "error_type": "ip_infringement",
        "feature": { "field": "cargo_name", "match_type": "brand_lexicon", "pattern": "brands.json" },
        "correction_action": "命中品牌词 → 强制 C 级人工审核，要求品牌授权证明；禁止 A 级自动拟单",
        "severity": "critical",
        "consumer": "risk_screen",
        "source_case": { "title": "CASETIFY 手机壳出口侵权案", "authority": "宁波海关", "year": 2026 },
        "verified": true
      }
    ]
  }
  ```
  - `feature.match_type` 枚举：`brand_lexicon` / `regex` / `keyword_list` / `numeric_threshold` / `enum` / `semantic_short`。
  ```json
  { "_meta": { "version": "1.0", "source": "公开处罚决定书" },
    "brands": [ { "brand": "CASETIFY", "aliases": ["casetify"], "risk_level": "critical", "source": "EC-0001" } ] }
  ```
- **功能清单**：
  - [ ] F1 `customs/error_cases.py`：`ErrorCaseEngine.match(cases, field, value)`，支持上述 6 种 `match_type`，返回命中的 `{id, correction_action, severity}` 列表。
  - [ ] F2 品牌库外置：`risk_screen._check_ip` 改读 `brands.json`；文件缺失时**回退** `hs_kb.detect_ip_risk`（硬编码 `KNOWN_BRANDS`，保留为底层兜底）。
  - [ ] F3 录入文档点名的真实案例（≥4 条，标注来源）：
    - CASETIFY 手机壳出口侵权案（宁波 2026）
    - 五品牌手机壳案（蛇口 2024）
    - 祛疤凝胶归类错误案
    - 低报 30% 罚 12 万案
  - [ ] F4 `decl_compliance` 接入 ② 的品名/归类特征：`error_type ∈ {misclassification, name_too_generic}` 的案例作为额外校验项，与现有 `GENERIC_NAME_BLACKLIST` 并列（不替换）。
- **接入点**：`risk_screen`（D 槽）、`decl_compliance`（C 槽）内部读取；无需新槽、无需改契约字段名（复用 `risk_flags[]` / `decl_compliance.issues[]`）。
- **边界**：案例只做"识别 + 提示修正动作"，**不生成申报内容**、**不代客申报**。
- **验收**：
  - 造 1 正例 + 3 问题例（品牌侵权 / 归类错误 / 品名过泛），`risk_screen.ask()` 命中 IP、`decl_compliance.ask()` 命中归类特征，打印命中 `case.id`。
  - 删除 `brands.json` 再跑，确认回退到 `hs_kb` 硬编码仍能命中（降级不中断）。

---

## 5. 卡片 ③：用户修正反馈飞轮（复利最高，埋点要早）

- **类型**：埋点 + 状态扩展 + 聚合回流 | **LLM：否**（聚合是统计）
- **文件**：`customs/state.py`（增量字段）、`agents/customs_orchestrator.py`（读写）、`web/api/server.py`（捕获入口）、`customs/correction_store.py`（新增：落盘 + 聚合）
- **字段设计**（`ShipmentState` 增量）：
  ```python
  corrections: List[Dict] = field(default_factory=list)
  # 每项：{ field, ai_value, human_value, scope(槽位/agent), rule_id?, reason?, source, at }
  # source ∈ "interactive_gate" / "manual_api" / "reclassify"
  ```
- **功能清单**：
  - [ ] F1 捕获入口：`web/api/server.py::_build_extra_context` 新增可选 `corrections` 参数（形如 `[{"field":"hs_code","ai_value":"3926909090","human_value":"3926901000"}]`）；经 `coord.dispatch(extra_context=…)` 透传。
  - [ ] F2 `CustomsOrchestrator._hydrate` 读取 `ctx["corrections"]` 写入 `state.corrections`；`_build_result` 把 `corrections` 一并放进 `data`。
  - [ ] F3 落盘：`correction_store.record(corrections, run_meta)` → `customs/data/correction_store.jsonl`（append-only，一行一条，带时间戳）。
  - [ ] F4 聚合：`correction_store.aggregate()` → 输出"高频修正 TopN"，键 = `(field, ai_value, human_value)`，值 = 次数；供人工审核后**回灌 ①②**（新增案例 / 校正品牌词）。
  - [ ] F5 把 `CustomsAgent._audit_trail` 升级为 correction-aware：留痕里区分"AI 建议值"与"人工终值"（当前只记前者）。
- **闭环公式映射**（文档：专有数据飞轮 = 嵌入业务流 + 捕获专家修正 + 回流训练）：

  | 环节 | 现状 | 本方案 |
  |---|---|---|
  | 嵌入业务流 | ✅ 已有全链路 | 不改 |
  | 捕获专家修正 | ❌ | F1–F3 |
  | 回流迭代 | ❌ | F4（回灌 ①/②） |

- **接入点**：`extra_context["corrections"]`（复用现有交互闸门通道，与订舱的 `interactive/selected_option` 同构）。
- **边界**：只"记录 + 聚合 + 提示"，**不自动训练、不自动改规则**；回灌前必须人工审核。
- **验收**：
  - 模拟一次"人工把 `hs_code` 从 A 改成 B"→ 跑一次 `dispatch(extra_context={"corrections":[…]})`，确认 `state.corrections` 非空、`correction_store.jsonl` 新增一行、`aggregate()` 输出该键 `count=1`。

---

## 6. 卡片 ④：海关-税务集成位（架构预留，比赛阶段不真接）

- **类型**：接口占位 | **LLM：否**
- **文件**：`customs/data/messages/ceb603_request.json`、`ceb603_receipt.json`（新增）；`state.py` + `customs_orchestrator.py`（增量）
- **字段设计**：
  ```python
  external_receipts: List[Dict] = field(default_factory=list)   # { type, payload, at }
  local_tax_restore: Dict = field(default_factory=dict)          # 属地还原（异地报关数据回属地税务）
  ```
  - `messages/ceb603_receipt.json`：标准回执占位 schema（`manifest_no` / `status` / `release_time` / `error_code?`），仅定义结构、不接真实通道。
- **功能清单**：
  - [ ] F1 定义 CEB603 请求/回执**占位 schema**（放 `messages/`），作为"标准报文进出"的架构证据。
  - [ ] F2 编排器新增 `receipt_hook(state, key)`（默认 no-op）：在 `declared` / `arrived` 等状态推进点可挂外部回执 → 写入 `state.external_receipts`。
  - [ ] F3 预留属地还原抽象字段 `local_tax_restore`，供未来"属地转关 → 属地税务"数据共享机制挂接。
  - [ ] F4 `_build_result` 输出 `external_receipts` 数量与 `local_tax_restore` 状态（"对接位就绪 / 未接入"）。
- **接入点**：`receipt_hook`（编排器）——**不改路由逻辑**，只在槽位成功后调用一次。
- **边界**：**不真对接海关/税务系统**；只证明"对接位已预留"（文档明说比赛阶段只需展示设计）。
- **验收**：
  - mock 一份回执 → 走 `receipt_hook`，确认 `state.external_receipts` 记录 1 条、`summary` 显示"对接位就绪"。

---

## 7. 兼容与红线（所有卡片通用）

1. **顶层零改动**：不改 `base.py` / `coordinator.py` / `main_coordinator.py` 的注册结构；一切走 `orchestrator.register` 与 `extra_context`。
2. **契约兼容**：不新增槽位 key，不重命名已有写字段；新增字段一律"增量"，缺省即回退旧行为。
3. **禁止编造**：每条规则 / 案例 / 品牌须带 `source` 与 `verified`（沿承 [AGENT_TASKS.md](../AGENT_TASKS.md) §1.4）。
4. **AI 边界**：不生成申报内容、不代客申报；只做校验、建议、留痕（呼应 [AI浓度清单.md](../AI浓度清单.md) 合规红线）。
5. **降级不中断**：新数据文件缺失 → 回退当前硬编码/默认值；不抛异常、不阻断下游。
6. **确定性优先**：②③④ 均零 LLM；不因本方案新增任何 AI 决策点。

---

## 8. 验收总表（DoD）

- [ ] ① `rules.json` 全部条目 `policy_validity` 结构化；过期告警在 `summary` + `audit_log` 可见；3 条 9610 专项已登记且职责不越界。
- [ ] ② `error_cases.json` 含 ≥4 条带来源真实案例；`brands.json` 外置生效；`risk_screen` / `decl_compliance` 能命中；删库可回退旧硬编码。
- [ ] ③ `corrections[]` 经交互闸门捕获、落盘 `correction_store.jsonl`、`aggregate()` 可出 TopN。
- [ ] ④ `messages/` 有 CEB603 请求/回执占位 schema；`receipt_hook` 可挂 mock 回执并写入 `external_receipts`。
- [ ] 全链路回归：`python main_coordinator.py --demo` 三个场景输出不劣化；`build_orchestrator().ask()` 6 槽状态仍全 `success`。
- [ ] 顶层文件（`base.py` / `coordinator.py` / `main_coordinator.py`）零 diff。

---

## 9. 实施顺序与每步可演示产出

| 步骤 | 内容 | 可演示产出 |
|---|---|---|
| Step 0 | 建 `customs/data/error_cases.json` / `brands.json` / `messages/` 空库（各带 1 条样例，标 `verified:false`） | 目录就位 + schema 可审阅 |
| Step 1 | ② 品牌外置 + `error_cases.py` 引擎 + `risk_screen`/`decl_compliance` 接入 | 一条命令命中"CASETIFY 侵权案" |
| Step 2 | ③ `corrections[]` + 捕获 + `correction_store` + 聚合 | 一次人工修正 → 落盘一行 → TopN 可见 |
| Step 3 | ① validity 结构化 + F 12 月例外 + B 167 号豁免 | 过期规则告警打印 |
| Step 4 | ④ messages schema + `receipt_hook` + `external_receipts` | mock 回执挂接演示 |
| Step 5 | 全链路回归 + 更新 `AGENT_TASKS.md` 回报 | demo 三场景不劣化 |

> 原则：**先做 ②（别人没有）+ ③（复利最高）**，因为这两块最能形成差异化、也最能直接转成答辩素材；①④ 是补齐及格线与"认知证明"，放后面。

---

## 10. 风险与未知

| 风险 | 说明 | 缓解 |
|---|---|---|
| 案例数据获取 | 公开处罚决定书分散、非结构化，需领域判断标注 | 先录文档已点名的 4 例，增量扩充；每条标来源 |
| ③ 需要用户量 | 飞轮要"先有用户再闭环再时间" | 先把**捕获与落盘**做出来（埋点早），数据可后补 |
| ④ 依赖资质 | 比赛阶段无法真对接 | 只做 schema + hook 预留，架构证明认知 |
| 时效维护 | 政策换版（GB/T 36597 等） | ① 的 `status` 告警兜住，人工确认更新 |

---

## 附录 A · 文件清单（改动一览）

| 文件 | 动作 | 归属 |
|---|---|---|
| `customs/data/rules.json` | 改（+ 结构化 validity + 3 条专项） | ① |
| `customs/data/brands.json` | 新增 | ② |
| `customs/data/error_cases.json` | 新增 | ② |
| `customs/error_cases.py` | 新增（特征匹配引擎） | ② |
| `agents/risk_screen.py` | 改（IP 改读 brands.json + 保兜底） | ② |
| `agents/decl_compliance.py` | 改（接入 ② 特征） | ② |
| `customs/state.py` | 改（+ `corrections[]` / `external_receipts[]` / `local_tax_restore`） | ③④ |
| `customs/correction_store.py` | 新增（落盘 + 聚合） | ③ |
| `agents/customs_orchestrator.py` | 改（+ `receipt_hook` + 有效期自检 + corrections 读写） | ①③④ |
| `web/api/server.py` | 改（`_build_extra_context` + `corrections`） | ③ |
| `customs/data/messages/ceb603_request.json` | 新增（占位 schema） | ④ |
| `customs/data/messages/ceb603_receipt.json` | 新增（占位 schema） | ④ |

## 附录 B · schema 示例（可直接照做）

**error_cases.json（1 条示例）**
```json
{
  "id": "EC-0003",
  "error_type": "misclassification",
  "feature": { "field": "cargo_name", "match_type": "keyword_list", "pattern": ["祛疤凝胶", "疤痕修复凝胶"] },
  "correction_action": "品名含'祛疤/药用'语义 → 需按药品/医疗器械归类核查，不得按普通化妆品编码申报",
  "severity": "critical",
  "consumer": "decl_compliance",
  "source_case": { "title": "祛疤凝胶归类错误案", "authority": "公开处罚决定书", "year": 2025 },
  "verified": true
}
```

**brands.json（1 条示例）**
```json
{ "brand": "CASETIFY", "aliases": ["casetify"], "risk_level": "critical", "source": "EC-0001" }
```

**ShipmentState 增量（③④）**
```python
corrections: List[Dict[str, Any]] = field(default_factory=list)
external_receipts: List[Dict[str, Any]] = field(default_factory=list)
local_tax_restore: Dict[str, Any] = field(default_factory=dict)
```

**交互闸门回传（③）**
```json
{
  "interactive": true,
  "corrections": [
    { "field": "hs_code", "ai_value": "3926909090", "human_value": "3926901000",
      "scope": "classify", "reason": "材质实为硫化橡胶", "source": "interactive_gate" }
  ]
}
```

**CEB603 回执占位（④）**
```json
{
  "_meta": { "version": "0.1", "status": "占位 schema，未接真实通道" },
  "manifest_no": "MANIFEST-001",
  "status": "released",
  "release_time": "2026-10-19T10:00:00Z",
  "error_code": null
}
```

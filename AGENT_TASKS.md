# 智关 Agent · 报关域子能力 Agent 任务分包规格

> 用途：把本文件中「第 4~9 节」的任务卡逐张复制给其他对话框实现（连同 §1 交付格式要求一起复制），做完按「第 10 节 回报模板」发回，统一汇总讨论。
> 版本：2026-10-14 | 范围：报关域 5 个新增能力 Agent + 1 个追踪 Agent 扩展

---

## 0. 现状与架构（动手前必读）

### 0.1 现有架构
- 顶层 `Coordinator`（[agents/coordinator.py](../agents/coordinator.py)）：意图识别 → 路由/串联 → 汇总。
- 4 个粗粒度 Agent：`transport` / `booking` / `customs` / `tracking`。
- `customs` 现为单体大 Agent（[agents/customs.py](../agents/customs.py)），内部已做：OCR → 信息缺口 → IP_RISK → 归类推理 → 三级分流 → 拟票 → 合规规则校验。

### 0.2 目标架构（两级，顶层零改动）
```
顶层 Coordinator（保持 4 个入口不变）
  ├── transport / booking
  ├── tracking ← 扩展为「链路追踪 + 在途/税务监控」（顶层最后一个 Agent）
  └── customs ← 由「报关编排器 CustomsOrchestrator」承担（对外 ask() 接口不变）
        ├── A 归类      （现有 CustomsAgent 改造收尾，输出四元组）
        ├── B 三单预检   （新增）
        ├── C 清单合规   （新增）
        ├── D 风险预判   （新增，IP_RISK 复用+聚合）
        ├── E 归并       （新增）
        └── F 节点监控   （新增，仅报关链路节点；在途/天气/税务已移至 tracking）
```
- 顶层 Coordinator 只多注册一个入口，`main_coordinator.py` 仅替换 customs 实例（已换为 `build_orchestrator()`）。
- 报关域内 6 个能力 Agent 由 `CustomsOrchestrator` 用显式状态机调度（状态转移写死，LLM 不参与路由）。
- **骨架已搭好**：`customs/state.py`（共享状态 + 状态机）+ `agents/customs_orchestrator.py`（编排器）。当前仅接入 A 归类，B~F 为预留空槽位；实现后 `orchestrator.register(key, agent)` 即可接入，无需改动编排器。
- **职责划分**：报关链路内的节点（汇总申报次月15日前、退运6个月）留在 F（报关域）；货物在途状态、极端天气报告、免税/退税登记窗口属于"生命周期监控"，归顶层 `tracking` Agent（见任务卡 G）。

### 0.3 三层"合规"别混
| 层 | 现有实现 | 动作 | 输入 | 输出 |
|---|---|---|---|---|
| 拟票 | `DeclarationGenerator.render()` + `declaration_9610.j2` | 生成 | cargo/hs_code/context | 清单草稿文本 |
| 规则风险校验 | `ComplianceRulesEngine.validate()` | 匹配 | cargo/目的国/税率 | 触发风险列表 |
| **清单合规（新增）** | `decl_compliance.py` | 校验 | 清单草稿(CEB603) | `{compliant, issues[]}` |

### 0.4 全局红线（所有 Agent 必须遵守）
1. **不得生成申报内容**：生成即担责，申报责任归属申报主体（277 号令）。AI 边界是"校验与建议"。
2. **不建模物流监管环节的控制权**（⑥⑦⑨⑩ 分拣/过机/查验/拼箱）：企业侧只有读取权，只能监控不能控制。
3. **确定性规则不用 LLM**：三单比对、八个同一归并、时间窗判断 → 零 LLM。
4. **每个输出必须带 `confidence` 与留痕字段**（可追溯、可问责）。

---

## 1. 交付格式要求（所有任务卡通用，复制任务卡时一并附上）

### 1.1 文件与命名
- 单文件交付：`agents/<key>.py`。
- 必须继承 [agents/base.py](../agents/base.py) 的 `BaseAgent`，实现 `ask(query, context) -> AgentResult`。
- `name` 属性 = `<key>`（全局唯一）；`description` 一句话。
- 不得修改 `base.py` / `coordinator.py` / 其他 Agent / 顶层接口。

### 1.2 接口契约
- 只读 context 中本 Agent 需要的字段；**不得修改** context（只读）。
- 输出写入 `AgentResult.data`，字段名 `snake_case`，见各任务卡的"输出契约"。
- `AgentResult` 其余字段：`summary`（给人看的中文摘要）、`confidence`（0~1 浮点）、`needs_human_review`（bool）、`next_hint`（给下游的提示）。

### 1.3 LLM 使用
- 每个任务卡已标注是否使用 LLM；**不得擅自引入**。
- 用 LLM 的 Agent：Key 从环境变量 `DEEPSEEK_API_KEY` 读取；**无 Key 时必须降级**（返回 `degraded=True`，置信度封顶 medium），不得抛异常中断。
- 日志/输出中**不得回显完整 Key**。

### 1.4 数据文件
- 需要知识库/规则表的，放 `customs/data/<name>.json`，顶层含 `_meta`（`source` / `verified` 或 `policy_validity`）。
- **禁止凭空编造**政策、编码、品牌库、代码表；每条须标注来源。

### 1.5 自测
- 文件末尾提供 `if __name__ == "__main__":` 或 `run_selftest()`，用 ≥2 条样例（1 正例 + 1 问题例）跑通并 `print` 关键输出。
- 交付时**粘贴真实运行输出**，不接受"应该能跑"。

### 1.6 依赖
- 如需新依赖，写入 `requirements.txt` 并在回报中单列。

### 1.7 骨架接入契约（已冻结，实现前必读）
骨架见 [customs/state.py](../customs/state.py) 与 [agents/customs_orchestrator.py](../agents/customs_orchestrator.py)。你的 Agent 必须严格按此契约读写，才能被编排器接上。

**槽位 key（`SLOT_ORDER`，register 时必须用其中之一）：**
`classify` / `three_doc` / `decl_compliance` / `risk_screen` / `merge` / `risk_monitor`

**读写契约（`AGENT_IO_CONTRACT`）——"读哪些 state 字段、写哪些 state 字段"：**

| key | 读 state 字段 | 写 state 字段（= `AgentResult.data` 必须包含的字段） |
|---|---|---|
| classify | order, sku_info | hs_code |
| three_doc | order, sku_info | three_doc_status |
| decl_compliance | declaration_draft, hs_code | decl_compliance |
| risk_screen | sku_info, order | risk_flags |
| merge | order | merge_result |
| risk_monitor | timeline, order | timeline |

**调用方式**：编排器调用 `agent.ask(query, context=<agent_ctx>)`，其中 `agent_ctx` = 原始 context + 你在契约中声明的"读字段"（值为当前 `ShipmentState` 的对应字段）。你从 `context` 里取这些字段即可，**不要自己假设别的字段存在**。

**输出方式**：把契约中"写字段"放进 `AgentResult.data`，例如 `risk_screen` 返回 `data={"risk_flags": [...]}`。编排器会自动回写进共享状态。

**共享状态 `ShipmentState` 字段**（详见 state.py）：
`order` · `sku_info` · `hs_code` · `declaration_draft` · `decl_elements` · `three_doc_status` · `decl_compliance` · `risk_flags` · `merge_result` · `timeline` · `status` · `tier` · `confidence` · `degraded` · `agent_slots` · `audit_log`

**降级链**：编排器已实现 `retry_once → degrade_mode(human_queue)`。你的 `ask()` 只需正常实现；抛异常会被编排器捕获并记录，无需自己包 try/except 兜底。

**自测**：用 `from agents.customs_orchestrator import CustomsOrchestrator` 建实例，`register("<你的key>", YourAgent())`，构造对应 state 字段后 `ask()` 验证回写。

---

## 2. 任务卡模板（空白，供你理解结构）

```
### 任务卡 X：<Agent 名>（<key>）
- 文件：agents/<key>.py
- 类型：<建议 | 校验 | 规则 | 算法 | 监控>
- LLM：<是 | 否>（<说明>）
- 输入：context 读取的字段
- 输出契约：data 字段（名: 类型）
- 功能清单：
  - [ ] F1 ...
  - [ ] F2 ...
- 边界（不做）：...
- 验收标准：...
```

---

## 3. 任务卡 A：归类 Agent（改造收尾）— ⚠️ 已基本完成

- 文件：`agents/customs.py`（现有，无需分包）
- 类型：建议
- LLM：是（LLM 只做候选选择与推理链，不写清单）
- 现状：
  - [x] 归一化打分 + 及格线 + NO_MATCH
  - [x] 双维度置信度（norm + gap）
  - [x] degraded 显式降级
  - [x] 槽位驱动提问（REQUIRED_SLOTS）
  - [x] IP_RISK 基础检测
  - [ ] **输出契约对齐**：现 `data.risks` 需并到统一的 `risk_flags[]`，供下游消费
- 输出契约（改造后）：`{hs_code, confidence, reasoning_chain, risk_flags[]}`
- 边界：只产出建议 + 置信度；不写清单（写入动作由编排器按置信度决定）。

---

## 4. 任务卡 B：三单预检 Agent（three_doc）

- 文件：`agents/three_doc.py` | 类型：校验 | **LLM：否**（语义相似度用嵌入模型，不用生成式 LLM）
- 说明：三单比对失败是扣包第一大原因；现有工具只"推送后等回执"，推送前预校验是空白。
- 输入（context 读取）：`platform_order`（订单 JSON）、`logistics`（运单数据）、`payment`（收款单，注意 167 号公告可能豁免）
- 输出契约：
  - `match: bool`
  - `mismatches: [{field: str, order_val: Any, logistic_val: Any, severity: str}]`（severity ∈ info/warning/critical）
  - `severity: str`（整体最高级）
- 功能清单：
  - [ ] F1 收件人信息比对（姓名/电话/地址）
  - [ ] F2 货值比对（订单金额 vs 运单申报价）
  - [ ] F3 币制一致性校验
  - [ ] F4 品名语义相似度（嵌入模型，非 LLM）
  - [ ] F5 数量 / 重量比对
  - [ ] F6 差异报告生成（mismatches + 可读说明）
- 边界：纯校验器；**不修改数据**（以平台订单为准），差异项返回给编排器走人工。
- 验收：2 正例（三单一致）+ 2 问题例（收件人不符 / 货值偏差），打印 mismatches。

---

## 5. 任务卡 C：清单合规 Agent（decl_compliance）

- 文件：`agents/decl_compliance.py` | 类型：校验 | **LLM：是（仅第二层语义判断）**
- 说明：与拟票的区别见 §0.3。本 Agent 只做"判断是否合规"，**不生成申报内容**。
- 输入（context 读取）：`declaration_draft`（CEB603 报文或结构化等价物）+ `hs_code`
- 输出契约：
  - `compliant: bool`
  - `issues: [{layer: str("format"|"semantic"), field: str, issue: str, severity: str, advice: str}]`
- 功能清单：
  - 第一层（确定性，零 LLM）：
    - [ ] F1 字段格式校验（长度/类型/正则）
    - [ ] F2 必填项校验
    - [ ] F3 代码表有效性（关区代码 / 币制代码 / 计量单位代码 → 查表）
  - 第二层（语义，LLM）：
    - [ ] F4 品名是否足够具体（"配件/礼品"级 → 直接标记退回）
    - [ ] F5 申报要素与归类结果自洽（如归 3926 但品名写"硅胶" → 冲突预警）
- 数据文件：`customs/data/ceb603_fields.json`（字段定义 + 必填性 + 代码表）
- 边界：LLM 只"判断是否合规"，**不生成申报内容**；生成即担责。
- 验收：1 合规草稿 + 2 问题草稿（缺字段 / 品名过泛），打印 issues。

---

## 6. 任务卡 D：风险预判 Agent（risk_screen）— ⚠️ 部分已有

- 文件：`agents/risk_screen.py` | 类型：规则引擎 + 轻量 NER | **LLM：否**
- 说明：**不搬家、只复用+聚合**。`hs_kb.detect_ip_risk()` 保留为底层能力函数（不动），本 Agent 复用它并扩展，作为风险统一出口 `risk_flags[]`；归类 Agent 保留 IP 自检作单跑兜底。编排器最终以本 Agent 的 `risk_flags[]` 为准。
- 输入（context 读取）：`cargo` / `sku_info`、`dest_port` / `dest_country`、`unit_price_usd`、`platform_price`
- 输出契约：
  - `risk_flags: [{type: str, detail: str, severity: str, advice: str}]`
  - type ∈ `ip` / `restricted` / `valuation`
- 功能清单：
  - [x] F1 知识产权品牌词匹配（基础版已有：`hs_kb.detect_ip_risk`）
  - [ ] F2 品牌库外置化（`customs/data/brands.json`，从公开处罚案例提取，标来源）
  - [ ] F3 禁限管制·带电（锂电池 UN38.3 等）
  - [ ] F4 禁限管制·液体 / 粉末 / 动植物产品
  - [ ] F5 按目的国资质要求（CE / FDA 等）
  - [ ] F6 货值异常（申报价 vs 平台成交价偏差 > 阈值）
  - [ ] F7 risk_flags[] 统一汇总（供编排器分流）
- 边界：规则 + 检索，**不推理**；输出 risk_flags[] 供编排器分流。
- 验收：1 无风险例 + 2 风险例（品牌侵权 / 带电未申报资质），打印 risk_flags。

---

## 7. 任务卡 E：归并 Agent（merge）

- 文件：`agents/merge.py` | 类型：纯确定性算法 | **LLM：否**（这是最不该用大模型的地方）
- 说明：次月 15 日前，将上月结关清单按"八个同一"归并生成报关单；做错直接影响整月退税/免税。
- 输入（context 读取）：`cleared_manifests`（上月全部结关清单列表）
- 输出契约：
  - `groups: [{group_key: dict, items: [ids], merged_count: int}]`
  - `conflicts: [{group_key: dict, reason: str, items: [ids]}]`
  - `preview: str`（归并预览说明）
- 功能清单：
  - [ ] F1 八键分组（表头五同：收发货人 / 运输方式 / 生产销售单位 / 运抵国 / 出境关别；表体三同：最终目的国 / 10 位商品编码 / 币制）
  - [ ] F2 组内潜在冲突检测（"看似同一实则不同"，如编码后四位不同但品名相同）
  - [ ] F3 归并预览 + 冲突说明生成
- 边界：零 LLM；只生成预览供人工确认后申报，**不代申报**。
- 验收：构造 ≥6 条清单（含 1 组冲突），打印 groups 与 conflicts。

---

## 8. 任务卡 F：节点监控 Agent（risk_monitor）

- 文件：`agents/risk_monitor.py` | 类型：状态机 + 日历引擎 | **LLM：否**
- 说明：**只守护报关链路内的确定性节点**，错过代价 = 视同内销补税 13%。货物在途状态、极端天气、免税/退税登记已移入顶层 `tracking` Agent（见任务卡 G），本卡不再包含。
- 输入（context 读取）：`timeline`（报关链路各清单状态）、`order`
- 输出契约：
  - `alerts: [{manifest_id: str, status: str, next_deadline: str, days_left: int, level: str}]`（level ∈ info/warning/urgent）
- 功能清单：
  - [ ] F1 报关链路状态机 + 日历引擎（每个清单 `{status, next_deadline, days_left}`）
  - [ ] F2 报关链路时间窗规则库：结关→汇总申报（次月 15 日前）；退运（6 个月内，2026 年第 16 号公告）
  - [ ] F3 临期自动升级提醒（info → warning → urgent）
- 数据文件：`customs/data/time_windows.json`（规则 + 来源 + policy_validity）
- 边界：只监控与提醒，**不代申报**。
- 验收：构造 3 条清单（1 临期 / 1 正常 / 1 超窗），打印 alerts。

---

## 9. 任务卡 G：追踪 Agent 扩展（tracking）— 顶层 Agent，非报关域槽位

- 文件：`agents/tracking.py`（扩展现有占位版） | 类型：状态机 + 日历引擎 + 天气查询 | **LLM：是（仅极端天气报告的自然语言生成）**
- 说明：顶层 Coordinator 的最后一个 Agent，扩展为「链路追踪 + 在途/税务监控」。从原报关域 F 移入：货物状态监控、极端天气报告、税务申报节点监控。
- 说明（注册方式）：本 Agent 注册在**顶层 Coordinator**（`coordinator.register("tracking", TrackingAgent())`），**不是** CustomsOrchestrator 的槽位；不要用 register(key) 挂到报关编排器上。
- 输入（顶层 Coordinator 传入的 context）：`tracking_status` / 运单号、`logistics_no`、`dest_country`、`route`、`timeline`（来自报关编排器）、`order`（含清单号/结关时间）
- 输出契约（`AgentResult.data`）：
  - 保留现有：`tracking_no`、`state`、`anomaly`、`suggestions`
  - 新增：
    - `alerts: [{manifest_id: str, status: str, next_deadline: str, days_left: int, level: str}]`
    - `weather_report: str`（仅极端天气时输出，否则空）
    - `tax_reminders: [{type: str, deadline: str, days_left: int}]`
- 功能清单：
  - [ ] T1 货物状态监控（在途异常告警：延误 > 3 天 / 扣关 / 温度异常）
  - [ ] T2 极端天气报告（天气 API → 命中极端天气时生成客户报告，LLM 润色）
  - [ ] T3 税务申报节点监控：结关→无票免税登记（深圳提前 2 天预约 + T+2 确认）；退税申报（36 个月内，越早越好）
- 数据文件：`customs/data/time_windows.json`（与 F 复用同一张表，税务段由本 Agent 消费）
- 边界：**只监控与提醒，不代申报**；天气报告仅在有极端天气时生成；不建模海关/场所系统（只有读取权）。
- 验收：构造 3 条清单（1 临期 / 1 正常 / 1 超窗），打印 alerts 与 tax_reminders；天气部分 mock 数据。

---

## 10. 回报模板（做完后按此格式发回，便于统一汇总）

```
### 交付物
- 文件：<path>
- 依赖新增：<requirements 追加 / 无>

### 接口
- name = "<key>"
- ask(query, context) 读取的 context 字段：<列表>
- data 输出字段：<字段名: 类型，逐条>

### 自测
- 命令：<...>
- 真实输出：<粘贴关键输出>

### 未完成 / 阻塞
- <...>

### 与规格的偏差
- <...>

### 用到的数据来源（若有 JSON 知识库）
- <文件: 来源 URL / 依据>
```

---

## 11. 实施优先级（来自全链路调研报告）

| 优先级 | 任务 | 交付物 |
|---|---|---|
| P0 | 编排器状态机 | ✅ 已完成（state.py + customs_orchestrator.py） |
| P0 | 任务卡 B 三单预检 | 字段比对器 + 语义相似度 + 差异报告 |
| P0 | 任务卡 C 清单合规（第一层） | CEB603 字段校验器 |
| P1 | 任务卡 D 风险预判 | 品牌库 + 资质规则表 |
| P1 | 任务卡 E 归并 | 八个同一分组 + 冲突检测 |
| P1 | 任务卡 F 节点监控 | 报关链路时间窗规则库 + 提醒引擎 |
| P1 | 任务卡 G 追踪扩展 | 在途监控 + 天气报告 + 税务节点 |
| P1 | 演示数据包 | 2 个真实处罚案例改写的端到端剧本 |

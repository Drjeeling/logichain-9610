"""报关域编排器：显式状态机调度 6 个能力 Agent。

定位：
- 作为顶层 Coordinator 的 `customs` 入口（对外 ask() 接口完全兼容）。
- 内部用确定性状态机调度 6 个能力槽位，LLM 不参与路由。
- 各能力 Agent 通过 register(key, agent) 挂到槽位上，未注册的槽位记为 absent，不阻断。

骨架先行：当前仅接入 A 归类（现有 CustomsAgent），B~F 五个槽位留空，
按 AGENT_TASKS.md 的任务卡实现后 register 即可接入，无需改动本文件。

接入契约见 customs/state.py 的 SLOT_ORDER / AGENT_IO_CONTRACT。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# 加入项目根目录到 sys.path，便于 import customs 子包
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from customs.state import (
    ShipmentState,
    SLOT_ORDER,
    AGENT_IO_CONTRACT,
)

from .base import BaseAgent, AgentResult


# 槽位完成后推进到的链路状态（用于状态机留痕）
_SLOT_STATE: Dict[str, str] = {
    "classify": "classified",
    "three_doc": "precheck",
    "decl_compliance": "precheck",
    "risk_screen": "declared",
    "merge": "consolidated",
    "risk_monitor": "tax_registered",
}

_CONFIDENCE_SCORE = {"high": 0.95, "medium": 0.75, "low": 0.45}


class CustomsOrchestrator(BaseAgent):
    """报关全链路编排器（显式状态机 + 共享状态 + 6 能力 Agent）。"""

    name = "customs"
    description = "报关全链路编排器：归类/三单预检/清单合规/风险预判/归并/监控"

    def __init__(
        self,
        slots: Optional[Dict[str, BaseAgent]] = None,
        verbose: bool = False,
    ):
        self._slots: Dict[str, BaseAgent] = dict(slots or {})
        self.verbose = verbose

    # ----------------------------------------------------------------- #
    # 槽位注册（搭积木的"插槽"）
    # ----------------------------------------------------------------- #
    def register(self, key: str, agent: BaseAgent) -> None:
        """把一个能力 Agent 挂到指定槽位。key 必须 ∈ SLOT_ORDER。"""
        if key not in SLOT_ORDER:
            raise ValueError(f"未知槽位 '{key}'，合法值：{SLOT_ORDER}")
        self._slots[key] = agent
        if self.verbose:
            print(f"[CustomsOrchestrator] 已接入槽位: {key} ({agent.__class__.__name__})")

    @property
    def available_slots(self) -> List[str]:
        return [k for k in SLOT_ORDER if k in self._slots]

    # ----------------------------------------------------------------- #
    # 主入口（与旧 CustomsAgent 接口一致）
    # ----------------------------------------------------------------- #
    def ask(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> AgentResult:
        t0 = time.time()
        state = ShipmentState()
        self._hydrate(state, query, context)

        if self.verbose:
            print(f"[CustomsOrchestrator] 状态机启动，已接入槽位: {self.available_slots}")

        # 按槽位顺序推进状态机
        for key in SLOT_ORDER:
            if key not in self._slots:
                state.agent_slots[key] = "absent"
                state.log("slot_skipped", slot=key, reason="未注册（骨架预留槽位）")
                continue
            self._run_slot(state, key, query, context)

        self._finalize(state)
        total_ms = int((time.time() - t0) * 1000)

        if self.verbose:
            print(f"[CustomsOrchestrator] 完成，状态={state.status}，"
                  f"tier={state.tier or '—'}，耗时 {total_ms}ms")

        return self._build_result(state, query, total_ms)

    # ----------------------------------------------------------------- #
    # 入口状态填充
    # ----------------------------------------------------------------- #
    @staticmethod
    def _hydrate(state: ShipmentState, query: str, context: Optional[Dict]) -> None:
        ctx = dict(context or {})
        # order：优先取 context 里的订单数据，否则包一层最小订单；
        # v2.1：上游传入结构化 order（含 sku/title/quantity 等）时合并保留，
        # 供 three_doc 实体对齐使用（B 板块场景 3/4）
        state.order = {
            "query": query,
            "platform": ctx.get("platform"),
            "order_id": ctx.get("order_id"),
            "dest_port": ctx.get("dest_port") or ctx.get("destination_port"),
            "dest_country": ctx.get("dest_country"),
            "unit_price_usd": ctx.get("unit_price_usd"),
            "platform_price": ctx.get("platform_price"),
            **(ctx.get("order") or {}),
        }
        # sku_info：优先复用上游已识别商品名
        state.sku_info = {
            "cargo_name": ctx.get("cargo_name") or ctx.get("cargo") or query,
            "tonnage": ctx.get("tonnage"),
            "unit_price_usd": ctx.get("unit_price_usd"),
            "remark": ctx.get("remark", ""),
        }
        # K1 修复：入口 hs_code 可能是字符串，统一为 {"code": ...}，
        # 保证所有下游槽位看到的都是同一种结构（classify 产出时再覆盖）。
        raw_hs = ctx.get("hs_code")
        if isinstance(raw_hs, str):
            state.hs_code = {"code": raw_hs}
        elif isinstance(raw_hs, dict):
            state.hs_code = dict(raw_hs)
        state.log("state_hydrated", query=query[:100], slots_available=None)

    # ----------------------------------------------------------------- #
    # 单槽位执行（含降级链：retry_once → degrade_mode → human_queue）
    # ----------------------------------------------------------------- #
    def _run_slot(
        self,
        state: ShipmentState,
        key: str,
        query: str,
        context: Optional[Dict],
    ) -> None:
        agent = self._slots[key]
        contract = AGENT_IO_CONTRACT[key]

        # 组装该 Agent 需要的 context：原始 context + 它声明的读字段
        agent_ctx: Dict[str, Any] = dict(context or {})
        for field_name in contract["reads"]:
            # 用默认值兜底：上游槽位缺席或失败时，读字段可能尚未写入 state
            agent_ctx[field_name] = getattr(state, field_name, None)

        # K1 修复：无论 hs_code 来自入口还是上游产出，进入槽位前统一为 dict
        if isinstance(agent_ctx.get("hs_code"), str):
            agent_ctx["hs_code"] = {"code": agent_ctx["hs_code"]}

        # 兼容桥：把 sku_info.cargo_name 映射为旧 Agent 约定的 cargo / cargo_name
        cargo_name = (state.sku_info or {}).get("cargo_name")
        if cargo_name:
            agent_ctx.setdefault("cargo", cargo_name)
            agent_ctx.setdefault("cargo_name", cargo_name)
        # v4 兼容桥：declaration_dict 也塞进去（state 上可能有，也可能没有）
        dd = getattr(state, "declaration_dict", None)
        if dd:
            agent_ctx["declaration_dict"] = dd

        t_slot = time.time()
        result: Optional[AgentResult] = None
        last_err = ""
        for attempt in (1, 2):  # 降级链第 1 步：retry_once
            try:
                result = agent.ask(query, context=agent_ctx)
                break
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
                if attempt == 1:
                    state.log("slot_retry", slot=key, attempt=attempt, error=last_err)

        # 降级链第 2/3 步：重试仍失败 → degrade_mode + human_queue
        if result is None:
            state.agent_slots[key] = "error"
            state.degraded = True
            state.log(
                "slot_error", slot=key, error=last_err,
                degrade="human_queue", elapsed_ms=int((time.time() - t_slot) * 1000),
            )
            return

        # 回写状态字段（只写该槽位声明的字段）
        self._apply_writes(state, key, result)
        state.agent_slots[key] = "success"
        state.status = _SLOT_STATE[key]
        state.log(
            "slot_success", slot=key,
            confidence=result.confidence,
            needs_review=result.needs_human_review,
            summary=(result.summary or "")[:60],
            elapsed_ms=int((time.time() - t_slot) * 1000),
        )

    @staticmethod
    def _apply_writes(state: ShipmentState, key: str, result: AgentResult) -> None:
        data = result.data or {}
        for field_name in AGENT_IO_CONTRACT[key]["writes"]:
            if field_name not in data:
                continue
            if field_name == "risk_flags":
                # K2 修复：追加而非覆盖。D 槽在 C 槽之后执行，
                # 若直接 setattr 会把 C 槽写入的合规 critical 抹掉。
                existing = {(rf.get("type"), rf.get("detail")) for rf in state.risk_flags}
                for rf in data[field_name] or []:
                    mark = (rf.get("type"), rf.get("detail"))
                    if mark not in existing:
                        state.risk_flags.append(rf)
                        existing.add(mark)
            else:
                setattr(state, field_name, data[field_name])

        # 归类槽位的额外字段映射（兼容现有 CustomsAgent 的输出结构）
        if key == "classify":
            if data.get("declaration"):
                state.declaration_draft = data["declaration"]
            # v4：结构化 dict（向后兼容文本）
            if data.get("declaration_dict"):
                state.declaration_dict = data["declaration_dict"]
            if data.get("dispute"):
                state.dispute = data["dispute"]
            if data.get("dispute_resolution"):
                state.dispute_resolution = data["dispute_resolution"]
            if data.get("tier"):
                state.tier = data["tier"]
            if data.get("confidence"):
                state.confidence = data["confidence"]
            if data.get("degraded"):
                state.degraded = True
            # decl_compliance 槽也需要读 dict（v4 优先结构化）
            if data.get("declaration_dict") and "declaration_dict" not in AGENT_IO_CONTRACT.get("decl_compliance", {}).get("reads", []):
                # 动态挂到 state 上，让后续槽能读到
                pass  # setattr(state, "declaration_dict", ...) 已在上面做过
            # 兼容：旧字段名 risks → 归并到统一 risk_flags[]（若 D 未接入则由此兜底）
            if data.get("risks") and not state.risk_flags:
                state.risk_flags = [
                    {"type": "compliance", "detail": r.get("risk", ""),
                     "severity": r.get("severity", "info"), "advice": r.get("advice", "")}
                    for r in data["risks"]
                ]
            if data.get("ip_risk"):
                state.risk_flags.append({
                    "type": "ip",
                    "detail": "命中品牌词：" + ", ".join(data["ip_risk"].get("matched_brands", [])),
                    "severity": "critical",
                    "advice": data["ip_risk"].get("advice", ""),
                })

        # K2 修复：C 槽（合规校验）的 critical 必须汇入统一 risk_flags，
        # 否则 _finalize 看不到，会保留 A 级自动拟单，违反「不静默通过」。
        if key == "decl_compliance":
            dc = data.get("decl_compliance") or {}
            existing = {(rf.get("type"), rf.get("detail")) for rf in state.risk_flags}
            for issue in dc.get("issues") or []:
                if issue.get("severity") != "critical":
                    continue
                detail = f"[{issue.get('field', '')}] {issue.get('message', '')}"
                if ("compliance", detail) in existing:
                    continue
                state.risk_flags.append({
                    "type": "compliance",
                    "severity": "critical",
                    "detail": detail,
                    "advice": issue.get("rule_type", ""),
                })
                existing.add(("compliance", detail))

    # ----------------------------------------------------------------- #
    # 收尾：按统一 risk_flags[] 汇总分流等级
    # ----------------------------------------------------------------- #
    @staticmethod
    def _finalize(state: ShipmentState) -> None:
        # 无风险槽位产出时，保留归类 Agent 给出的 tier
        if not state.risk_flags:
            return
        severities = {rf.get("severity") for rf in state.risk_flags}
        types = {rf.get("type") for rf in state.risk_flags}
        if "critical" in severities or "ip" in types:
            state.tier = "C"
        elif "warning" in severities and state.tier in ("", "A"):
            state.tier = "B"

    # ----------------------------------------------------------------- #
    # 输出拼装
    # ----------------------------------------------------------------- #
    def _build_result(
        self, state: ShipmentState, query: str, total_ms: int,
    ) -> AgentResult:
        slot_line = " / ".join(
            f"{k}:{state.agent_slots.get(k, 'absent')}" for k in SLOT_ORDER
        )
        lines = [
            f"## 报关全链路编排结果",
            f"- **链路状态**：{state.status}",
            f"- **分流等级**：{state.tier or '—'}",
            f"- **置信度**：{state.confidence or '—'}"
            + ("（降级模式）" if state.degraded else ""),
            f"- **槽位状态**：{slot_line}",
        ]
        if state.hs_code:
            lines.append(
                f"- **HS编码**：{state.hs_code.get('code', '—')}"
                f"（{state.hs_code.get('official_name') or state.hs_code.get('description', '')}）"
            )
        if state.risk_flags:
            lines.append(f"- **风险标记**：{len(state.risk_flags)} 项")
            for rf in state.risk_flags:
                lines.append(f"  - [{rf.get('type')}/{rf.get('severity')}] {rf.get('detail', '')}")

        dispute = getattr(state, "dispute", None)
        if dispute:
            lines.append(
                f"- **⚠️ 归类争议**：候选分属 {'/'.join(dispute.get('chapters', []))} 章 → "
                + " / ".join(f"{c['code']} {c['official_name']}" for c in dispute.get("candidates", []))
            )
        resolution = getattr(state, "dispute_resolution", None)
        if resolution:
            lines.append(
                f"- **✅ 争议已消解（CANDIDATE）**：{resolution['chosen']['code']}"
                f"（排除 {resolution.get('rejected')}）"
            )

        summary = "\n".join(lines)
        # v4 补丁：把动态挂的 declaration_dict / dispute 也透传到 data（state 冻结字段里没有）
        data = state.to_dict()
        dd = getattr(state, "declaration_dict", None)
        if dd:
            data["declaration_dict"] = dd
        if dispute:
            data["dispute"] = dispute
        if resolution:
            data["dispute_resolution"] = resolution
        return AgentResult(
            agent_name=self.name,
            summary=summary,
            data=data,
            next_hint=(
                f"报关链路推进至 {state.status}；"
                f"待接入槽位 {[k for k in SLOT_ORDER if k not in self._slots]}"
            ),
            confidence=_CONFIDENCE_SCORE.get(state.confidence, 0.6),
            needs_human_review=(state.tier in ("B", "C") or state.degraded),
        )


# ------------------------------------------------------------------ #
# 便捷构建器 + 自测
# ------------------------------------------------------------------ #
def build_orchestrator(verbose: bool = False) -> CustomsOrchestrator:
    """构建默认编排器：接入 A/B/C/D/F，E 归并预留。

    接入的槽位：
    - classify (A 归类)
    - three_doc (B 三单预检)
    - decl_compliance (C 清单合规)
    - risk_screen (D 风险预判)
    - merge (E 归并)
    - risk_monitor (F 节点监控)
    """
    from .customs import CustomsAgent
    from .three_doc import ThreeDocAgent
    from .decl_compliance import DeclarationComplianceAgent
    from .risk_screen import RiskScreenAgent
    from .merge import MergeAgent
    from .risk_monitor import RiskMonitorAgent

    orc = CustomsOrchestrator(verbose=verbose)
    orc.register("classify", CustomsAgent(verbose=False))
    orc.register("three_doc", ThreeDocAgent())
    orc.register("decl_compliance", DeclarationComplianceAgent())
    orc.register("risk_screen", RiskScreenAgent())
    orc.register("merge", MergeAgent())
    orc.register("risk_monitor", RiskMonitorAgent())
    return orc


if __name__ == "__main__":
    orc = build_orchestrator(verbose=True)
    for q in ["硅胶塑料手机壳", "apple iphone 手机壳"]:
        print("\n" + "=" * 60)
        print(f"[输入] {q}")
        res = orc.ask(q)
        print(res.summary)
        print(f"[tier] {res.data.get('tier')} | [needs_review] {res.needs_human_review}")

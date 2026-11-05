"""国际段订舱 Agent v3 —— 合规驱动的智能订舱推荐。

定位（差异化）：
不做订舱交易平台，做"合规驱动的智能订舱推荐"——
根据报关风险（IP侵权/危险品/制裁国/货值异常）反推最优口岸与航线，
输出可解释的"合规决策链"，给出参考方案与推荐理由。
国内外尚无产品把"报关合规风险"反向驱动订舱决策，这是空白点。

AI 参与点（8 处，提升 AI 浓度）：
  ① AI 询价解析（LLM）：自然语言 → 结构化询价要素
  ② AI 缺口澄清（LLM）：要素不全时生成澄清问题，不空转报错
  ③ AI 询价单生成（LLM）：生成给船公司的标准询价函（RFQ）
  ④ 风险预判复用（复用 RiskScreenAgent，规则）
  ⑤ 多目标打分 + 帕累托前沿（确定性算法）
  ⑥ AI 推荐理由 + 合规决策链解释（LLM）
  ⑦ AI 反事实合规优化（LLM）："补齐某单证 → 风险降级 → 解锁更优口岸/航线"，
     并把成本/时效差量化，把"风险提示"升级为"可执行的优化路径"
  ⑧ AI 议价与守利润建议（确定性区间 + LLM 话术）：基于同口岸真实报价分布给出
     开价/目标价/守利润底线与谈判筹码，输出可直接发出的议价话术

合规反推（C）：风险 → 口岸 → 航线 → 单证 → 申报策略 的可解释链路。

交互闸门（两阶段，context.interactive=True 时启用）：
  阶段1 "proposed"（提出方案）：出候选清单 + 推荐 + 决策链 + 反事实优化 + RFQ，
        但**不**生成议价与订舱指令，等待用户选方案；
  阶段2 "confirmed"（确认执行）：用户回传 selected_option（O1/O2…）后，
        才生成议价话术与订舱指令（含合规随附单证）。
  不传 interactive → stage="auto"，保持单轮直出（供自动化链路/回归测试）。

AI 边界：只给建议、不给申报编码、不代申报（呼应 CBP HQ H350722：
AI 给出六位以上归类编码属"报关业务"，须持牌；本 Agent 只做决策建议）。

输入 context（来自上游 transport）：
  destination_port / from_port、cargo_name / destination、weight / container_type / priority
  交互闸门：interactive（开启两阶段）、selected_option（阶段2 回传所选编号/船公司）
  守利润：budget / target_freight_usd（守利润底线）

输出 data：
  from_port, dest_port, schedules, chosen（字段兼容 v2）
  risk_flags, compliance_notes, compliance_chain
  recommended_reason, rfq_text, missing_slots, clarify_questions, ai_boundary
  counterfactual（反事实优化）、negotiation（议价与守利润）
  stage（auto/proposed/confirmed）、options（候选清单 + 编号）、booking_order（订舱指令）
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from .base import BaseAgent, AgentResult


# ------------------------------------------------------------------ #
# 口岸画像（演示数据；生产应对接口岸通关时效/查验率统计）
# inspection_rate: 查验率（越低越好）；dangerous_allowed: 是否允许危品
# ------------------------------------------------------------------ #
PORT_PROFILE: Dict[str, Dict[str, Any]] = {
    "深圳港": {"inspection_rate": 0.35, "dangerous_allowed": True, "note": "9610 主口岸，查验率高"},
    "广州港": {"inspection_rate": 0.28, "dangerous_allowed": True, "note": "综合口岸"},
    "宁波港": {"inspection_rate": 0.15, "dangerous_allowed": True, "note": "综合口岸，查验率中等偏低"},
    "上海港": {"inspection_rate": 0.20, "dangerous_allowed": True, "note": "国际大港，班次密集"},
    "义乌港": {"inspection_rate": 0.22, "dangerous_allowed": False, "note": "内陆口岸，驳船驳运"},
}

# 航线船期库（演示数据；生产应接航运公司实时数据 RAG）
LANES: Dict[tuple, List[Dict[str, Any]]] = {
    ("宁波港", "洛杉矶"): [
        {"carrier": "马士基", "vessel": "MAERSK EUREKA", "eta_days": 14,
         "price_per_40ft": 4200, "frequency": "每周一班", "reliability": 0.92,
         "dangerous_allowed": True},
        {"carrier": "中远海运", "vessel": "COSCO STAR", "eta_days": 16,
         "price_per_40ft": 3800, "frequency": "每周两班", "reliability": 0.88,
         "dangerous_allowed": True},
        {"carrier": "MSC", "vessel": "MSC GULSUN", "eta_days": 13,
         "price_per_40ft": 4500, "frequency": "每周一班", "reliability": 0.90,
         "dangerous_allowed": True},
    ],
    ("上海港", "洛杉矶"): [
        {"carrier": "马士基", "vessel": "MAERSK HONAM", "eta_days": 13,
         "price_per_40ft": 4400, "frequency": "每周一班", "reliability": 0.93,
         "dangerous_allowed": True},
        {"carrier": "中远海运", "vessel": "COSCO PACIFIC", "eta_days": 15,
         "price_per_40ft": 3900, "frequency": "每周两班", "reliability": 0.87,
         "dangerous_allowed": True},
        {"carrier": "ONE", "vessel": "ONE APUS", "eta_days": 14,
         "price_per_40ft": 4100, "frequency": "每周一班", "reliability": 0.89,
         "dangerous_allowed": True},
    ],
    ("深圳港", "洛杉矶"): [
        {"carrier": "中远海运", "vessel": "COSCO SHIPPING", "eta_days": 15,
         "price_per_40ft": 3600, "frequency": "每周两班", "reliability": 0.85,
         "dangerous_allowed": True},
        {"carrier": "长荣", "vessel": "EVER GIVEN", "eta_days": 16,
         "price_per_40ft": 3500, "frequency": "每周一班", "reliability": 0.84,
         "dangerous_allowed": True},
    ],
    ("宁波港", "汉堡"): [
        {"carrier": "马士基", "vessel": "MAERSK EMDEN", "eta_days": 30,
         "price_per_40ft": 5200, "frequency": "每周一班", "reliability": 0.91,
         "dangerous_allowed": True},
        {"carrier": "赫伯罗特", "vessel": "HAPAG BERLIN", "eta_days": 32,
         "price_per_40ft": 4900, "frequency": "每周一班", "reliability": 0.88,
         "dangerous_allowed": True},
    ],
    ("上海港", "汉堡"): [
        {"carrier": "地中海", "vessel": "MSC OSCAR", "eta_days": 29,
         "price_per_40ft": 5100, "frequency": "每周一班", "reliability": 0.90,
         "dangerous_allowed": True},
    ],
    ("宁波港", "新加坡"): [
        {"carrier": "中远海运", "vessel": "COSCO SINGAPORE", "eta_days": 7,
         "price_per_40ft": 1800, "frequency": "每周三班", "reliability": 0.93,
         "dangerous_allowed": True},
    ],
}

DESTINATION_PATTERNS = ["洛杉矶", "汉堡", "鹿特丹", "新加坡", "迪拜", "釜山", "纽约"]

# 城市名 → 起运港 ID（自然语言里用户常写"深圳"而非"深圳港"）
CITY_PORT_ALIASES = {
    "宁波": "宁波港", "上海": "上海港", "深圳": "深圳港",
    "广州": "广州港", "天津": "天津港", "青岛": "青岛港", "义乌": "义乌港",
}

# 制裁/禁运目的国（演示用，与 RiskScreenAgent.SANCTIONED_COUNTRIES 一致）：命中 → 订舱直接拦截
SANCTIONED_DESTS = ["朝鲜", "伊朗", "叙利亚", "古巴", "委内瑞拉"]

# 评分权重：按用户偏好切换
SCORE_WEIGHTS = {
    "cost": {"cost": 0.55, "time": 0.15, "reliability": 0.15, "compliance": 0.15},
    "time": {"cost": 0.15, "time": 0.55, "reliability": 0.15, "compliance": 0.15},
    "balanced": {"cost": 0.30, "time": 0.30, "reliability": 0.20, "compliance": 0.20},
}

# 硬性必填要素：缺失 → 走 AI 缺口澄清，不空转
REQUIRED_FIELDS = [("from_port", "起运港"), ("dest_port", "目的港")]

# 合规反推硬约束：IP/危险品高风险时，只保留"最低查验率 + 容差"内的口岸（越低越好）
COMPLIANCE_IR_TOLERANCE = 0.10

# AI 边界声明（只给建议，不代申报；呼应 CBP HQ H350722）
AI_BOUNDARY = (
    "本推荐由 AI 基于公开口岸/船期画像生成，仅作决策参考；"
    "不输出六位以上归类编码、不代客申报，最终申报责任归申报主体。"
)

# 风险类型 → 需准备的单证（合规反推的"单证"环节）；type 与 RiskScreenAgent 输出一致
RISK_DOC_MAP: Dict[str, List[str]] = {
    "ip": ["品牌授权书 / 商标注册证"],
    "dangerous": ["UN38.3 测试报告", "MSDS", "危险品包装证明"],
    "prohibited": ["品类检测报告 / 出口许可（视监管条件）"],
    "value": ["平台订单截图 / 成交与付款凭证"],
    "destination": ["出口许可证 / 合规证明"],
}

# 风险类型 → 补救动作（AI 反事实合规优化的输入）；说清"补齐什么，风险即降级"
REMEDIATION_MAP: Dict[str, Dict[str, str]] = {
    "ip": {"action": "补交品牌授权书 / 商标注册证",
           "effect": "消除 IP 侵权嫌疑，可回到原指定口岸出运、免于跨口岸调整"},
    "dangerous": {"action": "备齐 UN38.3 报告 + MSDS + 危包装证明",
                  "effect": "取得危品合规资质，可锁定具备危品通道的直达航线、减少中转"},
    "value": {"action": "备齐平台订单截图 / 成交与付款凭证",
              "effect": "佐证申报货值，避免低报质疑与查验滞期"},
    "prohibited": {"action": "补交品类检测报告 / 出口许可",
                   "effect": "满足监管条件，解除禁限管控"},
    "destination": {"action": "取得出口许可证 / 合规证明",
                    "effect": "解法极严格，通常无法仅靠单证补救，需人工评估"},
    "compliance": {"action": "补齐缺失单证 / 信息",
                   "effect": "消除合规疑点，恢复常规申报"},
}

# AI 议价：开局锚点折扣（相对"目标价"再压一档，供货主开局谈判）
NEGOTIATION_OPEN_DISCOUNT = 0.92
# AI 议价：可比船期容差（目标价仅取"不比所选慢超过 N 天"的替代档，避免为省钱牺牲时效）
NEGOTIATION_ETA_TOLERANCE = 2


def _llm_chat(prompt: str, max_tokens: int = 400, temperature: float = 0.2) -> Optional[str]:
    """统一的 LLM 调用入口。未配置 Key 或调用失败 → 返回 None（调用方降级）。"""
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1")
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return resp.choices[0].message.content.strip()
    except Exception:  # noqa: BLE001
        return None


class BookingAgent(BaseAgent):
    """国际段订舱方案推荐 Agent（合规驱动，v3）。"""

    name = "booking"
    description = "国际段订舱：合规驱动的舱位推荐 + AI 询价 + 风险反推"

    def can_handle(self, query: str) -> bool:
        keywords = ["订舱", "船期", "海运", "船公司", "国际段", "马士基", "中远",
                    "询价", "运价", "报价", "舱位", "航线"]
        has_dest = any(d in query for d in DESTINATION_PATTERNS)
        return has_dest or any(k in query for k in keywords)

    def ask(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> AgentResult:
        ctx = context or {}

        # ---- ① AI 询价解析 ----
        inquiry = self._parse_inquiry(query, ctx)

        # ---- ② AI 缺口澄清：要素不全 → 生成澄清问题，不空转报错 ----
        missing = self._missing_slots(inquiry)
        if missing:
            return self._clarify_result(missing, inquiry)

        from_port = inquiry["from_port"]
        dest_port = inquiry["dest_port"]

        # ---- ④ 风险预判能力复用（合规反推的输入）----
        risk_flags = self._gather_risk(inquiry, ctx)

        # ---- 合规反推（C）：制裁/禁运目的国 → 直接拦截 ----
        if any(rf.get("type") == "destination" for rf in risk_flags):
            return self._blocked_result(from_port, dest_port, risk_flags)

        # ---- 候选集：无合规风险时只用用户指定口岸；有风险时跨口岸扩展（反推最优口岸）----
        candidates = self._collect_candidates(from_port, dest_port, risk_flags)
        if not candidates:
            alt_ports = [fp for (fp, dp) in LANES if dp == dest_port and fp != from_port]
            return self._no_lane_result(from_port, dest_port, alt_ports, risk_flags)

        # ---- ⑤ 多目标打分 + 帕累托前沿 + 合规反推（C）----
        scored, compliance_notes = self._score_candidates(candidates, risk_flags, inquiry)

        if not scored:
            # 全部候选被合规过滤（如危险品口岸无危品通道）
            return self._blocked_result(from_port, dest_port, risk_flags)

        self._mark_pareto(scored)
        scored.sort(key=lambda x: x["score"], reverse=True)
        for i, s in enumerate(scored, 1):
            s["option_id"] = f"O{i}"

        # ---- 交互闸门：出方案 → 用户选方案 → 才议价/生成订舱指令 ----
        # interactive=True 时启用两阶段；否则保持单轮直出（自动化链路用）
        interactive = bool(ctx.get("interactive"))
        selected_raw = ctx.get("selected_option") or ctx.get("selected")
        selected = self._find_option(scored, selected_raw) if selected_raw else None
        selected_miss = bool(selected_raw) and selected is None
        chosen = selected or scored[0]
        if interactive:
            stage = "confirmed" if selected else "proposed"
        else:
            stage = "auto"

        # 合规反推：命中更优口岸 → 显式提示改由该口岸出运
        if chosen["origin_port"] != from_port:
            compliance_notes.append(
                f"合规反推：最优口岸为 {chosen['origin_port']}（查验率更低），"
                f"建议由 {from_port} 改由 {chosen['origin_port']} 出运"
            )

        # ---- 合规反推决策链（C）----
        chain = self._build_chain(from_port, dest_port, risk_flags, chosen)

        # ---- ⑦ AI 反事实合规优化（LLM）：补齐单证后可解锁的更优方案 ----
        counterfactual = self._counterfactual(
            from_port, dest_port, risk_flags, chosen, inquiry
        )

        # ---- ⑥ 推荐理由 + 决策链解释（LLM）----
        reason = self._gen_reason(chosen, scored, risk_flags, chain, inquiry)

        # ---- ③ AI 询价单生成（LLM）----
        rfq = self._gen_rfq(inquiry, chosen)

        # ---- ⑧ AI 议价与守利润建议（确认方案后生成）----
        # "提出方案"阶段不生成议价与订舱指令，等用户选定方案后再出（交互闸门）
        negotiation = None
        booking_order = None
        if stage != "proposed":
            negotiation = self._negotiate(chosen, scored, inquiry, ctx)
            booking_order = self._gen_booking_order(chosen, inquiry, risk_flags, chain)

        # ---- 输出 ----
        multi_port = len({s["origin_port"] for s in scored}) > 1
        summary_lines = [
            f"## {from_port} → {dest_port} 订舱方案推荐（合规驱动，共 {len(scored)} 条候选）\n",
            f"**询价要素**：箱型 {inquiry.get('container_type', '—')}｜"
            f"货重 {inquiry.get('weight') or '—'}｜偏好 {inquiry.get('priority') or '均衡'}",
            "",
        ]
        if multi_port:
            summary_lines += [
                "| 编号 | 起运港 | 船公司 | 船名 | 时效(天) | 运价($/40ft) | 可靠性 | 综合分 | 帕累托 |",
                "|---|---|---|---|---|---|---|---|---|",
            ]
        else:
            summary_lines += [
                "| 编号 | 船公司 | 船名 | 时效(天) | 运价($/40ft) | 可靠性 | 综合分 | 帕累托 |",
                "|---|---|---|---|---|---|---|---|",
            ]
        for s in scored:
            marks = []
            if s is scored[0]:
                marks.append("推荐")
            if stage == "confirmed" and s is chosen and s is not scored[0]:
                marks.append("已选")
            tag = f" 【{'/'.join(marks)}】" if marks else ""
            pareto = "✅" if s.get("pareto") else "—"
            oid = s.get("option_id", "—")
            if multi_port:
                summary_lines.append(
                    f"| {oid} | {s['origin_port']} | {s['carrier']}{tag} | {s['vessel']} | {s['eta_days']} | "
                    f"{s['price_per_40ft']} | {s['reliability']:.0%} | {s['score']:.3f} | {pareto} |"
                )
            else:
                summary_lines.append(
                    f"| {oid} | {s['carrier']}{tag} | {s['vessel']} | {s['eta_days']} | "
                    f"{s['price_per_40ft']} | {s['reliability']:.0%} | {s['score']:.3f} | {pareto} |"
                )

        if risk_flags:
            summary_lines.append(f"\n**合规风险**（{len(risk_flags)}）：")
            for rf in risk_flags:
                icon = "🔴" if rf.get("severity") == "critical" else "🟡"
                summary_lines.append(f"  - {icon} [{rf.get('type')}] {rf.get('detail', '')}")

        if compliance_notes:
            summary_lines.append("\n**合规反推**：")
            for n in compliance_notes:
                summary_lines.append(f"  - {n}")

        summary_lines.append("\n**合规决策链**：")
        for i, c in enumerate(chain, 1):
            summary_lines.append(f"  {i}. {c['step']}：{c['decision']}")

        if counterfactual:
            summary_lines.append("\n**AI 反事实合规优化**（补齐单证后可解锁）：")
            for c in counterfactual:
                summary_lines.append(f"  - 【{c['risk']}】{c['remediation']}")
                summary_lines.append(f"    → {c['benefit']}")

        summary_lines.append(f"\n**推荐理由**：{reason}")

        # ---- 交互闸门：提出方案阶段，请用户选方案 ----
        if stage == "proposed":
            summary_lines.append("\n**请选择要执行的方案**（回复编号即可，如 O2）：")
            if selected_miss:
                summary_lines.append(
                    f"  - ⚠️ 未找到所选方案「{selected_raw}」，请从下列编号中重新选择"
                )
            for s in scored:
                mark = "【推荐】" if s is scored[0] else ""
                summary_lines.append(
                    f"  - {s['option_id']}｜{s['origin_port']} {s['carrier']} {s['vessel']}｜"
                    f"{s['eta_days']}天｜${s['price_per_40ft']}/40ft{mark}"
                )
            summary_lines.append(
                "  → 确认方案后我再生成**议价话术**与**订舱指令**（含合规单证清单）"
            )

        if negotiation:
            n = negotiation
            summary_lines.append("\n**AI 议价与守利润建议**：")
            summary_lines.append(
                f"  - 同口岸报价区间：${n['market_low']}–${n['market_high']}/40ft"
                f"（当前推荐 ${n['current']}，{n['peer_count']} 家可比船司）"
            )
            summary_lines.append(
                f"  - 报价路径：开局锚点 ${n['opening']} → 目标价 ${n['target']}"
                f" → 守利润底线 ${n['ceiling']}"
            )
            summary_lines.append(
                f"  - 潜在节省：相对当前推荐 ${n['saving_vs_current']}/40ft；"
                f"相对区间最高价 ${n['saving_vs_high']}/40ft"
            )
            if n["levers"]:
                summary_lines.append("  - 谈判筹码：" + "；".join(n["levers"]))
            summary_lines.append(f"  - 议价话术：{n['script']}")

        if booking_order:
            label = (
                f"方案 {chosen.get('option_id')}，已确认"
                if stage == "confirmed"
                else f"方案 {chosen.get('option_id')}，拟订"
            )
            summary_lines.append(f"\n**订舱指令（{label}）**：\n```\n{booking_order}\n```")

        if rfq:
            summary_lines.append(f"\n**询价单（RFQ）**：\n```\n{rfq}\n```")

        summary_lines.append(f"\n> {AI_BOUNDARY}")

        return AgentResult(
            agent_name=self.name,
            summary="\n".join(summary_lines),
            data={
                "from_port": from_port,
                "dest_port": dest_port,
                "chosen_port": chosen["origin_port"],
                "container_type": inquiry.get("container_type"),
                "schedules": scored,
                "chosen": chosen,                 # 兼容 Coordinator._merge_context
                "risk_flags": risk_flags,
                "compliance_notes": compliance_notes,
                "compliance_chain": chain,
                "counterfactual": counterfactual,
                "negotiation": negotiation,
                "booking_order": booking_order,
                "stage": stage,
                "selected_option_id": chosen.get("option_id") if selected else None,
                "options": [
                    {
                        "option_id": s.get("option_id"),
                        "origin_port": s["origin_port"],
                        "carrier": s["carrier"],
                        "vessel": s["vessel"],
                        "eta_days": s["eta_days"],
                        "price_per_40ft": s["price_per_40ft"],
                        "score": s["score"],
                        "inspection_rate": s.get("inspection_rate"),
                        "pareto": s.get("pareto", False),
                        "is_recommended": s is scored[0],
                    }
                    for s in scored
                ],
                "recommended_reason": reason,
                "rfq_text": rfq,
                "missing_slots": [],
                "ai_boundary": AI_BOUNDARY,
            },
            next_hint=(
                "等待用户选定方案（传 selected_option=O1/O2… 且 interactive=True）后"
                "再生成议价话术与订舱指令"
                if stage == "proposed"
                else f"已确认方案 {chosen.get('option_id')}：{chosen['carrier']} {chosen['vessel']}"
                f"（{chosen['origin_port']}→{dest_port}）；"
                f"建议下游报关 Agent 按 {chosen['origin_port']} 口岸准备单证"
            ),
            confidence=0.85,
            needs_human_review=(
                stage == "proposed"
                or any(rf.get("severity") == "critical" for rf in risk_flags)
            ),
        )

    # ----------------------------------------------------------------- #
    # ① AI 询价解析
    # ----------------------------------------------------------------- #
    def _parse_inquiry(self, query: str, ctx: Dict) -> Dict[str, Any]:
        """解析询价要素。优先 LLM，失败回退规则。"""
        base = {
            "from_port": ctx.get("destination_port") or ctx.get("from_port"),
            "dest_port": ctx.get("dest_port") or ctx.get("destination") or self._extract_dest_port(query),
            "container_type": ctx.get("container_type"),
            "weight": ctx.get("weight") or ctx.get("tonnage"),
            "priority": ctx.get("priority"),
            "cargo_name": ctx.get("cargo_name"),
            "budget": ctx.get("budget") or ctx.get("target_freight_usd"),
        }

        llm = self._llm_parse(query, base)
        if llm:
            for k, v in llm.items():
                if v and not base.get(k):
                    base[k] = v

        # 规则兜底
        if not base.get("from_port"):
            base["from_port"] = self._extract_origin_port(query, ctx)
        # 城市名归一化（"深圳" → "深圳港"），避免因口岸 ID 不匹配而误判无船期
        base["from_port"] = self._normalize_port(base.get("from_port"))
        if not base.get("cargo_name"):
            base["cargo_name"] = self._extract_cargo(query)
        if not base.get("priority"):
            if any(k in query for k in ["预算", "便宜", "省钱", "价格", "最惠"]):
                base["priority"] = "cost"
            elif any(k in query for k in ["快", "急", "时效", "尽快"]):
                base["priority"] = "time"
            else:
                base["priority"] = "balanced"
        if not base.get("budget"):
            base["budget"] = self._extract_budget(query)
        if not base.get("container_type"):
            base["container_type"] = "40ft"
        return base

    @staticmethod
    def _normalize_port(port: Optional[str]) -> Optional[str]:
        if not port:
            return port
        port = port.strip()
        if port in PORT_PROFILE:
            return port
        return CITY_PORT_ALIASES.get(port, port)

    @staticmethod
    def _extract_budget(query: str) -> Optional[int]:
        """规则兜底：从"预算/上限/不超过 N 美元"抽守利润底线（需显式美元标记，避免币种误判）。"""
        m = re.search(
            r"(?:预算|上限|不超过|最多|以内)[^0-9]{0,8}\$?\s*(\d{3,6})\s*(?:美元|美金|USD|\$)",
            query,
        )
        if not m:
            m = re.search(
                r"(?:预算|上限|不超过|最多|以内)[^0-9]{0,8}\$\s*(\d{3,6})", query
            )
        return int(m.group(1)) if m else None

    @staticmethod
    def _llm_parse(query: str, base: Dict) -> Optional[Dict]:
        """LLM 从自然语言中解析询价要素。"""
        prompt = (
            f"从以下订舱需求中提取要素，只输出 JSON：\n"
            f"需求：{query}\n\n"
            f"字段：from_port(起运港), dest_port(目的港), container_type(箱型,如20ft/40ft/40HQ), "
            f"weight(货重,数字), cargo_name(货名), priority(cost/time/balanced)。\n"
            f"示例：{{\"from_port\": \"宁波港\", \"dest_port\": \"洛杉矶\", "
            f"\"container_type\": \"40ft\", \"weight\": 20, \"cargo_name\": \"手机壳\", \"priority\": \"cost\"}}"
        )
        text = _llm_chat(prompt, max_tokens=200, temperature=0.1)
        if not text:
            return None
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                import json
                return json.loads(m.group(0))
            except Exception:  # noqa: BLE001
                return None
        return None

    # ----------------------------------------------------------------- #
    # ② AI 缺口澄清
    # ----------------------------------------------------------------- #
    @staticmethod
    def _missing_slots(inquiry: Dict) -> List[Dict[str, str]]:
        return [{"field": f, "label": lbl} for f, lbl in REQUIRED_FIELDS if not inquiry.get(f)]

    def _clarify_result(
        self, missing: List[Dict[str, str]], inquiry: Dict,
    ) -> AgentResult:
        """要素不全时，用 LLM 生成澄清问题（回退模板），引导补全而非报错。"""
        labels = [m["label"] for m in missing]
        known = {k: v for k, v in inquiry.items() if v}
        questions = self._llm_clarify(labels, known)
        if not questions:
            questions = [f"请问{lb}是哪里？" for lb in labels]
            questions.append("请补充箱型（20ft/40ft/40HQ）与货重（吨）以便生成询价单。")

        lines = ["## 订舱询价 · 信息待补充", ""]
        lines.append(f"已识别：{known if known else '（无）'}")
        lines.append("")
        lines.append("**请补充以下信息**：")
        for i, q in enumerate(questions, 1):
            lines.append(f"  {i}. {q}")

        return AgentResult(
            agent_name=self.name,
            summary="\n".join(lines),
            data={
                "missing_slots": missing,
                "clarify_questions": questions,
                "known_fields": known,
            },
            next_hint="补齐要素后，将返回合规驱动的舱位推荐与询价单",
            confidence=0.4,
        )

    @staticmethod
    def _llm_clarify(labels: List[str], known: Dict) -> Optional[List[str]]:
        prompt = (
            "用户在订舱询价，但缺少以下要素：" + "、".join(labels) + "。\n"
            f"已知要素：{known}\n"
            "请生成不超过 3 条简洁的中文澄清问题，帮助补全信息（含推荐补箱型/货重）。"
            "每行一条，不要序号。"
        )
        text = _llm_chat(prompt, max_tokens=200, temperature=0.3)
        if not text:
            return None
        qs = [ln.strip(" -•.\t") for ln in text.splitlines() if ln.strip()]
        return qs[:3] or None

    # ----------------------------------------------------------------- #
    # ④ 风险预判能力复用
    # ----------------------------------------------------------------- #
    @staticmethod
    def _gather_risk(inquiry: Dict, ctx: Dict) -> List[Dict]:
        """收集风险标记：优先用 context 已有的，否则调用风险预判能力。"""
        if ctx.get("risk_flags"):
            return list(ctx["risk_flags"])

        cargo = inquiry.get("cargo_name") or ""
        dest = inquiry.get("dest_port") or ""
        if not cargo:
            return []

        try:
            from .risk_screen import RiskScreenAgent
            agent = RiskScreenAgent()
            res = agent.ask("订舱风险预扫描", context={
                "sku_info": {"cargo_name": cargo},
                "order": {"dest_country": dest, "unit_price_usd": ctx.get("unit_price_usd")},
            })
            return res.data.get("risk_flags", [])
        except Exception:  # noqa: BLE001
            return []

    # ----------------------------------------------------------------- #
    # 候选集：无合规风险只用指定口岸；有风险则跨口岸扩展（反推最优口岸）
    # ----------------------------------------------------------------- #
    def _collect_candidates(
        self, from_port: str, dest_port: str, risk_flags: List[Dict],
    ) -> List[Dict]:
        """收集候选航线。返回 [{"origin_port": ..., **lane}, ...]。"""
        pool: List[Dict] = [
            {"origin_port": from_port, **o} for o in LANES.get((from_port, dest_port), [])
        ]
        types = {rf.get("type") for rf in risk_flags}
        # 仅在有合规风险时，才把同目的港的其它口岸纳入候选（避免无谓改变起运地）
        if "ip" in types or "dangerous" in types:
            for (fp, dp), opts in LANES.items():
                if dp == dest_port and fp != from_port:
                    pool.extend({"origin_port": fp, **o} for o in opts)

        # IP 风险硬约束：按"最低查验率 + 容差"收窄口岸，避免仍推荐高查验率的起运地
        if "ip" in types:
            rates = {
                c["origin_port"]: PORT_PROFILE.get(c["origin_port"], {}).get("inspection_rate", 0.2)
                for c in pool
            }
            cutoff = min(rates.values()) + COMPLIANCE_IR_TOLERANCE
            pool = [c for c in pool if rates[c["origin_port"]] <= cutoff]
        return pool

    # ----------------------------------------------------------------- #
    # ⑤ 多目标打分 + 合规反推（各候选按自身口岸的查验率计合规分）
    # ----------------------------------------------------------------- #
    def _score_candidates(
        self, candidates: List[Dict], risk_flags: List[Dict], inquiry: Dict,
    ) -> tuple[List[Dict], List[str]]:
        notes: List[str] = []
        types = {rf.get("type") for rf in risk_flags}
        has_ip = "ip" in types
        has_dangerous = "dangerous" in types

        if has_ip:
            notes.append("检出 IP 侵权风险：已按'低查验率口岸优先'跨口岸反推最优口岸")
        if has_dangerous:
            notes.append("检出危险品（如锂电池）：仅保留口岸与航线均具备危品资质的方案")

        weights = dict(SCORE_WEIGHTS.get(inquiry.get("priority") or "balanced", SCORE_WEIGHTS["balanced"]))
        if has_ip or has_dangerous:
            weights["compliance"] += 0.15
            weights["cost"] = max(0.05, weights["cost"] - 0.15)

        min_cost = min(o["price_per_40ft"] for o in candidates)
        max_cost = max(o["price_per_40ft"] for o in candidates)
        min_t = min(o["eta_days"] for o in candidates)
        max_t = max(o["eta_days"] for o in candidates)

        scored: List[Dict] = []
        for cand in candidates:
            port = PORT_PROFILE.get(cand["origin_port"], {"inspection_rate": 0.2, "dangerous_allowed": True})
            # 危险品：口岸与航线都必须具备危品资质
            if has_dangerous and (
                not cand.get("dangerous_allowed", True) or not port.get("dangerous_allowed", True)
            ):
                continue

            cost = cand["price_per_40ft"]
            time_d = cand["eta_days"]
            reliability = cand["reliability"]

            cost_score = 1 - (cost - min_cost) / (max_cost - min_cost) if max_cost > min_cost else 1.0
            time_score = 1 - (time_d - min_t) / (max_t - min_t) if max_t > min_t else 1.0
            compliance_score = 1 - port["inspection_rate"]

            score = (
                weights["cost"] * cost_score
                + weights["time"] * time_score
                + weights["reliability"] * reliability
                + weights["compliance"] * compliance_score
            )
            s = dict(cand)
            s["score"] = round(score, 4)
            s["inspection_rate"] = port["inspection_rate"]
            s["score_breakdown"] = {
                "cost": round(cost_score, 3),
                "time": round(time_score, 3),
                "reliability": round(reliability, 3),
                "compliance": round(compliance_score, 3),
            }
            scored.append(s)

        return scored, notes

    # ----------------------------------------------------------------- #
    # 合规拦截输出（制裁国 / 全部候选被过滤）
    # ----------------------------------------------------------------- #
    def _blocked_result(self, from_port: str, dest_port: str, risk_flags: List[Dict]) -> AgentResult:
        reasons = [f"[{rf.get('type')}] {rf.get('detail', '')}" for rf in risk_flags]
        if not reasons:
            reasons = ["全部候选航线被合规规则过滤（无满足危品/监管要求的方案）"]
        chain = self._build_chain(from_port, dest_port, risk_flags, chosen=None)
        return AgentResult(
            agent_name=self.name,
            summary=(
                "## 订舱合规拦截\n"
                + "\n".join(f"- 🔴 {r}" for r in reasons)
                + "\n\n**合规决策链**：\n"
                + "\n".join(f"  {i}. {c['step']}：{c['decision']}" for i, c in enumerate(chain, 1))
                + f"\n\n> {AI_BOUNDARY}"
            ),
            data={
                "from_port": from_port, "dest_port": dest_port,
                "blocked": True, "risk_flags": risk_flags,
                "compliance_notes": reasons,
                "compliance_chain": chain, "ai_boundary": AI_BOUNDARY,
            },
            confidence=0.9,
            needs_human_review=True,
        )

    @staticmethod
    def _mark_pareto(scored: List[Dict]) -> None:
        """标注成本-时效双目标帕累托前沿（非支配解）。"""
        for a in scored:
            dominated = False
            for b in scored:
                if b is a:
                    continue
                cheaper = b["price_per_40ft"] <= a["price_per_40ft"]
                faster = b["eta_days"] <= a["eta_days"]
                better = (b["price_per_40ft"] < a["price_per_40ft"]) or (b["eta_days"] < a["eta_days"])
                if cheaper and faster and better:
                    dominated = True
                    break
            a["pareto"] = not dominated

    # ----------------------------------------------------------------- #
    # 合规决策链（C：风险 → 口岸 → 航线 → 单证 → 申报策略）
    # ----------------------------------------------------------------- #
    def _build_chain(
        self, from_port: str, dest_port: str,
        risk_flags: List[Dict], chosen: Optional[Dict],
    ) -> List[Dict[str, str]]:
        chain: List[Dict[str, str]] = []
        types = {rf.get("type") for rf in risk_flags}

        # 1) 风险识别
        if risk_flags:
            detail = "；".join(f"[{rf.get('type')}] {rf.get('detail', '')}" for rf in risk_flags)
        else:
            detail = "未检出显著合规风险"
        chain.append({"step": "风险识别", "detail": detail,
                      "decision": "已纳入合规反推" if risk_flags else "按常规流程"})

        # 2) 口岸选择
        chosen_port = chosen["origin_port"] if chosen else from_port
        ir = PORT_PROFILE.get(chosen_port, {}).get("inspection_rate", 0.2)
        if chosen and chosen_port != from_port:
            decision = f"建议由 {from_port} 改由 {chosen_port} 出运（查验率 {ir:.0%}，风险更低）"
        elif "ip" in types:
            decision = f"{from_port}（查验率 {ir:.0%}）偏高，建议改走低查验口岸或备齐品牌授权"
        elif "dangerous" in types:
            decision = f"{from_port} 须具备危品通道资质（危品泊位/堆场）"
        else:
            decision = f"沿用 {from_port}（查验率 {ir:.0%}）"
        chain.append({"step": "口岸选择", "detail": f"建议口岸 {chosen_port} 查验率 {ir:.0%}", "decision": decision})

        # 3) 航线要求
        if "dangerous" in types:
            route = "仅保留具备危品资质的船公司/航线（UN38.3、MSDS 齐备）"
        elif chosen:
            route = f"选定 {chosen['carrier']} {chosen['vessel']}（{chosen['eta_days']}天）"
        else:
            route = "暂无可用航线（被合规拦截）"
        chain.append({"step": "航线要求", "detail": dest_port, "decision": route})

        # 4) 单证准备（按风险类型反推）
        docs: List[str] = []
        for t in types:
            docs.extend(RISK_DOC_MAP.get(t, []))
        docs = list(dict.fromkeys(docs))  # 去重保序
        doc_str = "、".join(docs) if docs else "常规报关单证"
        chain.append({"step": "单证准备", "detail": doc_str,
                      "decision": f"出运前备齐：{doc_str}"})

        # 5) 申报策略（AI 边界：只建议不代申报）
        if "ip" in types or "value" in types:
            strategy = "如实申报品牌/型号与成交价，避免低报；不确定归类时人工复核"
        else:
            strategy = "按 9610 清单如实申报，保留完整证据链"
        chain.append({"step": "申报策略", "detail": "AI 不输出归类编码、不代申报", "decision": strategy})

        return chain

    # ----------------------------------------------------------------- #
    # ⑦ AI 反事实合规优化：补什么单证 → 解锁什么更优方案
    # ----------------------------------------------------------------- #
    def _counterfactual(
        self, from_port: str, dest_port: str,
        risk_flags: List[Dict], chosen: Optional[Dict], inquiry: Dict,
    ) -> List[Dict[str, str]]:
        """对每类风险给出"反事实"优化建议：若补齐单证，可解锁的更优结果。

        量化方式（不依赖 LLM）：把风险移除后，用用户原指定口岸的航线重打分，
        得到"无风险基线最优"，与当前受风险约束的方案对比成本/时效差。
        """
        if not risk_flags:
            return []

        # 无风险基线：仅用原指定口岸的航线，恢复默认权重
        baseline_best: Optional[Dict] = None
        origin_lanes = LANES.get((from_port, dest_port), [])
        if origin_lanes:
            base_scored, _ = self._score_candidates(
                [{"origin_port": from_port, **o} for o in origin_lanes], [], inquiry
            )
            if base_scored:
                baseline_best = max(base_scored, key=lambda x: x["score"])

        out: List[Dict[str, str]] = []
        for rf in risk_flags:
            t = rf.get("type")
            rem = REMEDIATION_MAP.get(t)
            if not rem:
                continue

            # 确定性量化（LLM 不可用时的回退，也作为可核验事实）
            parts: List[str] = []
            if baseline_best and chosen:
                d_cost = chosen["price_per_40ft"] - baseline_best["price_per_40ft"]
                d_days = chosen["eta_days"] - baseline_best["eta_days"]
                if d_cost:
                    parts.append(
                        f"可省约 ${d_cost}" if d_cost > 0 else f"运价高约 ${-d_cost}"
                    )
                if d_days:
                    parts.append(f"快约 {d_days} 天" if d_days > 0 else f"慢约 {-d_days} 天")

            narrative = self._llm_counterfactual(rf, rem, chosen, baseline_best)
            if narrative:
                benefit = narrative
            else:
                benefit = rem["effect"]
                if parts:
                    benefit += "；相比当前方案 " + "、".join(parts)
            out.append({
                "risk": f"[{t}] {rf.get('detail', '')}",
                "remediation": f"补救：{rem['action']}",
                "benefit": benefit,
            })
        return out

    @staticmethod
    def _llm_counterfactual(
        rf: Dict, rem: Dict, chosen: Optional[Dict], baseline_best: Optional[Dict],
    ) -> Optional[str]:
        """生成"补齐单证→解锁更优结果"的中文叙事（LLM，失败返回 None）。"""
        prompt = (
            "你是跨境物流合规顾问。用 1-2 句中文说明：若货主补齐单证，"
            "可解锁什么更优的订舱结果，尽量点出可量化的成本/时效差。不要杜撰数据。\n\n"
            f"当前合规风险：{rf.get('detail', '')}\n"
            f"补救动作：{rem['action']}\n"
            f"补救效果：{rem['effect']}\n"
        )
        if chosen and baseline_best:
            prompt += (
                f"当前受约束方案：{chosen['origin_port']} {chosen['carrier']} "
                f"${chosen['price_per_40ft']}/{chosen['eta_days']}天\n"
                f"无风险基线方案：{baseline_best['origin_port']} {baseline_best['carrier']} "
                f"${baseline_best['price_per_40ft']}/{baseline_best['eta_days']}天"
            )
        return _llm_chat(prompt, max_tokens=180, temperature=0.3)

    # ----------------------------------------------------------------- #
    # ⑧ AI 议价与守利润建议
    # ----------------------------------------------------------------- #
    def _negotiate(
        self, chosen: Dict, scored: List[Dict], inquiry: Dict, ctx: Dict,
    ) -> Optional[Dict[str, Any]]:
        """基于同口岸报价分布，给出议价路径（开价/目标价/守利润底线）与话术。

        确定性部分：区间、目标价、底线全部由真实候选价算出，不做假设；
        AI 部分：LLM 把区间与筹码润色成可直接发出的议价话术，失败回退模板。
        """
        port = chosen.get("origin_port")
        peers = [s for s in scored if s.get("origin_port") == port] or [chosen]
        prices = sorted(s["price_per_40ft"] for s in peers)
        if not prices:
            return None

        low, high = prices[0], prices[-1]
        cur = chosen["price_per_40ft"]

        # 目标价：只取"不比所选慢超过容差"的可比档里的最低价，避免为省钱牺牲时效
        comparable = [
            s for s in peers
            if s["eta_days"] <= chosen["eta_days"] + NEGOTIATION_ETA_TOLERANCE
        ]
        target = min((s["price_per_40ft"] for s in comparable), default=low)

        # 开局锚点：目标价再压一档，取整到百（避免高于目标价）
        opening = int(round(target * NEGOTIATION_OPEN_DISCOUNT / 100.0)) * 100
        if opening >= target:
            opening = max(0, target - 100)

        # 守利润底线：优先用货主预算/目标运费；否则"不高于当前推荐价"
        budget = ctx.get("budget") or ctx.get("target_freight_usd") or inquiry.get("budget")
        ceiling = int(budget) if budget else cur

        # 谈判筹码（只用已发生的真实事实，不杜撰）
        levers: List[str] = []
        if len(peers) >= 2:
            levers.append(f"同口岸 {len(peers)} 家船司竞争，替代方案充分")
        cheaper = [s for s in peers if s["price_per_40ft"] < cur]
        if cheaper:
            c = min(cheaper, key=lambda x: x["price_per_40ft"])
            levers.append(
                f"存在更省替代 {c['carrier']} ${c['price_per_40ft']}/40ft（{c['eta_days']}天）"
            )
        if len([s for s in peers if s.get("pareto")]) >= 2:
            levers.append("同口岸存在多条帕累托前沿方案，可横向比价")

        script = self._llm_negotiate(
            chosen, port, low, high, opening, target, ceiling, levers, inquiry
        )
        if not script:
            script = (
                f"我方拟订 {port} 至 {inquiry.get('dest_port')} 的 "
                f"{inquiry.get('container_type') or '40ft'}，同期贵司/市场报价区间 "
                f"${low}–${high}。我方可接受 ${target}，若能配合 ${opening} 我方可承诺多柜/"
                f"长期走货；若报价高于守利润底线 ${ceiling}，我方将改选替代方案。"
            )

        return {
            "peer_count": len(peers),
            "market_low": low,
            "market_high": high,
            "current": cur,
            "opening": opening,
            "target": target,
            "ceiling": ceiling,
            "saving_vs_current": cur - target,
            "saving_vs_high": high - target,
            "levers": levers,
            "script": script,
        }

    @staticmethod
    def _llm_negotiate(
        chosen: Dict, port: str, low: int, high: int,
        opening: int, target: int, ceiling: int,
        levers: List[str], inquiry: Dict,
    ) -> Optional[str]:
        """生成可直接发出的议价话术（LLM，失败返回 None → 调用方用模板）。"""
        prompt = (
            "你是跨境物流采购顾问。请用 2-3 句中文，为货主生成一段可直接发给船公司/货代的"
            "议价话术，并点明守利润底线。必须使用下列真实数字，不要杜撰。\n\n"
            f"航线：{port} → {inquiry.get('dest_port')}｜箱型：{inquiry.get('container_type')}\n"
            f"同期同口岸报价区间：${low}–${high}/40ft\n"
            f"所选方案：{chosen['carrier']} {chosen['vessel']}"
            f"（{chosen['eta_days']}天，${chosen['price_per_40ft']}）\n"
            f"开局锚点：${opening}｜目标价：${target}｜守利润底线：${ceiling}\n"
            f"谈判筹码：{'；'.join(levers) if levers else '（无额外筹码）'}"
        )
        return _llm_chat(prompt, max_tokens=220, temperature=0.4)

    # ----------------------------------------------------------------- #
    # 交互闸门：方案匹配 + 订舱指令
    # ----------------------------------------------------------------- #
    @staticmethod
    def _find_option(scored: List[Dict], sel: Any) -> Optional[Dict]:
        """按编号（O1/O2/2）、船公司或船名匹配用户所选方案。"""
        if sel is None:
            return None
        key = str(sel).strip()
        if not key:
            return None
        m = re.fullmatch(r"[oO]?\s*(\d+)", key)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(scored):
                return scored[idx]
        up = key.upper()
        for s in scored:
            if up in s["carrier"].upper() or up in s["vessel"].upper():
                return s
        return None

    @staticmethod
    def _gen_booking_order(
        chosen: Dict, inquiry: Dict, risk_flags: List[Dict], chain: List[Dict],
    ) -> str:
        """生成订舱指令（确定性模板）：航线、承运、运价与合规随附单证。"""
        docs = ""
        for step in chain:
            if step.get("step") == "单证准备":
                docs = step.get("detail", "")
        lines = [
            "【订舱指令 / BOOKING ORDER】",
            f"方案编号：{chosen.get('option_id', '—')}",
            f"起运港：{chosen['origin_port']}    目的港：{inquiry.get('dest_port')}",
            f"箱型：{inquiry.get('container_type')}    货重：{inquiry.get('weight') or '—'}",
            f"货名：{inquiry.get('cargo_name') or '—'}",
            f"承运：{chosen['carrier']} {chosen['vessel']}    航程：约 {chosen['eta_days']} 天",
            f"运价：${chosen['price_per_40ft']}/40ft（议价目标见议价话术）",
        ]
        if docs and docs != "常规报关单证":
            lines.append(f"随附单证：{docs}")
        if risk_flags:
            lines.append(
                "合规提示：" + "；".join(rf.get("detail", "") for rf in risk_flags)
            )
        lines.append("备注：本指令由 AI 生成，须经订舱人确认后发送；不代客申报。")
        return "\n".join(lines)

    # ----------------------------------------------------------------- #
    # ⑥ 推荐理由生成（LLM，回退模板）
    # ----------------------------------------------------------------- #
    def _gen_reason(
        self, chosen: Dict, scored: List[Dict],
        risk_flags: List[Dict], chain: List[Dict], inquiry: Dict,
    ) -> str:
        fallback = (
            f"{chosen['carrier']} {chosen['vessel']}（{chosen['eta_days']}天，"
            f"${chosen['price_per_40ft']}/40ft，可靠性 {chosen['reliability']:.0%}），"
            f"综合分 {chosen['score']:.3f}，在成本/时效/可靠性/合规四维加权下最优"
        )
        if risk_flags:
            fallback += f"；已纳入 {len(risk_flags)} 项合规风险的反推调整"

        options_txt = "\n".join(
            f"- {s['carrier']} {s['vessel']}: {s['eta_days']}天, ${s['price_per_40ft']}, "
            f"可靠性{s['reliability']:.0%}, 综合分{s['score']:.3f}, 帕累托={s.get('pareto')}"
            for s in scored
        )
        chain_txt = "；".join(c["decision"] for c in chain)
        prompt = (
            "你是跨境物流合规顾问。请基于以下信息，用 2-3 句中文说明为什么推荐该方案，"
            "并点出合规反推的关键动作。不要杜撰数据。\n\n"
            f"询价偏好：{inquiry.get('priority')}｜箱型：{inquiry.get('container_type')}\n"
            f"候选方案：\n{options_txt}\n"
            f"推荐：{chosen['carrier']} {chosen['vessel']}\n"
            f"合规决策链：{chain_txt}"
        )
        text = _llm_chat(prompt, max_tokens=250, temperature=0.3)
        return text or fallback

    # ----------------------------------------------------------------- #
    # ③ AI 询价单生成（RFQ）
    # ----------------------------------------------------------------- #
    def _gen_rfq(self, inquiry: Dict, chosen: Dict) -> Optional[str]:
        """生成给船公司的标准询价函。LLM 生成，回退模板。起运港取推荐口岸以保持一致。"""
        origin = chosen.get("origin_port") or inquiry.get("from_port")
        prompt = (
            "请生成一份简洁专业的中文海运订舱询价函（RFQ），用于发给船公司/货代，"
            "包含：起运港、目的港、箱型、货重、货名、期望船期、特殊要求。"
            "只需正文，不要多余解释。\n\n"
            f"起运港：{origin}｜目的港：{inquiry.get('dest_port')}\n"
            f"箱型：{inquiry.get('container_type')}｜货重：{inquiry.get('weight')}\n"
            f"货名：{inquiry.get('cargo_name') or '（未提供）'}｜偏好：{inquiry.get('priority')}\n"
            f"参考船期：{chosen['carrier']} {chosen['vessel']}（{chosen['eta_days']}天）"
        )
        text = _llm_chat(prompt, max_tokens=300, temperature=0.3)
        if text:
            return text
        # 回退模板
        return (
            f"【海运订舱询价函】\n"
            f"起运港：{origin}\n"
            f"目的港：{inquiry.get('dest_port')}\n"
            f"箱型：{inquiry.get('container_type')}\n"
            f"货重：{inquiry.get('weight') or '—'}\n"
            f"货名：{inquiry.get('cargo_name') or '—'}\n"
            f"期望偏好：{inquiry.get('priority')}\n"
            f"请报：海运费、附加费、船期、截关时间。"
        )

    # ----------------------------------------------------------------- #
    # 无直达航线：替代口岸
    # ----------------------------------------------------------------- #
    @staticmethod
    def _no_lane_result(
        from_port: str, dest_port: str, alt_ports: List[str], risk_flags: List[Dict],
    ) -> AgentResult:
        lines = [f"## 未找到 {from_port} → {dest_port} 的直接船期（演示库未覆盖）"]
        if alt_ports:
            lines.append(f"\n**替代口岸建议**：可从以下口岸出运至 {dest_port}：{', '.join(alt_ports)}")
        else:
            lines.append("\n暂无替代口岸，请补充或确认目的港。")
        return AgentResult(
            agent_name="booking",
            summary="\n".join(lines),
            data={"from_port": from_port, "dest_port": dest_port, "alt_ports": alt_ports},
            confidence=0.5,
        )

    # ----------------------------------------------------------------- #
    # 字段提取兜底
    # ----------------------------------------------------------------- #
    @staticmethod
    def _extract_origin_port(query: str, context: Optional[Dict]) -> Optional[str]:
        if context and (context.get("destination_port") or context.get("from_port")):
            return context.get("destination_port") or context.get("from_port")
        for port in PORT_PROFILE.keys():
            if port in query:
                return port
        return None

    @staticmethod
    def _extract_cargo(query: str) -> Optional[str]:
        """规则兜底：从 "<起点>N吨<货名>到<目的港>" / "发N吨<货名>到<目的港>" 中抽取货名。"""
        # ① 含"吨"：以"N吨"为锚点，取其后、目的地词之前的货名（兼容无"发/有/运"前缀）
        m = re.search(r"[\d.]+\s*吨\s*([^，,。\s到至]+?)\s*(?:到|至|去|发往)", query)
        if m:
            cargo = m.group(1).strip()
            if cargo and not cargo.isdigit():
                return cargo
        # ② 无"吨"：兼容"发/有/运<货名>到<目的港>"（货名不带数量）
        m = re.search(r"(?:发|有|运|出口|发货)\s*([^，,。\s到至]+?)\s*(?:到|至|去|发往)", query)
        if m:
            cargo = m.group(1).strip()
            if cargo and not cargo.isdigit():
                return cargo
        return None

    @staticmethod
    def _extract_dest_port(query: str) -> Optional[str]:
        for dest in DESTINATION_PATTERNS + SANCTIONED_DESTS:
            if dest in query:
                return dest
        return None


if __name__ == "__main__":
    agent = BookingAgent()

    # 测试1：普通商品，成本优先
    print("=" * 60)
    print("测试1：普通商品，成本优先")
    res = agent.ask("宁波港订舱到洛杉矶，预算优先", context={
        "destination_port": "宁波港",
        "cargo_name": "硅胶塑料手机壳",
        "weight": 20,
    })
    print(res.summary)
    print()

    # 测试2：IP侵权 → 合规反推
    print("=" * 60)
    print("测试2：IP侵权商品（合规反推）")
    res2 = agent.ask("深圳港订舱到洛杉矶", context={
        "destination_port": "深圳港",
        "cargo_name": "apple iphone 手机壳",
        "weight": 20,
    })
    print(res2.summary)
    print()

    # 测试3：危险品 → 危品通道
    print("=" * 60)
    print("测试3：锂电池危险品")
    res3 = agent.ask("宁波港订舱到洛杉矶，时效优先", context={
        "destination_port": "宁波港",
        "cargo_name": "充电宝锂电池",
        "weight": 5,
    })
    print(res3.summary)
    print()

    # 测试4：制裁国 → 拦截
    print("=" * 60)
    print("测试4：目的国制裁 → 拦截")
    res4 = agent.ask("宁波港订舱到朝鲜", context={
        "destination_port": "宁波港",
        "cargo_name": "硅胶手机壳",
    })
    print(res4.summary)
    print()

    # 测试5：要素缺失 → AI 缺口澄清
    print("=" * 60)
    print("测试5：要素缺失 → 缺口澄清")
    res5 = agent.ask("我要订舱发货，越快越好", context={"cargo_name": "日用百货"})
    print(res5.summary)
    print()

    # 测试6：自然语言预算 → AI 议价与守利润底线
    print("=" * 60)
    print("测试6：自然语言预算 → 议价与守利润")
    res6 = agent.ask("上海港订舱到洛杉矶，预算不超过4000美元", context={
        "destination_port": "上海港",
        "cargo_name": "LED 灯饰",
        "weight": 18,
    })
    print(res6.summary)
    print()

    # 测试7：交互闸门 · 阶段1 提出方案（等待用户选择，不出议价/指令）
    print("=" * 60)
    print("测试7：交互闸门 · 阶段1 提出方案（等待选择）")
    q7 = "深圳港订舱到洛杉矶"
    ctx7 = {"destination_port": "深圳港", "cargo_name": "apple iphone 手机壳", "weight": 20}
    res7 = agent.ask(q7, context={**ctx7, "interactive": True})
    print(res7.summary)
    print()

    # 测试8：交互闸门 · 阶段2 用户选定 O3 → 生成议价 + 订舱指令
    print("=" * 60)
    print("测试8：交互闸门 · 阶段2 用户选定 O3 → 议价 + 订舱指令")
    res8 = agent.ask(q7, context={**ctx7, "interactive": True, "selected_option": "O3"})
    print(res8.summary)

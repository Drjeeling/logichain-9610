"""D 风险预判 Agent。

9610 出口前置风险扫描，作为风险统一出口 risk_flags[]：
1. 知识产权侵权（品牌词匹配，复用 hs_kb.detect_ip_risk）
2. 禁限品 / 危险品 / 出口管制（基于 HS 编码的监管条件 + remark）
3. 货值异常（低报/高报，参考退税率与单价阈值）
4. 目的国制裁 / 禁运（简化版）

接入契约（state.py AGENT_IO_CONTRACT）：
- reads: sku_info, order, hs_code
- writes: risk_flags

不调用 LLM（纯规则 + 知识库匹配）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import BaseAgent, AgentResult

# HS 知识库路径
_HS_DATA_PATH = Path(__file__).resolve().parent.parent / "customs" / "data" / "hs_codes.json"

# 目的国制裁/禁运简化清单（演示用，生产应接商务部公告）
SANCTIONED_COUNTRIES = {
    "朝鲜", "伊朗", "叙利亚", "古巴", "委内瑞拉",
    "north korea", "iran", "syria", "cuba", "venezuela",
}

# 危险品关键词（从 remark 中提取）
DANGEROUS_KEYWORDS = [
    "危险品", "出口管制", "UN38.3", "MSDS", "易制毒",
    "爆炸", "易燃", "有毒", "放射性", "腐蚀",
]

# 低报单价阈值（USD）—— 低于此值触发低报风险
LOW_VALUE_THRESHOLD = 0.5


class RiskScreenAgent(BaseAgent):
    """风险预判 Agent：IP / 禁限 / 货值 / 目的国 四维扫描。"""

    name = "risk_screen"
    description = "9610 出口前置风险扫描：IP/禁限/货值/目的国"

    def __init__(self):
        self._hs_entries: List[Dict] = []
        self._load_hs()

    def _load_hs(self) -> None:
        try:
            with open(_HS_DATA_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._hs_entries = data.get("hs_codes", [])
        except Exception:
            self._hs_entries = []

    def ask(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> AgentResult:
        ctx = context or {}
        sku_info = ctx.get("sku_info") or {}
        order = ctx.get("order") or {}
        # K1 修复：上游可能传字符串税号（如 context.hs_code="3926909090"），
        # 统一归一化为 {"code": ...}，避免按 dict 取值时 AttributeError。
        hs_code = ctx.get("hs_code") or {}
        if isinstance(hs_code, str):
            hs_code = {"code": hs_code}
        elif not isinstance(hs_code, dict):
            hs_code = {}

        cargo_name = sku_info.get("cargo_name") or query or ""
        dest_country = order.get("dest_country") or order.get("dest_port") or ""
        unit_price = order.get("unit_price_usd") or sku_info.get("unit_price_usd")

        risk_flags: List[Dict[str, Any]] = []

        # ---- 1. IP 知识产权风险 ----
        ip_risk = self._check_ip(cargo_name)
        if ip_risk:
            risk_flags.append(ip_risk)

        # ---- 2. 禁限品 / 危险品 / 出口管制 ----
        prohibited = self._check_prohibited(cargo_name, hs_code)
        risk_flags.extend(prohibited)

        # ---- 3. 货值异常 ----
        value_risk = self._check_value(unit_price, hs_code)
        if value_risk:
            risk_flags.append(value_risk)

        # ---- 4. 目的国制裁 / 禁运 ----
        dest_risk = self._check_destination(dest_country)
        if dest_risk:
            risk_flags.append(dest_risk)

        # ---- 汇总 ----
        critical = [r for r in risk_flags if r.get("severity") == "critical"]
        warnings = [r for r in risk_flags if r.get("severity") == "warning"]

        result_data = {
            "risk_flags": risk_flags,
        }

        # ---- summary ----
        lines = [f"## 风险预判结果"]
        lines.append(f"- **扫描维度**：IP / 禁限品 / 货值 / 目的国")
        lines.append(f"- **风险总数**：{len(risk_flags)}（critical: {len(critical)}, warning: {len(warnings)}）")
        if risk_flags:
            for r in risk_flags:
                sev = "🔴" if r["severity"] == "critical" else "🟡"
                lines.append(f"  - {sev} [{r['type']}] {r['detail']}")
                if r.get("advice"):
                    lines.append(f"    → 建议：{r['advice']}")
        else:
            lines.append("- **风险**：未检出明显风险 ✓")

        return AgentResult(
            agent_name=self.name,
            summary="\n".join(lines),
            data=result_data,
            next_hint=(
                "未检出风险，可继续申报" if not critical
                else "存在严重风险，建议人工复核后再申报"
            ),
            confidence=0.9 if not risk_flags else 0.7,
            needs_human_review=bool(critical),
        )

    # ----------------------------------------------------------------- #
    # 1. IP 知识产权风险
    # ----------------------------------------------------------------- #
    @staticmethod
    def _check_ip(cargo_name: str) -> Optional[Dict]:
        try:
            from customs.hs_kb import HSKnowledgeBase
            ip = HSKnowledgeBase.detect_ip_risk(cargo_name)
            if ip and ip.get("is_risky"):
                return {
                    "type": "ip",
                    "severity": "critical",
                    "detail": f"命中品牌词：{', '.join(ip.get('matched_brands', []))}",
                    "advice": ip.get("advice", "需提供品牌授权证明"),
                }
        except Exception:
            pass
        return None

    # ----------------------------------------------------------------- #
    # 2. 禁限品 / 危险品 / 出口管制
    # ----------------------------------------------------------------- #
    def _check_prohibited(self, cargo_name: str, hs_code: Dict) -> List[Dict]:
        risks: List[Dict] = []
        code = hs_code.get("code") if hs_code else None
        entry = None

        if code:
            entry = next((e for e in self._hs_entries if e.get("code") == code), None)

        if entry:
            # 监管证件代码检查
            reg_code = entry.get("regulatory_code") or entry.get("supervision_code") or ""
            if reg_code and reg_code != "无":
                risks.append({
                    "type": "prohibited",
                    "severity": "warning",
                    "detail": f"HS {code} 监管证件代码为 {reg_code}，出口需提供相应监管证件",
                    "advice": f"核查 {reg_code} 对应的监管证件是否齐全（如 A=入境通关单、B=出口许可等）",
                })

            # remark 中的危险品/出口管制
            remark = entry.get("remark") or ""
            for kw in DANGEROUS_KEYWORDS:
                if kw in remark:
                    risks.append({
                        "type": "dangerous",
                        "severity": "critical",
                        "detail": f"HS {code} 涉及 {kw}（{remark[:60]}）",
                        "advice": "需提供相应安全鉴定报告（如 UN38.3/MSDS），并按危险品申报",
                    })
                    break

        # 品名直接命中带电/危险品关键词（锂电池等，F3 禁限·带电）
        for kw in ["锂电池", "锂离子", "充电宝", "蓄电池", "电池", "移动电源"]:
            if kw in cargo_name:
                risks.append({
                    "type": "dangerous",
                    "severity": "critical",
                    "detail": f"品名 '{cargo_name}' 命中带电/危险品词 '{kw}'",
                    "advice": "须按危险品申报，提供 UN38.3 测试报告与 MSDS",
                })
                break

        # 品名直接命中危险品关键词
        for kw in ["爆炸物", "武器", "枪支", "毒品", "象牙", "犀牛角"]:
            if kw in cargo_name:
                risks.append({
                    "type": "prohibited",
                    "severity": "critical",
                    "detail": f"品名 '{cargo_name}' 命中禁限词 '{kw}'",
                    "advice": "该商品属于禁止出口物品，不予受理申报",
                })
                break

        return risks

    # ----------------------------------------------------------------- #
    # 3. 货值异常
    # ----------------------------------------------------------------- #
    @staticmethod
    def _check_value(unit_price: Any, hs_code: Dict) -> Optional[Dict]:
        if unit_price is None:
            return None
        try:
            price = float(unit_price)
        except (TypeError, ValueError):
            return None

        # 单价为 0/缺失视为"未申报单价"，不作为低报依据
        if price <= 0:
            return None

        # 低报检测
        if price < LOW_VALUE_THRESHOLD:
            return {
                "type": "value",
                "severity": "warning",
                "detail": f"申报单价 ${price:.2f} 低于阈值 ${LOW_VALUE_THRESHOLD}，疑似低报",
                "advice": "核实成交价格，低报可能导致海关估价调整或处罚",
            }

        # 高报检测（简单阈值，>1000 USD/单位 警告）
        if price > 1000:
            return {
                "type": "value",
                "severity": "warning",
                "detail": f"申报单价 ${price:.2f} 偏高，需核实是否如实申报",
                "advice": "高报可能涉及骗取出口退税，需提供成交证明",
            }

        return None

    # ----------------------------------------------------------------- #
    # 4. 目的国制裁 / 禁运
    # ----------------------------------------------------------------- #
    @staticmethod
    def _check_destination(dest_country: str) -> Optional[Dict]:
        if not dest_country:
            return None
        dc = dest_country.strip().lower()
        for s in SANCTIONED_COUNTRIES:
            if s.lower() in dc:
                return {
                    "type": "destination",
                    "severity": "critical",
                    "detail": f"目的国 {dest_country} 属制裁/禁运国家",
                    "advice": "需向商务部申请出口许可证，否则不予出口",
                }
        return None


if __name__ == "__main__":
    agent = RiskScreenAgent()

    # 测试1：无风险
    print("=" * 50)
    print("测试1：普通商品无风险")
    res = agent.ask("报关", context={
        "sku_info": {"cargo_name": "硅胶塑料手机壳"},
        "order": {"dest_country": "美国", "unit_price_usd": 5.0},
    })
    print(res.summary)
    print()

    # 测试2：IP侵权
    print("=" * 50)
    print("测试2：苹果手机壳 IP侵权")
    res2 = agent.ask("报关", context={
        "sku_info": {"cargo_name": "apple iphone 手机壳"},
        "order": {"dest_country": "美国", "unit_price_usd": 5.0},
    })
    print(res2.summary)
    print()

    # 测试3：危险品 + 低报
    print("=" * 50)
    print("测试3：锂电池危险品 + 低报")
    res3 = agent.ask("报关", context={
        "sku_info": {"cargo_name": "充电宝"},
        "order": {"dest_country": "美国", "unit_price_usd": 0.3},
        "hs_code": {"code": "8507600090"},
    })
    print(res3.summary)
    print()

    # 测试4：制裁国家
    print("=" * 50)
    print("测试4：目的国制裁")
    res4 = agent.ask("报关", context={
        "sku_info": {"cargo_name": "硅胶手机壳"},
        "order": {"dest_country": "朝鲜", "unit_price_usd": 5.0},
    })
    print(res4.summary)

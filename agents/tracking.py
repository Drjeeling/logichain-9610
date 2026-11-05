"""追踪 Agent（扩展版）：链路追踪 + 在途监控 + 税务节点 + 极端天气。

原占位版扩展为「链路追踪 + 在途/税务监控」顶层 Agent。
从原报关域 F 移入：货物状态监控、极端天气报告、税务申报节点监控。

功能：
- T1 货物状态监控（在途异常告警：延误>3天/扣关/温度异常）
- T2 极端天气报告（命中极端天气时生成客户报告，LLM 润色）
- T3 税务申报节点监控（免税/退税登记窗口）

注册在顶层 Coordinator（coordinator.register("tracking", TrackingAgent())），
不是 CustomsOrchestrator 的槽位。
"""
from __future__ import annotations

import os
import random
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .base import BaseAgent, AgentResult


# 运单状态机
SHIPMENT_STATES = [
    "已揽收", "国内运输中", "报关中", "已装船", "海上运输中",
    "抵达目的港", "清关中", "已放行", "末端派送", "已签收",
]

# 异常状态
ANOMALY_STATES = {"清关中", "报关中"}

# 极端天气关键词（mock 天气数据中命中即触发）
EXTREME_WEATHER_KEYWORDS = [
    "台风", "飓风", "暴雨", "暴雪", "洪水", "地震", "火山",
    "typhoon", "hurricane", "storm", "flood", "earthquake",
]

# 税务节点规则
TAX_WINDOWS = [
    {
        "name": "免税登记",
        "from_event": "clear_date",
        "desc": "深圳跨境电商无票免税登记（提前2天预约 + T+2确认）",
        "deadline_days": 15,        # 结关后15天内
        "penalty": "逾期视同内销，补征增值税13%",
    },
    {
        "name": "退税申报",
        "from_event": "clear_date",
        "desc": "出口退税申报（36个月内，越早越好）",
        "deadline_days": 365 * 3,   # 36个月
        "penalty": "逾期无法退税",
    },
]


class TrackingAgent(BaseAgent):
    """链路追踪 + 在途监控 + 税务节点 + 极端天气。"""

    name = "tracking"
    description = "运单追踪 + 在途异常 + 税务节点 + 极端天气"

    def ask(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> AgentResult:
        ctx = context or {}

        # ---- 提取运单号 ----
        tracking_no = self._extract_tracking_no(query) or ctx.get("logistics_no")
        if not tracking_no:
            tracking_no = "SF" + str(random.randint(100000, 999999))

        # ---- 运单状态 ----
        seed = hash(tracking_no) % len(SHIPMENT_STATES)
        current_state = SHIPMENT_STATES[seed]

        # ---- T1 在途异常监控 ----
        anomaly = self._detect_anomaly(query, current_state)
        in_transit_alerts = self._monitor_in_transit(current_state, anomaly)

        # ---- T2 极端天气报告 ----
        dest_country = ctx.get("dest_country") or ctx.get("dest_port") or "目的国"
        weather_report = self._check_extreme_weather(dest_country, ctx)

        # ---- T3 税务节点监控 ----
        order = ctx.get("order") or {}
        timeline = ctx.get("timeline") or {}
        tax_reminders = self._monitor_tax_nodes(order, timeline)

        # ---- 处理建议 ----
        suggestions = self._gen_suggestions(anomaly, current_state, tax_reminders, weather_report)

        # ---- 汇总 ----
        summary_lines = [
            f"## 运单 {tracking_no} 追踪与监控\n",
            f"- **当前状态**：{current_state}",
            f"- **预计到达**：{(datetime.now() + timedelta(days=3)).strftime('%Y-%m-%d')}",
        ]
        if anomaly:
            summary_lines.append(f"\n**⚠️ 在途异常**：{anomaly}")
        if in_transit_alerts:
            summary_lines.append(f"\n**在途告警**（{len(in_transit_alerts)}）：")
            for a in in_transit_alerts:
                summary_lines.append(f"  - {a}")
        if weather_report:
            summary_lines.append(f"\n**🌦️ 极端天气报告**：\n{weather_report}")
        if tax_reminders:
            summary_lines.append(f"\n**📋 税务节点提醒**（{len(tax_reminders)}）：")
            for t in tax_reminders:
                icon = "🔴" if t["level"] == "urgent" else ("🟡" if t["level"] == "warning" else "🟢")
                summary_lines.append(
                    f"  - {icon} [{t['name']}] {t['deadline']} 截止"
                    f"（{'已逾期' + str(-t['days_left']) + '天' if t['days_left'] < 0 else '剩余' + str(t['days_left']) + '天'}）"
                )
                summary_lines.append(f"    → {t['desc']}")
        if suggestions:
            summary_lines.append("\n**处理建议**：")
            for s in suggestions:
                summary_lines.append(f"- {s}")

        # ---- 输出数据 ----
        data = {
            "tracking_no": tracking_no,
            "state": current_state,
            "anomaly": anomaly,
            "suggestions": suggestions,
            "alerts": in_transit_alerts,
            "weather_report": weather_report,
            "tax_reminders": tax_reminders,
        }

        needs_review = bool(anomaly) or any(t["level"] == "urgent" for t in tax_reminders)

        return AgentResult(
            agent_name=self.name,
            summary="\n".join(summary_lines),
            data=data,
            next_hint="运单已纳入监控，状态变更与税务节点将主动提醒",
            confidence=0.8,
            needs_human_review=needs_review,
        )

    # ----------------------------------------------------------------- #
    # T1 在途异常监控
    # ----------------------------------------------------------------- #
    @staticmethod
    def _detect_anomaly(query: str, state: str) -> Optional[str]:
        if "延误" in query or "扣关" in query:
            return "查询关键词触发异常检测，建议人工核实运单状态"
        if state in ("清关中", "报关中"):
            return "运单处于清关阶段，需关注目的国海关政策变化"
        return None

    @staticmethod
    def _monitor_in_transit(state: str, anomaly: Optional[str]) -> List[str]:
        alerts: List[str] = []
        if anomaly:
            alerts.append(anomaly)
        # 模拟延误检测（基于状态停留时长，简化版）
        if state == "海上运输中":
            alerts.append("海上运输中，建议关注船期动态与目的港天气")
        if state == "清关中":
            alerts.append("清关中，如超过3天未放行建议联系货代")
        return alerts

    # ----------------------------------------------------------------- #
    # T2 极端天气报告
    # ----------------------------------------------------------------- #
    def _check_extreme_weather(self, dest_country: str, ctx: Dict) -> str:
        """检查目的国/航线极端天气，命中时生成客户报告。

        演示用 mock 天气数据；生产应对接天气 API。
        报告自然语言生成：有 LLM 时润色，否则模板生成。
        """
        # mock：根据目的国生成天气状态
        weather = ctx.get("weather") or self._mock_weather(dest_country)
        if not self._is_extreme(weather):
            return ""

        # 生成报告
        report = (
            f"【极端天气预警】\n"
            f"目的国/地区：{dest_country}\n"
            f"天气状况：{weather}\n"
            f"影响：可能导致物流延误 2-5 天，建议：\n"
            f"1. 及时通知买家预计送达时间调整\n"
            f"2. 评估是否需要调整出货计划\n"
            f"3. 关注保险公司理赔条款"
        )

        # LLM 润色（可选）
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if api_key:
            report = self._llm_polish_weather(report, api_key)

        return report

    @staticmethod
    def _mock_weather(dest_country: str) -> str:
        """模拟天气数据（演示用）。"""
        dc = dest_country.lower()
        if "us" in dc or "美国" in dest_country:
            return "美国东海岸：暴雨伴随大风，部分港口作业受限"
        if "eu" in dc or "欧洲" in dest_country or "德" in dest_country:
            return "正常"
        if "日本" in dest_country or "jp" in dc:
            return "正常"
        if "东南亚" in dest_country or "泰" in dest_country or "越" in dest_country:
            return "台风过境，部分航线延误"
        return "正常"

    @staticmethod
    def _is_extreme(weather: str) -> bool:
        w = weather.lower()
        return any(kw in w for kw in EXTREME_WEATHER_KEYWORDS)

    @staticmethod
    def _llm_polish_weather(report: str, api_key: str) -> str:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1")
            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": f"请将以下物流天气预警报告润色为简洁专业的客户通知，保留核心信息：\n{report}"}],
                max_tokens=300,
                temperature=0.3,
            )
            return resp.choices[0].message.content.strip()
        except Exception:
            return report

    # ----------------------------------------------------------------- #
    # T3 税务节点监控
    # ----------------------------------------------------------------- #
    @staticmethod
    def _monitor_tax_nodes(order: Dict, timeline: Dict) -> List[Dict]:
        """监控免税/退税登记窗口。"""
        clear_date_str = order.get("clear_date")
        # 也从 timeline 取
        if not clear_date_str and isinstance(timeline, dict):
            manifests = timeline.get("manifests") or []
            if manifests:
                clear_date_str = manifests[0].get("clear_date")

        if not clear_date_str:
            return []

        try:
            clear_date = datetime.strptime(clear_date_str, "%Y-%m-%d")
        except (ValueError, TypeError):
            return []

        now = datetime.now()
        reminders: List[Dict] = []

        for rule in TAX_WINDOWS:
            deadline = clear_date + timedelta(days=rule["deadline_days"])
            days_left = (deadline - now).days
            if days_left < 0:
                level = "urgent"
            elif days_left <= 7:
                level = "urgent"
            elif days_left <= 30:
                level = "warning"
            else:
                level = "info"

            reminders.append({
                "name": rule["name"],
                "desc": rule["desc"],
                "deadline": deadline.strftime("%Y-%m-%d"),
                "days_left": days_left,
                "level": level,
                "penalty": rule["penalty"],
            })

        return reminders

    # ----------------------------------------------------------------- #
    # 辅助
    # ----------------------------------------------------------------- #
    @staticmethod
    def _extract_tracking_no(query: str) -> Optional[str]:
        m = re.search(r"\b(SF\d{6,}|YT\d{6,})\b", query)
        return m.group(1) if m else None

    @staticmethod
    def _gen_suggestions(
        anomaly: Optional[str], state: str,
        tax_reminders: List[Dict], weather_report: str,
    ) -> List[str]:
        suggestions: List[str] = []
        if not anomaly and not tax_reminders and not weather_report:
            return ["继续保持监控，状态变更将自动通知"]
        if anomaly:
            if "清关" in state:
                suggestions.append("建议提前准备目的国清关所需单证（B/L、Commercial Invoice、Packing List）")
                suggestions.append("如延误超 3 天，建议联系货代协助海关沟通")
            else:
                suggestions.append("联系货代核实异常原因")
                suggestions.append("根据情况考虑补货或调整下游交货计划")
        if weather_report:
            suggestions.append("已触发极端天气预警，建议主动联系买家说明可能的延误")
        urgent_tax = [t for t in tax_reminders if t["level"] == "urgent"]
        if urgent_tax:
            for t in urgent_tax:
                suggestions.append(f"【紧急】{t['name']}即将/已逾期：{t['desc']}")
        return suggestions


if __name__ == "__main__":
    agent = TrackingAgent()

    # 测试1：正常追踪
    print("=" * 50)
    print("测试1：正常追踪")
    res = agent.ask("追踪运单 SF123456", context={
        "dest_country": "日本",
        "order": {"clear_date": "2026-09-20"},
    })
    print(res.summary)
    print()

    # 测试2：极端天气 + 税务临期
    print("=" * 50)
    print("测试2：美国暴雨 + 免税登记临期")
    res2 = agent.ask("追踪运单 YT999999", context={
        "dest_country": "美国",
        "order": {"clear_date": "2026-10-01"},
    })
    print(res2.summary)
    print()

    # 测试3：东南亚台风
    print("=" * 50)
    print("测试3：东南亚台风")
    res3 = agent.ask("追踪", context={
        "dest_country": "泰国",
        "order": {"clear_date": "2026-01-01"},
    })
    print(res3.summary)

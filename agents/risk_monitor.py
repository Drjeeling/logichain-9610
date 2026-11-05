"""F 节点监控 Agent（报关域内）。

守护报关链路内的确定性时间节点，错过代价 = 视同内销补税 13%：
- 结关 → 汇总申报（次月 15 日前）
- 结关 → 退运（6 个月内，2026 年第 16 号公告）

临期自动升级：info → warning → urgent。

注意：货物在途状态、极端天气、免税/退税登记窗口属于"生命周期监控"，
已移入顶层 tracking Agent（任务卡 G），本 Agent 不包含。

接入契约（state.py AGENT_IO_CONTRACT）：
- reads: timeline, order
- writes: timeline

不调用 LLM（纯日历引擎 + 规则）。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .base import BaseAgent, AgentResult


# 报关链路时间窗规则
# rule: {name, from_status, window_days, deadline_text}
TIME_WINDOWS = [
    {
        "name": "汇总申报",
        "from_status": "departed",       # 结关/离境后
        "window_days": None,              # 次月15日前，特殊计算
        "deadline_text": "次月15日前",
        "severity_if_missed": "critical",
        "penalty": "逾期视同内销，补征增值税13%",
    },
    {
        "name": "退运申报",
        "from_status": "departed",
        "window_days": 180,               # 6个月
        "deadline_text": "6个月内",
        "severity_if_missed": "warning",
        "penalty": "逾期无法退运，需按一般贸易征税",
    },
]


class RiskMonitorAgent(BaseAgent):
    """报关链路节点监控 Agent：时间窗守护 + 临期提醒。"""

    name = "risk_monitor"
    description = "报关链路时间窗监控：汇总申报/退运临期提醒"

    def ask(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> AgentResult:
        ctx = context or {}
        timeline = ctx.get("timeline") or {}
        order = ctx.get("order") or {}

        now = now or datetime.now()

        # ---- 解析现有 timeline 中的清单 ----
        manifests = self._parse_manifests(timeline, order)

        alerts: List[Dict[str, Any]] = []

        for mf in manifests:
            clear_date = mf.get("clear_date")
            if not clear_date:
                continue

            try:
                clear_dt = datetime.strptime(clear_date, "%Y-%m-%d")
            except (ValueError, TypeError):
                continue

            for rule in TIME_WINDOWS:
                deadline = self._calc_deadline(clear_dt, rule)
                days_left = (deadline - now).days
                level = self._level_by_days(days_left)

                alerts.append({
                    "manifest_id": mf.get("manifest_id", "unknown"),
                    "status": mf.get("status", ""),
                    "window": rule["name"],
                    "clear_date": clear_date,
                    "next_deadline": deadline.strftime("%Y-%m-%d"),
                    "days_left": days_left,
                    "level": level,
                    "penalty": rule["penalty"],
                })

        # ---- 汇总 ----
        urgent = [a for a in alerts if a["level"] == "urgent"]
        warning = [a for a in alerts if a["level"] == "warning"]

        result_data = {
            "timeline": {
                "manifests": manifests,
                "alerts": alerts,
                "checked_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            }
        }

        # ---- summary ----
        lines = [f"## 报关链路节点监控"]
        lines.append(f"- **监控节点**：汇总申报（次月15日前）、退运（6个月内）")
        lines.append(f"- **监控清单数**：{len(manifests)}")
        lines.append(f"- **提醒总数**：{len(alerts)}（urgent: {len(urgent)}, warning: {len(warning)}）")

        if alerts:
            lines.append("- **节点明细**：")
            for a in alerts:
                icon = "🔴" if a["level"] == "urgent" else ("🟡" if a["level"] == "warning" else "🟢")
                status_text = (
                    f"已逾期 {-a['days_left']} 天" if a["days_left"] < 0
                    else f"剩余 {a['days_left']} 天"
                )
                lines.append(
                    f"  - {icon} [{a['manifest_id']}] {a['window']}："
                    f"{a['next_deadline']} 截止（{status_text}）"
                )
                if a["level"] != "info":
                    lines.append(f"    ⚠️ 逾期后果：{a['penalty']}")
        else:
            lines.append("- **提醒**：无待办节点")

        needs_review = bool(urgent)

        return AgentResult(
            agent_name=self.name,
            summary="\n".join(lines),
            data=result_data,
            next_hint=(
                "所有节点正常" if not urgent
                else "存在临期/逾期节点，请立即处理"
            ),
            confidence=0.95,
            needs_human_review=needs_review,
        )

    # ----------------------------------------------------------------- #
    # 解析清单
    # ----------------------------------------------------------------- #
    @staticmethod
    def _parse_manifests(timeline: Dict, order: Dict) -> List[Dict]:
        manifests = timeline.get("manifests") if isinstance(timeline, dict) else None
        if manifests:
            return manifests
        # 兜底：从 order 构造单条清单
        mf = {
            "manifest_id": order.get("order_id") or "MANIFEST-001",
            "status": order.get("status") or "departed",
            "clear_date": order.get("clear_date"),
        }
        return [mf] if mf["clear_date"] else []

    # ----------------------------------------------------------------- #
    # 截止日期计算
    # ----------------------------------------------------------------- #
    @staticmethod
    def _calc_deadline(clear_dt: datetime, rule: Dict) -> datetime:
        """根据规则计算截止日期。"""
        if rule["name"] == "汇总申报":
            # 次月15日前
            if clear_dt.month == 12:
                year, month = clear_dt.year + 1, 1
            else:
                year, month = clear_dt.year, clear_dt.month + 1
            return datetime(year, month, 15, 23, 59, 59)
        else:
            # 固定天数窗口
            return clear_dt + timedelta(days=rule["window_days"])

    @staticmethod
    def _level_by_days(days_left: int) -> str:
        if days_left < 0:
            return "urgent"
        elif days_left <= 3:
            return "urgent"
        elif days_left <= 7:
            return "warning"
        else:
            return "info"


if __name__ == "__main__":
    import datetime as dt

    agent = RiskMonitorAgent()

    # 测试1：临期（距次月15日还有5天）
    print("=" * 50)
    print("测试1：临期汇总申报（2026-10-10，结关2026-09-20，截止2026-10-15）")
    res = agent.ask("监控", context={
        "order": {"order_id": "ORD-001", "clear_date": "2026-09-20", "status": "departed"},
    }, now=dt.datetime(2026, 10, 10))
    print(res.summary)
    print()

    # 测试2：逾期
    print("=" * 50)
    print("测试2：逾期汇总申报（2026-10-20，截止2026-10-15已过）")
    res2 = agent.ask("监控", context={
        "order": {"order_id": "ORD-002", "clear_date": "2026-09-20", "status": "departed"},
    }, now=dt.datetime(2026, 10, 20))
    print(res2.summary)
    print()

    # 测试3：正常（还有20天）
    print("=" * 50)
    print("测试3：正常（2026-09-25，截止2026-10-15，剩20天）")
    res3 = agent.ask("监控", context={
        "order": {"order_id": "ORD-003", "clear_date": "2026-09-20", "status": "departed"},
    }, now=dt.datetime(2026, 9, 25))
    print(res3.summary)

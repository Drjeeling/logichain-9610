"""Function Calling 工具定义：让 DeepSeek Agent 能调用图搜索与评分器。

每个工具包含：
- name: 工具名（与 DeepSeek function calling 的 name 对应）
- description: 工具描述（给 LLM 看）
- parameters: 入参 JSON Schema
- handler: 实际执行函数

Agent 主体通过 dispatch_tool(name, args) 路由到对应 handler。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from core.graph import MultimodalGraph, RoutePlan
from core.scorer import RouteScorer, ScoredPlan
from core.seasonal import SeasonalConfig


# --------------------------------------------------------------------------- #
# 工具元信息
# --------------------------------------------------------------------------- #
TOOL_FIND_ROUTES = {
    "name": "find_routes",
    "description": (
        "查询起点到终点所有可行的多式联运路径。返回最多 K 条候选路径，"
        "每条路径包含节点序列、各段运输方式、各段运费/时效/碳排放。"
        "支持铁路、公路、河运三种方式及其组合。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "origin": {"type": "string", "description": "起点节点ID，如 '义乌'"},
            "destination": {"type": "string", "description": "终点节点ID，如 '宁波港'"},
            "k": {"type": "integer", "description": "返回的最大候选路径数", "default": 5},
        },
        "required": ["origin", "destination"],
    },
}

TOOL_SCORE_ROUTES = {
    "name": "score_routes",
    "description": (
        "对候选路径做多目标评分与帕累托前沿筛选，按权重输出排序后的方案。"
        "支持三种权重：cost_priority(省钱优先)/time_priority(时效优先)/balanced(折中)。"
        "返回每条路径的运费、时效、碳排放、综合分、是否在帕累托前沿、推荐标签。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "origin": {"type": "string", "description": "起点节点ID"},
            "destination": {"type": "string", "description": "终点节点ID"},
            "weight": {
                "type": "string",
                "enum": ["cost_priority", "time_priority", "balanced"],
                "default": "balanced",
                "description": "评分权重偏好",
            },
        },
        "required": ["origin", "destination"],
    },
}

TOOL_SEASONAL_FACTOR = {
    "name": "get_seasonal_factor",
    "description": (
        "查询指定月份、指定运输方式的季节系数。"
        "返回月度系数、运输方式系数和最终倍数，说明当前是否处于运价上浮/下调期。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "month": {"type": "integer", "description": "月份 1-12"},
            "mode": {
                "type": "string",
                "enum": ["rail", "river", "truck"],
                "description": "运输方式",
            },
        },
        "required": ["month", "mode"],
    },
}

TOOL_LIST_NODES = {
    "name": "list_nodes",
    "description": "列出图上所有可用节点，方便用户确认起讫地拼写。",
    "parameters": {"type": "object", "properties": {}},
}

ALL_TOOL_SCHEMAS = [
    TOOL_FIND_ROUTES,
    TOOL_SCORE_ROUTES,
    TOOL_SEASONAL_FACTOR,
    TOOL_LIST_NODES,
]


# --------------------------------------------------------------------------- #
# 工具执行器
# --------------------------------------------------------------------------- #
class ToolRegistry:
    """工具注册表，把工具名映射到实际 handler。"""

    def __init__(self, graph: MultimodalGraph, seasonal: SeasonalConfig):
        self.graph = graph
        self.seasonal = seasonal
        self._handlers: Dict[str, Callable[..., Dict[str, Any]]] = {
            "find_routes": self._find_routes,
            "score_routes": self._score_routes,
            "get_seasonal_factor": self._seasonal_factor,
            "list_nodes": self._list_nodes,
        }

    # ----- 路由入口 ----- #
    def dispatch(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        handler = self._handlers.get(name)
        if handler is None:
            return {"error": f"未知工具: {name}"}
        try:
            return handler(**(args or {}))
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    # ----- 工具实现 ----- #
    def _list_nodes(self) -> Dict[str, Any]:
        return {"nodes": self.graph.nodes()}

    def _find_routes(
        self,
        origin: str,
        destination: str,
        k: int = 5,
    ) -> Dict[str, Any]:
        plans = self.graph.find_k_shortest_routes(origin, destination, k=k)
        return {
            "origin": origin,
            "destination": destination,
            "count": len(plans),
            "routes": [self._plan_to_dict(p) for p in plans],
        }

    def _score_routes(
        self,
        origin: str,
        destination: str,
        weight: str = "balanced",
    ) -> Dict[str, Any]:
        plans = self.graph.find_k_shortest_routes(origin, destination, k=8)
        if not plans:
            return {
                "origin": origin,
                "destination": destination,
                "error": "未找到可行路径，请检查起讫地是否在节点列表中",
            }
        scorer = RouteScorer(weight=weight)
        scored = scorer.score(plans)
        return {
            "origin": origin,
            "destination": destination,
            "weight": weight,
            "total_routes": len(scored),
            "recommended": [
                self._scored_to_dict(s) for s in scored if s.recommend_tag
            ],
            "all_routes": [self._scored_to_dict(s) for s in scored],
        }

    def _seasonal_factor(self, month: int, mode: str) -> Dict[str, Any]:
        month_w = self.seasonal.monthly_factor.get(month, 1.0)
        mode_w = self.seasonal.mode_multiplier.get(mode, 1.0)
        return {
            "month": month,
            "mode": mode,
            "month_factor": month_w,
            "mode_multiplier": mode_w,
            "final_factor": month_w * mode_w,
            "explanation": self._seasonal_explain(month, mode, month_w, mode_w),
        }

    # ----- 序列化辅助 ----- #
    @staticmethod
    def _plan_to_dict(p: RoutePlan) -> Dict[str, Any]:
        return {
            "path": p.path_summary,
            "modes": p.modes_summary,
            "total_cost_per_ton": round(p.total_cost, 2),
            "total_hours": round(p.total_hours, 1),
            "total_carbon_g_per_ton": round(p.total_carbon_g, 1),
            "segments": [
                {
                    "from": s.from_id, "to": s.to_id, "mode": s.mode,
                    "distance_km": s.distance_km,
                    "price_per_ton": round(s.price_per_ton, 2),
                    "hours": s.hours,
                    "frequency": s.frequency,
                    "transfer_cost": s.transfer_cost,
                    "transfer_hours": s.transfer_hours,
                }
                for s in p.segments
            ],
        }

    @staticmethod
    def _scored_to_dict(s: ScoredPlan) -> Dict[str, Any]:
        return {
            "path": s.plan.path_summary,
            "modes": s.plan.modes_summary,
            "cost_per_ton": round(s.cost, 2),
            "hours": round(s.hours, 1),
            "carbon_g_per_ton": round(s.carbon_g, 1),
            "score": round(s.score, 3),
            "is_pareto": s.is_pareto,
            "tag": s.recommend_tag,
            # 新增：成本明细和季节说明
            "cost_breakdown_str": s.plan.cost_breakdown_str,
            "seasonal_notes": getattr(s.plan, "seasonal_notes", []),
            "l1_freight": round(s.plan.l1_freight, 2),
            "l2_transfer": round(s.plan.l2_transfer, 2),
            "l3_demurrage": round(s.plan.l3_demurrage, 2),
            "l4_risk_reserve": round(s.plan.l4_risk_reserve, 2),
        }

    @staticmethod
    def _seasonal_explain(month: int, mode: str, m_w: float, mode_w: float) -> str:
        mode_cn = {"rail": "铁路", "river": "河运", "truck": "公路"}
        if m_w > 1.15:
            season = "旺季，运价上浮"
        elif m_w < 0.95:
            season = "淡季，运价回落"
        else:
            season = "平稳期"
        return f"{month}月{mode_cn.get(mode, mode)}处于{season}（系数 {m_w:.2f}），运输方式系数 {mode_w:.2f}"

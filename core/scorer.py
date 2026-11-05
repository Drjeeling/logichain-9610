"""多目标评分与帕累托前沿：从所有候选路径中筛选最优方案。

支持三种推荐策略：
- cost_priority   : 省钱优先（运费权重 0.7 / 时效 0.3）
- time_priority   : 时效优先（运费权重 0.3 / 时效 0.7）
- balanced        : 折中（运费 0.5 / 时效 0.5）

先用帕累托前沿剔除"既贵又慢"的方案，再按权重打分排序。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

try:
    from paretoset import paretoset
    _HAS_PARETO = True
except ImportError:
    _HAS_PARETO = False

from .graph import RoutePlan


WEIGHT_PRESETS = {
    "cost_priority": (0.7, 0.3),
    "time_priority": (0.3, 0.7),
    "balanced":      (0.5, 0.5),
}


@dataclass
class ScoredPlan:
    plan: RoutePlan
    cost: float           # 总运费（元/吨）
    hours: float          # 总时效（小时）
    carbon_g: float       # 总碳排（g/吨）
    score: float          # 综合分（0~1，越小越优）
    is_pareto: bool       # 是否在帕累托前沿
    recommend_tag: str    # 标签：最省钱/最快/折中


class RouteScorer:
    """路径评分器：归一化 + 帕累托前沿 + 加权评分。"""

    def __init__(self, weight: str = "balanced"):
        if weight not in WEIGHT_PRESETS:
            raise ValueError(f"未知权重: {weight}, 可选: {list(WEIGHT_PRESETS)}")
        self.w_cost, self.w_time = WEIGHT_PRESETS[weight]

    def score(self, plans: List[RoutePlan]) -> List[ScoredPlan]:
        if not plans:
            return []

        # 1. 计算各方案的原始指标
        costs = [p.total_cost for p in plans]
        hours = [p.total_hours for p in plans]
        carbons = [p.total_carbon_g for p in plans]

        max_cost = max(costs) or 1.0
        max_hours = max(hours) or 1.0

        # 2. 帕累托前沿：剔除"既贵又慢"的方案（cost 与 hours 都更差）
        is_pareto = self._compute_pareto(costs, hours)

        # 3. 归一化加权评分（越小越优）
        scores = []
        for c, h in zip(costs, hours):
            s = self.w_cost * (c / max_cost) + self.w_time * (h / max_hours)
            scores.append(s)

        # 4. 打标签
        min_cost_idx = costs.index(min(costs))
        min_hours_idx = hours.index(min(hours))
        # 折中方案：从帕累托前沿里挑综合分最低的（排除最省钱/最快的极端方案）
        pareto_idx = [i for i, p in enumerate(is_pareto) if p]
        balanced_idx = self._pick_balanced(pareto_idx, scores, {min_cost_idx, min_hours_idx})

        tagged = set()
        results = []
        for i, p in enumerate(plans):
            tag = ""
            if i == min_cost_idx and i not in tagged:
                tag, tagged = "最省钱", tagged | {i}
            elif i == min_hours_idx and i not in tagged:
                tag, tagged = "最快", tagged | {i}
            elif i == balanced_idx and i not in tagged:
                tag, tagged = "折中(推荐)", tagged | {i}

            results.append(ScoredPlan(
                plan=p,
                cost=costs[i],
                hours=hours[i],
                carbon_g=carbons[i],
                score=scores[i],
                is_pareto=is_pareto[i],
                recommend_tag=tag,
            ))

        # 排序：标签优先（最省钱/最快/折中在前），其次综合分
        tag_priority = {"最省钱": 0, "最快": 1, "折中(推荐)": 2}
        results.sort(key=lambda x: (tag_priority.get(x.recommend_tag, 9), x.score))
        return results

    @staticmethod
    def _compute_pareto(costs: List[float], hours: List[float]) -> List[bool]:
        """计算帕累托前沿（cost 与 hours 都更小的方案留下）。"""
        if _HAS_PARETO:
            # paretoset 标准用法：sense=[min, min] 表示两个目标都最小化
            import numpy as np
            arr = np.array(list(zip(costs, hours)))
            mask = paretoset(arr, sense=["min", "min"])
            return [bool(x) for x in mask]

        # 兜底实现：手写 O(n²)
        n = len(costs)
        is_pareto = [True] * n
        for i in range(n):
            for j in range(n):
                if i == j:
                    continue
                # j 在 cost 和 hours 上都不劣于 i，且至少一项严格更优 -> i 被支配
                if (costs[j] <= costs[i] and hours[j] <= hours[i]
                        and (costs[j] < costs[i] or hours[j] < hours[i])):
                    is_pareto[i] = False
                    break
        return is_pareto

    @staticmethod
    def _pick_balanced(
        pareto_idx: List[int],
        scores: List[float],
        exclude: set,
    ) -> int:
        """从帕累托前沿里挑综合分最低的（排除最省钱/最快两个极端）。"""
        candidates = [i for i in pareto_idx if i not in exclude]
        if not candidates:
            # 兜底：从全量里挑综合分最低的
            candidates = [i for i in range(len(scores)) if i not in exclude]
        if not candidates:
            return -1
        return min(candidates, key=lambda i: scores[i])

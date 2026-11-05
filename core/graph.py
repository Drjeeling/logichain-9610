"""加权有向图：构建多式联运网络 + K-最短路径搜索 + 路径成本核算。

设计要点：
- 节点：起讫地 / 铁路站 / 河港 / 海港
- 边：每条边表示一段单一运输方式，带 (距离, 基准运价, 时效, 班次, 中转成本, 中转时长, 碳排放)
- 路径：多条边串联组成一条多式联运方案
- 季节系数：调用方传入，运价 = base_price × month_factor × mode_multiplier

预留接口 RailDataSource：未来若开放 95306 真实 API，只需替换此类即可。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import networkx as nx

from .seasonal import SeasonalConfig


DATA_DIR = Path(__file__).resolve().parent.parent / "data"


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Edge:
    from_id: str
    to_id: str
    mode: str                          # rail / river / truck
    distance_km: float
    base_price_per_km: float           # 基准运价（元/吨·公里）
    hours: float                       # 单段运输时效
    frequency: str                     # 班次描述
    transfer_cost: float = 0.0         # 起点换装成本（元/吨）
    transfer_hours: float = 0.0        # 起点换装时长（小时）
    carbon_g_per_ton_km: float = 0.0   # 碳排放（g/吨·公里）

    @classmethod
    def from_dict(cls, d: dict) -> "Edge":
        return cls(
            from_id=d["from"],
            to_id=d["to"],
            mode=d["mode"],
            distance_km=float(d["distance_km"]),
            base_price_per_km=float(d["base_price_per_km"]),
            hours=float(d["hours"]),
            frequency=d.get("frequency", "随时"),
            transfer_cost=float(d.get("transfer_cost", 0)),
            transfer_hours=float(d.get("transfer_hours", 0)),
            carbon_g_per_ton_km=float(d.get("carbon_g_per_ton_km", 0)),
        )


@dataclass
class RouteSegment:
    """路径中的一段，已应用季节系数。"""
    from_id: str
    to_id: str
    mode: str
    distance_km: float
    price_per_ton: float
    hours: float
    carbon_g_per_ton: float
    frequency: str
    transfer_cost: float
    transfer_hours: float


@dataclass
class RoutePlan:
    """一条完整的多式联运方案。"""
    segments: List[RouteSegment] = field(default_factory=list)
    # 新增：成本明细（区分 L1 干线 / L2 衔接 / L3 时间 / L4 风险）
    breakdown: Dict[str, float] = field(default_factory=dict)
    # 新增：季节说明（用于展示）
    seasonal_notes: List[str] = field(default_factory=list)

    @property
    def total_cost(self) -> float:
        """总成本（元/吨）= 干线运费 + 中转衔接费 + 时间滞期成本 + 风险准备金。"""
        return (
            self.l1_freight
            + self.l2_transfer
            + self.l3_demurrage
            + self.l4_risk_reserve
        )

    @property
    def l1_freight(self) -> float:
        """L1 干线运费。"""
        return sum(s.price_per_ton for s in self.segments)

    @property
    def l2_transfer(self) -> float:
        """L2 中转衔接费（已应用季节系数）。"""
        return sum(s.transfer_cost for s in self.segments)

    @property
    def l3_demurrage(self) -> float:
        """L3 时间滞期成本（来自 breakdown 或 0）。"""
        return self.breakdown.get("l3_demurrage", 0.0)

    @property
    def l4_risk_reserve(self) -> float:
        """L4 风险准备金（来自 breakdown 或 0）。"""
        return self.breakdown.get("l4_risk_reserve", 0.0)

    @property
    def total_hours(self) -> float:
        """总时效（小时）= 各段运输时长 + 各段中转时长 + 查验期望值。"""
        base = sum(s.hours + s.transfer_hours for s in self.segments)
        inspection = self.breakdown.get("inspection_hours", 0.0)
        return base + inspection

    @property
    def total_carbon_g(self) -> float:
        """总碳排放（g/吨）。"""
        return sum(s.carbon_g_per_ton for s in self.segments)

    @property
    def modes_summary(self) -> str:
        """运输方式组合描述，如 '公路→铁路→河运'。"""
        mode_cn = {"truck": "公路", "rail": "铁路", "river": "河运", "sea": "海运", "air": "空运"}
        return " → ".join(mode_cn.get(s.mode, s.mode) for s in self.segments)

    @property
    def path_summary(self) -> str:
        """路径节点序列描述。"""
        if not self.segments:
            return ""
        nodes = [self.segments[0].from_id]
        nodes.extend(s.to_id for s in self.segments)
        return " → ".join(nodes)

    @property
    def cost_breakdown_str(self) -> str:
        """成本明细字符串（用于演示）。"""
        lines = []
        if self.l1_freight > 0:
            lines.append(f"  L1 干线运费: {self.l1_freight:.2f} 元/吨")
        if self.l2_transfer > 0:
            lines.append(f"  L2 中转衔接: {self.l2_transfer:.2f} 元/吨")
        if self.l3_demurrage > 0:
            lines.append(f"  L3 时间滞期: {self.l3_demurrage:.2f} 元/吨")
        if self.l4_risk_reserve > 0:
            lines.append(f"  L4 风险准备金: {self.l4_risk_reserve:.2f} 元/吨")
        if self.breakdown.get("inspection_hours", 0) > 0:
            lines.append(f"  查验期望等待: {self.breakdown['inspection_hours']:.1f} 小时")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 数据源接口（预留 95306 真实 API 接入点）
# --------------------------------------------------------------------------- #
class RailDataSource:
    """铁路班次数据源。

    当前版本从静态 JSON 读取。未来 95306 开放 API 后，
    只需重写 fetch_edges() 方法返回同结构数据即可，
    上层 Agent 代码无需任何改动。
    """

    name = "static_95306"

    def __init__(self, edges_path: Optional[Path] = None):
        self.edges_path = edges_path or (DATA_DIR / "edges.json")

    def fetch_edges(self) -> List[dict]:
        with open(self.edges_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data["edges"]


# --------------------------------------------------------------------------- #
# 多式联运图
# --------------------------------------------------------------------------- #
class MultimodalGraph:
    """多式联运加权有向图。"""

    def __init__(
        self,
        rail_source: Optional[RailDataSource] = None,
        seasonal: Optional[SeasonalConfig] = None,
        month: Optional[int] = None,
    ):
        self.seasonal = seasonal or SeasonalConfig.from_json()
        self.month = month  # 若为 None，则不应用季节系数
        self.rail_source = rail_source or RailDataSource()
        # 同时维护两个图：
        # - _multigraph: 完整数据，支持同一对节点多种运输方式
        # - _simple_view: 折叠为 DiGraph，用于 shortest_simple_paths
        self._multigraph: nx.MultiDiGraph = nx.MultiDiGraph()
        self._simple_view: nx.DiGraph = nx.DiGraph()
        self._edges: List[Edge] = []
        self._build()

    def _build(self) -> None:
        raw_edges = self.rail_source.fetch_edges()
        self._edges = [Edge.from_dict(d) for d in raw_edges]
        for e in self._edges:
            # MultiDiGraph 用 (u, v, key) 三元组区分多重边
            self._multigraph.add_edge(
                e.from_id, e.to_id,
                key=e.mode,
                edge=e,
            )
            # simple_view 折叠：同一对节点只保留一条边，权重=edge_count=1
            if not self._simple_view.has_edge(e.from_id, e.to_id):
                self._simple_view.add_edge(
                    e.from_id, e.to_id,
                    edge_count=1,
                )

    # ----- 查询 ----- #
    def nodes(self) -> List[str]:
        return list(self._multigraph.nodes)

    def has_node(self, node: str) -> bool:
        return node in self._multigraph

    def node_attrs(self, node: str) -> Optional[Dict[str, Any]]:
        """查询节点属性（type/lng/lat 等），从 nodes.json 加载。"""
        # 懒加载节点属性
        if not hasattr(self, "_node_attrs_cache"):
            self._node_attrs_cache = {}
            try:
                nodes_path = DATA_DIR / "nodes.json"
                with open(nodes_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for n in data.get("nodes", []):
                    self._node_attrs_cache[n["id"]] = n
            except Exception:
                pass
        return self._node_attrs_cache.get(node)

    def nodes_by_type(self, node_type: str) -> List[str]:
        """查询某类型的所有节点（如 sea_port/river_port/rail_station）。"""
        if not hasattr(self, "_node_attrs_cache"):
            self.node_attrs("__init_cache__")  # 触发懒加载
        return [
            nid for nid, attrs in getattr(self, "_node_attrs_cache", {}).items()
            if attrs.get("type") == node_type
        ]

    # ----- 路径搜索 ----- #
    def find_k_shortest_routes(
        self,
        origin: str,
        dest: str,
        k: int = 5,
    ) -> List[RoutePlan]:
        """使用 Yen 算法枚举 K 条最短路径。

        在 _simple_view 上做 K-最短路径初筛（按中转次数），
        再在 _multigraph 上展开每条路径所有可能的运输方式组合。
        """
        if not self.has_node(origin) or not self.has_node(dest):
            return []
        try:
            simple_paths = list(
                nx.shortest_simple_paths(
                    self._simple_view, origin, dest, weight="edge_count"
                )
            )
        except nx.NetworkXNoPath:
            return []
        # 同时把同节点序列下的不同 mode 组合也展开
        plans: List[RoutePlan] = []
        for path_nodes in simple_paths[:k]:
            plans.extend(self._expand_mode_combinations(path_nodes))
        return plans

    def _expand_mode_combinations(self, path_nodes: List[str]) -> List[RoutePlan]:
        """对一条节点序列，枚举所有可能的运输方式组合。

        改造：中转费不再用 edge 的固定 transfer_cost，
        而是从 transfer_baseline.json 查 (前段mode, 当前段mode) 的 PERT 期望值，
        再应用月份中转季节系数（旺季港口更贵更慢）。
        """
        from itertools import product

        edge_options: List[List[Edge]] = []
        for u, v in zip(path_nodes[:-1], path_nodes[1:]):
            options = [
                d["edge"]
                for _, to_node, _key, d in self._multigraph.out_edges(u, keys=True, data=True)
                if to_node == v
            ]
            if not options:
                return []
            edge_options.append(options)

        plans = []
        for combo in product(*edge_options):
            segments = self._build_segments_with_baseline_transfer(combo)
            breakdown = self._compute_breakdown(segments)
            notes = self._collect_seasonal_notes()
            plans.append(RoutePlan(
                segments=segments,
                breakdown=breakdown,
                seasonal_notes=notes,
            ))
        return plans

    def _build_segments_with_baseline_transfer(
        self, combo: Tuple[Edge, ...]
    ) -> List[RouteSegment]:
        """构建路径段，中转费从基准表查 + 季节系数调。"""
        baseline = self._load_transfer_baseline()
        pair_table = baseline.get("mode_pair_transfer", {})

        # 中转季节系数（旺季港口更贵更慢）
        cost_mult, hours_mult, _ = (1.0, 1.0, "")
        if self.month is not None:
            cost_mult, hours_mult, _ = self.seasonal.transfer_factor(self.month)

        segments: List[RouteSegment] = []
        for i, e in enumerate(combo):
            # 干线运价：base × distance × 月份×方式系数
            base_price = e.base_price_per_km * e.distance_km
            if self.month is not None:
                adjusted = self.seasonal.adjust_price(base_price, self.month, e.mode)
            else:
                adjusted = base_price
            carbon = e.carbon_g_per_ton_km * e.distance_km

            # 中转费：第一段无中转，后续段从前段mode→当前mode 查表
            transfer_cost = 0.0
            transfer_hours = 0.0
            if i > 0:
                prev_mode = combo[i - 1].mode
                pair_key = f"{prev_mode}→{e.mode}"
                pair_data = pair_table.get(pair_key, {})
                base_transfer_cost = pair_data.get("cost_per_ton", e.transfer_cost)
                base_transfer_hours = pair_data.get("hours", e.transfer_hours)
                # 应用中转季节系数
                transfer_cost = base_transfer_cost * cost_mult
                transfer_hours = base_transfer_hours * hours_mult

            segments.append(RouteSegment(
                from_id=e.from_id,
                to_id=e.to_id,
                mode=e.mode,
                distance_km=e.distance_km,
                price_per_ton=adjusted,
                hours=e.hours,
                carbon_g_per_ton=carbon,
                frequency=e.frequency,
                transfer_cost=transfer_cost,
                transfer_hours=transfer_hours,
            ))
        return segments

    def _compute_breakdown(self, segments: List[RouteSegment]) -> Dict[str, float]:
        """计算 L3/L4 层成本：查验期望值 + 风险准备金。"""
        breakdown: Dict[str, float] = {}
        if self.month is None:
            return breakdown

        # L3：查验额外等待期望值（小时）
        inspection_h = self.seasonal.inspection_expected_hours(self.month)
        breakdown["inspection_hours"] = inspection_h

        # L3：滞期成本（简化估算：基础中转成本 × 风险系数）
        # 真实实现需结合货值、免堆期、滞箱费率，这里用简化版
        transfer_total = sum(s.transfer_cost for s in segments)
        breakdown["l3_demurrage"] = transfer_total * 0.1  # 估算 10% 滞期成本

        # L4：风险准备金（前 3 层小计 × 比例）
        l1 = sum(s.price_per_ton for s in segments)
        l2 = transfer_total
        l3 = breakdown["l3_demurrage"]
        risk_rate = self.seasonal.risk_reserve_rate(self.month)
        breakdown["l4_risk_reserve"] = (l1 + l2 + l3) * risk_rate

        return breakdown

    def _collect_seasonal_notes(self) -> List[str]:
        """收集季节说明，用于展示。"""
        notes = []
        if self.month is None:
            notes.append("未指定月份，运价按基准价计算")
            return notes
        _, _, t_note = self.seasonal.transfer_factor(self.month)
        if t_note:
            notes.append(f"中转季节: {t_note}（cost×{self.seasonal.transfer_factor(self.month)[0]:.2f}, hours×{self.seasonal.transfer_factor(self.month)[1]:.2f}）")
        insp_h = self.seasonal.inspection_expected_hours(self.month)
        if insp_h > 0:
            notes.append(f"查验期望等待: {insp_h:.1f} 小时")
        risk_rate = self.seasonal.risk_reserve_rate(self.month)
        season_tag = "旺季" if self.seasonal.is_peak_season(self.month) else "淡季"
        notes.append(f"{season_tag}风险准备金率: {risk_rate*100:.0f}%")
        return notes

    @staticmethod
    @lru_cache(maxsize=1)
    def _load_transfer_baseline() -> Dict:
        """加载中转费基准表（缓存）。"""
        path = DATA_DIR / "transfer_baseline.json"
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _edge_to_segment(self, e: Edge) -> RouteSegment:
        """（兼容旧接口）单边转段，不含跨段中转费。"""
        base_price = e.base_price_per_km * e.distance_km
        if self.month is not None:
            adjusted = self.seasonal.adjust_price(base_price, self.month, e.mode)
        else:
            adjusted = base_price
        carbon = e.carbon_g_per_ton_km * e.distance_km
        return RouteSegment(
            from_id=e.from_id,
            to_id=e.to_id,
            mode=e.mode,
            distance_km=e.distance_km,
            price_per_ton=adjusted,
            hours=e.hours,
            carbon_g_per_ton=carbon,
            frequency=e.frequency,
            transfer_cost=e.transfer_cost,
            transfer_hours=e.transfer_hours,
        )


# --------------------------------------------------------------------------- #
# 便捷入口
# --------------------------------------------------------------------------- #
def load_graph(month: Optional[int] = None) -> MultimodalGraph:
    """快速加载图，可选传入月份应用季节系数。"""
    return MultimodalGraph(month=month)

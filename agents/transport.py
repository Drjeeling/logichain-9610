"""国内段运输 Agent：迁移自 agent/agent.py，适配多 Agent 架构。

改动：
1. 继承 BaseAgent
2. ask() 返回 AgentResult 而非 str
3. 增加 can_handle() 用于协调器路由
4. 输出包含 next_hint，提示下游 Agent
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

from core.graph import MultimodalGraph
from core.seasonal import SeasonalConfig
from agent.tools import ToolRegistry
from .base import BaseAgent, AgentResult


class TransportAgent(BaseAgent):
    """国内段运输方案 Agent。"""

    name = "transport"
    description = "国内段多式联运方案选择（铁路/公路/河运）"

    def __init__(
        self,
        month: Optional[int] = None,
        use_llm: bool = False,
        verbose: bool = False,
    ):
        self.seasonal = SeasonalConfig.from_json()
        self.graph = MultimodalGraph(seasonal=self.seasonal, month=month)
        self.month = month
        self.use_llm = use_llm and bool(os.getenv("DEEPSEEK_API_KEY"))
        self.verbose = verbose
        self.registry = ToolRegistry(self.graph, self.seasonal)

    # ----------------------------------------------------------------- #
    # DeepSeek LLM 客户端
    # ----------------------------------------------------------------- #
    def _llm_chat(self, prompt: str, temperature: float = 0.1, max_tokens: int = 300) -> Optional[str]:
        """调用 DeepSeek Chat API，返回文本或 None。"""
        if not self.use_llm:
            return None
        try:
            from openai import OpenAI
            client = OpenAI(
                api_key=os.getenv("DEEPSEEK_API_KEY"),
                base_url="https://api.deepseek.com",
            )
            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            if self.verbose:
                print(f"[TransportAgent] LLM 调用失败: {e}")
            return None

    def _llm_parse_intent(self, query: str) -> Optional[Dict[str, Any]]:
        """用 LLM 从模糊自然语言里抽取结构化意图。

        触发条件：正则解析不出 origin 或 destination 时。
        """
        nodes = self.graph.nodes()
        # 国内港口/节点（排除国外机场如"洛杉矶机场"）
        domestic_nodes = [n for n in nodes if "机场" not in n and "洛杉矶" not in n]
        prompt = f"""从用户物流需求中抽取【国内段】起点和终点。输出 JSON。

国内段可用节点：{", ".join(domestic_nodes)}

【关键规则】：
- origin 和 destination 都必须是【国内段】节点，即上面列表中的精确ID
- 用户提到的国外目的地（洛杉矶/汉堡/新加坡等）不是国内段 destination，应忽略
- 用户提到国外目的地时，destination 应选最近的国内海港（如"到洛杉矶"→"宁波港"或"上海港"）
- 若用户提到的国内城市不在列表中，选最近的节点（如"苏州"→"上海港"，"杭州"→"义乌"）
- weight 三选一：cost_priority(省钱)/time_priority(最快)/balanced(折中)
- use_air：仅当用户明确说"加急/空运/急件/空运直飞"时为 true；说"海运/走海运/船运"时必须为 false

只输出 JSON，不要解释：
{{"origin": "...", "destination": "...", "weight": "...", "use_air": false}}

用户需求：{query}"""
        content = self._llm_chat(prompt, temperature=0.0, max_tokens=100)
        if not content:
            return None
        try:
            import json, re
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.MULTILINE)
            data = json.loads(content)
            # 校验 origin/dest 必须在【国内】节点列表里（排除国外机场）
            if data.get("origin") not in domestic_nodes or data.get("destination") not in domestic_nodes:
                return None
            if data.get("weight") not in ("cost_priority", "time_priority", "balanced"):
                data["weight"] = "balanced"
            data["use_air"] = bool(data.get("use_air", False))
            return data
        except Exception as e:
            if self.verbose:
                print(f"[TransportAgent] LLM 意图解析 JSON 解析失败: {e}")
            return None

    def _llm_explain_recommendation(self, query: str, recommended: List[Dict]) -> str:
        """让 LLM 用自然语言解释为什么推荐这些方案。"""
        if not recommended:
            return ""
        # 取前 3 条方案，简化字段
        plans_text = ""
        for i, r in enumerate(recommended[:3], 1):
            plans_text += (
                f"\n{i}. [{r.get('tag', '候选')}] {r.get('modes', '')} "
                f"路径: {r.get('path', '')} 成本: {r.get('cost_per_ton')} 元/吨 "
                f"时效: {r.get('hours')} 小时 碳排: {r.get('carbon_g_per_ton')} g/吨"
            )
        prompt = f"""用户需求：{query}

候选运输方案：{plans_text}

请用 3-5 句话，向货主解释为什么推荐这几个方案，重点说明：
1. 最省钱方案为什么便宜（哪段运输方式成本低）
2. 最快方案为什么快（哪段运输方式时效快）
3. 折中方案在权衡什么
不要复述数据，要讲运输逻辑。"""
        return self._llm_chat(prompt, temperature=0.7, max_tokens=400) or ""

    # ----------------------------------------------------------------- #
    # BaseAgent 接口
    # ----------------------------------------------------------------- #
    def can_handle(self, query: str) -> bool:
        """判断是否是国内运输相关需求。"""
        keywords = ["运输", "国内段", "铁路", "公路", "河运", "联运", "班次", "运费"]
        # 同时要求查询里出现节点名
        has_node = any(n in query for n in self.graph.nodes())
        return has_node or any(k in query for k in keywords)

    def ask(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> AgentResult:
        """处理运输需求，返回结构化结果。

        全流程模式（用户要去国外港口如洛杉矶）：
        - 自动找最近的国内海港作为国内段终点
        - 输出建议国内段终点，传递给下游订舱 Agent
        """
        if self.verbose:
            print(f"[TransportAgent] 收到: {query}")

        # 1. 先用正则解析（快、免费、确定性高）
        intent = self._parse_intent(query)
        origin = intent.get("origin")
        dest = intent.get("destination")

        # 2. 正则解析不出 origin 或 dest 时，降级到 LLM 解析
        llm_used = False
        if (not origin or not dest) and self.use_llm:
            llm_intent = self._llm_parse_intent(query)
            if llm_intent:
                # 合并：LLM 解析结果覆盖正则
                intent.update(llm_intent)
                origin = intent.get("origin")
                dest = intent.get("destination")
                llm_used = True
                if self.verbose:
                    print(f"[TransportAgent] LLM 意图解析成功: {intent}")

        if self.verbose:
            print(f"[TransportAgent] 意图: {intent} (LLM: {llm_used})")

        weight = intent.get("weight", "balanced")

        # 全流程模式兜底：用户没指定国内段终点时，自动选最近海港
        auto_dest = None
        if not dest:
            auto_dest = self._pick_nearest_seaport(origin) if origin else None
            if auto_dest:
                dest = auto_dest
                if self.verbose:
                    print(f"[TransportAgent] 全流程模式：自动选最近海港 {dest}")

        if not origin or not dest:
            return AgentResult(
                agent_name=self.name,
                summary=(
                    "请明确起点和终点，可用节点：\n"
                    + ", ".join(self.graph.nodes())
                    + "\n示例：'20吨货从义乌到宁波港，预算优先'"
                    + ("\n（已尝试 LLM 解析但仍无法识别）" if self.use_llm else "")
                ),
                confidence=0.3,
            )

        scored = self.registry.dispatch("score_routes", {
            "origin": origin, "destination": dest, "weight": weight
        })
        if "error" in scored:
            # LLM 可能挑了一个图里不可达的海港（如成都→宁波港），
            # 此时回退到"几何最近且可达"的海港再试一次。
            fallback = self._pick_nearest_seaport(origin) if origin else None
            if fallback and fallback != dest:
                dest = fallback
                auto_dest = dest
                scored = self.registry.dispatch("score_routes", {
                    "origin": origin, "destination": dest, "weight": weight
                })
        if "error" in scored:
            return AgentResult(
                agent_name=self.name,
                summary=f"未找到 {origin} → {dest} 的可行路径，请确认起讫地。",
                confidence=0.4,
            )

        recommended = scored.get("recommended", [])
        all_routes = scored.get("all_routes", [])

        # 拼装自然语言总结
        prefix = ""
        if auto_dest:
            prefix = (
                f"_（全流程模式：自动选择国内段终点 **{auto_dest}**，"
                f"国际段由订舱 Agent 处理）_\n\n"
            )
        summary_lines = [
            f"## {origin} → {dest} 多式联运方案推荐（共 {len(all_routes)} 条候选）\n",
            prefix,
        ]
        for r in recommended:
            summary_lines.append(self._format_recommendation(r))
        summary_lines.append(f"\n**季节系数**：{self._build_seasonal_note(intent)}")

        # LLM 推荐解释（启用 LLM 时附加）
        if self.use_llm and recommended:
            explanation = self._llm_explain_recommendation(query, recommended)
            if explanation:
                summary_lines.append(f"\n**📊 推荐理由**：\n{explanation}")

        # 提取首选方案数据，传给下游 Agent
        chosen = recommended[0] if recommended else (all_routes[0] if all_routes else None)
        next_hint = ""
        downstream_data: Dict[str, Any] = {}
        if chosen:
            downstream_data = {
                "selected_path": chosen.get("path", ""),
                "selected_modes": chosen.get("modes", ""),
                "cost_per_ton": chosen.get("cost_per_ton"),
                "hours": chosen.get("hours"),
                "destination_port": dest,  # 下游订舱 Agent 用作起运港
                "tonnage": intent.get("tonnage"),  # 下游客重透传（booking 询价要素）
            }
            next_hint = (
                f"国内段已选：{chosen.get('modes','')} → {dest}，"
                f"建议下游订舱 Agent 查询 {dest} 的国际段船期"
            )

        return AgentResult(
            agent_name=self.name,
            summary="\n".join(summary_lines),
            data={
                "intent": intent,
                "recommended": recommended,
                "all_routes": all_routes,
                "chosen": downstream_data,
                "destination_port": dest,
                "tonnage": intent.get("tonnage"),  # 顶层透传：下游 booking 读询价要素
            },
            next_hint=next_hint,
            confidence=0.9,
        )

    # ----------------------------------------------------------------- #
    # 全流程模式辅助
    # ----------------------------------------------------------------- #
    def _pick_nearest_seaport(self, origin: Optional[str]) -> Optional[str]:
        """全流程模式：为任意起点选最近海港（基于经纬度直线距离）。

        优先用 geopy 精确算距离；geopy 不可用或起点不在节点库时，
        降级到正则匹配 + 硬编码兜底。
        """
        if not origin:
            return None

        # 方案 A：从节点库查 origin 的经纬度，再算最近海港
        try:
            origin_node = self.graph.node_attrs(origin)
            if origin_node and origin_node.get("lng") and origin_node.get("lat"):
                origin_lng = float(origin_node["lng"])
                origin_lat = float(origin_node["lat"])
                # 找所有 sea_port 类型的节点
                sea_port_ids = self.graph.nodes_by_type("sea_port")
                sea_ports = [
                    (nid, self.graph.node_attrs(nid)) for nid in sea_port_ids
                    if self.graph.node_attrs(nid) and self.graph.node_attrs(nid).get("lng")
                ]
                if sea_ports:
                    ordered = sorted(
                        sea_ports,
                        key=lambda x: self._haversine_km(
                            origin_lat, origin_lng,
                            float(x[1]["lat"]), float(x[1]["lng"])
                        )
                    )
                    # 选最近的"可达"海港：几何最近但图里不可达时，继续往后找
                    for nid, attrs in ordered:
                        if not self.graph.find_k_shortest_routes(origin, nid, k=1):
                            continue
                        if self.verbose:
                            dist = self._haversine_km(
                                origin_lat, origin_lng,
                                float(attrs["lat"]), float(attrs["lng"])
                            )
                            print(f"[TransportAgent] 可达最近海港：{origin} → {nid} ({dist:.0f}km)")
                        return nid
                    # 全部不可达：退回几何最近（保持原行为）
                    return ordered[0][0]
        except Exception as e:
            if self.verbose:
                print(f"[TransportAgent] 经纬度匹配失败，降级: {e}")

        # 方案 B：geopy 在线查询（沙箱可能用不了，本地能跑）
        try:
            from geopy.distance import geodesic
            from geopy.geocoders import Nominatim
            geolocator = Nominatim(user_agent="logistics_agent", timeout=3)
            origin_loc = geolocator.geocode(origin)
            if origin_loc:
                sea_port_ids = self.graph.nodes_by_type("sea_port")
                sea_ports = [
                    (nid, self.graph.node_attrs(nid)) for nid in sea_port_ids
                    if self.graph.node_attrs(nid) and self.graph.node_attrs(nid).get("lng")
                ]
                if sea_ports:
                    nearest = min(
                        sea_ports,
                        key=lambda x: geodesic(
                            (origin_loc.latitude, origin_loc.longitude),
                            (float(x[1]["lat"]), float(x[1]["lng"]))
                        ).km
                    )
                    return nearest[0]
        except Exception:
            pass  # geopy 在线失败，继续降级

        # 方案 C：硬编码兜底（最坏情况）
        nearest_map = {
            "义乌": "宁波港",
            "成都": "上海港",
            "重庆": "上海港",
        }
        return nearest_map.get(origin)

    @staticmethod
    def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
        """Haversine 公式算两点间球面距离（km），不依赖外部库。"""
        from math import radians, sin, cos, asin, sqrt
        R = 6371.0  # 地球半径 km
        dlat = radians(lat2 - lat1)
        dlng = radians(lng2 - lng1)
        a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng/2)**2
        return 2 * R * asin(sqrt(a))

    # ----------------------------------------------------------------- #
    # 内部辅助（沿用原 agent.py 的实现）
    # ----------------------------------------------------------------- #
    def _parse_intent(self, query: str) -> Dict[str, Any]:
        # 起讫地匹配：按文字中出现位置排序
        matched = []
        for node in self.graph.nodes():
            idx = query.find(node)
            if idx >= 0:
                matched.append((idx, node))
        matched.sort()
        origin = matched[0][1] if len(matched) >= 1 else None
        dest = matched[1][1] if len(matched) >= 2 else None

        if any(k in query for k in ["省钱", "预算", "便宜", "最低成本", "成本"]):
            weight = "cost_priority"
        elif any(k in query for k in ["最快", "时效", "紧急", "加急"]):
            weight = "time_priority"
        else:
            weight = "balanced"

        # 加急空运识别：用户提到加急/空运/急件时，目标机场优先
        use_air = any(k in query for k in ["加急", "空运", "急件", "航空", "air", "飞机"])
        if use_air and dest and "机场" not in dest:
            # 全流程加急：把目的港改为对应国外机场
            # 图里只有洛杉矶机场节点；未覆盖的目的地保持原值，避免把汉堡错配到洛杉矶
            air_dest_map = {"洛杉矶": "洛杉矶机场"}
            dest = air_dest_map.get(dest, dest)

        # 兼容小数（单据自动生成的 query 是 "6.0吨"；只匹配 \d+ 会误取 "0吨"）
        m = re.search(r"(\d+(?:\.\d+)?)\s*吨", query)
        tonnage = float(m.group(1)) if m else 20

        return {
            "origin": origin,
            "destination": dest,
            "weight": weight,
            "tonnage": tonnage,
            "month": self.month,
            "use_air": use_air,
        }

    def _build_seasonal_note(self, intent: Dict[str, Any]) -> str:
        if not self.month:
            return "未指定月份，运价按基准价计算"
        notes = []
        for mode in ["rail", "river", "truck"]:
            r = self.registry.dispatch("get_seasonal_factor", {
                "month": self.month, "mode": mode
            })
            notes.append(f"{r['explanation']}（最终系数 {r['final_factor']:.2f}）")
        return "；".join(notes)

    @staticmethod
    def _format_recommendation(r: Dict[str, Any]) -> str:
        # 取出 plan 上的成本明细（如果有）
        breakdown_str = r.get("cost_breakdown_str", "")
        lines = [
            f"### 【{r.get('tag', '推荐')}】\n",
            f"- 路径：{r.get('path', '')}\n",
            f"- 运输方式：{r.get('modes', '')}\n",
            f"- 总成本：{r.get('cost_per_ton')} 元/吨（含 L1 干线 + L2 中转 + L3 滞期 + L4 风险）\n",
            f"- 总时效：{r.get('hours')} 小时（含查验期望等待）\n",
            f"- 总碳排放：{r.get('carbon_g_per_ton')} g/吨\n",
            f"- 综合评分：{r.get('score')}（越小越优）\n",
            f"- 帕累托前沿：{'是' if r.get('is_pareto') else '否'}\n",
        ]
        if breakdown_str:
            lines.append(f"\n**成本明细**：\n```\n{breakdown_str}\n```\n")
        seasonal_notes = r.get("seasonal_notes", [])
        if seasonal_notes:
            lines.append("**季节说明**：\n")
            for n in seasonal_notes:
                lines.append(f"- {n}")
        return "\n".join(lines)

"""国内段运输 Agent 主体：协调 DeepSeek + 工具调用 + 路径评分。

工作流程：
1. 用户用自然语言提出运输需求
2. DeepSeek 解析意图，调用对应工具（find_routes/score_routes/...）
3. 工具返回结构化数据
4. DeepSeek 基于数据生成自然语言推荐

支持两种运行模式：
- LLM 模式：真实调用 DeepSeek API（需配置 DEEPSEEK_API_KEY）
- RULE 模式：未配置 Key 时自动降级，用规则模板生成回复，保证 demo 可跑

Demo 与答辩演示建议优先用 RULE 模式（稳定可控），重点场景再切 LLM 模式。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from core.graph import MultimodalGraph
from core.scorer import RouteScorer
from core.seasonal import SeasonalConfig
from .prompts import SYSTEM_PROMPT, FOLLOWUP_TEMPLATE
from .tools import ALL_TOOL_SCHEMAS, ToolRegistry


class TransportAgent:
    """国内段运输方案 Agent。"""

    def __init__(
        self,
        month: Optional[int] = None,
        use_llm: bool = True,
        verbose: bool = False,
    ):
        self.seasonal = SeasonalConfig.from_json()
        self.graph = MultimodalGraph(seasonal=self.seasonal, month=month)
        self.month = month
        self.use_llm = use_llm and bool(os.getenv("DEEPSEEK_API_KEY"))
        self.verbose = verbose
        self.registry = ToolRegistry(self.graph, self.seasonal)
        self._client = None
        if self.use_llm:
            try:
                from openai import OpenAI  # DeepSeek 兼容 OpenAI SDK
                self._client = OpenAI(
                    api_key=os.getenv("DEEPSEEK_API_KEY"),
                    base_url="https://api.deepseek.com",
                )
            except ImportError:
                self.use_llm = False

    # ----------------------------------------------------------------- #
    # 对外主入口
    # ----------------------------------------------------------------- #
    def ask(self, user_query: str) -> str:
        """用户提问 -> 返回回复文本。"""
        if self.verbose:
            print(f"[Agent] 收到用户需求: {user_query}")

        # 简单意图识别：从用户输入中提取起讫地、偏好
        intent = self._parse_intent(user_query)
        if self.verbose:
            print(f"[Agent] 意图解析: {intent}")

        if self.use_llm:
            return self._run_with_llm(user_query, intent)
        return self._run_with_rules(user_query, intent)

    # ----------------------------------------------------------------- #
    # LLM 协同模式
    # ----------------------------------------------------------------- #
    def _run_with_llm(self, user_query: str, intent: Dict[str, Any]) -> str:
        # 第一轮：直接调用 score_routes 工具，拿到结构化结果
        plans_text = self._build_plans_text(intent)
        seasonal_note = self._build_seasonal_note(intent)

        prompt = FOLLOWUP_TEMPLATE.format(
            user_query=user_query,
            n=intent.get("n_routes", 0),
            plans_text=plans_text,
            month=self.month or "未指定",
            seasonal_note=seasonal_note,
        )

        try:
            resp = self._client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=1500,
            )
            return resp.choices[0].message.content
        except Exception as e:
            if self.verbose:
                print(f"[Agent] LLM 调用失败，降级到规则模式: {e}")
            return self._run_with_rules(user_query, intent)

    # ----------------------------------------------------------------- #
    # 规则模式（不依赖 LLM，保证 demo 稳定）
    # ----------------------------------------------------------------- #
    def _run_with_rules(self, user_query: str, intent: Dict[str, Any]) -> str:
        origin = intent.get("origin")
        dest = intent.get("destination")
        weight = intent.get("weight", "balanced")

        if not origin or not dest:
            return (
                "请明确起点和终点，可用节点：\n"
                + ", ".join(self.graph.nodes())
                + "\n示例：'20吨货从义乌到宁波港，预算优先'"
            )

        scored = self.registry.dispatch("score_routes", {
            "origin": origin, "destination": dest, "weight": weight
        })
        if "error" in scored:
            return f"未找到 {origin} → {dest} 的可行路径，请确认起讫地。"

        recommended = scored.get("recommended", [])
        all_routes = scored.get("all_routes", [])

        # 拼装输出
        lines = [f"## {origin} → {dest} 多式联运方案推荐（共 {len(all_routes)} 条候选）\n"]
        if recommended:
            for r in recommended:
                lines.append(self._format_recommendation(r))
        else:
            # 兜底：直接展示前 3 条
            for r in all_routes[:3]:
                lines.append(self._format_route(r))

        lines.append(f"\n**季节系数说明**：{self._build_seasonal_note(intent)}")
        lines.append(
            f"\n**用户偏好**：{weight}（cost=省钱 / time=时效 / balanced=折中）"
        )
        return "\n".join(lines)

    # ----------------------------------------------------------------- #
    # 意图解析（简单关键词匹配，LLM 不可用时兜底）
    # ----------------------------------------------------------------- #
    def _parse_intent(self, query: str) -> Dict[str, Any]:
        text = query.lower()
        # 起讫地匹配：按用户文字中出现位置排序，第一个是 origin，第二个是 dest
        matched = []
        for node in self.graph.nodes():
            idx = query.find(node)
            if idx >= 0:
                matched.append((idx, node))
        matched.sort()
        origin = matched[0][1] if len(matched) >= 1 else None
        dest = matched[1][1] if len(matched) >= 2 else None

        # 偏好识别
        if any(k in query for k in ["省钱", "预算", "便宜", "最低成本", "成本"]):
            weight = "cost_priority"
        elif any(k in query for k in ["最快", "时效", "紧急", "加急"]):
            weight = "time_priority"
        else:
            weight = "balanced"

        # 货量提取（简单正则）
        import re
        m = re.search(r"(\d+)\s*吨", query)
        tonnage = int(m.group(1)) if m else 20

        return {
            "origin": origin,
            "destination": dest,
            "weight": weight,
            "tonnage": tonnage,
            "month": self.month,
        }

    # ----------------------------------------------------------------- #
    # 文本构建
    # ----------------------------------------------------------------- #
    def _build_plans_text(self, intent: Dict[str, Any]) -> str:
        origin = intent.get("origin")
        dest = intent.get("destination")
        weight = intent.get("weight", "balanced")
        if not origin or not dest:
            return "（无法识别起讫地，请让用户重新输入）"
        scored = self.registry.dispatch("score_routes", {
            "origin": origin, "destination": dest, "weight": weight
        })
        if "error" in scored:
            return scored["error"]
        intent["n_routes"] = scored.get("total_routes", 0)
        lines = []
        for r in scored.get("recommended", []) or scored.get("all_routes", [])[:3]:
            tag = r.get("tag") or ""
            lines.append(
                f"- [{tag}] {r.get('path', '')} | {r.get('modes', '')} | "
                f"运费 {r.get('cost_per_ton')} 元/吨 | {r.get('hours')} h | "
                f"碳排 {r.get('carbon_g_per_ton')} g/吨 | 综合分 {r.get('score')}"
            )
        return "\n".join(lines)

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
        return (
            f"### 【{r.get('tag', '推荐')}】\n"
            f"- 路径：{r.get('path', '')}\n"
            f"- 运输方式：{r.get('modes', '')}\n"
            f"- 总运费：{r.get('cost_per_ton')} 元/吨\n"
            f"- 总时效：{r.get('hours')} 小时\n"
            f"- 总碳排放：{r.get('carbon_g_per_ton')} g/吨\n"
            f"- 综合评分：{r.get('score')}（越小越优，1.0 为最高分）\n"
            f"- 帕累托前沿：{'是' if r.get('is_pareto') else '否'}\n"
        )

    @staticmethod
    def _format_route(r: Dict[str, Any]) -> str:
        return (
            f"- {r.get('path', '')} | {r.get('modes', '')} | "
            f"{r.get('cost_per_ton')} 元/吨 | {r.get('hours')} h | "
            f"碳排 {r.get('carbon_g_per_ton')} g/吨"
        )


def build_agent(month: Optional[int] = None, use_llm: bool = True, verbose: bool = True) -> TransportAgent:
    """便捷构造入口。"""
    return TransportAgent(month=month, use_llm=use_llm, verbose=verbose)

"""意图识别模块：从用户输入识别需要哪些 Agent，以及执行模式。

执行模式：
- single: 只调用一个 Agent（用户问某个具体环节）
- pipeline: 全流程串联（用户问"出海全流程"）
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

try:  # 独立调用时也加载 .env
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass


@dataclass
class Intent:
    """用户意图。"""
    mode: str  # "single" | "pipeline"
    agents_needed: List[str]  # 例如 ["transport", "customs", "booking", "tracking"]
    reason: str = ""

    def __str__(self) -> str:
        return f"Intent(mode={self.mode}, agents={self.agents_needed})"


# 全流程触发关键词
PIPELINE_KEYWORDS = [
    "全流程", "全链路", "出海", "出口", "整票", "全部",
    "从国内到国外", "到国外", "怎么发", "完整方案",
    "从头到尾", "全程", "一条龙", "都安排", "全包", "端到端", "一站式",
]

# 各 Agent 触发关键词
AGENT_KEYWORDS = {
    "transport": ["运输", "国内段", "铁路", "公路", "河运", "联运", "班次", "运费"],
    "customs": ["报关", "HS", "税则", "单证", "发票", "装箱单", "归类", "关税"],
    "booking": ["订舱", "船期", "海运", "船公司", "国际段", "马士基", "中远",
                "询价", "舱位", "运价", "报价", "航线"],
    "tracking": ["追踪", "运单", "物流状态", "在途", "到哪", "延误", "扣关"],
}

# 全流程默认串联顺序
PIPELINE_ORDER = ["transport", "booking", "customs", "tracking"]


def recognize(query: str, available_agents: List[str]) -> Intent:
    """识别用户意图，决定调用哪些 Agent 及执行模式。

    Args:
        query: 用户原始输入
        available_agents: 当前可用的 Agent 名列表（按 name 字段过滤）

    Returns:
        Intent 对象，包含 mode 和 agents_needed
    """
    # 1. 全流程触发：明确的全链路关键词
    if any(k in query for k in PIPELINE_KEYWORDS):
        # 过滤出可用的 Agent
        needed = [a for a in PIPELINE_ORDER if a in available_agents]
        if len(needed) >= 2:
            return Intent(
                mode="pipeline",
                agents_needed=needed,
                reason="检测到全流程关键词，触发多 Agent 串联",
            )

    # 2. 识别具体环节关键词
    matched = []
    for agent, keywords in AGENT_KEYWORDS.items():
        if agent in available_agents and any(k in query for k in keywords):
            matched.append(agent)

    if matched:
        # 单 Agent 或少数 Agent 路由
        # 如果命中多个，仍按 pipeline 串联
        ordered = [a for a in PIPELINE_ORDER if a in matched]
        if len(ordered) >= 2:
            return Intent(
                mode="pipeline",
                agents_needed=ordered,
                reason=f"命中多个环节关键词：{matched}，按顺序串联",
            )
        return Intent(
            mode="single",
            agents_needed=ordered,
            reason=f"命中单个环节关键词：{matched}",
        )

    # 3. 兜底：识别起讫地节点（如"义乌到宁波港"）→ 默认走 transport
    if any(loc in query for loc in ["义乌", "成都", "重庆", "宁波港", "上海港", "武汉"]):
        return Intent(
            mode="single",
            agents_needed=["transport"] if "transport" in available_agents else [],
            reason="识别到起讫地节点，默认路由到 transport",
        )

    # 4. 规则完全无法识别：交给 LLM 兜底（由 Coordinator 决定是否启用）
    return Intent(mode="single", agents_needed=[], reason="规则未命中，待 LLM 兜底")


_AGENT_DESC = {
    "transport": "国内段运输：起运地到口岸的铁路/公路/河运/多式联运方案、运费、时效、碳排放",
    "booking": "国际段订舱：船期、船公司、舱位、海运报价、议价",
    "customs": "报关：HS 归类、三单一致、申报要素、合规校验、风险与单证归并",
    "tracking": "在途追踪：物流状态、节点异常、延误、扣关、天气影响",
}


def llm_recognize(query: str, available_agents: List[str],
                  timeout: float = 15.0) -> Optional[Intent]:
    """LLM 意图兜底：仅当规则完全未命中时调用。

    返回 None 表示模型不可用/输出非法，调用方应回退到"无法识别，向用户提问"。
    """
    if not available_agents or not os.getenv("DEEPSEEK_API_KEY"):
        return None

    system = (
        "你是跨境电商出海物流的意图分类器。可用 Agent：\n"
        + "\n".join(f"- {a}：{_AGENT_DESC.get(a, '')}" for a in available_agents)
        + "\n\n规则：\n"
        "1. 用户只问一个环节 → mode=single，agents 只放该环节；\n"
        "2. 用户要多个环节或全流程 → mode=pipeline，agents 按顺序放需要的环节；\n"
        "3. agents 只能从上面的可用列表里选，不能编造；\n"
        "4. 与物流无关或信息不足以判断 → agents 返回空数组。\n"
        '只返回 JSON：{"mode":"single|pipeline","agents":["transport"],"reason":"一句话理由"}'
    )
    user = f"用户输入：{query}\n可用 Agent：{available_agents}"

    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=os.getenv("DEEPSEEK_API_KEY"),
            base_url="https://api.deepseek.com",
            timeout=timeout,
        )
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=200,
            response_format={"type": "json_object"},
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S)
        data = json.loads(raw)
    except Exception:
        return None

    allowed = set(available_agents)
    picked = []
    for a in data.get("agents") or []:
        if a in allowed and a in PIPELINE_ORDER and a not in picked:
            picked.append(a)
    picked = [a for a in PIPELINE_ORDER if a in picked]
    if not picked:
        return Intent(mode="single", agents_needed=[], reason="LLM 判为非物流/信息不足")

    mode = "pipeline" if data.get("mode") == "pipeline" and len(picked) >= 2 else "single"
    if mode == "single":
        picked = picked[:1]
    return Intent(
        mode=mode,
        agents_needed=picked,
        reason=f"[LLM兜底] {data.get('reason', '')}".strip(),
    )

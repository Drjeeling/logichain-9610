"""BaseAgent 抽象基类与标准化输出 AgentResult。

所有子 Agent 必须实现 ask(query, context) 方法。
context 是上游 Agent 的输出，用于多 Agent 串联。

AgentResult 包含：
- agent_name: 哪个 Agent 处理的
- summary: 自然语言总结（给用户看）
- data: 结构化数据（给下游 Agent 用）
- next_hint: 给下游 Agent 的提示（如"已选宁波港→推荐订舱Agent查询船期"）
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class AgentResult:
    """Agent 标准化输出。"""
    agent_name: str
    summary: str
    data: Dict[str, Any] = field(default_factory=dict)
    next_hint: str = ""               # 给下游 Agent 的提示
    confidence: float = 1.0           # 置信度 0~1
    needs_human_review: bool = False  # 是否需要人工复核

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent": self.agent_name,
            "summary": self.summary,
            "data": self.data,
            "next_hint": self.next_hint,
            "confidence": self.confidence,
            "needs_human_review": self.needs_human_review,
        }


class BaseAgent(ABC):
    """Agent 抽象基类。"""

    name: str = "base"
    description: str = ""

    @abstractmethod
    def ask(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> AgentResult:
        """处理用户请求，返回标准化结果。

        Args:
            query: 用户原始或经协调器传递的需求
            context: 上游 Agent 的输出（流水线模式下使用）
        """
        ...

    def can_handle(self, query: str) -> bool:
        """判断本 Agent 是否能处理该请求。默认 True，子类可重写。"""
        return True

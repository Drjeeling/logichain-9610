"""多 Agent 协调器架构。

设计要点：
1. BaseAgent 抽象基类：所有子 Agent 必须实现 ask() 方法
2. AgentResult 标准化输出：方便协调器串联调用
3. Coordinator 协调器：识别意图 → 路由 → 串联调用 → 汇总输出
4. 占位 Agent：其他 3 个 Agent 用 Stub 实现，后续替换真实逻辑即可

调用模式：
- 单 Agent 路由：用户问"国内运输" → 协调器只调用 TransportAgent
- 全流程串联：用户问"出海全流程" → 协调器依次调用 4 个 Agent，前一个输出作为后一个输入
"""

from .base import BaseAgent, AgentResult
from .transport import TransportAgent
from .customs import CustomsAgent
from .customs_orchestrator import CustomsOrchestrator, build_orchestrator
from .booking import BookingAgent
from .tracking import TrackingAgent
from .coordinator import Coordinator

__all__ = [
    "BaseAgent",
    "AgentResult",
    "TransportAgent",
    "CustomsAgent",
    "CustomsOrchestrator",
    "build_orchestrator",
    "BookingAgent",
    "TrackingAgent",
    "Coordinator",
]

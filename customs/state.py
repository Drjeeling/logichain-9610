"""报关域共享状态对象 + 显式状态机定义。

设计原则（对应全链路调研报告第 3.4 节）：
1. 状态转移在代码里写死，不由 LLM 决定下一跳。
2. 每个 Agent 只读状态对象中"自己需要的字段"，只写"自己拥有的字段"。
3. 任何 Agent 失败 → 编排器走降级链：重试(1次) → 降级模式 → 人工队列。
4. 全链路 audit_log：每次 Agent 调用记录输入摘要/输出/置信度/时间戳。

本模块不依赖任何 Agent 实现，可被编排器与各能力 Agent 共同引用。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List

# ------------------------------------------------------------------ #
# 能力 Agent 槽位（骨架的"插座标准"，冻结）
# ------------------------------------------------------------------ #
# 5 个新增能力 Agent + 1 个已有归类 Agent，全部由编排器按 key 注册。
SLOT_ORDER: List[str] = [
    "classify",         # A 归类（已有，改造收尾）
    "three_doc",        # B 三单预检（新增）
    "decl_compliance",  # C 清单合规（新增）
    "risk_screen",      # D 风险预判（新增，IP 复用+聚合）
    "merge",            # E 归并（新增）
    "risk_monitor",     # F 风险监控（新增）
]

# 每个槽位的接入契约：读哪些 state 字段、写哪些 state 字段。
# 各能力 Agent 实现时必须严格匹配；未注册的槽位编排器记 skipped，不阻断。
AGENT_IO_CONTRACT: Dict[str, Dict[str, List[str]]] = {
    "classify": {"reads": ["order", "sku_info"], "writes": ["hs_code", "field_gaps"]},
    "three_doc": {"reads": ["order", "sku_info"], "writes": ["three_doc_status"]},
    "decl_compliance": {"reads": ["declaration_draft", "hs_code"], "writes": ["decl_compliance"]},
    "risk_screen": {"reads": ["sku_info", "order", "hs_code"], "writes": ["risk_flags"]},
    "merge": {
        "reads": ["order", "sku_info", "hs_code", "declaration_dict", "decl_compliance"],
        "writes": ["merge_result"],
    },
    "risk_monitor": {"reads": ["timeline", "order"], "writes": ["timeline"]},
}

# ------------------------------------------------------------------ #
# 显式状态机（确定性，不由 LLM 路由）
# ------------------------------------------------------------------ #
# 链路状态（对应全链路 13 环节的报关侧）
LINK_STATES: List[str] = [
    "order_received", "classified", "precheck", "declared",
    "arrived", "inspected", "departed", "consolidated",
    "tax_registered", "closed",
]

# 状态 → 该状态要执行的能力槽位（一个状态可触发多个）
STATE_ACTIONS: Dict[str, List[str]] = {
    "order_received": ["classify"],
    "classified": ["three_doc", "decl_compliance"],
    "precheck": ["risk_screen"],
    "declared": [],
    "arrived": [],
    "inspected": [],
    "departed": ["merge"],
    "consolidated": ["risk_monitor"],
    "tax_registered": [],
    "closed": [],
}

# 状态推进表（写死，不由 LLM 决定）
STATE_TRANSITIONS: Dict[str, str] = {
    "order_received": "classified",
    "classified": "precheck",
    "precheck": "declared",
    "declared": "arrived",
    "arrived": "inspected",
    "inspected": "departed",
    "departed": "consolidated",
    "consolidated": "tax_registered",
    "tax_registered": "closed",
    "closed": "closed",
}

# 降级链（Agent 失败时依次尝试）
DEGRADE_CHAIN: List[str] = ["retry_once", "degrade_mode", "human_queue"]


@dataclass
class ShipmentState:
    """报关域类型化共享状态。

    各能力 Agent 通过编排器传入的 state 读写，不直接互传 data。
    """

    # ---- 输入（编排器入口填充）----
    order: Dict[str, Any] = field(default_factory=dict)
    sku_info: Dict[str, Any] = field(default_factory=dict)

    # ---- A 归类产出 ----
    hs_code: Dict[str, Any] = field(default_factory=dict)

    # ---- 申报产物 ----
    declaration_draft: str = ""
    declaration_dict: Dict[str, Any] = field(default_factory=dict)
    field_gaps: Dict[str, Any] = field(default_factory=dict)
    decl_elements: Dict[str, Any] = field(default_factory=dict)

    # ---- B 三单预检产出 ----
    three_doc_status: Dict[str, Any] = field(default_factory=dict)

    # ---- C 清单合规产出 ----
    decl_compliance: Dict[str, Any] = field(default_factory=dict)

    # ---- D 风险预判产出 ----
    risk_flags: List[Dict[str, Any]] = field(default_factory=list)

    # ---- E 归并产出 ----
    merge_result: Dict[str, Any] = field(default_factory=dict)

    # ---- F 监控产出（仅报关链路节点）----
    timeline: Dict[str, Any] = field(default_factory=dict)

    # ---- 流程元数据 ----
    status: str = "order_received"       # 当前链路状态
    tier: str = ""                        # 分流等级 A/B/C
    confidence: str = ""                  # high/medium/low
    degraded: bool = False                # 是否走了降级模式
    agent_slots: Dict[str, str] = field(default_factory=dict)  # 槽位 → success/skipped/error/absent
    audit_log: List[Dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    def log(self, step: str, **fields: Any) -> None:
        """写一条留痕。"""
        entry: Dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "step": step,
        }
        entry.update(fields)
        self.audit_log.append(entry)

    def transition(self) -> str:
        """按状态推进表前进到下一个状态，返回新状态。"""
        self.status = STATE_TRANSITIONS.get(self.status, "closed")
        return self.status

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "tier": self.tier,
            "confidence": self.confidence,
            "degraded": self.degraded,
            "hs_code": self.hs_code,
            "field_gaps": self.field_gaps,
            "declaration_dict": self.declaration_dict,
            "declaration": self.declaration_draft,
            "risk_flags": self.risk_flags,
            "three_doc_status": self.three_doc_status,
            "decl_compliance": self.decl_compliance,
            "merge_result": self.merge_result,
            "timeline": self.timeline,
            "agent_slots": self.agent_slots,
            "audit_log": self.audit_log,
        }

"""多 Agent 协调器 Demo 入口。

演示场景：
1. 单 Agent 路由：只问国内运输
2. 全流程串联：完整出海流程（运输→订舱→报关→追踪）

运行方式：
    cd /workspace/transport_agent
    python main_coordinator.py --demo
    python main_coordinator.py --query "..."
    python main_coordinator.py           # 交互式
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 自动加载 .env（python-dotenv），让 DEEPSEEK_API_KEY 等环境变量可用
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass  # 没装 python-dotenv 时，回退到系统环境变量

# 启动时打印 LLM 启用状态，方便调试
if os.getenv("DEEPSEEK_API_KEY") and not os.getenv("DEEPSEEK_API_KEY", "").startswith("sk-your-key-here"):
    print("[系统] DeepSeek API Key 已配置，LLM 智能解析已启用 ✓")
else:
    print("[系统] 未配置 DEEPSEEK_API_KEY，使用规则解析（如需 LLM，请填写 .env）")

from agents import (
    Coordinator, TransportAgent,
    build_orchestrator, BookingAgent, TrackingAgent,
)


DEMO_QUERIES = [
    # 单 Agent 场景
    "我在义乌有20吨货到宁波港，预算优先，11月发运",
    # 全流程串联场景
    "义乌发20吨日用百货到洛杉矶，11月全流程方案，预算优先",
    "成都30吨锂电池到洛杉矶，要全流程，12月发运",
]


def build_coordinator(month: int = 11, verbose: bool = True) -> Coordinator:
    """构建包含全部 4 个 Agent 的协调器。

    customs 入口由「报关编排器 CustomsOrchestrator」承担（对外接口不变），
    其内部 6 个能力槽位（classify/three_doc/decl_compliance/risk_screen/merge/risk_monitor）
    可增量接入，无需改动本函数。
    """
    coord = Coordinator(verbose=verbose)
    # use_llm=True：当 DEEPSEEK_API_KEY 已配置时，自动启用 LLM 意图解析 + 推荐解释
    coord.register("transport", TransportAgent(month=month, use_llm=True, verbose=False))
    coord.register("booking", BookingAgent())
    coord.register("customs", build_orchestrator())
    coord.register("tracking", TrackingAgent())
    return coord


def run_demo(month: int = 11):
    coord = build_coordinator(month=month, verbose=True)
    print("\n" + "=" * 70)
    print(f"多 Agent 协调器 Demo  | 月份={month}")
    print(f"已注册 Agent: {coord.available_agents}")
    print("=" * 70)

    for q in DEMO_QUERIES:
        result = coord.dispatch(q)
        print("\n" + "─" * 70)
        print("[最终输出]")
        print(result.final_summary)

    print("\n" + "=" * 70)
    print("Demo 完成")
    print("=" * 70)


def run_query(query: str, month: int = 11):
    coord = build_coordinator(month=month, verbose=True)
    result = coord.dispatch(query)
    print("\n" + "─" * 70)
    print("[最终输出]")
    print(result.final_summary)


def run_interactive(month: int = 11):
    coord = build_coordinator(month=month, verbose=True)
    print("\n" + "=" * 70)
    print("多 Agent 协调器交互模式（输入 quit 退出）")
    print(f"月份: {month}")
    print(f"可用 Agent: {coord.available_agents}")
    print("示例：'义乌20吨货到宁波港' 或 '义乌发20吨到洛杉矶全流程'")
    print("=" * 70)
    while True:
        try:
            q = input("\n[你] ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见")
            break
        if q.lower() in {"quit", "exit", "q"}:
            break
        if not q:
            continue
        result = coord.dispatch(q)
        print("\n[最终输出]")
        print(result.final_summary)


def main():
    parser = argparse.ArgumentParser(description="多 Agent 协调器")
    parser.add_argument("--demo", action="store_true", help="跑内置 Demo")
    parser.add_argument("--query", help="单次查询")
    parser.add_argument("--month", type=int, default=11, help="月份 1-12")
    args = parser.parse_args()

    if args.demo:
        run_demo(month=args.month)
    elif args.query:
        run_query(args.query, month=args.month)
    else:
        run_interactive(month=args.month)


if __name__ == "__main__":
    main()

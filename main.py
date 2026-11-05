"""国内段运输 Agent 演示入口。

运行方式：
    cd /workspace/transport_agent
    python main.py                  # 交互式
    python main.py --demo          # 跑内置 Demo 案例
    python main.py --query "..."   # 单次查询

环境变量：
    DEEPSEEK_API_KEY  可选，配置后启用 LLM 模式，未配置自动用规则模式
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 让 transport_agent 目录可直接作为包根
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
from agent.agent import build_agent


DEMO_QUERIES = [
    "我在义乌有20吨日用百货要发到宁波港，预算尽量低，10月份发运",
    "20吨货从义乌到宁波港，时效紧急，要最快",
    "成都30吨货到上海港，要折中方案，11月发运",
    "重庆到武汉的25吨货，省钱优先，12月发运",
]


def run_demo(month: int = 10, use_llm: bool = False):
    agent = build_agent(month=month, use_llm=use_llm, verbose=True)
    print("=" * 70)
    print(f"国内段运输 Agent Demo  | 月份={month}  | LLM模式={use_llm}")
    print("=" * 70)

    for q in DEMO_QUERIES:
        print(f"\n{'-'*70}\n[用户] {q}\n{'-'*70}")
        reply = agent.ask(q)
        print(f"[Agent]\n{reply}")

    print("\n" + "=" * 70)
    print("Demo 跑完，所有候选节点：", agent.graph.nodes())
    print("=" * 70)


def run_query(query: str, month: int = 10, use_llm: bool = False):
    agent = build_agent(month=month, use_llm=use_llm, verbose=True)
    reply = agent.ask(query)
    print(reply)


def run_interactive(month: int = 10, use_llm: bool = False):
    agent = build_agent(month=month, use_llm=use_llm, verbose=True)
    print("=" * 70)
    print("国内段运输 Agent 交互模式（输入 quit 退出）")
    print(f"月份: {month}  | LLM: {use_llm}")
    print("可用节点：", ", ".join(agent.graph.nodes()))
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
        print(f"\n[Agent]\n{agent.ask(q)}")


def main():
    parser = argparse.ArgumentParser(description="国内段运输 Agent")
    parser.add_argument("--demo", action="store_true", help="跑内置 Demo 案例")
    parser.add_argument("--query", help="单次查询")
    parser.add_argument("--month", type=int, default=10, help="月份 1-12，默认 10")
    parser.add_argument("--llm", action="store_true", help="启用 DeepSeek LLM 模式")
    args = parser.parse_args()

    load_dotenv()
    use_llm = args.llm or bool(os.getenv("DEEPSEEK_API_KEY"))

    if args.demo:
        run_demo(month=args.month, use_llm=use_llm)
    elif args.query:
        run_query(args.query, month=args.month, use_llm=use_llm)
    else:
        run_interactive(month=args.month, use_llm=use_llm)


if __name__ == "__main__":
    main()

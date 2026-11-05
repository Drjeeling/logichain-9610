"""多 Agent 协调器：识别意图 → 路由 → 串联调用 → 汇总输出。

支持两种执行模式：
1. single：单 Agent 路由，只调用一个 Agent
2. pipeline：全流程串联，前一个 Agent 的输出作为后一个的 context

增强功能：
- 异常容错：单 Agent 报错不阻断下游 Agent
- 调用历史 trace：每次调用都记录，便于回溯调试
- 流程可视化：输出文本流程图，方便演示
- 状态摘要：每个 Agent 处理后输出关键状态

后续替换真实 Agent 时，只需在 register 时传入新实例，协调器无需改动。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .base import BaseAgent, AgentResult
from .intent import recognize, llm_recognize, Intent


@dataclass
class AgentStep:
    """单步 Agent 调用记录。"""
    agent_name: str
    query: str
    context_keys: List[str]              # 上游传入的 context 字段
    status: str                           # "success" / "error" / "skipped"
    duration_sec: float
    result_summary: str                   # 输出摘要
    next_hint: str
    error: Optional[str] = None


@dataclass
class CoordinatorResult:
    """协调器最终输出。"""
    intent: Intent
    results: List[AgentResult] = field(default_factory=list)
    final_summary: str = ""
    steps: List[AgentStep] = field(default_factory=list)  # 调用 trace
    total_duration_sec: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": {
                "mode": self.intent.mode,
                "agents_needed": self.intent.agents_needed,
                "reason": self.intent.reason,
            },
            "results": [r.to_dict() for r in self.results],
            "final_summary": self.final_summary,
            "steps": [
                {
                    "agent": s.agent_name,
                    "status": s.status,
                    "duration": round(s.duration_sec, 2),
                    "summary": s.result_summary[:80],
                    "next_hint": s.next_hint,
                    "error": s.error,
                } for s in self.steps
            ],
            "total_duration": round(self.total_duration_sec, 2),
        }


class Coordinator:
    """多 Agent 协调器。"""

    def __init__(
        self,
        agents: Optional[Dict[str, BaseAgent]] = None,
        verbose: bool = True,
        continue_on_error: bool = True,
        enable_ocr_preprocess: bool = True,
        use_llm_intent: bool = True,
    ):
        """
        Args:
            continue_on_error: 单 Agent 报错时是否继续调用下游（默认 True）
            enable_ocr_preprocess: 入口是否启用 OCR 预处理（识图→抽字段→存 context）
        """
        self._agents: Dict[str, BaseAgent] = agents or {}
        self.verbose = verbose
        self.continue_on_error = continue_on_error
        self.enable_ocr_preprocess = enable_ocr_preprocess
        self.use_llm_intent = use_llm_intent
        # OCR 预处理器（懒加载，避免无图片时也加载 paddleocr）
        self._ocr_extractor = None

    def _get_ocr(self):
        """懒加载 OCR 抽取器。"""
        if self._ocr_extractor is None:
            try:
                from customs.ocr import OCRExtractor
                use_vlm = bool(__import__("os").getenv("DEEPSEEK_API_KEY"))
                self._ocr_extractor = OCRExtractor(use_vlm=use_vlm, verbose=self.verbose)
            except ImportError:
                self._ocr_extractor = False  # 标记不可用
        return self._ocr_extractor if self._ocr_extractor is not False else None

    def _preprocess_input(self, query: str) -> tuple[str, Optional[Dict[str, Any]]]:
        """入口预处理：识别图片输入 → 抽取字段 → 拼成增强 query + context。

        返回 (enhanced_query, pre_context)：
        - enhanced_query：拼了图片字段的自然语言 query，下游 agent 用
        - pre_context：抽取的结构化字段，作为初始 context 传给第一个 agent
        """
        if not self.enable_ocr_preprocess:
            return query, None

        # 判断是否含图片路径（纯路径或「文字需求（发票见 /path/x.png）」均可）
        m = re.search(r"[^\s（）()]+\.(?:jpg|jpeg|png|bmp)", query, re.I)
        if not m:
            return query, None
        image_path = m.group(0)

        if self.verbose:
            print(f"[Coordinator] 检测到图片输入，启动 OCR 预处理...")

        ocr = self._get_ocr()
        if not ocr:
            if self.verbose:
                print(f"[Coordinator] OCR 模块不可用，原样传递 query")
            return query, None

        cargo = ocr.extract(image_path)
        if self.verbose:
            print(f"[Coordinator] OCR 抽取结果: {cargo.to_dict()}")

        # 拼成增强 query：保留用户文字需求 + 图片字段转文字描述
        text_query = re.sub(r"（发票见[^）]*）", "", query).strip()
        parts = []
        if cargo.cargo_name and cargo.cargo_name != "未识别商品":
            parts.append(cargo.cargo_name)
        if cargo.tonnage > 0:
            parts.append(f"{cargo.tonnage}吨")
        enhanced = " ".join(x for x in [text_query, *parts] if x) or query

        # 同时把图片字段存入 pre_context，作为初始 context
        pre_context = {
            "from_image": True,
            "image_path": image_path,
            "cargo_info": cargo.to_dict(),
            "cargo_name": cargo.cargo_name,
            "tonnage": cargo.tonnage,
            "unit_price_usd": cargo.unit_price_usd,
        }
        return enhanced, pre_context

    def register(self, name: str, agent: BaseAgent) -> None:
        """注册一个 Agent。"""
        self._agents[name] = agent
        if self.verbose:
            print(f"[Coordinator] 已注册 Agent: {name} ({agent.__class__.__name__})")

    @property
    def available_agents(self) -> List[str]:
        return list(self._agents.keys())

    # ----------------------------------------------------------------- #
    # 主入口
    # ----------------------------------------------------------------- #
    def dispatch(
        self,
        query: str,
        extra_context: Optional[Dict[str, Any]] = None,
    ) -> CoordinatorResult:
        """用户提问 → [OCR预处理] → 识别意图 → 路由/串联 → 汇总输出。

        extra_context：调用方注入的初始 context（如交互闸门的
        interactive / selected_option），会与 OCR 预处理结果合并，随流水线透传。
        """
        start = time.time()
        if self.verbose:
            print(f"\n{'='*70}")
            print(f"[Coordinator] 用户需求: {query}")
            print(f"{'='*70}")

        # 0. OCR 预处理（图片输入 → 抽字段 → 增强 query + 初始 context）
        enhanced_query, pre_context = self._preprocess_input(query)
        # 0.1 注入外部初始 context（如 interactive / selected_option）
        if extra_context:
            pre_context = {**(pre_context or {}), **extra_context}
        if pre_context and self.verbose:
            print(f"[Coordinator] OCR 预处理完成: enhanced_query='{enhanced_query}'")

        # 1. 意图识别（用增强后的 query）
        intent = recognize(enhanced_query, self.available_agents)
        if not intent.agents_needed and self.use_llm_intent:
            # K4：规则未命中时用 LLM 兜底；失败则保持空意图，向用户提问而不是乱路由
            fallback = llm_recognize(enhanced_query, self.available_agents)
            if fallback is not None:
                intent = fallback
        if self.verbose:
            print(f"[Coordinator] 意图识别: {intent}")

        if not intent.agents_needed:
            return CoordinatorResult(
                intent=intent,
                final_summary="无法识别意图，请明确需求，例如：'义乌20吨货到宁波港，预算优先'",
            )

        # 2. 按模式执行（pre_context 作为初始 context 传入）
        if intent.mode == "single":
            results, steps = self._run_single(enhanced_query, intent.agents_needed[0], pre_context)
        else:  # pipeline
            results, steps = self._run_pipeline(enhanced_query, intent.agents_needed, pre_context)

        # 3. 汇总最终输出
        final = self._summarize(query, results)
        total_dur = time.time() - start
        if self.verbose:
            print(f"\n[Coordinator] 执行完成，共 {len(results)} 个 Agent 参与，"
                  f"耗时 {total_dur:.2f}s")
            print(self._render_flow_chart(steps))

        return CoordinatorResult(
            intent=intent,
            results=results,
            final_summary=final,
            steps=steps,
            total_duration_sec=total_dur,
        )

    # ----------------------------------------------------------------- #
    # 执行模式
    # ----------------------------------------------------------------- #
    def _run_single(
        self,
        query: str,
        agent_name: str,
        pre_context: Optional[Dict[str, Any]] = None,
    ) -> tuple[List[AgentResult], List[AgentStep]]:
        """单 Agent 路由执行。"""
        agent = self._agents[agent_name]
        result, step = self._safe_call(agent, query, context=pre_context, step_num=1, total=1)
        return [result], [step]

    def _run_pipeline(
        self,
        query: str,
        agent_names: List[str],
        pre_context: Optional[Dict[str, Any]] = None,
    ) -> tuple[List[AgentResult], List[AgentStep]]:
        """全流程串联：前一个的输出作为后一个的 context。

        异常容错：单 Agent 报错时记录错误但不中断，下游 Agent 用最后成功的 context 继续。
        pre_context：协调器 OCR 预处理产生的初始 context（如图片抽取的字段）。
        """
        results: List[AgentResult] = []
        steps: List[AgentStep] = []
        # 初始 context = OCR 预处理结果（若有）
        context: Optional[Dict[str, Any]] = pre_context

        n = len(agent_names)
        for i, name in enumerate(agent_names):
            agent = self._agents[name]
            if self.verbose:
                print(f"\n[Coordinator] 步骤 {i+1}/{n}: 调用 {name}")
                if context:
                    print(f"[Coordinator] 上游 context keys: {list(context.keys())}")

            # 安全调用：捕获异常
            result, step = self._safe_call(
                agent, query, context=context, step_num=i+1, total=n
            )
            steps.append(step)

            # 异常处理
            if step.status == "error":
                if self.verbose:
                    print(f"[Coordinator] ⚠️ Agent {name} 报错: {step.error}")
                if not self.continue_on_error:
                    break
                # 用一个空 AgentResult 占位，下游继续
                results.append(AgentResult(
                    agent_name=name,
                    summary=f"⚠️ {name} Agent 调用失败：{step.error}",
                    confidence=0.0,
                ))
                continue

            results.append(result)

            # 更新 context，传给下一个 Agent
            context = self._merge_context(context, result)

            if self.verbose and result.next_hint:
                print(f"[Coordinator] 下游提示: {result.next_hint}")

        return results, steps

    # ----------------------------------------------------------------- #
    # 安全调用 + trace
    # ----------------------------------------------------------------- #
    def _safe_call(
        self,
        agent: BaseAgent,
        query: str,
        context: Optional[Dict[str, Any]],
        step_num: int,
        total: int,
    ) -> tuple[AgentResult, AgentStep]:
        """安全调用一个 Agent，捕获异常并记录 trace。"""
        ctx_keys = list(context.keys()) if context else []
        start = time.time()

        try:
            result = agent.ask(query, context=context)
            dur = time.time() - start
            summary_short = result.summary[:80].replace("\n", " ")
            step = AgentStep(
                agent_name=agent.name,
                query=query[:60],
                context_keys=ctx_keys,
                status="success",
                duration_sec=dur,
                result_summary=summary_short,
                next_hint=result.next_hint,
            )
            if self.verbose:
                print(f"[Coordinator] ✓ {agent.name} 完成 ({dur:.2f}s)")
            return result, step
        except Exception as e:
            dur = time.time() - start
            err_msg = f"{type(e).__name__}: {e}"
            step = AgentStep(
                agent_name=agent.name,
                query=query[:60],
                context_keys=ctx_keys,
                status="error",
                duration_sec=dur,
                result_summary="(调用失败)",
                next_hint="",
                error=err_msg,
            )
            if self.verbose:
                print(f"[Coordinator] ✗ {agent.name} 失败: {err_msg}")
            # 返回兜底 AgentResult，避免外层 None
            return AgentResult(
                agent_name=agent.name,
                summary=f"⚠️ {agent.name} Agent 调用失败：{err_msg}",
                confidence=0.0,
            ), step

    # ----------------------------------------------------------------- #
    # Context 合并
    # ----------------------------------------------------------------- #
    @staticmethod
    def _merge_context(
        old: Optional[Dict[str, Any]],
        result: AgentResult,
    ) -> Dict[str, Any]:
        """把上游 Agent 输出合并到 context，传给下游。

        关键字段：
        - chosen.destination_port: 来自 transport，作为下游 booking 的起运港
        - from_port / dest_port: 来自 booking，传给 customs
        - 全部 result.data 字段透传
        """
        new_ctx = dict(old or {})
        new_ctx.update(result.data)
        new_ctx["previous_summary"] = result.summary
        new_ctx["previous_agent"] = result.agent_name

        # 显式提取关键字段，避免下游找不到
        chosen = result.data.get("chosen") or {}
        if chosen.get("destination_port"):
            new_ctx["destination_port"] = chosen["destination_port"]
            new_ctx["from_port"] = chosen["destination_port"]

        # booking 输出的 from_port/dest_port 也保留
        if result.data.get("from_port") and not new_ctx.get("from_port"):
            new_ctx["from_port"] = result.data["from_port"]
        if result.data.get("dest_port"):
            new_ctx["dest_port"] = result.data["dest_port"]

        return new_ctx

    # ----------------------------------------------------------------- #
    # 汇总输出
    # ----------------------------------------------------------------- #
    @staticmethod
    def _summarize(query: str, results: List[AgentResult]) -> str:
        """把各 Agent 的 summary 拼成最终输出。"""
        if not results:
            return "未找到可用 Agent"
        if len(results) == 1:
            return results[0].summary

        parts = [f"## 多 Agent 协同处理结果\n用户需求：{query}\n"]
        for i, r in enumerate(results, 1):
            parts.append(f"\n### 步骤 {i}: {r.agent_name} Agent")
            parts.append(r.summary)
            if r.next_hint:
                parts.append(f"\n_下一步提示: {r.next_hint}_")

        parts.append("\n---\n**全流程执行完成**")
        return "\n".join(parts)

    # ----------------------------------------------------------------- #
    # 流程可视化
    # ----------------------------------------------------------------- #
    @staticmethod
    def _render_flow_chart(steps: List[AgentStep]) -> str:
        """渲染文本流程图，便于演示。"""
        if not steps:
            return ""

        lines = ["\n[流程图]"]
        for i, s in enumerate(steps):
            icon = {"success": "✓", "error": "✗", "skipped": "—"}.get(s.status, "?")
            lines.append(
                f"  {i+1}. [{icon}] {s.agent_name}  "
                f"({s.duration_sec:.2f}s) → {s.result_summary}"
            )
            if s.next_hint:
                lines.append(f"      ↓ {s.next_hint}")
        return "\n".join(lines)

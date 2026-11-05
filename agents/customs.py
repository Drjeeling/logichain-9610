"""报关 Agent（智关 Agent 升级版）。

三大核心创新：
1. 可解释归类推理链 — 不只输出 HS 编码，还输出推理路径 + 依据 + 反事实对比 + 置信度
2. 主动提问机制 — 信息不完整时不猜，而是生成结构化提问
3. 三级置信度分流 — 高置信自动拟单 / 中置信强制确认 / 低置信挂起提问

场景聚焦：跨境电商 9610 出口申报清单
轻量留痕：记录 AI 建议的编码、引用的规则、人工修改轨迹

协调器代码无需改动，对外接口完全兼容。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# 加入项目根目录到 sys.path，便于 import customs 子包
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from customs.ocr import OCRExtractor, CargoInfo
from customs.hs_kb import HSKnowledgeBase
from customs.declaration import DeclarationGenerator, ComplianceRulesEngine
from .base import BaseAgent, AgentResult
from pathlib import Path

# 尝试加载字段字典（v4 新资产，缺失时回退到旧制单逻辑）
try:
    import json as _json
    _FIELD_DICT_PATH = Path(__file__).resolve().parent.parent / "customs" / "data" / "field_dict.json"
    FIELD_DICT = _json.loads(_FIELD_DICT_PATH.read_text(encoding="utf-8")) if _FIELD_DICT_PATH.exists() else None
except Exception:
    FIELD_DICT = None

# 字段缺口自主分析器（规则驱动：缺什么/为什么/自解还是提问）
try:
    from customs.field_gap import FieldGapAnalyzer
    _GAP_ANALYZER = FieldGapAnalyzer()
except Exception:
    _GAP_ANALYZER = None


class CustomsAgent(BaseAgent):
    """报关单证智能体（智关 Agent 升级版）。"""

    name = "customs"
    description = "可解释归类推理 + 主动提问 + 三级置信度分流 + 9610申报清单 + 合规校验 + 留痕存证"

    def __init__(
        self,
        use_ocr: bool = True,
        use_chroma: bool = False,
        verbose: bool = False,
    ):
        self.use_ocr = use_ocr
        self.verbose = verbose
        self.ocr = OCRExtractor(use_vlm=True, verbose=verbose)
        self.hs_kb = HSKnowledgeBase(use_chroma=use_chroma, verbose=verbose)
        self.decl_gen = DeclarationGenerator()
        self.rules_engine = ComplianceRulesEngine()
        # 轻量留痕：记录本次决策链
        self._audit_trail: List[Dict] = []

    # ----------------------------------------------------------------- #
    # BaseAgent 接口
    # ----------------------------------------------------------------- #
    def can_handle(self, query: str) -> bool:
        keywords = [
            "报关", "HS", "税则", "单证", "发票", "装箱单", "归类", "关税",
            "9610", "跨境电商", "申报清单", "退税",
            # 20 品类覆盖
            "充电宝", "移动电源", "手机壳", "LED", "灯带", "T恤", "收纳盒",
            "玩具", "美妆", "保温杯", "钢化膜", "耳环", "运动鞋", "智能手表",
            "瑜伽垫", "假发", "蓝牙耳机", "指甲", "钥匙扣", "硅胶", "帆布", "数据线",
            "锂电池", "服装", "家具", "日用百货", "机电",
        ]
        return any(k in query for k in keywords)

    def ask(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> AgentResult:
        """处理报关需求（智关 Agent 全流程）。"""
        if self.verbose:
            print(f"[CustomsAgent] 收到: {query}")
            if context:
                print(f"[CustomsAgent] 上游 context: {list(context.keys())}")

        # 初始化留痕
        audit_steps = []
        t0 = time.time()

        # 1. OCR / 文字抽取商品信息
        cargo_info = self._extract_cargo(query, context)
        cargo_name = cargo_info.cargo_name
        if self.verbose:
            print(f"[CustomsAgent] 商品信息: {cargo_info.to_dict()}")

        if not cargo_name or cargo_name == "未识别商品":
            return AgentResult(
                agent_name=self.name,
                summary=(
                    "⚠️ 未能从输入中识别商品信息。\n"
                    "请明确商品名（如'充电宝'/'手机壳'/'LED灯带'/'数据线'），"
                    "或上传发票图片路径。"
                ),
                confidence=0.3,
            )

        audit_steps.append({
            "step": "商品信息提取",
            "input": query[:100],
            "output": cargo_name,
            "source": cargo_info.source,
            "elapsed_ms": int((time.time() - t0) * 1000),
        })

        # 2. 主动提问：检测信息缺口
        info_gap = self.hs_kb.detect_info_gap(cargo_name)
        if info_gap and self.verbose:
            print(f"[CustomsAgent] 信息缺口检测: {info_gap['question']}")

        # 2.5 知识产权风险独立检测（P1 修正：跨境电商高频触发条件）
        t_ip = time.time()
        ip_risk = self.hs_kb.detect_ip_risk(cargo_name)
        if ip_risk and self.verbose:
            print(f"[CustomsAgent] IP_RISK 命中品牌词: {ip_risk['matched_brands']}")
        audit_steps.append({
            "step": "知识产权风险检测",
            "ip_risky": bool(ip_risk and ip_risk.get("is_risky")),
            "matched_brands": (ip_risk or {}).get("matched_brands", []),
            "elapsed_ms": int((time.time() - t_ip) * 1000),
        })

        # 3. RAG 检索 HS 编码候选
        candidates = self.hs_kb.lookup(cargo_name, top_k=3)
        if not candidates:
            return AgentResult(
                agent_name=self.name,
                summary=f"⚠️ 知识库中未找到匹配「{cargo_name}」的 HS 编码。\n"
                        f"建议人工核查海关税则。",
                confidence=0.4,
            )

        if self.verbose:
            print(f"[CustomsAgent] 候选 HS: {[c['code'] for c in candidates]}")

        # 4. 可解释归类推理：LLM 在候选中选择 + 输出推理链
        t1 = time.time()
        chosen_hs = self.hs_kb.choose(candidates, cargo_name)
        reasoning_chain = chosen_hs.get("reasoning_chain", [])
        counterfactual = chosen_hs.get("counterfactual", {})
        confidence_level = chosen_hs.get("confidence", "medium")
        confidence_reason = chosen_hs.get("confidence_reason", "")
        degraded = bool(chosen_hs.get("degraded", False))

        if self.verbose:
            print(f"[CustomsAgent] 选择: {chosen_hs['code']} - {chosen_hs['description']}")
            print(f"[CustomsAgent] 置信度: {confidence_level}")
            if degraded:
                print(f"[CustomsAgent] 降级标记: degraded=True（推理链缺失）")
            if reasoning_chain:
                print(f"[CustomsAgent] 推理链: {len(reasoning_chain)} 步")

        audit_steps.append({
            "step": "HS编码归类",
            "candidates": [c["code"] for c in candidates],
            "selected": chosen_hs["code"],
            "confidence": confidence_level,
            "has_reasoning": len(reasoning_chain) > 0,
            "degraded": degraded,
            "elapsed_ms": int((time.time() - t1) * 1000),
        })

        # 4.5 跨章候选争议检测（D/B 板块：39 章 vs 85 章不得单选）
        dispute = self._detect_dispute(candidates)
        resolution: Optional[Dict[str, Any]] = None
        if dispute:
            # B板块 v2.1：事实驱动的争议消解——官方分列维度事实齐备时可给出 CANDIDATE
            resolution = self._resolve_dispute_with_facts(dispute, context, candidates)
            if resolution:
                chosen_hs = resolution["chosen"]
                reasoning_chain = resolution["reasoning"] + list(reasoning_chain)
                confidence_reason = (confidence_reason + "；" if confidence_reason else "") + (
                    f"争议已由分列维度事实消解 → CANDIDATE {chosen_hs['code']}"
                    f"（归类结论待判，人工一键确认后 REVIEWED）"
                )
                if self.verbose:
                    print(f"[CustomsAgent] 争议消解: → {chosen_hs['code']}（CANDIDATE）")
            else:
                # 证据不足时不得宣称高置信（报告 §2.5）
                if confidence_level == "high":
                    confidence_level = "medium"
                confidence_reason = (confidence_reason + "；" if confidence_reason else "") + (
                    f"跨章候选并存（{'/'.join(dispute['chapters'])} 章），存在归类争议，分列维度事实缺失"
                )
                # NEED_MORE_FACTS：追问来自候选条目的官方分列维度（数据驱动）
                open_questions: List[str] = []
                for c in candidates:
                    dim = c.get("split_dimension")
                    if dim:
                        open_questions.append(f"同一箱体内扬声器单元数量？（分列维度：{dim}）")
                        open_questions.append("多个扬声器单元是否装于同一箱体（是否共用箱体）？")
                if open_questions:
                    dispute["open_questions"] = open_questions

        # 5. 三级置信度分流（P1：传入 IP_RISK + degraded 触发条件）
        tier = self._determine_tier(
            confidence_level, cargo_name, chosen_hs, context,
            ip_risk=ip_risk,
            degraded=degraded,
            dispute=dispute,
        )
        if self.verbose:
            print(f"[CustomsAgent] 分流等级: {tier}")

        # 6. 先生成结构化 dict，再渲染 9610 申报清单（v4：dict→JSON 是主输出，文本模板向后兼容）
        t2 = time.time()
        declaration_dict = self._build_declaration_dict(
            cargo=cargo_name,
            hs_code=chosen_hs,
            context=context,
            cargo_info=cargo_info.to_dict(),
        )
        # 6.5 字段缺口自主分析（规则驱动）：缺什么 / 为什么 / 能否自解 / 还是提问
        field_gaps = None
        if _GAP_ANALYZER is not None:
            field_gaps = _GAP_ANALYZER.analyze(
                declaration_dict, context or {}, chosen_hs,
                (context or {}).get("doc_type", ""),
            )
            declaration_dict = _GAP_ANALYZER.apply_self_resolved(declaration_dict, field_gaps)
        declaration = self._render_9610(
            decl_dict=declaration_dict,
            reasoning_chain=reasoning_chain,
            counterfactual=counterfactual,
            confidence=confidence_level,
            confidence_reason=confidence_reason,
        )

        audit_steps.append({
            "step": "申报清单生成",
            "template": "declaration_9610.j2",
            "trade_mode": "9610",
            "dict_fields": len(declaration_dict),
            "elapsed_ms": int((time.time() - t2) * 1000),
        })

        # 7. 合规校验
        t3 = time.time()
        risks = self.rules_engine.validate(cargo_name, chosen_hs, context)
        if dispute and not resolution:
            risks.append({
                "rule_id": "DISPUTE",
                "rule_name": "跨章候选归类争议",
                "risk": "候选编码分属 " + "/".join(dispute["chapters"]) + " 章，不能单选："
                        + "；".join(f"{c['code']} {c['official_name']}" for c in dispute["candidates"]),
                "advice": dispute["advice"],
                "severity": "warning",
            })
        elif dispute and resolution:
            risks.append({
                "rule_id": "DISPUTE_RESOLVED",
                "rule_name": "归类争议已消解（事实驱动）",
                "risk": f"跨章候选争议已按分列维度事实消解 → CANDIDATE {resolution['chosen']['code']}；"
                        f"排除 {resolution['rejected']}（{resolution['rejected_reason']}）",
                "advice": "归类结论为 CANDIDATE，人工一键确认后转为 REVIEWED；如需权威结论可申请归类预裁定",
                "severity": "info",
            })
        audit_steps.append({
            "step": "合规规则校验",
            "rules_triggered": [r["rule_id"] for r in risks],
            "max_severity": max((r["severity"] for r in risks), default="none"),
            "elapsed_ms": int((time.time() - t3) * 1000),
        })

        # 8. 留痕
        self._audit_trail.append({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "query": query[:200],
            "cargo": cargo_name,
            "hs_code": chosen_hs["code"],
            "confidence": confidence_level,
            "tier": tier,
            "risks": [r["rule_id"] for r in risks],
            "steps": audit_steps,
            "total_elapsed_ms": int((time.time() - t0) * 1000),
        })

        # 9. 拼装输出
        summary_lines = self._build_summary(
            cargo_name=cargo_name,
            chosen_hs=chosen_hs,
            cargo_info=cargo_info,
            context=context,
            declaration=declaration,
            risks=risks,
            reasoning_chain=reasoning_chain,
            counterfactual=counterfactual,
            confidence_level=confidence_level,
            confidence_reason=confidence_reason,
            tier=tier,
            info_gap=info_gap,
            ip_risk=ip_risk,
            audit_steps=audit_steps,
        )

        if dispute:
            if resolution:
                summary_lines.append(
                    f"- **✅ 归类争议已消解（CANDIDATE）**：分列维度事实齐备 → "
                    f"`{resolution['chosen']['code']}` {resolution['chosen'].get('official_name') or ''}；"
                    f"排除 `{resolution['rejected']}`（{resolution['rejected_reason']}）"
                )
                summary_lines.append("- **处置**：不拦截制单；人工一键确认后 CANDIDATE → REVIEWED")
            else:
                summary_lines.append(
                    f"- **⚠️ 归类争议（DISPUTE / NEED_MORE_FACTS）**：候选分属 {'/'.join(dispute['chapters'])} 章 → "
                    + " / ".join(f"`{c['code']}` {c['official_name']}" for c in dispute["candidates"])
                    + "；不拦截制单，须人工确认"
                )
                if dispute.get("open_questions"):
                    summary_lines.append("- **需补充的分列维度事实（追问）**：")
                    for q in dispute["open_questions"]:
                        summary_lines.append(f"  - ❓ {q}")
                summary_lines.append(f"- **建议动作**：{dispute['advice']}")

        # 字段缺口自主分析结果上浮（规则驱动，非预制答案）
        if field_gaps and (field_gaps["ask_user"] or field_gaps["self_resolved"]):
            summary_lines.append(
                f"- **字段缺口自主分析**：必填 {field_gaps['required_total']} 项，"
                f"已具备 {field_gaps['filled']}，可自解 {len(field_gaps['self_resolved'])}，"
                f"需提问 {len(field_gaps['ask_user'])}"
            )
            for item in field_gaps["self_resolved"][:3]:
                summary_lines.append(
                    f"  - ⚙️ 自解 [{item['field']}] = {item['value']}（{item['method']}）"
                )
            for item in field_gaps["ask_user"][:5]:
                summary_lines.append(
                    f"  - ❓ 提问 [{item['field']}] {item['question']}——{item['reason'][:70]}"
                )

        needs_review = (
            tier in ("B", "C")
            or bool(ip_risk and ip_risk.get("is_risky"))
            or bool(field_gaps and field_gaps["ask_user"])
            or any(r["severity"] in ("warning", "critical") for r in risks)
        )
        confidence_score = {"high": 0.95, "medium": 0.75, "low": 0.45}.get(
            confidence_level, 0.6
        )

        # 给下游 tracking 的提示
        next_hint = ""
        if context and ((context.get("dest_port") or context.get("destination_port"))):
            dest_str = context.get("dest_port") or context.get("destination_port")
            next_hint = (
                f"报关清单已生成（目的港：{dest_str}），"
                f"HS编码 {chosen_hs['code']}，置信度 {confidence_level}；"
                f"建议下游追踪 Agent 创建运单并监控清关"
            )
            if risks:
                next_hint += f"；注意：{risks[0]['risk']}"
            if ip_risk and ip_risk.get("is_risky"):
                next_hint += f"；知识产权风险：{ip_risk['advice'][:30]}..."

        return AgentResult(
            agent_name=self.name,
            summary="\n".join(summary_lines),
            data={
                "cargo": cargo_name,
                "cargo_info": cargo_info.to_dict(),
                "hs_code": chosen_hs,
                "candidates": [c["code"] for c in candidates],
                "dispute": dispute,
                "dispute_resolution": resolution,
                "field_gaps": field_gaps,
                "declaration_dict": declaration_dict,  # v4：结构化主输出
                "declaration": declaration,              # 向后兼容文本
                "risks": risks,
                "reasoning_chain": reasoning_chain,
                "counterfactual": counterfactual,
                "confidence": confidence_level,
                "confidence_reason": confidence_reason,
                "degraded": degraded,
                "tier": tier,
                "info_gap": info_gap,
                "ip_risk": ip_risk,
                "audit_trail": audit_steps,
                "destination_port": (context or {}).get("destination_port"),
                "dest_port": (context or {}).get("dest_port"),
            },
            next_hint=next_hint,
            confidence=confidence_score,
            needs_human_review=needs_review,
        )

    @staticmethod
    def _detect_dispute(candidates: List[Dict]) -> Optional[Dict[str, Any]]:
        """跨章候选争议检测：同一查询命中 ≥2 个不同 2 位章的候选。

        场景 1（3926909090 / 8518220000）的依据：不得单选，须输出 DISPUTE
        并建议归类预裁定（海关总署令第 236 号第 7/13 条）。
        """
        valid = [c for c in (candidates or []) if c.get("_norm_score", 0) >= 0.35]
        chapters = sorted({c.get("code", "")[:2] for c in valid if c.get("code")})
        if len(chapters) < 2:
            return None
        return {
            "chapters": chapters,
            "candidates": [
                {
                    "code": c.get("code"),
                    "official_name": c.get("official_name") or c.get("description"),
                    "chapter": c.get("code", "")[:2],
                    "score": c.get("_norm_score"),
                    "confidence": c.get("confidence", "candidate"),
                }
                for c in valid
            ],
            "advice": "不得单选；制单可继续但须人工确认，建议向备案地直属海关申请归类预裁定"
                      "（货物拟进出口 3 个月前提出，有效期 3 年）",
        }

    # ----------------------------------------------------------------- #
    # 事实驱动的争议消解（B 板块 v2.1，通用规则评估器）
    # ----------------------------------------------------------------- #
    @staticmethod
    def _resolve_dispute_with_facts(
        dispute: Optional[Dict[str, Any]],
        context: Optional[Dict[str, Any]],
        candidates: List[Dict],
    ) -> Optional[Dict[str, Any]]:
        """用候选条目自带的 resolution_rule（数据规则）评估事实，消解跨章争议。

        机制与依据完全来自知识库条目（hs_codes.json）：
        - when_facts：事实满足条件（支持布尔与 ">=n"/"<=n"/"==n" 数值比较）
        - tax_heading_note / split_dimension / combination_rule：论证链素材
        逻辑层只做通用条件评估，不内置任何具体税号/场景——换一个有争议的
        税号时补数据即可，不改代码。事实缺失或条件不满足 → None（保留 DISPUTE）。
        """
        if not dispute:
            return None
        ctx = context or {}
        facts = ctx.get("cargo_facts") or {}
        if not facts:
            return None

        by_code = {c.get("code"): c for c in candidates}

        def _fact_ok(value, cond) -> bool:
            if isinstance(cond, bool):
                return value is cond
            if isinstance(cond, str):
                s = cond.strip()
                ops = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b,
                       "==": lambda a, b: a == b, ">": lambda a, b: a > b,
                       "<": lambda a, b: a < b}
                for op, fn in ops.items():
                    if s.startswith(op):
                        try:
                            return fn(float(value), float(s[len(op):]))
                        except (TypeError, ValueError):
                            return False
            return value == cond

        for code, cand in by_code.items():
            rule = cand.get("resolution_rule")
            if not rule:
                continue
            when = rule.get("when_facts") or {}
            # 全部条件满足才消解；任一事实缺失即不满足
            if not all(
                k in facts and _fact_ok(facts.get(k), cond)
                for k, cond in when.items()
            ):
                continue
            dimension = cand.get("split_dimension", "")
            # 事实匹配句（由事实值组装，不预制）
            fact_bits = []
            for k in when:
                if k in facts:
                    fact_bits.append(f"{k}={facts[k]}")
            reasoning = [
                rule.get("tax_heading_note", "品目条文命中"),
                f"税则分列维度：{dimension}——按分列维度事实判定" if dimension else "税则分列维度判定",
                "事实匹配：" + "、".join(fact_bits) + "（上游 cargo_facts 提供）",
                rule.get("combination_rule", "归类总规则：按基本特征判定"),
            ]
            rejected = next((k for k in by_code if k != code), None)
            return {
                "chosen": cand,
                "rejected": rejected,
                "rejected_reason": rule.get("reject_reason", "按基本特征排除"),
                "dimension": dimension,
                "reasoning": reasoning,
                "status": "CANDIDATE",
            }
        return None

    # ----------------------------------------------------------------- #
    # 三级置信度分流
    # ----------------------------------------------------------------- #
    @staticmethod
    def _determine_tier(
        confidence: str,
        cargo: str,
        hs_code: Dict,
        context: Optional[Dict],
        ip_risk: Optional[Dict] = None,
        degraded: bool = False,
        dispute: Optional[Dict] = None,
    ) -> str:
        """三级分流：A=自动拟单 / B=强制确认 / C=挂起提问。

        P1 修正：
        - IP_RISK（命中品牌词）→ C 级（强制人工审核知识产权）
        - degraded=True（无 LLM 推理链）→ 至少 B 级（禁止 A 级自动拟单）

        判定逻辑：
        - A 级：高置信 + 非高风险品类 + 非反倾销/出口管制 + 非 IP_RISK + 非 degraded
        - B 级：中置信，或 degraded=True，或涉及中等风险
        - C 级：低置信，或涉及反倾销/出口管制/归类争议/IP_RISK
        """
        ctx = context or {}
        dest = ctx.get("dest_port") or ctx.get("dest_country") or ""

        # C 级：IP_RISK 命中品牌词 → 强制人工审核知识产权
        if ip_risk and ip_risk.get("is_risky"):
            return "C"

        # C 级：低置信 或 涉及高风险
        if confidence == "low":
            return "C"

        # 检查是否涉及反倾销/出口管制
        remark = (hs_code or {}).get("remark", "")
        high_risk_keywords = ["反倾销", "出口管制", "危险品", "301 关税"]
        if any(k in remark for k in high_risk_keywords):
            return "C"

        # 目的国为美国且涉及反倾销品类
        if "美国" in dest and any(
            k in cargo for k in ["服装", "家具", "钢铁", "陶瓷"]
        ):
            return "C"

        # 归类争议：不拦截制单，但强制人工确认（B 级）
        if dispute:
            return "B"

        # degraded=True：推理链缺失，强制至少 B 级（禁止 A 级自动拟单）
        if degraded:
            return "B"

        # B 级：中置信 或 有风险提示
        if confidence == "medium":
            return "B"

        # A 级：高置信 + 低风险
        return "A"

    # ----------------------------------------------------------------- #
    # 9610 申报清单：dict→JSON 先，再渲染文本
    # ----------------------------------------------------------------- #
    def _build_declaration_dict(
        self,
        cargo: str,
        hs_code: Dict,
        context: Optional[Dict],
        cargo_info: Dict,
    ) -> Dict[str, Any]:
        """v4：先生成结构化 dict（dict → JSON 主输出）。

        字段映射依据 field_dict.json；缺失字段标记为 null 或用默认值。
        这是制单环节的真正产物——校验将基于此 dict 而非渲染文本。
        """
        ctx = context or {}
        info = cargo_info or {}
        tonnage = info.get("tonnage")

        # 数量口径：优先成交件数 quantity（A 板块"1000 件 / 350 千克"），
        # 重量 tonnage 仅作兜底——件数不得用重量替代（field_dict 约束）
        qty = ctx.get("quantity") if ctx.get("quantity") is not None else info.get("quantity")
        qty_from_tonnage = False
        if qty is None:
            qty = tonnage
            qty_from_tonnage = True

        unit = ctx.get("unit") or info.get("unit") or ("千克" if qty_from_tonnage else "件")
        unit_price = (
            ctx.get("unit_price")
            if ctx.get("unit_price") is not None
            else (ctx.get("unit_price_usd") if ctx.get("unit_price_usd") is not None
                  else info.get("unit_price_usd"))
        )
        total = ctx.get("total_amount")
        if total is None:
            try:
                if qty and unit_price:
                    total = float(qty) * float(unit_price)
            except (TypeError, ValueError):
                pass

        hs = hs_code or {}
        # 规范品名：海关 official_name 为"其他…"这类兜底名目时，优先用具体品名
        official = hs.get("official_name") or hs.get("description")
        cargo_spec = cargo
        if official and not official.startswith("其他"):
            cargo_spec = official

        decl = {
            "trade_mode": ctx.get("trade_mode") or "9610",

            # 申报基本信息
            "platform": ctx.get("platform") or None,
            "order_id": ctx.get("order_id") or None,
            "logistics_no": ctx.get("logistics_no") or None,

            # 商品信息
            "cargo_spec": cargo_spec,
            "cargo_common": cargo,
            "hs_code": hs.get("code") or None,
            "hs_desc": hs.get("description") or None,
            "tariff_rate": hs.get("tariff_rate") or "未知",
            "supervision_code": hs.get("supervision_code") or "无",
            "material": info.get("material") or ctx.get("material") or None,
            "usage": info.get("usage") or ctx.get("usage") or None,
            # 申报要素（报关单/汇总申报用；9610 清单栏目另校）
            "brand_type": ctx.get("brand_type"),
            "export_preferential": ctx.get("export_preferential"),
            "brand": ctx.get("brand"),
            "model": ctx.get("model"),
            "gtin": ctx.get("gtin"),
            "cas": ctx.get("cas"),
            "structure_type": ctx.get("structure_type"),
            "speaker_spec": ctx.get("speaker_spec"),
            "appearance": ctx.get("appearance"),
            "production_sales_unit": ctx.get("production_sales_unit"),

            # 数量与价格
            "quantity": qty,
            "unit": unit,
            "unit_price": unit_price,
            "total_price": total,
            "currency": ctx.get("currency") or "USD",

            # 收发货
            "shipper": ctx.get("shipper") or None,
            "consignee": ctx.get("consignee") or None,
            "from_port": ctx.get("from_port") or ctx.get("destination_port") or None,
            "dest_port": ctx.get("dest_port") or None,
            "origin_country": "中国",

            # 包装
            "package_type": info.get("package_type") or ctx.get("package_type") or "纸箱",
            "gross_weight": info.get("gross_weight") or ctx.get("gross_weight") or None,
            "net_weight": info.get("net_weight") or ctx.get("net_weight") or None,
            "package_count": ctx.get("package_count") or 1,
        }

        return decl

    def _render_9610(
        self,
        decl_dict: Dict,
        reasoning_chain: List[str],
        counterfactual: Dict,
        confidence: str,
        confidence_reason: str,
    ) -> str:
        """v4：基于结构化 dict 渲染 9610 文本清单（向后兼容）。"""
        try:
            template = self.decl_gen.env.get_template("declaration_9610.j2")
        except Exception:
            return ""

        d = decl_dict or {}

        # 旧模板参数名 → 新 dict key 的映射
        hs = {"official_name": d.get("cargo_spec"),
              "description": d.get("hs_desc"),
              "code": d.get("hs_code"),
              "tariff_rate": d.get("tariff_rate"),
              "supervision_code": d.get("supervision_code"),
              "remark": None}

        return template.render(
            cargo=d.get("cargo_common"),
            hs_code=hs,
            from_port=d.get("from_port"),
            dest_port=d.get("dest_port"),
            tonnage=d.get("quantity"),
            unit=d.get("unit"),
            unit_price=d.get("unit_price"),
            total_usd=d.get("total_price"),
            currency=d.get("currency"),
            package_type=d.get("package_type"),
            gross_weight=d.get("gross_weight"),
            net_weight=d.get("net_weight"),
            supervision_code=d.get("supervision_code"),
            trade_mode=d.get("trade_mode"),
            platform=d.get("platform"),
            order_id=d.get("order_id"),
            logistics_no=d.get("logistics_no"),
            shipper=d.get("shipper"),
            consignee=d.get("consignee"),
            package_count=d.get("package_count"),
            reasoning_chain=reasoning_chain,
            counterfactual=counterfactual,
            confidence=confidence,
            confidence_reason=confidence_reason,
        )

    # ----------------------------------------------------------------- #
    # 输出拼装
    # ----------------------------------------------------------------- #
    @staticmethod
    def _build_summary(
        cargo_name: str,
        chosen_hs: Dict,
        cargo_info: CargoInfo,
        context: Optional[Dict],
        declaration: str,
        risks: List[Dict],
        reasoning_chain: List[str],
        counterfactual: Dict,
        confidence_level: str,
        confidence_reason: str,
        tier: str,
        info_gap: Optional[Dict],
        ip_risk: Optional[Dict],
        audit_steps: List[Dict],
    ) -> List[str]:
        """拼装 Agent 输出摘要。"""
        tier_label = {"A": "A级·自动拟单", "B": "B级·需人工确认", "C": "C级·挂起提问"}.get(tier, tier)
        tier_icon = {"A": "🟢", "B": "🟡", "C": "🔴"}.get(tier, "⚪")
        conf_icon = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(confidence_level, "⚪")

        lines = [
            f"## 智关 Agent · 报关单证智能生成（{cargo_name}）\n",
            f"### 归类结果",
            f"- **HS编码**：{chosen_hs['code']}（{chosen_hs.get('official_name') or chosen_hs.get('description', '')}）",
            f"- **商品规范名**：{chosen_hs.get('official_name', chosen_hs.get('description', '—'))}",
            f"- **关税税率**：{chosen_hs.get('tariff_rate', '未知')}",
            f"- **监管证件**：{chosen_hs.get('supervision_code', '无')}",
            f"- **置信度**：{conf_icon} {confidence_level}",
        ]
        if confidence_reason:
            lines.append(f"  - 判定依据：{confidence_reason}")

        # 三级分流
        lines.append(f"- **分流等级**：{tier_icon} {tier_label}")

        # 可解释推理链
        if reasoning_chain:
            lines.append(f"\n### 可解释归类推理链")
            for i, step in enumerate(reasoning_chain, 1):
                lines.append(f"{i}. {step}")

        # 反事实对比
        if counterfactual and counterfactual.get("confused_code"):
            lines.append(f"\n### 反事实对比")
            lines.append(f"- 易混编码：{counterfactual['confused_code']}")
            lines.append(f"- 排除理由：{counterfactual.get('why_not', '—')}")

        # 主动提问
        if info_gap:
            lines.append(f"\n### ⚠️ 主动提问（信息缺口检测）")
            lines.append(f"**问题**：{info_gap.get('question', '')}")
            if info_gap.get("options"):
                lines.append(f"**选项**：{' / '.join(info_gap['options'])}")
            if info_gap.get("why_matters"):
                lines.append(f"**为什么重要**：{info_gap['why_matters']}")

        # 知识产权风险（IP_RISK）
        if ip_risk and ip_risk.get("is_risky"):
            lines.append(f"\n### 🚨 知识产权风险（IP_RISK）")
            lines.append(f"- **命中品牌词**：{', '.join(ip_risk.get('matched_brands', []))}")
            lines.append(f"- **处理建议**：{ip_risk.get('advice', '')}")
            lines.append(f"- **强制分流**：已进入 C 级人工审核，禁止 A 级自动拟单")

        # 申报清单
        lines.append(f"\n### 9610 申报清单草稿")
        lines.append("```")
        lines.append(declaration)
        lines.append("```")

        # 合规风险
        if risks:
            lines.append(f"\n### 合规风险提示")
            for r in risks:
                icon = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(r["severity"], "•")
                lines.append(f"- {icon} [{r['rule_id']}] {r['risk']}")
                lines.append(f"  - 处理建议：{r['advice']}")

        # 留痕摘要
        total_ms = sum(s.get("elapsed_ms", 0) for s in audit_steps)
        lines.append(f"\n### 决策留痕")
        lines.append(f"- 总耗时：{total_ms}ms")
        lines.append(f"- 步骤数：{len(audit_steps)}")
        lines.append(f"- 数据来源：{cargo_info.source}")

        return lines

    # ----------------------------------------------------------------- #
    # 内部辅助
    # ----------------------------------------------------------------- #
    def _extract_cargo(self, query: str, context: Optional[Dict]) -> CargoInfo:
        """从用户输入或 context 提取商品信息。

        优先级：
        1. 协调器 OCR 预处理的结果（context.cargo_name / from_image）
        2. context.cargo（上游 Agent 已识别）
        3. query 是图片路径 → OCR + VLM（customs 自己再识一次，兜底）
        4. query 是文字 → 文字抽取
        """
        # 1. 协调器已 OCR 预处理：直接复用，避免重复识图
        if context and context.get("from_image") and context.get("cargo_name"):
            return CargoInfo(
                cargo_name=context["cargo_name"],
                tonnage=context.get("tonnage", 0),
                unit_price_usd=context.get("unit_price_usd", 0),
                source="coordinator_ocr",
            )

        # 1.5 演示/上游已给出结构化品名与金额：直接采用，不再走 OCR 猜品名
        if context and context.get("cargo_name"):
            return CargoInfo(
                cargo_name=context["cargo_name"],
                tonnage=context.get("tonnage", 0) or 0,
                unit_price_usd=context.get("unit_price_usd", 0) or 0,
                material=context.get("material", "") or "",
                usage=context.get("usage", "") or "",
                source="structured_context",
            )

        # 2. 上游已有 cargo
        if context and context.get("cargo"):
            return CargoInfo(
                cargo_name=context["cargo"],
                tonnage=context.get("tonnage", 0),
                source="upstream_context",
            )

        # 3. OCR 模块统一处理（自动判断图片路径 / 纯文字）
        if self.use_ocr:
            return self.ocr.extract(query)

        # 4. 关闭 OCR 时纯文字抽取
        return self.ocr._extract_from_text(query)

    def get_audit_trail(self) -> List[Dict]:
        """获取留痕记录（供审计导出用）。"""
        return self._audit_trail

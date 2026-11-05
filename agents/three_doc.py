"""B 三单预检 Agent。

9610 出口"三单对碰"：订单 / 运单 / 清单 三单的关键字段必须一致，
任一不符（收件人/货值/品名/件数/重量）直接扣包。

本 Agent 职责：
1. 从 order / sku_info / declaration_draft / waybill 中提取三单关键字段。
2. 逐字段比对，品名用语义相似度（默认 difflib，可替换为嵌入模型）。
3. 输出差异报告（mismatches + 严重等级 + 一致性分数）。

接入契约（state.py AGENT_IO_CONTRACT）：
- reads: order, sku_info
- writes: three_doc_status

不调用 LLM（品名语义相似度走 difflib/嵌入，属于确定性计算）。
"""
from __future__ import annotations

import difflib
from typing import Any, Dict, List, Optional

from .base import BaseAgent, AgentResult


# 三单对碰的关键字段定义
# field: (中文名, 是否数值, 容差)
COMPARE_FIELDS = [
    ("recipient", "收件人", False, None),
    ("cargo_name", "品名", False, None),     # 品名走语义相似度
    ("quantity", "件数", True, 0),              # 件数必须完全一致
    ("weight", "重量(kg)", True, 0.05),         # 重量允许 5% 误差
    ("value_usd", "货值(USD)", True, 0.03),     # 货值允许 3% 误差
]


class ThreeDocAgent(BaseAgent):
    """三单预检 Agent：订单/运单/清单 一致性校验。"""

    name = "three_doc"
    description = "9610 三单对碰：订单/运单/清单 关键字段一致性校验"

    # 品名相似度阈值：< 0.6 判定为实质不同商品（critical）
    NAME_SIMILARITY_THRESHOLD = 0.6

    def ask(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> AgentResult:
        ctx = context or {}
        # v2.1（B 板块场景 3/4）：结构化三单（order/payment/waybill/declaration +
        # product_master）走实体对齐路径；旧字段走通用比对路径
        if self._has_structured_docs(ctx):
            return self._ask_structured(query, ctx)
        return self._ask_generic(query, ctx)

    # ----------------------------------------------------------------- #
    # 结构化路径（B 板块 v2.1 场景 3/4）
    # ----------------------------------------------------------------- #
    @staticmethod
    def _has_structured_docs(ctx: Dict[str, Any]) -> bool:
        return bool(
            (ctx.get("order") and ctx.get("declaration"))
            and (ctx.get("waybill") or ctx.get("payment"))
        )

    def _ask_structured(self, query: str, ctx: Dict[str, Any]) -> AgentResult:
        order = ctx.get("order") or {}
        payment = ctx.get("payment") or {}
        waybill = ctx.get("waybill") or {}
        declaration = ctx.get("declaration") or {}
        master = ctx.get("product_master") or []

        findings: List[Dict[str, Any]] = []   # 统一问题清单
        evidence: Dict[str, Any] = {}          # 归一化证据链等

        # ---- ① SKU 实体对齐（场景 4 核心机制：非数值比对）----
        sku_result = self._check_sku_alignment(order, declaration, master)
        evidence["sku_alignment"] = sku_result["status"]
        if sku_result["status"] == "conflict":
            findings.append({
                "field": "SKU实体对齐",
                "severity": "critical",
                "order_val": order.get("sku"),
                "doc_val": declaration.get("sku"),
                "doc_name": "清单",
                "detail": sku_result["detail"],
            })
        elif sku_result["status"] == "unverified":
            findings.append({
                "field": "SKU实体对齐",
                "severity": "warning",
                "order_val": order.get("sku"),
                "doc_val": declaration.get("sku"),
                "doc_name": "清单",
                "detail": "SKU 主数据映射不可靠，先标'映射不确定'，不直接判商品不一致",
            })

        # ---- ② 品名：SKU 对齐 → 归一化链 PASS（名称差异≠业务异常）----
        name_check = self._check_cargo_names(order, declaration, waybill, master, sku_result)
        evidence["cargo_name"] = name_check["status"]
        evidence["normalization_chain"] = name_check.get("chain")
        if name_check["status"] == "mismatch":
            findings.append({
                "field": "品名",
                "severity": "critical",
                "order_val": order.get("title") or order.get("cargo_name"),
                "doc_val": declaration.get("cargo_name"),
                "doc_name": "清单",
                "detail": "品名归一化后仍无法对齐",
            })

        # ---- ③ 数值比对（件数/金额/重量）----
        numeric_checks = []
        for doc_name, doc in (("清单", declaration), ("运单", waybill)):
            if not doc:
                continue
            pairs = [
                ("数量", order.get("quantity"), doc.get("quantity"), 0),
            ]
            if doc_name == "清单":
                pairs.append(("金额", order.get("amount"), doc.get("amount"), 0.03))
            for field_name, ov, dv, tol in pairs:
                if ov is None or dv is None:
                    continue
                numeric_checks.append(f"{field_name}({doc_name})")
                if not self._numeric_match(ov, dv, tol):
                    findings.append({
                        "field": field_name,
                        "severity": "critical" if tol == 0 else "warning",
                        "order_val": ov, "doc_val": dv, "doc_name": doc_name,
                        "detail": f"订单 {ov} vs {doc_name} {dv}",
                    })
        evidence["numeric_checks"] = numeric_checks

        # ---- ④ 收件人：缩写兼容 → WARNING 保留原值（不自动补全）----
        if order.get("recipient") and waybill.get("recipient"):
            level = self._recipient_match_level(str(order["recipient"]), str(waybill["recipient"]))
            evidence["recipient_match"] = level
            if level == "abbreviated":
                findings.append({
                    "field": "收件人",
                    "severity": "warning",
                    "order_val": order.get("recipient"),
                    "doc_val": waybill.get("recipient"),
                    "doc_name": "运单",
                    "detail": f"疑似同一人（{waybill['recipient']} 疑为 {order['recipient']} 的简写），"
                              f"保留原值不做自动补全，待人工核实",
                })
            elif level == "different":
                findings.append({
                    "field": "收件人",
                    "severity": "critical",
                    "order_val": order.get("recipient"),
                    "doc_val": waybill.get("recipient"),
                    "doc_name": "运单",
                    "detail": "收件人不一致",
                })

        # ---- ⑤ 支付单要素核验（内部风控口径，非法定提交要求）----
        payment_check = self._check_payment(payment)
        evidence["payment"] = payment_check
        if payment_check["status"] == "blocked":
            findings.append({
                "field": "支付单要素",
                "severity": "critical",
                "order_val": "—",
                "doc_val": "仅商户名：" + str(payment.get("merchant", "")),
                "doc_name": "支付单",
                "detail": payment_check["detail"],
            })

        # ---- 汇总判定 ----
        has_critical = any(f["severity"] == "critical" for f in findings)
        has_warning = any(f["severity"] == "warning" for f in findings)
        if has_critical:
            match_status = "fail"
        elif has_warning:
            match_status = "warning"
        else:
            match_status = "pass"

        result_data = {
            "three_doc_status": {
                "match": match_status,
                "mode": "structured",
                "evidence": evidence,
                "mismatches": findings,
                "payment_check": payment_check,
                "sku_alignment": sku_result,
            }
        }

        lines = ["## 三单预检结果（实体对齐模式）"]
        lines.append(f"- **一致性状态**：{self._status_text(match_status)}")
        lines.append(f"- **SKU实体对齐**：{sku_result['status']}"
                     + (f"（{sku_result['detail']}）" if sku_result.get("detail") else ""))
        if evidence.get("normalization_chain"):
            lines.append(f"- **品名归一化链**：{' → '.join(evidence['normalization_chain'])}")
        lines.append(f"- **数值比对**：{', '.join(numeric_checks) if numeric_checks else '无'}")
        lines.append(f"- **支付单核验**：{payment_check['status']}"
                     + (f"（{payment_check['detail']}）" if payment_check.get("detail") else ""))
        if findings:
            lines.append(f"- **问题清单**（{len(findings)}）：")
            for f in findings:
                icon = "🔴" if f["severity"] == "critical" else "🟡"
                lines.append(f"  - {icon} [{f['field']}] {f.get('detail') or ''}"
                             f"（{f['order_val']!r} vs {f['doc_val']!r}）")
        else:
            lines.append("- **问题清单**：无 ✓")

        return AgentResult(
            agent_name=self.name,
            summary="\n".join(lines),
            data=result_data,
            next_hint=(
                "三单一致，可推送申报" if match_status == "pass"
                else "存在阻断项，拦截提交并输出断裂字段清单"
            ),
            confidence=0.95 if match_status == "pass" else 0.5,
            needs_human_review=match_status != "pass",
        )

    # ----------------------------------------------------------------- #
    # SKU 实体对齐 + 主数据解析
    # ----------------------------------------------------------------- #
    @staticmethod
    def _resolve_master(sku: Optional[str], master: List[Dict]) -> Optional[Dict]:
        if not sku or not master:
            return None
        return next((m for m in master if m.get("sku") == sku), None)

    def _check_sku_alignment(
        self, order: Dict, declaration: Dict, master: List[Dict],
    ) -> Dict[str, Any]:
        o_sku, d_sku = order.get("sku"), declaration.get("sku")
        if not o_sku or not d_sku:
            return {"status": "skipped", "detail": "缺少 SKU，无法做实体对齐"}
        if o_sku == d_sku:
            m = self._resolve_master(o_sku, master)
            return {
                "status": "pass",
                "order_sku": o_sku, "declaration_sku": d_sku,
                "master": m.get("name") if m else None,
                "hs_code": m.get("hs_code") if m else None,
            }
        m_o = self._resolve_master(o_sku, master)
        m_d = self._resolve_master(d_sku, master)
        if m_o and m_d:
            return {
                "status": "conflict",
                "order_sku": o_sku, "declaration_sku": d_sku,
                "detail": f"订单 SKU（{m_o.get('model')}）与清单 SKU（{m_d.get('model')}）"
                          f"在商品主数据中适用机型不同，指向不同规格商品",
            }
        return {
            "status": "unverified",
            "order_sku": o_sku, "declaration_sku": d_sku,
            "detail": "SKU 不同但主数据无法完全解析（B级证据），标记映射不确定",
        }

    def _check_cargo_names(
        self, order: Dict, declaration: Dict, waybill: Dict,
        master: List[Dict], sku_result: Dict,
    ) -> Dict[str, Any]:
        """品名差异判定：SKU 对齐 + 规格一致 → PASS（正常表达差异），输出归一化链。"""
        if sku_result.get("status") == "pass":
            m = self._resolve_master(order.get("sku"), master)
            chain = [
                "订单营销标题：" + str(order.get("title") or order.get("cargo_name") or ""),
                "归一化提取：手机壳 + 机型词",
                f"SKU 主数据：{order.get('sku')}"
                + (f"（{m.get('name')} / {m.get('model')}）" if m else ""),
                f"HS编码：{m.get('hs_code')}" if m and m.get("hs_code") else "HS编码：待归类",
            ]
            return {"status": "pass_normalized", "chain": chain}
        if sku_result.get("status") == "conflict":
            return {"status": "mismatch",
                    "detail": "SKU 实体冲突，品名一致性无从建立"}
        # 无 SKU：退回相似度
        o_name = str(order.get("title") or order.get("cargo_name") or "")
        d_name = str(declaration.get("cargo_name") or "")
        sim = self._name_similarity(o_name, d_name)
        if sim < self.NAME_SIMILARITY_THRESHOLD:
            return {"status": "mismatch", "detail": f"相似度 {sim:.2f} 低于阈值"}
        return {"status": "pass_similarity", "chain": [f"相似度 {sim:.2f}"]}

    # ----------------------------------------------------------------- #
    # 收件人匹配：完全 / 缩写兼容 / 不同
    # ----------------------------------------------------------------- #
    @staticmethod
    def _recipient_match_level(a: str, b: str) -> str:
        """'Müller, Hans' vs 'Müller,H' → abbreviated（WARNING，保留原值）。"""
        na, nb = a.strip(), b.strip()
        if na == nb:
            return "exact"
        pa = [p.strip() for p in na.split(",") if p.strip()]
        pb = [p.strip() for p in nb.split(",") if p.strip()]
        if len(pa) == len(pb) == 2:
            surname_eq = pa[0].lower() == pb[0].lower()
            given_abbr = pb[1][0].lower() == pa[1][0].lower() and (
                pa[1].startswith(pb[1]) or pb[1].startswith(pa[1])
            )
            if surname_eq and given_abbr:
                return "abbreviated"
        return "different"

    # ----------------------------------------------------------------- #
    # 支付单要素核验（内部风控口径）
    # ----------------------------------------------------------------- #
    @staticmethod
    def _check_payment(payment: Dict) -> Dict[str, Any]:
        """167 号公告：不得要求企业提供收款单（法定口径）；
        本校验为企业内部风控（INTERNAL_CONTROL）：支付单要素不全 → 拦截并提示补充凭证，
        既不认定匹配也不认定虚假。"""
        if not payment:
            return {"status": "not_applicable",
                    "detail": "未提供支付单（167号公告：海关不要求传输收款单，不作为法定校验项）"}
        required = [("order_id", "订单号"), ("transaction_no", "流水号"), ("amount", "金额")]
        missing = [label for key, label in required if not payment.get(key)]
        if missing:
            return {
                "status": "blocked",
                "rule_type": "INTERNAL_CONTROL",
                "detail": f"支付单缺少：{'、'.join(missing)}；仅商户名不足以核验"
                          f"（内部风控口径，非法定提交要求；提示补充凭证，不认定匹配亦不认定虚假）",
                "missing": missing,
            }
        return {"status": "pass", "rule_type": "INTERNAL_CONTROL"}

    # ----------------------------------------------------------------- #
    # 通用比对路径（旧接口，向后兼容）
    # ----------------------------------------------------------------- #
    def _ask_generic(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> AgentResult:
        ctx = context or {}
        order = ctx.get("order") or {}
        sku_info = ctx.get("sku_info") or {}
        declaration_draft = ctx.get("declaration_draft") or ""
        waybill = ctx.get("waybill") or ctx.get("logistics") or {}

        # ---- 提取三单字段 ----
        order_doc = self._extract_order(order, sku_info)
        waybill_doc = self._extract_waybill(waybill, order, sku_info)
        decl_doc = self._extract_declaration(declaration_draft, sku_info)

        docs = {
            "订单": order_doc,
            "运单": waybill_doc,
            "清单": decl_doc,
        }

        # 检查哪些单据缺失
        missing = [name for name, d in docs.items() if not d]

        mismatches: List[Dict[str, Any]] = []
        checked_fields: List[str] = []

        # ---- 逐字段比对（订单 vs 运单 vs 清单）----
        for field_key, field_name, is_numeric, tolerance in COMPARE_FIELDS:
            # 收集所有非空单据的该字段值
            values = {}
            for doc_name, d in docs.items():
                if d and d.get(field_key) is not None:
                    values[doc_name] = d[field_key]

            if len(values) < 2:
                # 只有一份或没有数据，无法比对
                if values:
                    checked_fields.append(f"{field_name}(仅{list(values)[0]}有值)")
                continue

            checked_fields.append(field_name)
            self._compare_field(field_key, field_name, values, is_numeric, tolerance, mismatches)

        # ---- 一致性评分 ----
        total_compared = len([f for f in checked_fields if "(" not in f])
        mismatch_count = len(mismatches)
        consistency_score = (
            1.0 if total_compared == 0
            else max(0.0, 1.0 - mismatch_count / max(total_compared, 1))
        )

        # 严重等级判定
        has_critical = any(m["severity"] == "critical" for m in mismatches)
        has_warning = any(m["severity"] == "warning" for m in mismatches)

        if missing:
            match_status = "incomplete"
            needs_review = True
        elif has_critical:
            match_status = "fail"
            needs_review = True
        elif has_warning:
            match_status = "warning"
            needs_review = True
        else:
            match_status = "pass"
            needs_review = False

        result_data = {
            "three_doc_status": {
                "match": match_status,
                "consistency_score": round(consistency_score, 3),
                "checked_fields": checked_fields,
                "mismatches": mismatches,
                "missing_docs": missing,
            }
        }

        # ---- summary ----
        summary_lines = [f"## 三单预检结果"]
        summary_lines.append(f"- **一致性状态**：{self._status_text(match_status)}")
        summary_lines.append(f"- **一致性分数**：{consistency_score:.1%}")
        summary_lines.append(f"- **比对字段**：{', '.join(checked_fields) if checked_fields else '无（数据不足）'}")
        if missing:
            summary_lines.append(f"- **缺失单据**：{', '.join(missing)}（无法完整比对）")
        if mismatches:
            summary_lines.append(f"- **差异项**（{len(mismatches)}）：")
            for m in mismatches:
                sev = "🔴" if m["severity"] == "critical" else "🟡"
                summary_lines.append(
                    f"  - {sev} [{m['field']}] {m['order_val']!r} vs {m['doc_val']!r}"
                    + (f"（相似度 {m.get('similarity', 0):.2f}）" if "similarity" in m else "")
                )
        else:
            summary_lines.append("- **差异项**：无")

        return AgentResult(
            agent_name=self.name,
            summary="\n".join(summary_lines),
            data=result_data,
            next_hint=(
                "三单一致，可推送申报" if match_status == "pass"
                else "存在差异，需修正后重新比对"
            ),
            confidence=min(consistency_score, 0.98),
            needs_human_review=needs_review,
        )

    # ----------------------------------------------------------------- #
    # 字段提取
    # ----------------------------------------------------------------- #
    @staticmethod
    def _extract_order(order: Dict, sku_info: Dict) -> Dict[str, Any]:
        """从订单数据提取关键字段。"""
        if not order and not sku_info:
            return {}
        return {
            "recipient": order.get("recipient") or order.get("consignee"),
            "cargo_name": sku_info.get("cargo_name") or order.get("cargo_name"),
            "quantity": order.get("quantity"),
            "weight": order.get("weight"),
            "value_usd": order.get("value_usd") or order.get("unit_price_usd"),
        }

    @staticmethod
    def _extract_waybill(waybill: Dict, order: Dict, sku_info: Dict) -> Dict[str, Any]:
        """从运单数据提取关键字段。

        若运单数据缺失，回退用订单数据兜底（演示场景下运单常未生成）。
        实际生产环境应强制要求运单数据。
        """
        if not waybill:
            return {}
        return {
            "recipient": waybill.get("recipient") or waybill.get("consignee"),
            "cargo_name": waybill.get("cargo_name") or waybill.get("goods_name"),
            "quantity": waybill.get("quantity") or waybill.get("pieces"),
            "weight": waybill.get("weight"),
            "value_usd": waybill.get("value_usd") or waybill.get("declared_value"),
        }

    @staticmethod
    def _extract_declaration(declaration_draft: str, sku_info: Dict) -> Dict[str, Any]:
        """从申报清单草稿提取关键字段。

        declaration_draft 是归类 Agent 生成的申报文本，从中解析品名/货值等。
        若解析失败，用 sku_info 兜底。
        """
        result: Dict[str, Any] = {}
        if declaration_draft:
            # 简单解析：从文本中提取 "品名：xxx" / "货值：xxx" 等
            import re
            name_m = re.search(r"品名[：:]\s*(.+)", declaration_draft)
            if name_m:
                result["cargo_name"] = name_m.group(1).strip()
            value_m = re.search(r"(?:货值|申报价值|USD)[：:\s]*([\d.]+)", declaration_draft)
            if value_m:
                try:
                    result["value_usd"] = float(value_m.group(1))
                except ValueError:
                    pass
        # 兜底
        if "cargo_name" not in result and sku_info.get("cargo_name"):
            result["cargo_name"] = sku_info["cargo_name"]
        return result

    # ----------------------------------------------------------------- #
    # 字段比对
    # ----------------------------------------------------------------- #
    def _compare_field(
        self,
        field_key: str,
        field_name: str,
        values: Dict[str, Any],
        is_numeric: bool,
        tolerance: Optional[float],
        mismatches: List[Dict],
    ) -> None:
        """比对一个字段在多份单据中的值。"""
        # 取基准值（订单优先，其次运单）
        base_doc = "订单" if "订单" in values else list(values)[0]
        base_val = values[base_doc]

        for doc_name, val in values.items():
            if doc_name == base_doc:
                continue

            if is_numeric:
                if not self._numeric_match(base_val, val, tolerance):
                    mismatches.append({
                        "field": field_name,
                        "order_val": base_val,
                        "doc_val": val,
                        "doc_name": doc_name,
                        "severity": "critical" if tolerance == 0 else "warning",
                    })
            elif field_key == "cargo_name":
                # 品名走语义相似度
                sim = self._name_similarity(str(base_val), str(val))
                if sim < self.NAME_SIMILARITY_THRESHOLD:
                    mismatches.append({
                        "field": field_name,
                        "order_val": base_val,
                        "doc_val": val,
                        "doc_name": doc_name,
                        "severity": "critical",
                        "similarity": round(sim, 3),
                    })
                elif sim < 0.85:
                    mismatches.append({
                        "field": field_name,
                        "order_val": base_val,
                        "doc_val": val,
                        "doc_name": doc_name,
                        "severity": "warning",
                        "similarity": round(sim, 3),
                    })
            else:
                # 文本字段完全匹配
                if str(base_val).strip() != str(val).strip():
                    mismatches.append({
                        "field": field_name,
                        "order_val": base_val,
                        "doc_val": val,
                        "doc_name": doc_name,
                        "severity": "critical",
                    })

    @staticmethod
    def _numeric_match(a: Any, b: Any, tolerance: Optional[float]) -> bool:
        """数值比对：tolerance=None 要求完全相等；否则允许相对误差。"""
        try:
            fa, fb = float(a), float(b)
        except (TypeError, ValueError):
            return str(a) == str(b)
        if tolerance is None or tolerance == 0:
            return fa == fb
        if fa == 0:
            return fb == 0
        return abs(fa - fb) / abs(fa) <= tolerance

    @staticmethod
    def _name_similarity(a: str, b: str) -> float:
        """品名语义相似度。

        当前用 difflib.SequenceMatcher（确定性，无需外部依赖）。
        生产环境可替换为嵌入模型（如 text2vec / bge）以获得真正的语义理解。
        """
        if not a or not b:
            return 0.0
        a_clean = a.strip().lower()
        b_clean = b.strip().lower()
        if a_clean == b_clean:
            return 1.0
        return difflib.SequenceMatcher(None, a_clean, b_clean).ratio()

    @staticmethod
    def _status_text(status: str) -> str:
        return {
            "pass": "✅ 三单一致",
            "warning": "🟡 存在差异（需关注）",
            "fail": "🔴 三单不一致（禁止推送）",
            "incomplete": "⚪ 数据不全（缺少单据）",
        }.get(status, status)


if __name__ == "__main__":
    agent = ThreeDocAgent()

    # 测试1：三单一致
    print("=" * 50)
    print("测试1：三单一致")
    ctx = {
        "order": {"recipient": "John Smith", "quantity": 100, "weight": 50.0, "value_usd": 500.0},
        "sku_info": {"cargo_name": "硅胶塑料手机壳"},
        "waybill": {"recipient": "John Smith", "cargo_name": "硅胶手机壳", "quantity": 100, "weight": 50.2, "value_usd": 500.0},
        "declaration_draft": "品名：硅胶塑料手机壳\n货值：500 USD",
    }
    res = agent.ask("报关硅胶手机壳", context=ctx)
    print(res.summary)
    print()

    # 测试2：品名实质不同
    print("=" * 50)
    print("测试2：品名实质不同")
    ctx2 = {
        "order": {"recipient": "John Smith", "quantity": 100, "weight": 50.0, "value_usd": 500.0},
        "sku_info": {"cargo_name": "蓝牙耳机"},
        "waybill": {"recipient": "John Smith", "cargo_name": "有线耳机", "quantity": 100, "weight": 50.0, "value_usd": 500.0},
    }
    res2 = agent.ask("报关蓝牙耳机", context=ctx2)
    print(res2.summary)
    print()

    # 测试3：货值不符
    print("=" * 50)
    print("测试3：货值不符")
    ctx3 = {
        "order": {"recipient": "John Smith", "quantity": 100, "weight": 50.0, "value_usd": 500.0},
        "sku_info": {"cargo_name": "硅胶手机壳"},
        "waybill": {"recipient": "John Smith", "cargo_name": "硅胶手机壳", "quantity": 100, "weight": 50.0, "value_usd": 300.0},
    }
    res3 = agent.ask("报关硅胶手机壳", context=ctx3)
    print(res3.summary)

"""B 板块 Golden Answer v2.1 验收测试（四场景 + E 板块管制要素）。

依据：
- A 板块《1102_四场景输入字段值 v2.1》（fixtures：customs/data/demo_scenarios.json）
- B 板块《1102_GoldenAnswer v2.1》

运行：python3 test_golden_v21.py
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agents.customs import CustomsAgent
from agents.three_doc import ThreeDocAgent
from agents.decl_compliance import DeclarationComplianceAgent
from agents.customs_orchestrator import build_orchestrator

_SCENARIO_PATH = _PROJECT_ROOT / "customs" / "data" / "demo_scenarios.json"
_SCENARIOS = json.loads(_SCENARIO_PATH.read_text(encoding="utf-8"))


def load_scenario(scenarios, sid):
    """解析场景（支持 base 引用 + context_overrides，对应 A 板块场景 4 的复制设计）。"""
    s = next(x for x in scenarios if x["id"] == sid)
    if s.get("base"):
        base = load_scenario(scenarios, s["base"])
        ctx = dict(base["context"])
        ctx.update(s.get("context_overrides") or {})
        return {
            **base,
            "id": s["id"],
            "name": s.get("name", base["name"]),
            "doc_type": s.get("doc_type", base.get("doc_type")),
            "query": s.get("query", base["query"]),
            "context": ctx,
            "expected": s.get("expected", base.get("expected")),
        }
    return s


class GoldenAnswerV21Test(unittest.TestCase):
    """B 板块 v2.1 四场景 golden answer。"""

    @classmethod
    def setUpClass(cls):
        cls.customs = CustomsAgent(verbose=False)
        cls.three_doc = ThreeDocAgent()
        cls.decl = DeclarationComplianceAgent()
        cls.master = _SCENARIOS["product_master"]

    # ----------------------------------------------------------------- #
    # 场景 1：归类争议（两遍演示）
    # ----------------------------------------------------------------- #
    def test_s1_with_facts_candidate(self):
        """第一遍（证据充足）：CANDIDATE 8518220000 + 理由链 + 排除 3926909090。"""
        s = load_scenario(_SCENARIOS["scenarios"], "scenario_1_dispute")
        ctx = dict(s["context"])
        ctx["cargo_facts"] = s["cargo_facts"]
        r = self.customs.ask(s["query"], context=ctx)
        d = r.data
        self.assertEqual(d["hs_code"]["code"], "8518220000")
        res = d.get("dispute_resolution")
        self.assertIsNotNone(res, "事实齐备时争议应被消解")
        self.assertEqual(res["status"], "CANDIDATE")
        self.assertEqual(res["rejected"], "3926909090")
        # 论证口径：品目条文 → 分列维度 → 事实匹配（禁止要素反推）
        joined = " ".join(res["reasoning"])
        for kw in ["品目 85.18", "分列维度", "规则三（二）"]:
            self.assertIn(kw, joined)
        self.assertNotIn("要素差集", joined)
        self.assertEqual(d["tier"], "B", "CANDIDATE 须人工确认 → B 级")

    def test_s1_without_facts_need_more(self):
        """第二遍（删除扬声器资料）：DISPUTE + NEED_MORE_FACTS 追问分列维度。"""
        s = load_scenario(_SCENARIOS["scenarios"], "scenario_1_dispute")
        r = self.customs.ask(s["query"], context=dict(s["context"]))
        d = r.data
        self.assertIn("3926909090", d["candidates"])
        self.assertIn("8518220000", d["candidates"])
        self.assertIsNotNone(d["dispute"])
        qs = " ".join(d["dispute"].get("open_questions") or [])
        self.assertIn("扬声器单元数量", qs)
        self.assertIn("是否共用", qs)
        self.assertEqual(d["tier"], "B")

    # ----------------------------------------------------------------- #
    # 场景 2：申报要素缺失（报关单口径，8 项要素七态）
    # ----------------------------------------------------------------- #
    def test_s2_element_blocks(self):
        s = load_scenario(_SCENARIOS["scenarios"], "scenario_2_elements")
        r = self.customs.ask(s["query"], context=dict(s["context"]))
        decl_dict = r.data["declaration_dict"]
        cr = self.decl.ask(s["query"], context={
            "declaration_dict": decl_dict,
            "hs_code": r.data["hs_code"],
            "doc_type": "declaration_form",
        })
        issues = cr.data["decl_compliance"]["issues"]
        critical_fields = {i["field"] for i in issues if i["severity"] == "critical"}
        # 5 项阻断：品牌类型/出口享惠/品牌/型号（missing）+ 材质（format_error）
        for field in ["品牌类型", "出口享惠情况", "品牌（中文或外文名称）", "型号", "材质（塑料品种）"]:
            self.assertIn(field, critical_fields, f"{field} 应为 critical 阻断")
        # GTIN 选填缺失 → 不阻断；CAS 物品 → 不阻断
        self.assertNotIn("GTIN", critical_fields)
        self.assertNotIn("CAS", critical_fields)
        infos = {i["field"] for i in issues if i["severity"] == "info"}
        self.assertIn("GTIN", infos, "GTIN 应有 info 提示（可填或明示无）")
        self.assertIn("CAS", infos, "CAS 应有 info 判定链（物品 → 不适用）")

    # ----------------------------------------------------------------- #
    # 场景 3：三单校验（9610 清单）
    # ----------------------------------------------------------------- #
    def test_s3_three_doc(self):
        s = load_scenario(_SCENARIOS["scenarios"], "scenario_3_three_doc")
        ctx = dict(s["context"])
        ctx["product_master"] = self.master
        r = self.three_doc.ask(s["query"], context=ctx)
        st = r.data["three_doc_status"]
        # 品名差异：SKU 对齐 + 规格一致 → PASS（归一化链）
        self.assertEqual(st["sku_alignment"]["status"], "pass")
        self.assertEqual(st["evidence"]["cargo_name"], "pass_normalized")
        chain = " ".join(st["evidence"]["normalization_chain"])
        self.assertIn("CASE-IP15P-001", chain)
        self.assertIn("3926909090", chain)
        # 支付单：缺订单号/流水号/金额 → BLOCKED（内部风控口径）
        self.assertEqual(st["payment_check"]["status"], "blocked")
        self.assertEqual(st["payment_check"]["rule_type"], "INTERNAL_CONTROL")
        # 收件人：Müller,H 缩写 → WARNING 保留原值（不自动补全）
        self.assertEqual(st["evidence"]["recipient_match"], "abbreviated")
        rec = [f for f in st["mismatches"] if f["field"] == "收件人"]
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["severity"], "warning")
        self.assertEqual(rec[0]["doc_val"], "Müller,H", "物流单原值必须保留")
        # 总体：拦截提交
        self.assertEqual(st["match"], "fail")

    def test_s3_production_sales_unit_78(self):
        """生产销售单位填平台名 → BLOCKED（78 号公告：B 或 C 均可，平台名唯一错误）。"""
        s = load_scenario(_SCENARIOS["scenarios"], "scenario_3_three_doc")
        ctx = s["context"]
        issues = DeclarationComplianceAgent._validate_production_sales_unit(
            {"platform": ctx["platform"],
             "production_sales_unit": ctx["production_sales_unit"]},
            ctx,
        )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["severity"], "critical")
        self.assertEqual(issues[0]["rule_type"], "LEGAL_REQUIREMENT")

    # ----------------------------------------------------------------- #
    # 场景 4：伪一致性 SKU 冲突
    # ----------------------------------------------------------------- #
    def test_s4_sku_conflict(self):
        s = load_scenario(_SCENARIOS["scenarios"], "scenario_4_sku_conflict")
        ctx = dict(s["context"])
        ctx["product_master"] = self.master
        r = self.three_doc.ask(s["query"], context=ctx)
        st = r.data["three_doc_status"]
        # 数值全对（陷阱）
        self.assertTrue(st["evidence"]["numeric_checks"], "数值比对应全部执行")
        num_findings = [f for f in st["mismatches"] if f["field"] in ("数量", "金额")]
        self.assertEqual(num_findings, [], "数值校验应 PASS（伪一致性陷阱）")
        # SKU 实体对齐 → conflict → BLOCKED
        self.assertEqual(st["sku_alignment"]["status"], "conflict")
        self.assertEqual(st["match"], "fail")

    # ----------------------------------------------------------------- #
    # E 板块：禁限管制申报要素（显式类别覆盖 + 按税号自动触发）
    # ----------------------------------------------------------------- #
    def test_control_elements_3014_explicit(self):
        """显式 control_category 覆盖路径（3014 石墨案例，教学口径）。"""
        issues = DeclarationComplianceAgent._validate_control_elements(
            {"control_category": "3014"}, {"code": "8545900000"})
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["severity"], "critical")
        self.assertIn("石墨及其制品", issues[0]["message"])
        self.assertIn("4 问", issues[0]["message"])

    def test_control_elements_auto_by_hs(self):
        """自动触发：受管制且类别码已知 → 必答参数自动要求（无需场景声明）。"""
        # 8801009090 气球/飞艇 → 类别 3001（无人驾驶航空器）
        issues = DeclarationComplianceAgent._validate_control_elements(
            {}, {"code": "8801009090"})
        self.assertTrue(issues, "8801009090 受管制且类别已知，应自动触发必答参数")
        self.assertEqual(issues[0]["severity"], "critical")
        self.assertIn("3001", issues[0]["message"])

    def test_control_elements_auto_no_category(self):
        """自动触发：受管制但类别码缺失 → 警告人工核对，不猜测。"""
        issues = DeclarationComplianceAgent._validate_control_elements(
            {}, {"code": "3926909010"})
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["severity"], "warning")
        self.assertIn("control_mark=3", issues[0]["message"])

    def test_control_elements_uncontrolled(self):
        """非管制税号 → 不触发（3926909090 无标记）。"""
        issues = DeclarationComplianceAgent._validate_control_elements(
            {}, {"code": "3926909090"})
        self.assertEqual(issues, [])

    # ----------------------------------------------------------------- #
    # 单证适用性路由（GAP-CEB603-SCOPE 修复）
    # ----------------------------------------------------------------- #
    def test_doc_type_routing_declaration_form(self):
        """报关单口径：CEB603 清单专属栏位 → not_applicable，而非 critical。"""
        agent = DeclarationComplianceAgent()
        fields = {k: "" for k in (
            "跨境电商平台", "订单编号", "物流运单号", "商品名称", "HS编码")}
        issues = agent._validate_format(fields, {"doc_type": "declaration_form"})
        na = {i["field"] for i in issues if i["severity"] == "not_applicable"}
        crit = {i["field"] for i in issues if i["severity"] == "critical"}
        self.assertIn("跨境电商平台", na)
        self.assertIn("订单编号", na)
        self.assertIn("物流运单号", na)
        self.assertNotIn("跨境电商平台", crit)
        # 9610 清单口径下仍是必填 critical
        issues_9610 = agent._validate_format(fields, {"doc_type": "list_9610"})
        crit_9610 = {i["field"] for i in issues_9610 if i["severity"] == "critical"}
        self.assertIn("跨境电商平台", crit_9610)

    # ----------------------------------------------------------------- #
    # 字段缺口自主分析（规则驱动：缺什么/为什么/自解或提问）
    # ----------------------------------------------------------------- #
    def test_field_gap_analyzer_scenario2(self):
        """场景 2：报关单口径 → 要素缺口进入 ask_user，附规则原因。"""
        from customs.field_gap import FieldGapAnalyzer
        ga = FieldGapAnalyzer()
        s = load_scenario(_SCENARIOS["scenarios"], "scenario_2_elements")
        r = self.customs.ask(s["query"], context=dict(s["context"]))
        gaps = r.data.get("field_gaps")
        self.assertIsNotNone(gaps, "classify 槽应输出字段缺口分析")
        ask_fields = {g["field"] for g in gaps["ask_user"]}
        for elem in ("申报要素·品牌类型", "申报要素·出口享惠情况", "申报要素·型号"):
            self.assertIn(elem, ask_fields, f"{elem} 应进入提问清单")
        # 每条提问必须带原因（规则依据 + 缺失来源）
        for g in gaps["ask_user"]:
            self.assertTrue(g.get("reason"), "提问必须附结构化原因")

    def test_field_gap_analyzer_derivation(self):
        """派生自解：总价缺失但数量×单价可算 → self_resolved。"""
        from customs.field_gap import FieldGapAnalyzer
        ga = FieldGapAnalyzer()
        gaps = ga.analyze(
            {"quantity": 100, "unit_price": 6.8, "currency": "USD"},
            {}, {"code": "3926909090"}, "list_9610",
        )
        solved = {g["key"]: g for g in gaps["self_resolved"]}
        self.assertIn("total_price", solved, "总价应派生自解")
        self.assertEqual(solved["total_price"]["value"], 680)
        self.assertEqual(solved["total_price"]["provenance"], "DERIVED")

    # ----------------------------------------------------------------- #
    # 编排器集成：四场景全链路
    # ----------------------------------------------------------------- #
    def test_orchestrator_s1_with_facts(self):
        s = load_scenario(_SCENARIOS["scenarios"], "scenario_1_dispute")
        ctx = dict(s["context"])
        ctx["cargo_facts"] = s["cargo_facts"]
        orc = build_orchestrator(verbose=False)
        r = orc.ask(s["query"], context=ctx)
        data = r.data
        self.assertEqual(data["hs_code"]["code"], "8518220000")
        self.assertIsNotNone(data.get("dispute_resolution"))
        # 6 槽全部 success
        slots = data.get("agent_slots") or {}
        for k in ("classify", "three_doc", "decl_compliance", "risk_screen", "merge", "risk_monitor"):
            self.assertEqual(slots.get(k), "success", f"槽位 {k} 应 success")

    def test_orchestrator_s3(self):
        s = load_scenario(_SCENARIOS["scenarios"], "scenario_3_three_doc")
        ctx = dict(s["context"])
        ctx["product_master"] = self.master
        orc = build_orchestrator(verbose=False)
        r = orc.ask(s["query"], context=ctx)
        data = r.data
        td = (data.get("three_doc_status") or {})
        self.assertEqual(td.get("sku_alignment", {}).get("status"), "pass")
        self.assertEqual(td.get("payment_check", {}).get("status"), "blocked")
        dc = (data.get("decl_compliance") or {})
        crit_fields = {i["field"] for i in dc.get("issues", []) if i["severity"] == "critical"}
        self.assertIn("生产销售单位", crit_fields, "编排链路应透出 78 号生产销售单位阻断")


    # ----------------------------------------------------------------- #
    # K1–K3 编排级契约回归
    # ----------------------------------------------------------------- #
    def test_orchestrator_k1_hs_code_string(self):
        """K1：入口 hs_code 传字符串时，risk_screen 不得因类型假设而报错。"""
        s = load_scenario(_SCENARIOS["scenarios"], "scenario_2_elements")
        ctx = dict(s["context"])
        self.assertIsInstance(ctx.get("hs_code"), str, "场景 2 应以字符串税号进入")
        orc = build_orchestrator(verbose=False)
        r = orc.ask(s["query"], context=ctx)
        slots = r.data.get("agent_slots") or {}
        self.assertEqual(slots.get("risk_screen"), "success",
                         "risk_screen 应容忍字符串 hs_code，而非 AttributeError")

    def test_orchestrator_k2_compliance_critical_upgrades_tier(self):
        """K2：合规 critical 必须汇入 risk_flags，并把分流等级升到 C。"""
        s = load_scenario(_SCENARIOS["scenarios"], "scenario_2_elements")
        orc = build_orchestrator(verbose=False)
        r = orc.ask(s["query"], context=dict(s["context"]))
        data = r.data
        flags = data.get("risk_flags") or []
        compliance_critical = [
            f for f in flags
            if f.get("type") == "compliance" and f.get("severity") == "critical"
        ]
        self.assertTrue(compliance_critical, "合规 critical 应写入统一 risk_flags")
        self.assertEqual(data.get("tier"), "C", "存在合规 critical 时不得停留在 A 级")

    def test_orchestrator_k3_merge_consumes_upstream(self):
        """K3：归并预检应消费上游产物，而非只读原始 order。"""
        ctx = {
            "cargo_name": "硅胶手机壳",
            "hs_code": "3926909090",
            "doc_type": "declaration_form",
            "quantity": 1000,
            "unit": "个",
            "currency": "USD",
            "dest_country": "美国",
            "model": "iPhone 15",
            "shipper": "深圳智关科技有限公司",
            "consignee": "John Smith",
            "gross_weight": 50,
            "total_amount": 2000,
        }
        orc = build_orchestrator(verbose=False)
        r = orc.ask("硅胶手机壳出口美国", context=ctx)
        merge = (r.data.get("merge_result") or {})
        single = merge.get("single_check") or {}
        self.assertTrue(single.get("complete"),
                        f"上游已提供要素，归并不应再报缺失：{single.get('missing')}")
        effective = merge.get("effective_record") or {}
        self.assertEqual(effective.get("hs_code"), "3926909090")
        self.assertIn("深圳智关科技有限公司", str(effective.get("shipper_consignee") or ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)

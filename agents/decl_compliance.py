"""C 清单合规 Agent。

9610 出口申报清单（CEB603 报文）合规校验，分两层：
- 第一层（确定性）：必填字段、格式、枚举值、逻辑一致性校验。
- 第二层（LLM 可选）：商品名称语义合规性判断（是否足够具体、是否含禁限描述）。

接入契约（state.py AGENT_IO_CONTRACT）：
- reads: declaration_draft, hs_code
- writes: decl_compliance
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from .base import BaseAgent, AgentResult

try:  # 申报要素目录（报关单/汇总申报用）
    from customs.decl_elements import DeclElementsKB
    _DECL_ELEMENTS = DeclElementsKB()
except Exception:  # 缺数据或路径不可用时不阻断清单校验
    _DECL_ELEMENTS = None

try:  # 禁限管制申报要素参数（E 板块）
    from customs.control_params import ControlParamsKB
    _CONTROL_PARAMS = ControlParamsKB()
except Exception:
    _CONTROL_PARAMS = None

try:  # HS 税号管制状态（自动触发，无需场景显式声明）
    from customs.hs_control import HSControlMap
    _HS_CONTROL = HSControlMap()
except Exception:
    _HS_CONTROL = None


# 申报要素名 → declaration_dict 键（同名变体一并映射）
_ELEMENT_KEY_MAP = {
    "品牌类型": "brand_type",
    "出口享惠情况": "export_preferential",
    "用途": "usage",
    "材质（塑料品种）": "material",
    "材质": "material",
    "品牌（中文或外文名称）": "brand",
    "型号": "model",
    "GTIN": "gtin",
    "CAS": "cas",
    "结构类型（是否有箱体）": "structure_type",
    "喇叭数量和尺寸": "speaker_spec",
    "喇叭尺寸": "speaker_spec",
    "外观": "appearance",
}


# CEB603 核心字段规范（简化版，覆盖表头+表体关键字段）
# field: (中文名, 必填, 校验类型, 枚举/规则)
FIELD_SPEC = [
    ("跨境电商平台", "跨境电商平台", True, "non_empty", None),
    ("订单编号", "订单编号", True, "non_empty", None),
    ("物流运单号", "物流运单号", True, "non_empty", None),
    ("商品名称", "商品名称", True, "non_empty", None),
    ("HS编码", "HS编码", True, "hs_code", None),
    ("申报数量", "申报数量", True, "positive_number", None),
    ("申报单价", "申报单价", True, "positive_number", None),
    ("申报总价", "申报总价", True, "positive_number", None),
    ("币制", "币制", True, "enum", ["USD", "EUR", "JPY", "GBP", "CNY", "HKD"]),
    ("境内发货人", "境内发货人", True, "non_empty", None),
    ("境外收货人", "境外收货人", True, "non_empty", None),
    ("起运口岸", "起运口岸", True, "non_empty", None),
    ("目的国", "目的国", True, "non_empty", None),
    ("毛重", "毛重", True, "positive_number", None),
    ("净重", "净重", True, "positive_number", None),
]

# 笼统品名黑名单（第二层兜底，LLM 不可用时使用）
GENERIC_NAME_BLACKLIST = [
    "杂货", "日用品", "样品", "配件", "零件", "货物", "商品",
    "物品", "产品", "塑料制品", "金属制品", "木制品", "纺织品",
    "电子配件", "misc", "sample", "parts", "goods", "products",
]


class DeclarationComplianceAgent(BaseAgent):
    """清单合规校验 Agent：CEB603 字段格式 + 品名语义合规。"""

    name = "decl_compliance"
    description = "9610 申报清单合规校验：字段格式 + 品名语义"

    def ask(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> AgentResult:
        ctx = context or {}
        draft = ctx.get("declaration_draft") or ""
        hs_code = ctx.get("hs_code") or {}
        # v4：优先读结构化 dict（classify 槽产出），没有再从文本反解
        decl_dict = ctx.get("declaration_dict") or None

        # ---- 解析申报清单字段 ----
        fields = self._resolve_fields(decl_dict, draft)
        # 用 hs_code 补全缺失字段
        if hs_code:
            fields.setdefault("HS编码", hs_code.get("code", ""))
            fields.setdefault("商品名称", hs_code.get("official_name") or hs_code.get("description", ""))

        # ---- 第一层：确定性格式校验（含单证适用性路由）----
        format_issues = self._validate_format(fields, ctx)

        # ---- 第二层：品名语义合规 ----
        name_issues = self._validate_name_semantics(fields.get("商品名称", ""))

        # ---- 第三层（仅报关单/汇总申报）：申报要素完备性 ----
        element_issues = self._validate_elements(decl_dict, hs_code, ctx)

        # ---- 第四层：生产销售单位 78 号公告校验（B 板块 v2.1 场景 3）----
        psu_issues = self._validate_production_sales_unit(decl_dict, ctx)

        # ---- 第五层（E 板块）：禁限管制申报要素（按税号自动触发）----
        control_issues = self._validate_control_elements(ctx, hs_code)

        all_issues = format_issues + name_issues + element_issues + psu_issues + control_issues

        # 判定合规等级
        critical = [i for i in all_issues if i["severity"] == "critical"]
        warnings = [i for i in all_issues if i["severity"] == "warning"]

        if not decl_dict and not draft:
            status = "no_draft"
            compliant = False
            needs_review = True
        elif critical:
            status = "non_compliant"
            compliant = False
            needs_review = True
        elif warnings:
            status = "needs_review"
            compliant = False
            needs_review = True
        else:
            status = "compliant"
            compliant = True
            needs_review = False

        result_data = {
            "decl_compliance": {
                "status": status,
                "compliant": compliant,
                "checked_fields": list(fields.keys()),
                "issues": all_issues,
                "critical_count": len(critical),
                "warning_count": len(warnings),
            }
        }

        # ---- summary ----
        lines = [f"## 清单合规校验结果"]
        lines.append(f"- **合规状态**：{self._status_text(status)}")
        lines.append(f"- **已检字段**：{len(fields)} 项")
        if critical:
            lines.append(f"- **严重问题**（{len(critical)}）：")
            for i in critical:
                lines.append(f"  - 🔴 [{i['field']}] {i['message']}")
        if warnings:
            lines.append(f"- **警告**（{len(warnings)}）：")
            for i in warnings:
                lines.append(f"  - 🟡 [{i['field']}] {i['message']}")
        infos = [i for i in all_issues if i["severity"] == "info"]
        if infos:
            lines.append(f"- **提示（不阻断）**（{len(infos)}）：")
            for i in infos:
                lines.append(f"  - ℹ️ [{i['field']}] {i['message']}")
        if not critical and not warnings:
            lines.append("- **问题**：无，清单合规 ✓")

        return AgentResult(
            agent_name=self.name,
            summary="\n".join(lines),
            data=result_data,
            next_hint=(
                "清单合规，可推送申报" if compliant
                else "存在不合规项，修正后重新校验"
            ),
            confidence=0.9 if compliant else 0.5,
            needs_human_review=needs_review,
        )

    # ----------------------------------------------------------------- #
    # 第三层：申报要素完备性（报关单/汇总申报）
    # ----------------------------------------------------------------- #
    @staticmethod
    def _validate_elements(
        decl_dict: Optional[Dict[str, Any]],
        hs_code: Optional[Dict[str, Any]],
        ctx: Dict[str, Any],
    ) -> List[Dict[str, str]]:
        """按税号最长前缀匹配官方要素清单，逐项检查必填项。

        仅在 ctx["doc_type"] == "declaration_form" 时执行——9610 清单使用
        CEB603 栏目，不适用涉税规范申报要素，避免把报关单要求误加到清单上。
        空值 = 未提供；"无品牌"/"不适用" 等文本视为已填（区分"缺失"与"明确无"）。
        """
        if ctx.get("doc_type") != "declaration_form" or _DECL_ELEMENTS is None:
            return []
        code = (hs_code or {}).get("code", "")
        hit = _DECL_ELEMENTS.lookup(code)
        if not hit:
            return [{
                "field": "申报要素",
                "severity": "warning",
                "message": f"税号 {code} 未在已抓取要素表（1,202 个编码键）中命中，"
                           f"要素清单需人工核对规范申报目录",
                "rule_type": "DECL_ELEMENT_CATALOG",
            }]

        data = decl_dict or {}
        issues: List[Dict[str, str]] = []
        generic_material = {"塑料", "金属", "橡胶", "纺织", "复合材料"}
        for item in hit["elements"]:
            name = item["element"]
            key = _ELEMENT_KEY_MAP.get(name)
            value = data.get(key) if key else None
            text = "" if value is None else str(value).strip()
            if item["required"] and not text:
                issues.append({
                    "field": name,
                    "severity": "critical",
                    "message": f"申报要素缺失（必填，{hit['key']} 清单）：{name}",
                    "rule_type": "DECL_ELEMENT_REQUIRED",
                })
            elif name == "材质（塑料品种）" and text in generic_material:
                issues.append({
                    "field": name,
                    "severity": "critical",
                    "message": f"申报要素表述不达标：材质须写具体品种（如 TPU+PC），当前为「{text}」",
                    "rule_type": "DECL_ELEMENT_FORMAT",
                })
            elif name == "GTIN" and not text:
                # B板块 v2.1：选填缺失不阻断，提示可填或明示无
                issues.append({
                    "field": "GTIN",
                    "severity": "info",
                    "message": "GTIN 为选填（required=0），缺失不阻断；可填全球贸易项目代码，或明示「无 GTIN」",
                    "rule_type": "DECL_ELEMENT_OPTIONAL",
                })
            elif name == "CAS" and not text:
                # B板块 v2.1：判定链机械得出（商品形态=物品 → 无单一 CAS 号），非猜测
                issues.append({
                    "field": "CAS",
                    "severity": "info",
                    "message": "CAS 判定链：商品形态=物品（工业制品，非化学单体）→ 无单一 CAS 号 "
                               "→ 建议申报「不适用」（系统按槽位规则机械判定，附判定链输出）",
                    "rule_type": "DECL_ELEMENT_OPTIONAL",
                })
        return issues

    # ----------------------------------------------------------------- #
    # 第四层：生产销售单位（78 号公告）
    # ----------------------------------------------------------------- #
    @staticmethod
    def _validate_production_sales_unit(
        decl_dict: Optional[Dict[str, Any]],
        ctx: Dict[str, Any],
    ) -> List[Dict[str, str]]:
        """78 号公告：生产销售单位应填生产厂家或销售单位，不得填平台/代理企业名称。

        v2.1 口径：正确值 = 销售单位（企业B）或生产厂家（工厂C）均可，
        平台名是唯一错误——不做"必须是工厂"的收紧判定。
        """
        d = decl_dict or {}
        psu = (d.get("production_sales_unit") or ctx.get("production_sales_unit") or "").strip()
        platform = (d.get("platform") or ctx.get("platform") or "").strip()
        if not psu or not platform:
            return []
        if psu == platform:
            return [{
                "field": "生产销售单位",
                "severity": "critical",
                "message": f"生产销售单位填了电商平台名称「{psu}」：78 号公告要求填生产厂家或销售单位，"
                           f"不得填平台/代理企业名称；请改为销售单位或生产厂家",
                "rule_type": "LEGAL_REQUIREMENT",
                "source": "海关总署 2026 年第 78 号公告（涉管制物项口径，参照适用）",
            }]
        return []

    # ----------------------------------------------------------------- #
    # 第五层（E 板块）：禁限管制申报要素
    # ----------------------------------------------------------------- #
    @staticmethod
    def _validate_control_elements(ctx: Dict[str, Any], hs_code: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
        """涉管制物项的必答参数问题校验（128 条 / 16 类）。

        v2 自动触发：按 HS 税号查管制映射（6,812 个受管制税号）——
        受管制且申报类别码已知 → 自动要求该类别必答问题，无需场景声明；
        受管制但类别码缺失 → 警告人工核对（映射表类别链接仅 125 条，不猜测）。
        ctx.control_category 显式声明可覆盖自动映射（用于映射缺失的教学案例）。
        """
        if _CONTROL_PARAMS is None:
            return []
        code = (hs_code or {}).get("code", "")
        explicit = ctx.get("control_category")
        if explicit:
            categories = [str(explicit)]
        else:
            if _HS_CONTROL is None or not code:
                return []
            ctrl = _HS_CONTROL.check(code)
            if not ctrl["controlled"]:
                return []
            categories = ctrl["declaration_categories"]
            if not categories:
                return [{
                    "field": "管制申报要素",
                    "severity": "warning",
                    "message": f"税号 {code}（{ctrl['g_name']}）受管制"
                               f"（control_mark={ctrl['control_mark']}），但申报类别码未在映射表中，"
                               f"须人工核对类别后补答必问参数",
                    "rule_type": "CONTROL_MAPPING",
                }]
        issues: List[Dict[str, str]] = []
        for cat in categories:
            v = _CONTROL_PARAMS.validate_answers(cat, ctx.get("control_answers"))
            if not v["hit"]:
                issues.append({
                    "field": "管制申报要素",
                    "severity": "warning",
                    "message": f"申报类别码 {cat} 未在管制参数表（16 类）命中，需人工核对类别",
                    "rule_type": "CONTROL_MAPPING",
                })
                continue
            if v["unanswered"]:
                preview = "；".join(v["unanswered"][:2]) + ("…" if len(v["unanswered"]) > 2 else "")
                issues.append({
                    "field": "管制申报要素",
                    "severity": "critical",
                    "message": f"禁限管制申报要素未答（{v['name']} {cat}，共 {v['total']} 问，"
                               f"未答 {len(v['unanswered'])} 项）：{preview}（按税号 {code} 自动触发）",
                    "rule_type": "CONTROL_ITEM_REQUIRED",
                })
        return issues

    # ----------------------------------------------------------------- #
    # 解析入口：优先 dict，fallback 文本
    # ----------------------------------------------------------------- #
    @staticmethod
    def _resolve_fields(
        decl_dict: Optional[Dict[str, Any]],
        draft_text: str,
    ) -> Dict[str, str]:
        """v4：结构化 dict 优先，没有再 parse 文本。

        dict 是 CustomsAgent._build_declaration_dict() 产物，key 为英文 snake_case；
        本函数把它映射成 FIELD_SPEC 使用的中文 key。
        """
        if decl_dict:
            d = decl_dict or {}
            fields = {
                "跨境电商平台": d.get("platform"),
                "订单编号": d.get("order_id"),
                "物流运单号": d.get("logistics_no"),
                "商品名称": d.get("cargo_spec") or d.get("cargo_common"),
                "HS编码": d.get("hs_code"),
                "申报数量": d.get("quantity"),
                "计量单位": d.get("unit"),
                "申报单价": d.get("unit_price"),
                "申报总价": d.get("total_price"),
                "币制": d.get("currency"),
                "境内发货人": d.get("shipper"),
                "境外收货人": d.get("consignee"),
                "起运口岸": d.get("from_port"),
                "目的国": d.get("dest_country") or d.get("dest_port"),
                "毛重": d.get("gross_weight"),
                "净重": d.get("net_weight"),
            }
            # 统一为字符串（FIELD_SPEC 校验逻辑是 str.strip()）
            return {k: (str(v) if v is not None else "") for k, v in fields.items()}
        # fallback：文本反解
        return DeclarationComplianceAgent._parse_draft(draft_text)

    @staticmethod
    def _parse_draft(draft: str) -> Dict[str, str]:
        """从申报清单文本中解析关键字段。"""
        fields: Dict[str, str] = {}
        if not draft:
            return fields

        # 按行解析 "字段名：值" 或 "字段名：值"
        patterns = {
            "跨境电商平台": r"跨境电商平台[：:]\s*(.+)",
            "订单编号": r"订单编号[：:]\s*(.+)",
            "物流运单号": r"物流运单号[：:]\s*(.+)",
            "商品名称": r"商品名称[（(]规范[)）][：:]\s*(.+)",
            "商品俗称": r"商品俗称[：:]\s*(.+)",
            "HS编码": r"HS编码[（(]10位[)）][：:]\s*([\dA-Z]+)",
            "申报数量": r"申报数量[：:]\s*([\d.]+)",
            "计量单位": r"计量单位[：:]\s*(.+)",
            "申报单价": r"申报单价[：:]\s*([\d.]+)",
            "申报总价": r"申报总价[：:]\s*([\d.]+)",
            "币制": r"币制[：:]\s*([A-Z]+)",
            "境内发货人": r"境内发货人[：:]\s*(.+)",
            "境外收货人": r"境外收货人[：:]\s*(.+)",
            "起运口岸": r"起运口岸[：:]\s*(.+)",
            "目的国": r"目的国(?:/港)?[：:]\s*(.+)",
            "毛重": r"毛重[：:]\s*([\d.]+)",
            "净重": r"净重[：:]\s*([\d.]+)",
        }
        for key, pat in patterns.items():
            m = re.search(pat, draft)
            if m:
                val = m.group(1).strip()
                if val and val != "____":
                    fields[key] = val
        return fields

    # ----------------------------------------------------------------- #
    # 第一层：格式校验
    # ----------------------------------------------------------------- #
    def _validate_format(self, fields: Dict[str, str], ctx: Optional[Dict[str, Any]] = None) -> List[Dict]:
        issues: List[Dict] = []
        spec_map = {s[0]: s for s in FIELD_SPEC}
        # 单证适用性路由（GAP-CEB603-SCOPE 修复）：
        # CEB603 清单专属栏位对报关单（declaration_form）不适用 → not_applicable，而非 critical
        doc_type = (ctx or {}).get("doc_type")
        list_only = {"跨境电商平台", "订单编号", "物流运单号"}

        for field_key, cn_name, required, vtype, rule in FIELD_SPEC:
            val = fields.get(field_key, "").strip()

            if doc_type == "declaration_form" and field_key in list_only:
                if not val:
                    issues.append({
                        "field": field_key,
                        "severity": "not_applicable",
                        "message": f"{cn_name}为 CEB603 清单栏位，报关单（declaration_form）口径不适用",
                        "rule_type": "DOC_TYPE_SCOPE",
                    })
                continue

            # 必填检查
            if not val:
                if required:
                    issues.append({
                        "field": field_key,
                        "severity": "critical",
                        "message": f"{cn_name}为空（必填字段）",
                        "rule_type": "MANDATORY_FIELD",
                    })
                continue

            # 类型校验
            if vtype == "hs_code":
                if not re.fullmatch(r"\d{10}", val):
                    issues.append({
                        "field": field_key,
                        "severity": "critical",
                        "message": f"HS编码 '{val}' 格式错误，应为10位数字",
                        "rule_type": "FORMAT_HS_CODE",
                    })
            elif vtype == "positive_number":
                try:
                    fv = float(val)
                    if fv <= 0:
                        issues.append({
                            "field": field_key,
                            "severity": "critical",
                            "message": f"{cn_name} '{val}' 必须为正数",
                            "rule_type": "FORMAT_NUMBER",
                        })
                except ValueError:
                    issues.append({
                        "field": field_key,
                        "severity": "critical",
                        "message": f"{cn_name} '{val}' 不是有效数字",
                        "rule_type": "FORMAT_NUMBER",
                    })
            elif vtype == "enum":
                if val not in (rule or []):
                    issues.append({
                        "field": field_key,
                        "severity": "warning",
                        "message": f"币制 '{val}' 不在常用枚举 {rule} 中",
                        "rule_type": "FORMAT_ENUM",
                    })

        # 逻辑一致性校验
        self._validate_consistency(fields, issues)
        return issues

    @staticmethod
    def _validate_consistency(fields: Dict[str, str], issues: List[Dict]) -> None:
        """逻辑一致性：单价×数量≈总价、净重≤毛重。"""
        try:
            qty = float(fields.get("申报数量", 0))
            price = float(fields.get("申报单价", 0))
            total = float(fields.get("申报总价", 0))
            if qty > 0 and price > 0 and total > 0:
                expected = qty * price
                if abs(expected - total) / expected > 0.05:
                    issues.append({
                        "field": "逻辑一致性",
                        "severity": "warning",
                        "message": f"单价×数量={expected:.2f} 与总价 {total} 偏差超5%",
                        "rule_type": "LOGIC_CONSISTENCY",
                    })
        except (ValueError, ZeroDivisionError):
            pass

        try:
            gross = float(fields.get("毛重", 0))
            net = float(fields.get("净重", 0))
            if gross > 0 and net > 0 and net > gross:
                issues.append({
                    "field": "逻辑一致性",
                    "severity": "critical",
                    "message": f"净重 {net}kg 大于毛重 {gross}kg",
                    "rule_type": "LOGIC_CONSISTENCY",
                })
        except ValueError:
            pass

    # ----------------------------------------------------------------- #
    # 第二层：品名语义合规
    # ----------------------------------------------------------------- #
    def _validate_name_semantics(self, cargo_name: str) -> List[Dict]:
        """判断商品名称是否足够具体、是否含禁限描述。

        优先用 LLM（若配置 DEEPSEEK_API_KEY），否则用关键词兜底。
        """
        if not cargo_name:
            return []
        issues: List[Dict] = []

        # 兜底规则：笼统词黑名单
        for word in GENERIC_NAME_BLACKLIST:
            if word in cargo_name.lower():
                issues.append({
                    "field": "商品名称",
                    "severity": "warning",
                    "message": f"商品名称 '{cargo_name}' 含笼统词 '{word}'，不够具体，可能被要求补申报要素",
                    "rule_type": "NAME_SEMANTICS",
                })
                break

        # 品名过短（<3字符）也警告
        if len(cargo_name.strip()) < 3:
            issues.append({
                "field": "商品名称",
                "severity": "warning",
                "message": f"商品名称 '{cargo_name}' 过短，建议补充材质/用途/规格",
                "rule_type": "NAME_SEMANTICS",
            })

        # LLM 语义判断（可选）
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if api_key:
            llm_issue = self._llm_name_check(cargo_name, api_key)
            if llm_issue:
                issues.append(llm_issue)

        return issues

    @staticmethod
    def _llm_name_check(cargo_name: str, api_key: str) -> Optional[Dict]:
        """LLM 判断品名是否具体、是否含禁限描述。"""
        try:
            from openai import OpenAI
            client = OpenAI(
                api_key=api_key,
                base_url="https://api.deepseek.com/v1",
            )
            prompt = (
                f"判断以下报关商品名称是否合规（具体、无禁限描述）：\n"
                f"商品名称：{cargo_name}\n\n"
                f"合规标准：\n"
                f"1. 具体：应包含材质/用途/规格，如'硅胶塑料手机壳'合规，'塑料制品'不合规\n"
                f"2. 不含禁限词：如'象牙'、'武器'、'毒品'等\n\n"
                f"只回答 JSON 格式：{{\"compliant\": true或false, \"reason\": \"原因说明\"}}"
            )
            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=150,
                temperature=0.1,
            )
            import json
            text = resp.choices[0].message.content.strip()
            # 提取 JSON
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                data = json.loads(m.group(0))
                if not data.get("compliant", True):
                    return {
                        "field": "商品名称",
                        "severity": "warning",
                        "message": f"LLM判定：{data.get('reason', '品名不合规')}",
                        "rule_type": "NAME_SEMANTICS_LLM",
                    }
        except Exception:
            pass  # LLM 失败不阻断，兜底规则已覆盖
        return None

    @staticmethod
    def _status_text(status: str) -> str:
        return {
            "compliant": "✅ 合规",
            "needs_review": "🟡 需复核",
            "non_compliant": "🔴 不合规（禁止推送）",
            "no_draft": "⚪ 无申报清单",
        }.get(status, status)


if __name__ == "__main__":
    agent = DeclarationComplianceAgent()

    # 测试1：合规清单
    print("=" * 50)
    print("测试1：合规清单")
    draft1 = """======================================================
      跨境电子商务零售出口（9610）申报清单
======================================================
【申报基本信息】
监管方式：9610
跨境电商平台：SHEIN
订单编号：ORD20261015001
物流运单号：YT1234567890

【商品信息】
商品名称（规范）：硅胶塑料手机壳
商品俗称：手机壳
HS编码（10位）：3926909090
商品描述：塑料制手机保护壳

【数量与价格】
申报数量：100
计量单位：千克
申报单价：5.00
申报总价：500.00
币制：USD

【收发货信息】
境内发货人：深圳市XX贸易有限公司
境外收货人：John Smith
起运口岸：深圳
目的国：美国

【包装信息】
包装方式：纸箱
毛重：50.0 kg
净重：48.0 kg
件数：10
======================================================"""
    res = agent.ask("报关", context={"declaration_draft": draft1, "hs_code": {"code": "3926909090"}})
    print(res.summary)
    print()

    # 测试2：HS编码错误 + 笼统品名
    print("=" * 50)
    print("测试2：HS编码错误 + 笼统品名")
    draft2 = draft1.replace("3926909090", "3926").replace("硅胶塑料手机壳", "塑料制品")
    res2 = agent.ask("报关", context={"declaration_draft": draft2, "hs_code": {"code": "3926"}})
    print(res2.summary)
    print()

    # 测试3：缺失必填字段
    print("=" * 50)
    print("测试3：缺失必填字段")
    draft3 = "商品名称：手机壳\nHS编码：3926909090\n申报数量：100\n申报总价：500\n币制：USD"
    res3 = agent.ask("报关", context={"declaration_draft": draft3, "hs_code": {"code": "3926909090"}})
    print(res3.summary)

"""字段缺口自主分析器（Field Gap Analyzer）。

目标：AI 在查询规则之后，自主判断——
  还缺什么字段？为什么缺？能不能自行解决？还是必须提问用户？

判断依据全部来自规则资产（不来自任何预制的标准答案）：
  1. field_dict.json      —— 栏位必填条件、来源链、缺失处理策略
  2. decl_elements.json   —— 税号申报要素必填清单（报关单口径）
  3. hs_control_map.json  —— 税号管制状态 → 自动触发的必答参数（E 板块）

输出四类处置：
  self_resolved  可自行解决（默认值/派生计算），补值并标记 provenance
  ask_user       必须提问（附结构化原因：规则依据 + 缺失来源）
  not_applicable 单证口径不适用（doc_type 路由）
  filled         已具备
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from customs.decl_elements import DeclElementsKB
    _DECL_ELEMENTS = DeclElementsKB()
except Exception:
    _DECL_ELEMENTS = None

try:
    from customs.hs_control import HSControlMap
    from customs.control_params import ControlParamsKB
    _HS_CONTROL = HSControlMap()
    _CONTROL_PARAMS = ControlParamsKB()
except Exception:
    _HS_CONTROL = None
    _CONTROL_PARAMS = None

_FIELD_DICT_PATH = Path(__file__).resolve().parent / "data" / "field_dict.json"

# 申报要素名 → declaration_dict 键（与 decl_compliance 保持一致）
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

# field_dict.template_var（旧模板变量名）→ declaration_dict 键（v4 结构化主输出）别名
_TEMPLATE_KEY_ALIAS = {
    "total_usd": "total_price",
}

# field_dict.source_field 与 declaration_dict 键不一致时的候选映射。
# 例：商品名称（规范）在 field_dict 里 source_field=official_name，
# 但结构化产物里是 cargo_common / cargo_spec / hs_desc；HS编码 source_field=code，
# 产物键是 hs_code；申报数量 template_var=tonnage，产物键是 quantity。
_KEY_CANDIDATES = {
    "official_name": ["cargo_common", "cargo_spec", "hs_desc"],
    "code": ["hs_code"],
    "tonnage": ["quantity"],
    "unit_price_usd": ["unit_price"],
}


def _resolve_field(d: Dict[str, Any], key: str):
    """在声明 dict 中按候选键查找，返回 (实际键, 值)。"""
    if d.get(key) not in (None, "", "____"):
        return key, d.get(key)
    for alt in _KEY_CANDIDATES.get(key, []):
        if d.get(alt) not in (None, "", "____"):
            return alt, d.get(alt)
    return key, d.get(key)

# 真正可派生的字段：字段 → (派生方法说明, 计算函数)
_DERIVATIONS = {
    "total_price": (
        "总价 = 数量 × 单价（arithmetic 派生）",
        lambda d: (float(d["quantity"]) * float(d["unit_price"]))
        if d.get("quantity") and d.get("unit_price")
        and _is_num(d.get("quantity")) and _is_num(d.get("unit_price")) else None,
    ),
}


def _is_num(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


class FieldGapAnalyzer:
    """规则驱动的字段缺口分析：缺什么 / 为什么 / 自解还是提问。"""

    def __init__(self, path: Path = _FIELD_DICT_PATH):
        self._fields: List[Dict] = []
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            for section in data.get("sections", []):
                for f in section.get("fields", []):
                    f = dict(f)
                    f["_section"] = section.get("section_name", "")
                    self._fields.append(f)

    # ----------------------------------------------------------------- #
    # 主入口
    # ----------------------------------------------------------------- #
    def analyze(
        self,
        decl_dict: Dict[str, Any],
        ctx: Dict[str, Any],
        hs_code: Optional[Dict[str, Any]],
        doc_type: str = "",
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "self_resolved": [],
            "ask_user": [],
            "not_applicable": [],
            "filled": 0,
            "required_total": 0,
        }
        d = decl_dict or {}

        # ---- 第一层：field_dict 栏位规则 ----
        for f in self._fields:
            cond = f.get("required_condition")
            if cond != "always":
                continue  # 条件必填/选填暂不入必缺判定（条件求值器待规则完备后接入）
            label = f.get("cn_label", f.get("template_var") or f.get("source_field", ""))
            # 单证口径路由：不适用栏位不参与必填统计
            if doc_type and self._doc_type_scopes(f, doc_type) is False:
                result["not_applicable"].append(
                    {"field": label, "reason": f"{doc_type} 口径不适用该栏位"})
                continue
            result["required_total"] += 1
            key = f.get("template_var") or f.get("source_field")
            if not key:
                continue
            key = _TEMPLATE_KEY_ALIAS.get(key, key)
            resolved_key, val = _resolve_field(d, key)
            if val not in (None, "", "____"):
                result["filled"] += 1
                continue
            action = f.get("missing_action")
            source = f.get("source")
            reason = (
                f"{label} 为必填栏位（{f.get('_section', '')}）；"
                f"来源链 {source}→{f.get('source_field') or key} 未取到值："
                + ("上游 context 未提供" if source == "context"
                   else "抽取器未产出" if source in ("cargo_info", "ocr")
                   else "知识库/规则未产出" if source in ("hs_code",)
                   else "未提供")
            )
            # 能否自行解决
            if action == "fill_default" and f.get("default_value") is not None:
                result["self_resolved"].append({
                    "field": label, "key": key,
                    "value": f["default_value"],
                    "method": f"默认值（field_dict: {f.get('notes', '')[:40]}）",
                    "provenance": "DEFAULT",
                })
            elif resolved_key in _DERIVATIONS and _DERIVATIONS[resolved_key][1](d) is not None:
                result["self_resolved"].append({
                    "field": label, "key": resolved_key,
                    "value": round(_DERIVATIONS[resolved_key][1](d), 2),
                    "method": _DERIVATIONS[resolved_key][0],
                    "provenance": "DERIVED",
                })
            else:
                result["ask_user"].append({
                    "field": label, "key": key,
                    "question": f"请提供「{label}」",
                    "reason": reason + f"；缺失处理策略={action}",
                    "source_hint": source,
                })

        # ---- 第二层：申报要素（仅报关单/汇总申报口径）----
        if doc_type == "declaration_form" and _DECL_ELEMENTS:
            code = (hs_code or {}).get("code", "")
            hit = _DECL_ELEMENTS.lookup(code)
            if hit:
                for item in hit["elements"]:
                    name = item["element"]
                    key = _ELEMENT_KEY_MAP.get(name)
                    if not item["required"] or not key:
                        continue
                    result["required_total"] += 1
                    val = d.get(key)
                    if val not in (None, "", "____"):
                        result["filled"] += 1
                        continue
                    result["ask_user"].append({
                        "field": f"申报要素·{name}",
                        "key": key,
                        "question": f"请提供申报要素「{name}」（{hit['key']} 涉税规范申报目录必填）",
                        "reason": f"税号 {code} 必填要素，来源=商品资料/上游 context 均未提供；"
                                  f"若确无该项（如无品牌），须明示「无」——留空 ≠ 明示无",
                        "source_hint": "cargo_info",
                    })
            else:
                result["ask_user"].append({
                    "field": "申报要素清单",
                    "key": None,
                    "question": f"税号 {code} 不在要素目录中，请人工核对规范申报目录",
                    "reason": "要素目录（1,202 键）未命中——数据覆盖缺口，非申报人缺失",
                    "source_hint": "kb_coverage",
                })

        # ---- 第三层：管制必答参数（按税号自动触发）----
        if _HS_CONTROL and _CONTROL_PARAMS:
            code = (hs_code or {}).get("code", "")
            ctrl = _HS_CONTROL.check(code)
            if ctrl["controlled"]:
                answers = ctx.get("control_answers") or {}
                for cat in ctrl["declaration_categories"]:
                    v = _CONTROL_PARAMS.validate_answers(cat, answers)
                    if not v["hit"]:
                        continue
                    for q in v["unanswered"]:
                        result["ask_user"].append({
                            "field": f"管制参数·{v['name']}",
                            "key": f"control_{cat}",
                            "question": q,
                            "reason": f"税号 {code} 受管制（control_mark={ctrl['control_mark']}），"
                                      f"类别 {cat} 必答参数未答——按管制映射自动触发",
                            "source_hint": "control_mapping",
                        })
                if not ctrl["declaration_categories"]:
                    result["ask_user"].append({
                        "field": "管制参数·类别核对",
                        "key": None,
                        "question": f"税号 {code}（{ctrl['g_name']}）受管制，请人工核对申报类别码后补答必问参数",
                        "reason": f"受管制（control_mark={ctrl['control_mark']}）但类别码未在映射表"
                                  f"（类别链接仅 125 条）——数据覆盖缺口，不猜测",
                        "source_hint": "control_mapping",
                    })
        return result

    # ----------------------------------------------------------------- #
    @staticmethod
    def _doc_type_scopes(field: Dict, doc_type: str) -> Optional[bool]:
        """栏位对单证口径的适用性：True 适用 / False 不适用 / None 未知。"""
        fc = str(field.get("field_code", ""))
        if doc_type == "declaration_form" and fc.startswith("column_") and fc in (
            "column_platform", "column_order_id", "column_logistics_no",
        ):
            return False
        if doc_type == "list_9610" and fc.startswith("element_"):
            return False
        return None

    # ----------------------------------------------------------------- #
    def apply_self_resolved(self, decl_dict: Dict[str, Any], gaps: Dict[str, Any]) -> Dict[str, Any]:
        """把可自解字段写回申报 dict（带 provenance 标记）。"""
        for item in gaps.get("self_resolved", []):
            if item.get("key"):
                decl_dict[item["key"]] = item["value"]
                decl_dict.setdefault("_provenance", {})[item["key"]] = item.get("provenance", "DERIVED")
        return decl_dict

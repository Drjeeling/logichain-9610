"""9610 申报清单：结构化组装 + 逐栏溯源 + Excel 导出。

设计约束：
1. 栏目定义只从 field_dict.json 读取，不在代码里另维护一份。
2. 每个栏目都带 status（FILLED / DERIVED / DEFAULTED / NEED_INPUT / BLOCK / EMPTY），
   前端与 Excel 都能回答"这个格子为什么空/为什么是这个值"。
3. 缺失处理遵循 field_dict 的 missing_action：block 阻断定稿、ask_user 进补填表单、
   warn 允许导出但标注、fill_default 用默认值。
"""
from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

FIELD_DICT_PATH = Path(__file__).resolve().parent / "data" / "field_dict.json"

# template_var / 字段名前缀推导不到的、或推导结果与结构化 dict 不一致的，在这里显式覆盖
DECL_KEY_OVERRIDES = {
    "column_quantity": "quantity",      # template_var 是 tonnage，业务口径以件数为准
    "column_total_price": "total_price",  # template_var 是 total_usd（模板旧变量）
}

STATUS_LABELS = {
    "FILLED": "已填",
    "DERIVED": "派生",
    "DEFAULTED": "默认值",
    "NEED_INPUT": "待补填",
    "BLOCK": "阻断",
    "EMPTY": "空（选填）",
    "WARN": "缺失（警告）",
}

# 导出清单时的栏目分组（表头 / 表体）
HEADER_SECTIONS = ("header", "parties", "package")
ITEM_SECTIONS = ("goods", "quantity_price", "special_2026")


def load_field_dict(path: Path = FIELD_DICT_PATH) -> Dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


FIELD_DICT = load_field_dict()


def _decl_key(field: Dict) -> str:
    code = field["field_code"]
    if code in DECL_KEY_OVERRIDES:
        return DECL_KEY_OVERRIDES[code]
    if field.get("template_var"):
        return field["template_var"]
    for prefix in ("column_", "element_"):
        if code.startswith(prefix):
            return code[len(prefix):]
    return code


def _is_required(field: Dict, doc_type: str) -> bool:
    cond = field.get("required_condition")
    if cond == "always":
        return True
    if cond == "conditional":
        return False
    if isinstance(cond, dict):
        return cond.get("doc_type") == doc_type
    return False


def _blank(val: Any) -> bool:
    return val is None or val == "" or (isinstance(val, str) and not val.strip())


def _resolve_field(field: Dict, decl: Dict, doc_type: str) -> Dict:
    """解析单个栏目：取值 → 缺失处理 → 产出带溯源的行。"""
    key = _decl_key(field)
    value = decl.get(key)
    status = "FILLED"
    note = field.get("notes") or ""

    if _blank(value):
        missing_action = field.get("missing_action") or "warn"
        required = _is_required(field, doc_type)
        default = field.get("default_value")

        if missing_action == "fill_default" and not _blank(default):
            value = default
            status = "DEFAULTED"
        elif required and missing_action == "block":
            status = "BLOCK"
        elif required and missing_action == "ask_user":
            status = "NEED_INPUT"
        elif required:
            status = "WARN"
        else:
            status = "EMPTY"

    return {
        "code": field["field_code"],
        "label": field["cn_label"],
        "key": key,
        "type": field.get("type"),
        "value": value,
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "source": field.get("source"),
        "missing_action": field.get("missing_action"),
        "required": _is_required(field, doc_type),
        "note": note,
    }


def build_payload(
    declaration: Optional[Dict[str, Any]] = None,
    edits: Optional[Dict[str, Any]] = None,
    meta: Optional[Dict[str, Any]] = None,
    doc_type: str = "9610_list",
) -> Dict[str, Any]:
    """把扁平申报 dict 组装成可预览、可导出、可追责的结构化清单。

    edits 用于"补填"：只覆盖非空值，避免前端空输入把已有值清掉。
    """
    decl = dict(declaration or {})
    for k, v in (edits or {}).items():
        if not _blank(v):
            decl[k] = v

    header_rows: List[Dict] = []
    item_rows: List[Dict] = []
    gaps: List[Dict] = []
    seen = set()

    for section in FIELD_DICT.get("sections", []):
        sid = section.get("section_id")
        for field in section.get("fields", []):
            row = _resolve_field(field, decl, doc_type)
            row["section"] = sid
            if sid in HEADER_SECTIONS:
                header_rows.append(row)
            elif sid in ITEM_SECTIONS:
                item_rows.append(row)
            if row["status"] in ("BLOCK", "NEED_INPUT", "WARN", "EMPTY") and row["code"] not in seen:
                if row["status"] != "EMPTY":
                    seen.add(row["code"])
                    gaps.append({
                        "code": row["code"],
                        "key": row["key"],
                        "label": row["label"],
                        "status": row["status"],
                        "status_label": row["status_label"],
                        "action": row["missing_action"],
                        "note": row["note"],
                    })

    counts = {k: 0 for k in STATUS_LABELS}
    for row in header_rows + item_rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1

    blocked = counts.get("BLOCK", 0) > 0
    ready = not blocked and counts.get("NEED_INPUT", 0) == 0

    totals = {
        "quantity": decl.get("quantity"),
        "unit": decl.get("unit"),
        "total_price": decl.get("total_price"),
        "currency": decl.get("currency"),
        "gross_weight": decl.get("gross_weight"),
        "net_weight": decl.get("net_weight"),
        "package_count": decl.get("package_count"),
    }

    return {
        "doc_type": doc_type,
        "doc_title": "跨境电子商务零售出口申报清单（9610）",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "declaration": decl,
        "header": header_rows,
        "items": [{"no": 1, "rows": item_rows}],
        "totals": totals,
        "gaps": gaps,
        "counts": counts,
        "blocked": blocked,
        "ready": ready,
        "field_dict_version": FIELD_DICT.get("_meta", {}).get("schema_version"),
        "meta": meta or {},
    }


# ------------------------------------------------------------------ #
# Excel 导出
# ------------------------------------------------------------------ #
def _value_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def export_xlsx(payload: Dict[str, Any]) -> bytes:
    """三张表：申报清单（可打印）/ 字段溯源 / 缺口与依据。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    thin = Side(style="thin", color="B7B7B7")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_fill = PatternFill("solid", fgColor="EBF1F7")
    warn_fill = PatternFill("solid", fgColor="FFF2CC")
    block_fill = PatternFill("solid", fgColor="FCE4E4")

    # ---------------- Sheet 1：申报清单 ----------------
    ws = wb.active
    ws.title = "申报清单"
    ws.merge_cells("A1:F1")
    ws["A1"] = payload.get("doc_title", "9610 申报清单")
    ws["A1"].font = Font(name="黑体", size=14, bold=True)
    ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 28

    ws.merge_cells("A2:F2")
    meta = payload.get("meta") or {}
    sub = " · ".join(
        str(x) for x in [
            f"监管方式 {payload.get('declaration', {}).get('trade_mode', '9610')}",
            f"分流等级 {(meta.get('tier') or '—')}",
            meta.get("case_no") or "",
            f"生成时间 {payload.get('generated_at', '')}",
        ] if x
    )
    ws["A2"] = sub
    ws["A2"].font = Font(name="等线", size=9, color="666666")
    ws["A2"].alignment = Alignment(horizontal="center")

    # 表头区：3 组「栏目 | 值」
    header_rows = payload.get("header", [])
    row_idx = 4
    for i in range(0, len(header_rows), 3):
        chunk = header_rows[i:i + 3]
        for j, item in enumerate(chunk):
            col = 1 + j * 2
            label_cell = ws.cell(row=row_idx, column=col, value=item["label"])
            value_cell = ws.cell(row=row_idx, column=col + 1, value=_value_text(item["value"]))
            for c in (label_cell, value_cell):
                c.border = border
                c.font = Font(name="等线", size=10)
                c.alignment = Alignment(vertical="center", wrap_text=True)
            label_cell.fill = head_fill
            if item["status"] in ("BLOCK", "NEED_INPUT"):
                value_cell.fill = block_fill
            elif item["status"] in ("WARN", "EMPTY"):
                value_cell.fill = warn_fill
        row_idx += 1

    # 表体
    row_idx += 1
    item_rows = (payload.get("items") or [{}])[0].get("rows", [])
    columns = [
        ("项号", None, 6),
        ("商品编号", "column_hs_code", 16),
        ("商品名称", "column_cargo_spec", 24),
        ("规格型号", "element_model", 16),
        ("数量", "column_quantity", 10),
        ("单位", "column_unit", 8),
        ("单价", "column_unit_price", 10),
        ("总价", "column_total_price", 12),
        ("币制", "column_currency", 8),
    ]
    by_code = {r["code"]: r for r in item_rows}
    header_line = row_idx
    for j, (title, _, width) in enumerate(columns, start=1):
        cell = ws.cell(row=header_line, column=j, value=title)
        cell.font = Font(name="黑体", size=10, bold=True)
        cell.fill = head_fill
        cell.border = border
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[get_column_letter(j)].width = width
    ws.row_dimensions[header_line].height = 22

    body_line = header_line + 1
    ws.cell(row=body_line, column=1, value=1)
    for j, (_, code, _) in enumerate(columns[1:], start=2):
        if code is None:
            continue
        item = by_code.get(code, {})
        ws.cell(row=body_line, column=j, value=_value_text(item.get("value")))
    for j in range(1, len(columns) + 1):
        cell = ws.cell(row=body_line, column=j)
        cell.border = border
        cell.font = Font(name="等线", size=10)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[body_line].height = 30

    # 合计与提示
    totals = payload.get("totals") or {}
    row_idx = body_line + 2
    total_text = (
        f"合计：数量 {_value_text(totals.get('quantity'))} {totals.get('unit') or ''}　"
        f"总价 {_value_text(totals.get('total_price'))} {totals.get('currency') or ''}　"
        f"毛重 {_value_text(totals.get('gross_weight'))}　净重 {_value_text(totals.get('net_weight'))}　"
        f"件数 {_value_text(totals.get('package_count'))}"
    )
    ws.merge_cells(start_row=row_idx, start_column=1, end_row=row_idx, end_column=6)
    ws.cell(row=row_idx, column=1, value=total_text).font = Font(name="等线", size=10, bold=True)

    counts = payload.get("counts") or {}
    gap_count = len(payload.get("gaps") or [])
    row_idx += 1
    ws.merge_cells(start_row=row_idx, start_column=1, end_row=row_idx, end_column=6)
    if payload.get("blocked"):
        tip = f"⚠ 阻断项 {counts.get('BLOCK', 0)} 个，补齐前不可定稿；缺口明细见「缺口与依据」表。"
    elif gap_count:
        tip = f"提示：待补填 {counts.get('NEED_INPUT', 0)} 项、警告 {counts.get('WARN', 0)} 项，明细见「缺口与依据」表。"
    else:
        tip = "字段完整，可提交。"
    ws.cell(row=row_idx, column=1, value=tip).font = Font(name="等线", size=9, color="8A6D3B")

    # ---------------- Sheet 2：字段溯源 ----------------
    ws2 = wb.create_sheet("字段溯源")
    headers = ["区块", "栏目", "字段值", "状态", "来源", "缺失处理", "说明"]
    for j, title in enumerate(headers, start=1):
        cell = ws2.cell(row=1, column=j, value=title)
        cell.font = Font(name="黑体", size=10, bold=True)
        cell.fill = head_fill
        cell.border = border
    widths = [14, 22, 24, 12, 14, 12, 46]
    for j, w in enumerate(widths, start=1):
        ws2.column_dimensions[get_column_letter(j)].width = w

    line = 2
    for item in header_rows + item_rows:
        values = [
            item.get("section") or "",
            item["label"],
            _value_text(item.get("value")),
            item["status_label"],
            item.get("source") or "",
            item.get("missing_action") or "",
            item.get("note") or "",
        ]
        for j, v in enumerate(values, start=1):
            cell = ws2.cell(row=line, column=j, value=v)
            cell.border = border
            cell.font = Font(name="等线", size=9)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if item["status"] in ("BLOCK", "NEED_INPUT"):
                cell.fill = block_fill
            elif item["status"] in ("WARN", "EMPTY"):
                cell.fill = warn_fill
        line += 1

    # ---------------- Sheet 3：缺口与依据 ----------------
    ws3 = wb.create_sheet("缺口与依据")
    headers3 = ["栏目", "字段键", "状态", "处理动作", "说明"]
    for j, title in enumerate(headers3, start=1):
        cell = ws3.cell(row=1, column=j, value=title)
        cell.font = Font(name="黑体", size=10, bold=True)
        cell.fill = head_fill
        cell.border = border
    for j, w in enumerate([24, 18, 12, 12, 60], start=1):
        ws3.column_dimensions[get_column_letter(j)].width = w

    line = 2
    for gap in payload.get("gaps") or []:
        values = [gap["label"], gap["key"], gap["status_label"], gap["action"] or "", gap.get("note") or ""]
        for j, v in enumerate(values, start=1):
            cell = ws3.cell(row=line, column=j, value=v)
            cell.border = border
            cell.font = Font(name="等线", size=9)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        line += 1
    if not payload.get("gaps"):
        ws3.cell(row=2, column=1, value="无缺口").font = Font(name="等线", size=9)

    # ---------------- Sheet 4：风险与分流（有报关编排元信息时才生成） ----------------
    tier_action = meta.get("tier_action") or {}
    risk_flags = meta.get("risk_flags") or []
    decl_compliance = meta.get("decl_compliance") or {}
    compliance_issues = decl_compliance.get("issues") or []
    slots = meta.get("agent_slots") or {}
    if meta.get("tier") or risk_flags or compliance_issues or tier_action:
        ws4 = wb.create_sheet("风险与分流")
        headers4 = ["类别", "等级/状态", "内容"]
        for j, title in enumerate(headers4, start=1):
            cell = ws4.cell(row=1, column=j, value=title)
            cell.font = Font(name="黑体", size=10, bold=True)
            cell.fill = head_fill
            cell.border = border
        for j, w in enumerate([18, 14, 80], start=1):
            ws4.column_dimensions[get_column_letter(j)].width = w

        line = 2
        ws4.cell(row=line, column=1, value="分流等级")
        ws4.cell(row=line, column=2, value=meta.get("tier") or "—")
        ws4.cell(row=line, column=3, value=tier_action.get("label") or "")
        line += 1
        for act in tier_action.get("actions") or []:
            ws4.cell(row=line, column=1, value="处置动作")
            ws4.cell(row=line, column=2, value=tier_action.get("tier") or "")
            ws4.cell(row=line, column=3, value=act)
            line += 1
        for rf in risk_flags:
            ws4.cell(row=line, column=1, value=f"风险标记/{rf.get('type', '')}")
            ws4.cell(row=line, column=2, value=rf.get("severity") or "")
            ws4.cell(row=line, column=3, value=rf.get("detail") or "")
            line += 1
        for issue in compliance_issues:
            ws4.cell(row=line, column=1, value=f"合规问题/{issue.get('field', '')}")
            ws4.cell(row=line, column=2, value=issue.get("severity") or "")
            ws4.cell(row=line, column=3, value=issue.get("message") or "")
            line += 1
        for key, val in slots.items():
            ws4.cell(row=line, column=1, value=f"槽位/{key}")
            ws4.cell(row=line, column=2, value=val)
            ws4.cell(row=line, column=3, value="")
            line += 1
        if line == 2:
            ws4.cell(row=2, column=1, value="无风险与分流信息")
        for row in ws4.iter_rows(min_row=2, max_row=max(2, line - 1), max_col=3):
            for cell in row:
                cell.border = border
                cell.font = Font(name="等线", size=9)
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                if str(cell.value or "").lower() in ("critical", "error"):
                    cell.fill = block_fill
                elif str(cell.value or "").lower() in ("warning", "absent"):
                    cell.fill = warn_fill

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()

"""单证视图：按海关总署 2016 年第 26 号公告附件 2 的版式组装 42 项表头 + 16 项表体。

版式（位置、跨列、条目序号）只从 data/form_layout.json 读取；
取值优先用申报 dict，其次复用 field_dict 已解析出的 header 行，
最后才落到「待补」——保证单证视图与校验视图口径一致。

每个单元格额外携带 provenance 溯源信息，供前端点击值后弹出「这个数怎么来的」卡片。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

LAYOUT_PATH = Path(__file__).resolve().parent / "data" / "form_layout.json"

# 关区代码（JGS/T 18）
CUSTOMS_CODES = {
    "宁波": "3101", "上海": "2201", "深圳": "5303", "广州": "5101",
    "义乌": "2921", "青岛": "4201", "杭州": "2901", "南京": "2301",
    "厦门": "3701", "北京": "1101", "成都": "7901", "天津": "0201",
}
# 国家（地区）代码（JGS-20）
COUNTRY_CODES = {
    "美国": "USA", "德国": "DEU", "日本": "JPN", "英国": "GBR", "法国": "FRA",
    "新加坡": "SGP", "加拿大": "CAN", "澳大利亚": "AUS", "荷兰": "NLD",
    "韩国": "KOR", "意大利": "ITA", "西班牙": "ESP",
}
# 主要目的港标识代码
PORT_CODES = {
    "洛杉矶": "USLAX", "长滩": "USLGB", "纽约": "USNYC", "汉堡": "DEHAM",
    "鹿特丹": "NLRTM", "新加坡": "SGSIN", "东京": "JPTYO", "大阪": "JPOSA",
    "悉尼": "AUSYD", "温哥华": "CAVAN",
}
CURRENCY_CODES = {"USD": "502", "EUR": "300", "JPY": "116", "GBP": "303", "CNY": "142", "HKD": "110"}
UNIT_CODES = {"PCS": "007", "件": "007", "个": "007", "千克": "035", "KG": "035", "公斤": "035", "套": "014", "台": "021"}
UNIT_NAMES = {"PCS": "个", "件": "个", "个": "个", "千克": "千克", "KG": "千克", "公斤": "千克", "套": "套", "台": "台"}
PACKAGE_CODES = {"纸箱": "2", "木箱": "1", "托盘": "9", "编织袋": "6", "桶装": "8"}
TRANSPORT_CODES = {"air": "4 航空运输", "航空": "4 航空运输", "sea": "2 水路运输",
                   "海运": "2 水路运输", "express": "5 邮件运输", "邮件": "5 邮件运输"}

# 规则名 → {解释, 拟人化说明}
RULE_META: Dict[str, Dict[str, str]] = {
    "customs_code": {
        "explain": "按 JGS/T 18 关区代码表，从口岸名称关键字匹配",
        "persona": "我读了起运口岸「{from_port}」，从 JGS/T 18 关区代码表里匹配出关区代码「{value}」。",
    },
    "export_port": {
        "explain": "关区代码 + 口岸中文名拼接",
        "persona": "我把匹配到的关区代码「{code}」和口岸名「{port}」拼在一起，得到「{value}」。",
    },
    "exit_date": {
        "explain": "优先取清关日期，其次发票日期；格式 YYYYMMDD",
        "persona": "我从发票/清关日读到了日期，转成 YYYYMMDD 格式「{value}」。",
    },
    "declaration_datetime": {
        "explain": "取系统当前时点，格式 YYYYMMDDhhmmss",
        "persona": "这是我现在跑制单的时间戳，直接填「{value}」。",
    },
    "pre_entry_no": {
        "explain": "B + 8 位日期 + 9 位流水号",
        "persona": "我按预录入编号规则（B + 日期 + 流水）生成了这个号。是演示值，接系统后会换成真实预录入号。",
    },
    "list_no": {
        "explain": "4 位关区 + 4 位年 + 进出口标记 E + 9 位流水",
        "persona": "我按附件 2 的清单编号规则，用关区代码「{code}」+ 年份 + E + 流水号拼出来的。",
    },
    "dest_country_code": {
        "explain": "按 JGS-20 国家（地区）代码表匹配；已是三位大写 ISO 码则原样保留",
        "persona": "我读了目的国「{raw}」，查 JGS-20 得到国家代码「{value}」。",
    },
    "dest_port_code": {
        "explain": "境外最终目的港标识代码；已是 5 位大写代码则原样保留",
        "persona": "我从目的港「{raw}」匹配出标识代码「{value}」。如果已经是 5 位代码我就直接用了。",
    },
    "transport_mode": {
        "explain": "按 JGS-20 运输方式代码表匹配",
        "persona": "我查了运输方式是「{raw}」，按代码表转成「{value}」。",
    },
    "voyage_no": {
        "explain": "读取航班/航次号",
        "persona": "我从单据里取了航班/航次号「{value}」。",
    },
    "licence_no": {
        "explain": "无监管证件填「无」",
        "persona": "本票没有许可证，按规范填「无」。",
    },
    "package_code": {
        "explain": "包装种类代码 + 名称；按 JGS-20 匹配",
        "persona": "我读到包装是「{raw}」，查代码表得「{code}」，拼成「{value}」。",
    },
    "remark": {
        "explain": "拼接贸易方式、HS、风险标记",
        "persona": "我把贸易方式、HS 编码和风险标记拼了一下，作为备注。",
    },
    "spec_model": {
        "explain": "材质 | 品牌 | 型号 三段拼接",
        "persona": "我把材质「{material}」、品牌「{brand}」、型号「{model}」用「|」拼起来填规格型号。",
    },
    "barcode": {
        "explain": "读取商品条形码，无则填「无」",
        "persona": "没读到条形码，按规范填「无」。",
    },
    "currency_code": {
        "explain": "按 JGS-20 币制代码表匹配",
        "persona": "我读到币制是「{raw}」，转成代码「{value}」。",
    },
    "unit_code": {
        "explain": "按 JGS-20 计量单位代码表匹配",
        "persona": "我读到计量单位是「{raw}」，转成「{value}」。",
    },
    "legal_unit_code": {
        "explain": "零售出口默认法定计量单位 = 千克",
        "persona": "零售出口的法定计量单位默认是千克（035）。",
    },
    "legal_quantity": {
        "explain": "法定数量 = 净重（千克）",
        "persona": "法定数量就是净重「{value}」千克。",
    },
    "item_no": {
        "explain": "本票单商品，项号 = 1",
        "persona": "只有 1 项商品，项号填 1。",
    },
    "freight_currency": {
        "explain": "默认美元（502）",
        "persona": "运费默认按美元 502 填。",
    },
    "insurance_currency": {
        "explain": "默认美元（502）",
        "persona": "保费默认按美元 502 填。",
    },
}

STATUS_MAP = {
    "FILLED": "ok",
    "DERIVED": "derive",
    "DEFAULTED": "default",
    "NEED_INPUT": "need",
    "BLOCK": "need",
    "WARN": "need",
    "EMPTY": "empty",
}

SOURCE_LABEL = {
    "decl": "发票/单据直接读取",
    "rule": "规则派生",
    "sim": "演示模拟值",
    "const": "系统常量",
    "na": "不适用",
    "need": "待补",
}


def load_layout() -> Dict:
    with open(LAYOUT_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


LAYOUT = load_layout()


def _blank(v: Any) -> bool:
    return v is None or v == "" or (isinstance(v, str) and not v.strip())


def _num(v: Any, kind: Optional[str]) -> Any:
    if _blank(v):
        return v
    try:
        if kind == "int":
            return f"{float(v):,.0f}"
        if kind == "decimal":
            return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return v
    return v


def _ymd(v: Any) -> Optional[str]:
    if _blank(v):
        return None
    s = str(v).replace("-", "").replace("/", "").strip()
    return s[:8] if len(s) >= 8 else None


def _match(mapping: Dict[str, str], text: Any, default: Optional[str] = None) -> Optional[str]:
    if _blank(text):
        return default
    s = str(text)
    for key, val in mapping.items():
        if key in s:
            return val
    return default


class _Ctx:
    def __init__(self, decl: Dict, rows: Dict[str, Dict], meta: Dict, totals: Dict):
        self.decl = decl or {}
        self.rows = rows
        self.meta = meta or {}
        self.totals = totals or {}
        self.now = datetime.now()

    def from_decl(self, cell: Dict) -> Tuple[Any, Optional[str]]:
        """返回 (值, 来源定位字符串)"""
        key = cell.get("key")
        field = cell.get("field")
        value = self.decl.get(key)
        source_loc = f"单据字段「{key}」"
        if _blank(value) and field:
            row = self.rows.get(field)
            if row and not _blank(row.get("value")):
                value = row["value"]
                source_loc = f"识别结果 [{row.get('label', field)}]"
        if not _blank(value) and key == "unit_price":
            source_loc = "发票商品行 · 单价栏"
        elif not _blank(value) and key == "quantity":
            source_loc = "发票商品行 · 数量栏"
        elif not _blank(value) and key == "total_price":
            source_loc = "发票合计 · 金额栏"
        elif not _blank(value) and key == "gross_weight":
            source_loc = "装箱单 · 毛重栏"
        elif not _blank(value) and key == "net_weight":
            source_loc = "装箱单 · 净重栏"
        elif not _blank(value) and key == "order_id":
            source_loc = "电商平台订单号"
        elif not _blank(value) and key == "logistics_no":
            source_loc = "物流运单号"
        elif not _blank(value) and key == "shipper":
            source_loc = "发票抬头 · 卖方"
        elif not _blank(value) and key == "cargo_spec":
            source_loc = "发票商品描述"
        return value, source_loc

    def status_for(self, cell: Dict, value: Any) -> str:
        row = self.rows.get(cell.get("field") or "")
        if row:
            status = STATUS_MAP.get(row.get("status"), "ok")
            if not _blank(value):
                return "ok" if status == "need" else status
            return status
        return "ok" if not _blank(value) else ("need" if cell.get("required") else "empty")

    def rule(self, name: str) -> Any:
        """公开的规则调用，只返回值（保持旧接口兼容）"""
        val, _ = self._resolve_rule(name)
        return val

    def _resolve_rule(self, name: str) -> Tuple[Any, Dict[str, Any]]:
        """执行派生规则，返回 (值, 溯源信息)"""
        d = self.decl
        meta = {"rule": name}

        if name == "customs_code":
            raw = d.get("from_port")
            code = _match(CUSTOMS_CODES, raw, "3101")
            meta.update({"raw": raw, "match_from": "JGS/T 18 关区代码表"})
            return code, meta

        if name == "export_port":
            raw = d.get("from_port")
            code = _match(CUSTOMS_CODES, raw, "3101")
            port = str(raw or "").replace("港", "").split()[0] or "宁波"
            meta.update({"raw": raw, "code": code, "port": port})
            return f"{code} {port}", meta

        if name == "exit_date":
            raw = d.get("clear_date") or d.get("invoice_date")
            val = _ymd(raw) or _ymd(d.get("invoice_date")) or self.now.strftime("%Y%m%d")
            meta.update({"raw": raw, "source_field": "clear_date / invoice_date"})
            return val, meta

        if name == "declaration_datetime":
            return self.now.strftime("%Y%m%d%H%M%S"), meta

        if name == "pre_entry_no":
            return "B" + self.now.strftime("%Y%m%d") + "000123456", meta

        if name == "list_no":
            code = _match(CUSTOMS_CODES, d.get("from_port"), "3101")
            meta.update({"code": code})
            return f"{code}{self.now.strftime('%Y')}E000123456", meta

        if name == "dest_country_code":
            raw = d.get("dest_country") or ""
            if raw and len(str(raw)) == 3 and str(raw).isupper():
                meta.update({"raw": raw, "note": "已是三位大写 ISO 码，原样保留"})
                return raw, meta
            val = _match(COUNTRY_CODES, raw)
            meta.update({"raw": raw, "match_from": "JGS-20 国家（地区）代码表"})
            return val, meta

        if name == "dest_port_code":
            raw = d.get("dest_port") or ""
            if raw and len(str(raw)) == 5 and str(raw).isupper():
                return raw, meta
            meta.update({"raw": raw, "match_from": "主要目的港标识代码映射表"})
            return _match(PORT_CODES, raw) or raw or None, meta

        if name == "transport_mode":
            raw = str(self.meta.get("transport_mode") or d.get("transport_mode") or "").lower()
            meta.update({"raw": raw, "match_from": "JGS-20 运输方式代码表"})
            return TRANSPORT_CODES.get(raw), meta

        if name == "voyage_no":
            raw = d.get("flight_no") or d.get("voyage_no")
            meta.update({"raw": raw, "source_field": "flight_no / voyage_no"})
            return raw, meta

        if name == "licence_no":
            raw = d.get("supervision_code") or "无"
            meta.update({"raw": raw})
            return "无" if str(raw).strip() in ("无", "None", "") else raw, meta

        if name == "package_code":
            raw = d.get("package_type")
            code = _match(PACKAGE_CODES, raw, "2")
            meta.update({"raw": raw, "code": code, "match_from": "JGS-20 包装种类代码表"})
            return f"{code} {raw or '纸箱'}", meta

        if name == "remark":
            hs = d.get("hs_code") or "未归类"
            flags = (self.meta.get("risk_flags") or [])
            tail = f"风险标记 {len(flags)} 项" if flags else "无风险标记"
            meta.update({"hs": hs, "risk_count": len(flags)})
            return f"{d.get('trade_mode') or '9610'} 零售出口；1 项商品归入 {hs}；{tail}", meta

        if name == "spec_model":
            material = d.get("material") or "材质未申报"
            brand = d.get("brand") or "无品牌"
            model = d.get("model") or "型号未列名"
            meta.update({"material": material, "brand": brand, "model": model})
            return "|".join(str(p) for p in (material, brand, model)), meta

        if name == "barcode":
            raw = d.get("barcode")
            meta.update({"raw": raw})
            return raw or "无", meta

        if name == "currency_code":
            raw = str(d.get("currency") or "USD").upper()
            meta.update({"raw": raw, "match_from": "JGS-20 币制代码表"})
            return CURRENCY_CODES.get(raw, "502"), meta

        if name == "unit_code":
            raw = str(d.get("unit") or "个")
            code = UNIT_CODES.get(raw.upper()) or UNIT_CODES.get(raw) or "007"
            meta.update({"raw": raw, "code": code, "match_from": "JGS-20 计量单位代码表"})
            return f"{code} {UNIT_NAMES.get(raw.upper()) or UNIT_NAMES.get(raw) or raw}", meta

        if name == "legal_unit_code":
            return "035 千克", meta

        if name == "legal_quantity":
            raw = d.get("net_weight")
            meta.update({"raw": raw, "source_field": "net_weight (净重)"})
            return raw, meta

        if name == "item_no":
            return 1, meta

        if name == "freight_currency":
            return "502 美元", meta

        if name == "insurance_currency":
            return "502 美元", meta

        return None, meta

    def _persona(self, rule_name: str, ctx: Dict, final_value: Any) -> str:
        template = RULE_META.get(rule_name, {}).get("persona")
        if not template:
            return f"按规则「{rule_name}」派生得到「{final_value}」。"
        try:
            return template.format(**ctx, value=final_value)
        except KeyError:
            return template

    def resolve(self, cell: Dict) -> Dict:
        src = cell.get("from")
        provenance: Dict[str, Any] = {
            "source_type": src,
            "source_label": SOURCE_LABEL.get(src, "未知"),
            "source_loc": "",
            "rule_name": None,
            "rule_explain": "",
            "persona": "",
            "raw_value": None,
        }
        rule_ctx: Dict[str, Any] = {}

        if src == "sim":
            value, status = cell.get("value"), "sim"
            provenance.update({
                "source_loc": "系统演示值",
                "rule_explain": "正式申报需替换为企业真实海关备案编码",
                "persona": "这是演示模拟值，接系统后会换成企业真实备案编码。",
                "raw_value": cell.get("value"),
            })
        elif src == "const":
            value, status = cell.get("value"), "ok"
            provenance.update({
                "source_loc": "系统常量",
                "rule_explain": cell.get("note", ""),
                "persona": cell.get("note", "") or "固定值。",
                "raw_value": cell.get("value"),
            })
        elif src == "na":
            value, status = cell.get("value") or "—", "empty"
            provenance.update({
                "source_loc": "不适用",
                "rule_explain": cell.get("note", ""),
                "persona": cell.get("note", "") or "本票不适用此项，填 —。",
            })
        elif src == "need":
            value, status = cell.get("value") or "待补", "need"
            provenance.update({
                "source_loc": "待补",
                "rule_explain": cell.get("note", "必填项，需录入"),
                "persona": cell.get("note", "此项必填但当前没数据，需要补录。"),
            })
        elif src == "decl":
            value, source_loc = self.from_decl(cell)
            raw = value
            status = self.status_for(cell, value)
            provenance.update({
                "source_loc": source_loc,
                "rule_explain": "OCR / 结构化抽取得到，直接填入",
                "persona": f"我从「{source_loc}」读到「{raw}」，直接填进来了。",
                "raw_value": raw,
            })
        else:  # rule
            rule_name = cell.get("rule") or ""
            value, rule_ctx = self._resolve_rule(rule_name)
            status = cell.get("status") or "derive"
            if _blank(value):
                status = "need" if cell.get("required") else "empty"
                value = "待补" if status == "need" else "—"

            rule_meta = RULE_META.get(rule_name, {})
            provenance.update({
                "source_loc": f"规则派生（{rule_name}）",
                "rule_name": rule_name,
                "rule_explain": rule_meta.get("explain", "规则派生"),
                "persona": self._persona(rule_name, rule_ctx, value),
                "raw_value": rule_ctx.get("raw"),
            })

        value = _num(value, cell.get("kind"))
        if not _blank(value) and not isinstance(value, (int, float)):
            value = str(value)
        if _blank(value):
            status = "need" if cell.get("required") else "empty"
            value = "待补" if status == "need" else "—"

        provenance["final_value"] = str(value)
        return {
            "no": cell.get("no"),
            "label": cell.get("label"),
            "value": value,
            "status": status,
            "span": cell.get("span") or 1,
            "align": cell.get("align") or "center",
            "note": cell.get("note") or "",
            "provenance": provenance,
        }


def build_form(decl: Dict, header_rows: List[Dict], meta: Optional[Dict], totals: Optional[Dict]) -> Dict[str, Any]:
    rows = {r.get("code"): r for r in (header_rows or [])}
    ctx = _Ctx(decl or {}, rows, meta or {}, totals or {})

    header = [[ctx.resolve(c) for c in row] for row in LAYOUT.get("header_rows", [])]
    strip = [ctx.resolve(c) for c in LAYOUT.get("strip", [])]

    columns = []
    for col in LAYOUT.get("body_columns", []):
        resolved = ctx.resolve(col)
        resolved["width"] = col.get("width")
        columns.append(resolved)

    items = [{
        "no": 1,
        "cells": [dict(ctx.resolve(col), width=col.get("width"), align=col.get("align") or "center")
                  for col in LAYOUT.get("body_columns", [])],
    }]

    qty = totals.get("quantity")
    total = totals.get("total_price")
    unit = decl.get("unit") or "个"
    totals_text = (
        "1 项 · 申报数量合计 "
        + (f"{float(qty):,.0f}" if qty not in (None, "") else "—")
        + f" {unit} · 总价 "
        + (f"{float(total):,.2f}" if total not in (None, "") else "—")
        + f" {decl.get('currency') or 'USD'}"
    )

    pending = sum(1 for c in strip if c["status"] == "need") + sum(
        1 for row in header for c in row if c["status"] == "need"
    )
    simulated = sum(1 for c in strip if c["status"] == "sim") + sum(
        1 for row in header for c in row if c["status"] == "sim"
    )

    return {
        "schema_version": LAYOUT.get("schema_version"),
        "doc_title": LAYOUT.get("doc_title"),
        "basis": LAYOUT.get("basis"),
        "code_sets": LAYOUT.get("code_sets"),
        "list_no": ctx.rule("list_no"),
        "header": header,
        "strip": strip,
        "columns": columns,
        "items": items,
        "totals_text": totals_text,
        "entry_total": "42 / 42",
        "pending_count": pending,
        "sim_count": simulated,
        "status_text": f"{pending + simulated} 项待核验" if (pending + simulated) else "字段齐备",
        "footer": {
            "operator": (meta or {}).get("operator") or "—",
            "unit": "智关9610",
            "declarant": (meta or {}).get("declarant") or "—",
            "address": (meta or {}).get("address") or "—",
            "phone": (meta or {}).get("phone") or "—",
            "zip": (meta or {}).get("zip") or "—",
            "fill_date": ctx.now.strftime("%Y%m%d"),
        },
    }

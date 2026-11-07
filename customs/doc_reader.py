"""单据识别模块：把"甩进来的单据文件"识别成结构化要素，直接喂给全链路。

设计目标（对应产品诉求"甩进去文件就可以分析开跑"）：
- 支持多种单据载体：商业发票图片(PNG/JPG)、Excel/CSV 电子发票、JSON 电子单据、纯文本
- 图片走轻量 OCR（rapidocr-onnxruntime）提字 → DeepSeek 结构化抽取 → 正则兜底
- 结构化文件直接解析，无需 OCR
- 统一输出 DocFields（单据要素），并可直接 to_query() 生成全链路 query

AI 边界：
- AI 用于"读单据"（OCR + 语义结构化），这是规则判不了的模糊识别
- 关键要素（目的国、运价、货值）最终仍由下游规则链校验，AI 不直接决定是否放行
"""
from __future__ import annotations

import csv
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# 加载 .env（DeepSeek API Key），保证脱离 server 单独运行时也能启用 LLM 抽取
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

# 节点清单（用于把单据里的起运地/目的港对齐到系统内可路由的节点）
_NODES_PATH = Path(__file__).resolve().parent.parent / "data" / "nodes.json"

_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}
_SHEET_EXT = {".xlsx", ".xls", ".csv"}
_JSON_EXT = {".json"}

# 境外目的港/城市 → 规范名（用于查询语句与下游匹配）
_DEST_ALIASES = {
    "los angeles": "洛杉矶", "la": "洛杉矶", "洛杉矶": "洛杉矶", "laX": "洛杉矶",
    "hamburg": "汉堡", "汉堡": "汉堡",
    "rotterdam": "鹿特丹", "鹿特丹": "鹿特丹",
    "singapore": "新加坡", "新加坡": "新加坡",
    "dubai": "迪拜", "迪拜": "迪拜",
    "busan": "釜山", "釜山": "釜山",
    "new york": "纽约", "纽约": "纽约",
}

# 目的国 → 用于风险预判（制裁国检测）
_COUNTRY_ALIASES = {
    "usa": "美国", "u.s.a": "美国", "united states": "美国", "美国": "美国", "us": "美国",
    "germany": "德国", "德国": "德国", "deutschland": "德国",
    "uk": "英国", "united kingdom": "英国", "英国": "英国",
    "north korea": "朝鲜", "朝鲜": "朝鲜",
    "iran": "伊朗", "伊朗": "伊朗",
    "syria": "叙利亚", "叙利亚": "叙利亚",
    "cuba": "古巴", "古巴": "古巴",
    "venezuela": "委内瑞拉", "委内瑞拉": "委内瑞拉",
}

# 优先级线索词
_COST_HINTS = ["省钱", "预算", "便宜", "最低成本", "成本优", "cost", "经济", "价格优先"]
_TIME_HINTS = ["最快", "时效", "加急", "尽快", "紧急", "fastest", "限时"]


@dataclass
class DocFields:
    """从单据识别出的结构化要素。"""
    doc_type: str = "商业发票"
    seller: str = ""              # 出口商 / 发货人
    buyer: str = ""               # 境外买方 / 收货人
    invoice_no: str = ""
    invoice_date: str = ""
    contract_no: str = ""
    platform: str = ""            # 跨境电商平台
    order_id: str = ""            # 订单编号
    logistics_no: str = ""        # 物流运单号
    clear_date: str = ""          # 结关日（用于退税/免税期限倒计时）
    origin: str = ""              # 国内起运地（城市/口岸）
    dest: str = ""                # 境外目的港/城市
    dest_country: str = ""        # 目的国
    trade_terms: str = ""         # 贸易条款 FOB/CIF/EXW
    cargo_name: str = ""          # 主货名
    tonnage: float = 0.0          # 总毛重（吨）
    gross_weight: float = 0.0     # 毛重（千克）
    net_weight: float = 0.0       # 净重（千克）
    package_count: int = 0        # 件数/箱数
    quantity: float = 0.0         # 总数量（件/箱）
    unit: str = ""
    unit_price_usd: float = 0.0   # 代表单价
    total_amount_usd: float = 0.0
    currency: str = "USD"
    priority: str = "balanced"    # cost / time / balanced
    month: int = 0
    special_notes: str = ""       # 品名里的特殊属性（带电/品牌等）
    items: List[Dict[str, Any]] = field(default_factory=list)
    raw_text: str = ""
    source: str = "unknown"       # image_ocr / excel / csv / json / text
    confidence: float = 0.0
    evidence: List[str] = field(default_factory=list)   # 识别依据（可解释性）

    def to_dict(self) -> Dict[str, Any]:
        return {
            "doc_type": self.doc_type,
            "seller": self.seller,
            "buyer": self.buyer,
            "invoice_no": self.invoice_no,
            "invoice_date": self.invoice_date,
            "contract_no": self.contract_no,
            "platform": self.platform,
            "order_id": self.order_id,
            "logistics_no": self.logistics_no,
            "clear_date": self.clear_date,
            "origin": self.origin,
            "dest": self.dest,
            "dest_country": self.dest_country,
            "trade_terms": self.trade_terms,
            "cargo_name": self.cargo_name,
            "tonnage": self.tonnage,
            "gross_weight": self.gross_weight,
            "net_weight": self.net_weight,
            "package_count": self.package_count,
            "quantity": self.quantity,
            "unit": self.unit,
            "unit_price_usd": self.unit_price_usd,
            "total_amount_usd": self.total_amount_usd,
            "currency": self.currency,
            "priority": self.priority,
            "month": self.month,
            "special_notes": self.special_notes,
            "items": self.items,
            "source": self.source,
            "confidence": self.confidence,
            "evidence": self.evidence,
        }

    def to_query(self) -> str:
        """把单据要素转成全链路 query（适配 transport/booking 的规则解析）。"""
        origin = self.origin or self.dest or ""
        cargo = self.cargo_name or "货物"
        weight = self.tonnage if self.tonnage else 20
        priority_map = {"cost": "省钱", "time": "最快", "balanced": "折中"}
        prio = priority_map.get(self.priority, "折中")
        q = f"{origin}发{weight}吨{cargo}到{self.dest}，{prio}优先，全流程"
        if self.month:
            q = f"{origin}发{weight}吨{cargo}到{self.dest}，{self.month}月，{prio}优先，全流程"
        return q

    def to_context(self) -> Dict[str, Any]:
        """转成注入给 Coordinator 的初始 context（让下游 Agent 直接拿到要素）。"""
        return {
            "from_doc": True,
            "doc_type": self.doc_type,
            "cargo_name": self.cargo_name,
            "tonnage": self.tonnage,
            # 收发货人：单据上的 seller/buyer 直接对应制单的境内发货人/境外收货人
            "shipper": self.seller or None,
            "consignee": self.buyer or None,
            "quantity": self.quantity or None,
            "unit": self.unit or None,
            "unit_price": self.unit_price_usd or None,
            "total_amount": self.total_amount_usd or None,
            "currency": self.currency or "USD",
            "unit_price_usd": self.unit_price_usd,
            "dest_country": self.dest_country,
            "dest_port": self.dest or self.dest_country,
            "origin": self.origin,
            "trade_terms": self.trade_terms,
            "priority": self.priority,
            # 制单/合规必填项：单据里带就透传，缺失时由下游标记待补
            "platform": self.platform or None,
            "order_id": self.order_id or None,
            "logistics_no": self.logistics_no or None,
            "gross_weight": self.gross_weight or None,
            "net_weight": self.net_weight or None,
            "package_count": self.package_count or None,
            "invoice_date": self.invoice_date or None,
            "clear_date": self.clear_date or None,
            "sku_info": {
                "cargo_name": self.cargo_name,
                "tonnage": self.tonnage,
                "unit_price_usd": self.unit_price_usd,
                "remark": self.special_notes,
            },
            "order": {
                "dest_country": self.dest_country,
                "dest_port": self.dest,
                "unit_price_usd": self.unit_price_usd,
                "invoice_no": self.invoice_no,
                "invoice_date": self.invoice_date or None,
                "clear_date": self.clear_date or None,
                "trade_terms": self.trade_terms,
            },
            "doc_fields": self.to_dict(),
        }


class DocumentReader:
    """单据识别器：图片 OCR / 表格 / JSON / 文本 → DocFields。"""

    def __init__(self, use_llm: bool = True, verbose: bool = False):
        self.use_llm = use_llm and bool(os.getenv("DEEPSEEK_API_KEY"))
        self.verbose = verbose
        self._ocr = None
        self._nodes: List[str] = self._load_nodes()

    # ----------------------------------------------------------------- #
    # 主入口
    # ----------------------------------------------------------------- #
    def read(self, path: str) -> DocFields:
        ext = Path(path).suffix.lower()
        if ext in _IMAGE_EXT:
            text = self._ocr_image(path)
            fields = self._extract(text, source="image_ocr")
            fields.raw_text = text
            return fields
        if ext in _SHEET_EXT:
            return self._read_sheet(path, ext)
        if ext in _JSON_EXT:
            return self._read_json(path)
        # 兜底：当纯文本
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
        return self._extract(text, source="text")

    # ----------------------------------------------------------------- #
    # 图片 OCR（rapidocr-onnxruntime）
    # ----------------------------------------------------------------- #
    def _get_ocr(self):
        if self._ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            self._ocr = RapidOCR()
        return self._ocr

    def _ocr_image(self, path: str) -> str:
        try:
            engine = self._get_ocr()
            result, _ = engine(path)
            if not result:
                return ""
            return "\n".join(item[1] for item in result)
        except Exception as e:  # noqa: BLE001
            if self.verbose:
                print(f"[DocReader] OCR 失败: {e}")
            return ""

    # ----------------------------------------------------------------- #
    # 结构化文件：Excel / CSV
    # ----------------------------------------------------------------- #
    def _read_sheet(self, path: str, ext: str) -> DocFields:
        rows: List[List[str]] = []
        if ext == ".csv":
            with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
                rows = [[c.strip() for c in r] for r in csv.reader(f)]
        else:
            import openpyxl
            wb = openpyxl.load_workbook(path, data_only=True)
            ws = wb.active
            for row in ws.iter_rows(values_only=True):
                rows.append(["" if v is None else str(v).strip() for v in row])

        # 展开成 key:value 文本，复用同一套语义抽取
        lines = []
        header_keys = None
        data_started = False
        for r in rows:
            cells = [c for c in r if c != ""]
            if not cells:
                continue
            # 前部 key/value 块：A 列是标签
            if not data_started and len(cells) <= 3 and r and r[0]:
                lines.append(f"{r[0]} {(' '.join(cells[1:])) if len(cells) > 1 else ''}".strip())
            lines.append(" | ".join(cells))
        text = "\n".join(lines)
        fields = self._extract(text, source=f"{ext.strip('.')}_sheet")
        fields.raw_text = text
        return fields

    # ----------------------------------------------------------------- #
    # 结构化文件：JSON
    # ----------------------------------------------------------------- #
    def _read_json(self, path: str) -> DocFields:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        f = DocFields(source="json")
        f.doc_type = str(data.get("doc_type") or data.get("单据类型") or "电子单据")
        f.seller = str(data.get("seller") or data.get("出口商") or "")
        f.buyer = str(data.get("buyer") or data.get("买方") or "")
        f.invoice_no = str(data.get("invoice_no") or data.get("发票号") or "")
        f.invoice_date = str(data.get("invoice_date") or data.get("日期") or "")
        f.contract_no = str(data.get("contract_no") or data.get("合同号") or "")
        f.platform = str(data.get("platform") or data.get("跨境电商平台") or "")
        f.order_id = str(data.get("order_id") or data.get("订单编号") or data.get("订单号") or "")
        f.logistics_no = str(data.get("logistics_no") or data.get("物流运单号") or data.get("运单号") or "")
        f.clear_date = str(data.get("clear_date") or data.get("结关日") or "")
        f.origin = str(data.get("origin") or data.get("起运地") or "")
        f.dest = str(data.get("dest") or data.get("目的港") or "")
        f.dest_country = str(data.get("dest_country") or data.get("目的国") or "")
        f.trade_terms = str(data.get("trade_terms") or data.get("贸易条款") or "")
        f.cargo_name = str(data.get("cargo_name") or data.get("货名") or "")
        f.tonnage = _to_float(data.get("tonnage") or data.get("总毛重吨"))
        f.gross_weight = _to_float(data.get("gross_weight") or data.get("毛重"))
        f.net_weight = _to_float(data.get("net_weight") or data.get("净重"))
        f.package_count = int(_to_float(data.get("package_count") or data.get("件数") or data.get("箱数")))
        f.quantity = _to_float(data.get("quantity") or data.get("总数量"))
        f.unit = str(data.get("unit") or data.get("单位") or "")
        f.unit_price_usd = _to_float(data.get("unit_price_usd") or data.get("单价"))
        f.total_amount_usd = _to_float(data.get("total_amount_usd") or data.get("总金额"))
        f.currency = str(data.get("currency") or "USD")
        f.special_notes = str(data.get("special_notes") or data.get("备注") or "")
        f.items = list(data.get("items") or [])
        f.month = int(data.get("month") or 0)
        f.priority = self._infer_priority(str(data.get("priority") or ""))
        f.confidence = 0.98
        f.evidence.append("结构化单据直接解析（JSON）")
        self._normalize(f)
        return f

    # ----------------------------------------------------------------- #
    # 语义抽取（LLM 优先，正则兜底）
    # ----------------------------------------------------------------- #
    def _extract(self, raw_text: str, source: str) -> DocFields:
        fields = DocFields(source=source)
        if not raw_text.strip():
            fields.evidence.append("未提取到文本")
            return fields

        llm = self._llm_extract(raw_text) if self.use_llm else None
        regex = self._regex_extract(raw_text)

        if llm:
            fields.evidence.append("AI（DeepSeek）结构化抽取")
            merged = {**regex, **{k: v for k, v in llm.items() if v not in (None, "", 0)}}
            conf = 0.9
        else:
            merged = regex
            fields.evidence.append("正则规则抽取（无 LLM）")
            conf = 0.6

        for k, v in merged.items():
            if k == "items":
                fields.items = v
            elif hasattr(fields, k):
                setattr(fields, k, v)

        fields.month = int(merged.get("month") or fields.month or 0)
        fields.source = source
        self._normalize(fields)
        fields.confidence = conf if (fields.origin and fields.dest and fields.cargo_name) else conf * 0.6
        return fields

    # ---- LLM 结构化抽取 ---- #
    def _llm_extract(self, raw_text: str) -> Optional[Dict[str, Any]]:
        try:
            from openai import OpenAI
            client = OpenAI(
                api_key=os.getenv("DEEPSEEK_API_KEY"),
                base_url="https://api.deepseek.com",
            )
            prompt = (
                "你是跨境物流单据识别引擎。从下面的单据文本中抽取结构化要素，只输出 JSON：\n"
                "{\n"
                '  "doc_type": "单据类型(商业发票/装箱单/报关单/订单)",\n'
                '  "seller": "出口商/发货人",\n'
                '  "buyer": "境外买方/收货人",\n'
                '  "invoice_no": "发票号",\n'
                '  "invoice_date": "日期(YYYY-MM-DD)",\n'
                '  "contract_no": "合同号",\n'
                '  "platform": "跨境电商平台",\n'
                '  "order_id": "订单编号",\n'
                '  "logistics_no": "物流运单号",\n'
                '  "clear_date": "结关日(YYYY-MM-DD,没有则空)",\n'
                '  "origin": "国内起运地(城市或口岸,中文)",\n'
                '  "dest": "境外目的港/城市(中文,如洛杉矶/汉堡)",\n'
                '  "dest_country": "目的国(中文,如美国/德国)",\n'
                '  "trade_terms": "贸易条款(如FOB/CIF/EXW)",\n'
                '  "cargo_name": "主货名(若含品牌/带电属性请保留原文,如iPhone手机壳、锂电池充电宝)",\n'
                '  "tonnage": 总毛重吨数(数字,千克请除以1000),\n'
                '  "gross_weight": 毛重千克数(数字),\n'
                '  "net_weight": 净重千克数(数字),\n'
                '  "package_count": 件数/箱数(数字),\n'
                '  "quantity": 总数量(数字),\n'
                '  "unit": "单位(PCS/CTN/SET)",\n'
                '  "unit_price_usd": 代表单价(数字,美元),\n'
                '  "total_amount_usd": 总金额(数字,美元),\n'
                '  "currency": "币种",\n'
                '  "month": 发运月份(数字),\n'
                '  "special_notes": "特殊属性备注(品牌/带电/危险品等)",\n'
                '  "items": [{"desc":"","qty":0,"unit_price":0,"amount":0}]\n'
                "}\n"
                "只输出 JSON，不要解释。缺失字段给空字符串或 0。\n\n单据文本：\n" + raw_text
            )
            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=800,
            )
            content = resp.choices[0].message.content.strip()
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.MULTILINE)
            m = re.search(r"\{.*\}", content, re.DOTALL)
            if m:
                return json.loads(m.group(0))
        except Exception as e:  # noqa: BLE001
            if self.verbose:
                print(f"[DocReader] LLM 抽取失败: {e}")
        return None

    # ---- 正则兜底抽取 ---- #
    def _regex_extract(self, text: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        low = text.lower()

        # 单据类型
        if "装箱单" in text or "packing list" in low:
            out["doc_type"] = "装箱单"
        elif "报关单" in text or "customs declaration" in low:
            out["doc_type"] = "出口货物报关单"
        else:
            out["doc_type"] = "商业发票"

        # 发票号 / 日期 / 合同号
        m = re.search(r"(?:invoice\s*no|发票号|发票编号)[.：:\s]*([A-Za-z0-9\-]+)", text, re.I)
        if m:
            out["invoice_no"] = m.group(1)
        m = re.search(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})", text)
        if m:
            out["invoice_date"] = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
            out["month"] = int(m.group(2))
        # 装运月份优先：取"未带日"的年-月（如 2025-11），避开发票日期
        m = re.search(r"(20\d{2})[-/.年](\d{1,2})(?![-/月.\d])", text)
        if m:
            out["month"] = int(m.group(2))
        m = re.search(r"(?:contract|合同号|合同编号)[.：:\s]*([A-Za-z0-9\-]+)", text, re.I)
        if m:
            out["contract_no"] = m.group(1)
        m = re.search(r"(?:跨境电商平台|平台|platform)[.：:\s]*([A-Za-z\u4e00-\u9fa5]{2,20})", text, re.I)
        if m:
            out["platform"] = m.group(1).strip()
        m = re.search(r"(?:订单编号|订单号|order\s*(?:no|id))[.：:\s]*([A-Za-z0-9\-_]{4,})", text, re.I)
        if m:
            out["order_id"] = m.group(1)
        m = re.search(r"(?:物流运单号|运单号|tracking\s*(?:no|number))[.：:\s]*([A-Za-z0-9\-_]{4,})", text, re.I)
        if m:
            out["logistics_no"] = m.group(1)
        m = re.search(r"(?:结关日|结关日期|clearance\s*date)[.：:\s]*(20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2})", text, re.I)
        if m:
            out["clear_date"] = re.sub(r"[年月/.]", "-", m.group(1)).rstrip("-")

        # 毛重/净重/件数（千克口径）
        gw = re.search(r"(?:毛重|gross\s*weight|G\.?W\.?)[.：:\s]*([\d,]*\.?\d+)\s*(KGS?|千克|公斤|吨)?", text, re.I)
        if gw:
            val = _to_float(gw.group(1).replace(",", ""))
            out["gross_weight"] = round(val * 1000, 2) if (gw.group(2) or "").startswith("吨") else val
        nw = re.search(r"(?:净重|net\s*weight|N\.?W\.?)[.：:\s]*([\d,]*\.?\d+)\s*(KGS?|千克|公斤|吨)?", text, re.I)
        if nw:
            val = _to_float(nw.group(1).replace(",", ""))
            out["net_weight"] = round(val * 1000, 2) if (nw.group(2) or "").startswith("吨") else val
        pc = re.search(r"(?:件数|箱数|总箱数|packages?|ctns?)[.：:\s]*(\d[\d,]*)", text, re.I)
        if pc:
            out["package_count"] = int(_to_float(pc.group(1).replace(",", "")))

        # 起运地：命中系统节点
        for node in self._nodes:
            if node in text:
                out["origin"] = node
                break
        if not out.get("origin"):
            m = re.search(r"(?:起运地|起运港|发货地|from)[.：:\s]*([^\n|,，]{2,12})", text, re.I)
            if m:
                out["origin"] = m.group(1).strip()

        # 目的港 / 目的国
        for alias, canon in _DEST_ALIASES.items():
            if alias in low or alias in text:
                out["dest"] = canon
                break
        if not out.get("dest"):
            m = re.search(r"(?:目的港|目的地|目的国|to|ship\s*to)[.：:\s]*([^\n|,，]{2,16})", text, re.I)
            if m:
                raw = m.group(1).strip()
                out["dest"] = _DEST_ALIASES.get(raw.lower(), raw)
        for alias, canon in _COUNTRY_ALIASES.items():
            if alias.isascii():
                # 拉丁字母别名按整词匹配，避免 "us" 误命中 business/customs 等词
                hit = re.search(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])", low)
            else:
                hit = alias in text
            if hit:
                out["dest_country"] = canon
                break

        # 贸易条款
        m = re.search(r"\b(FOB|CIF|CFR|EXW|CIP|DDP|DAP)\b", text, re.I)
        if m:
            out["trade_terms"] = m.group(1).upper()

        # 货名
        out["cargo_name"] = self._match_cargo_name(text)

        # 数量与单位
        m = re.search(r"(\d[\d,]*)\s*(PCS|CTNS?|SETS?|件|箱|套|个)", text, re.I)
        if m:
            out["quantity"] = _to_float(m.group(1).replace(",", ""))
            out["unit"] = m.group(2).upper()

        # 毛重（吨）：支持 kg / 吨 / KGS
        out["tonnage"] = self._extract_weight(text)

        # 金额：优先取"USD 前缀的金额"，取最大值即单据总金额（避免误取数量）
        usd_amts = re.findall(r"USD\s*([\d,]+\.\d{2})", text, re.I)
        if usd_amts:
            out["total_amount_usd"] = max(_to_float(a) for a in usd_amts)
        else:
            m = re.search(r"(?:total|合计|总计|总额)[^\d]{0,12}(\d[\d,]*\.?\d*)", text, re.I)
            if m:
                out["total_amount_usd"] = _to_float(m.group(1).replace(",", ""))
        m = re.search(r"(?:unit\s*price|单价)[^\d]{0,8}(\d[\d,]*\.?\d*)", text, re.I)
        if m:
            out["unit_price_usd"] = _to_float(m.group(1).replace(",", ""))

        # 特殊属性备注
        notes = []
        for kw in ["锂电池", "充电宝", "电池", "UN38.3", "MSDS", "危险品"]:
            if kw in text:
                notes.append(kw)
        for brand in ["iphone", "apple", "华为", "小米", "nike", "迪士尼", "disney"]:
            if brand in low:
                notes.append(brand)
        if notes:
            out["special_notes"] = "、".join(dict.fromkeys(notes))

        out["priority"] = self._infer_priority(text)
        return out

    def _extract_weight(self, text: str) -> float:
        # 优先显式吨
        m = re.search(r"(?:毛重|净重|总重|weight|g\.?w\.?|n\.?w\.?)?[^\d]{0,8}(\d[\d,]*\.?\d*)\s*(?:吨|TON\b)", text, re.I)
        if m:
            return _to_float(m.group(1).replace(",", ""))
        # 千克：取最大值作为毛重（毛重 ≥ 净重，可避开净重/分项重量误取）
        kgs = re.findall(r"(\d[\d,]*\.?\d*)\s*(?:KGS?\b|千克|公斤)", text, re.I)
        if kgs:
            return round(max(_to_float(k) for k in kgs) / 1000.0, 2)
        return 0.0

    @staticmethod
    def _match_cargo_name(text: str) -> str:
        # 先抓"含品牌词的品名整行"，保证 IP 风险能被下游命中
        m = re.search(
            r"([^\n]{0,48}?(?:iphone|apple|honor|华为|小米|xiaomi|nike|迪士尼|disney)[^\n]{0,24})",
            text, re.I,
        )
        if m:
            return m.group(1).strip()
        # 再匹配特殊品类（保证带电/危险品能命中）
        for kw in ["手机保护壳", "手机壳", "保护壳", "充电宝", "移动电源", "锂电池", "锂离子",
                   "蓝牙耳机", "数据线", "塑料制品", "日用百货"]:
            if kw in text or kw.lower() in text.lower():
                return kw
        try:
            from customs.hs_kb import HSKnowledgeBase
            kb = HSKnowledgeBase()
            for entry in kb.all_entries():
                for kw in entry.get("keywords", []):
                    if kw and kw.lower() in text.lower():
                        return entry.get("category", "") or kw
        except Exception:  # noqa: BLE001
            pass
        m = re.search(r"(?:品名|货物名称|description|commodity)[.：:\s]*([^\n|,，]{2,20})", text, re.I)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _infer_priority(text: str) -> str:
        low = text.lower()
        if any(k in low for k in _COST_HINTS):
            return "cost"
        if any(k in low for k in _TIME_HINTS):
            return "time"
        return "balanced"

    # ----------------------------------------------------------------- #
    # 规整：把 origin/dest 对齐到系统节点与规范名
    # ----------------------------------------------------------------- #
    def _normalize(self, f: DocFields) -> None:
        if f.dest:
            f.dest = _DEST_ALIASES.get(f.dest.lower(), f.dest)
        # origin 若给的是口岸全称，保留；若给英文城市，尝试映射到系统节点
        # 只在"节点名是 origin 的子串"时匹配（节点按长度降序，优先命中更具体的口岸名）
        if f.origin:
            for node in self._nodes:
                if node in f.origin:
                    f.origin = node
                    break
        _dest_country = {
            "洛杉矶": "美国", "汉堡": "德国", "鹿特丹": "荷兰",
            "新加坡": "新加坡", "迪拜": "阿联酋", "釜山": "韩国", "纽约": "美国",
        }
        if f.dest in _dest_country:
            # 规范目的港 → 目的国 是权威映射，覆盖别名扫描的误判
            f.dest_country = _dest_country[f.dest]
        if not f.special_notes and f.cargo_name:
            for kw in ["锂电池", "充电宝", "电池"]:
                if kw in f.cargo_name:
                    f.special_notes = kw
            if "iphone" in f.cargo_name.lower() or "apple" in f.cargo_name.lower():
                f.special_notes = (f.special_notes + "、品牌:iPhone").strip("、")

    @staticmethod
    def _load_nodes() -> List[str]:
        try:
            with open(_NODES_PATH, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            # 按字符串长度降序，优先匹配更具体的节点名（如"深圳港"先于"深圳"）
            return sorted({n["id"] for n in data.get("nodes", [])}, key=len, reverse=True)
        except Exception:  # noqa: BLE001
            return []


def _to_float(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"[\d,]*\.?\d+", str(v))
    return float(m.group(0).replace(",", "")) if m else 0.0


if __name__ == "__main__":
    import sys
    r = DocumentReader(verbose=True)
    for p in sys.argv[1:]:
        f = r.read(p)
        print(json.dumps(f.to_dict(), ensure_ascii=False, indent=2))
        print("QUERY:", f.to_query())

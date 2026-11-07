"""真实业务场景单据生成器。

生成四个"甩进去就能跑"的真实单据，覆盖 A/B/C 三档分流：
- 场景一 · IP侵权类（C）：3C 配件出口 → 洛杉矶（品牌 IP critical）
- 场景二 · 危险品类（C）：电子配件出口 → 汉堡（锂电池危险品 critical）
- 场景三 · 合规直通（A）：运动鞋出口 → 洛杉矶（字段齐全、无风险）
- 场景四 · 低报预警（B）：硅胶手机壳出口 → 洛杉矶（低单价 warning）

每个场景输出两种载体：
- 商业发票图片 PNG（真实排版，走 OCR 识别路径）
- 电子单据 JSON（结构化，走直接解析路径）

运行：
    cd /workspace/transport_agent
    python scenarios/generate_docs.py
"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SCENARIOS_DIR = Path(__file__).resolve().parent
FONT_REG = "/usr/share/fonts/opentype/noto-cjk-otf/NotoSansCJKsc-Regular.otf"
FONT_BOLD = "/usr/share/fonts/opentype/noto-cjk-otf/NotoSansCJKsc-Bold.otf"

W, H = 1240, 1654
MARGIN = 60
INK = (24, 30, 42)
MUTED = (90, 100, 116)
LINE = (150, 158, 172)
ACCENT = (18, 72, 152)
BG_SOFT = (240, 244, 250)


# ------------------------------------------------------------------ #
# 四个真实业务场景（数据）
# ------------------------------------------------------------------ #
SCENARIOS = [
    {
        "id": "scene1_ip",
        "name": "场景一 · IP侵权类（C级）",
        "subtitle": "3C配件出口 · 深圳 → 洛杉矶",
        "doc_type": "商业发票",
        "seller": "深圳市锐讯科技有限公司",
        "seller_en": "SHENZHEN RUIXUN TECHNOLOGY CO., LTD.",
        "seller_addr": "广东省深圳市宝安区西乡街道固戍二路 68 号 A 栋",
        "seller_tel": "Tel: +86-755-8300 6688",
        "seller_id": "统一社会信用代码: 91440300MA5FXXXX6K",
        "buyer": "TechGear Trading Inc.",
        "buyer_addr": "2450 S Atlantic Blvd, Commerce, CA 90040, USA",
        "buyer_tel": "Tel: +1-323-5700 2210",
        "invoice_no": "RX-INV-20261015-01",
        "invoice_date": "2026-10-15",
        "contract_no": "RX-TG-2026-1008",
        "platform": "Amazon",
        "order_id": "AMZ-2026-10-8842",
        "logistics_no": "SF1234567890123",
        "clear_date": "2026-10-25",
        "trade_terms": "FOB SHENZHEN",
        "origin": "深圳港 Shenzhen",
        "dest": "LOS ANGELES, USA",
        "dest_cn": "洛杉矶 · 美国",
        "payment": "T/T 30% / 70% B/L",
        "currency": "USD",
        "items": [
            {"no": 1, "desc": "iPhone 15 Pro 手机保护壳 (TPU+PC)", "desc_en": "iPhone 15 Pro Protective Case",
             "hs": "3926.90", "qty": 8000, "unit": "PCS", "price": 0.98, "amount": 7840.00},
            {"no": 2, "desc": "iPhone 14 手机保护壳 (带 Apple Logo)", "desc_en": "iPhone 14 Case with Apple Logo",
             "hs": "3926.90", "qty": 4000, "unit": "PCS", "price": 1.19, "amount": 4760.00},
        ],
        "total_qty": 12000, "total_amount": 12600.00,
        "net_weight": "4,800 KGS", "gross_weight": "5,200 KGS", "ctns": "3 CTNS",
        "net_kg": 4800, "gross_kg": 5200, "pkgs": 3,
        "remark": "货物含第三方品牌标识（iPhone / Apple），出口前须提供品牌授权证明。\nMarks: N/M  " + "3 CTNS  Made in China",
        "priority": "cost", "month": 11,
        "risk_tag": "IP 侵权风险（品牌词：iPhone / Apple）",
    },
    {
        "id": "scene2_dg",
        "name": "场景二 · 危险品类（C级）",
        "subtitle": "电子配件出口 · 义乌 → 汉堡",
        "doc_type": "商业发票",
        "seller": "义乌市恒通电子配件有限公司",
        "seller_en": "YIWU HENGTONG ELECTRONIC CO., LTD.",
        "seller_addr": "浙江省义乌市北苑街道春晗路 128 号",
        "seller_tel": "Tel: +86-579-8555 1200",
        "seller_id": "统一社会信用代码: 91330782MA2HXXXX7P",
        "buyer": "EuroTech Handels GmbH",
        "buyer_addr": "Hafenstrasse 45, 20359 Hamburg, Germany",
        "buyer_tel": "Tel: +49-40-3006 7788",
        "invoice_no": "HT-EXP-20261015-02",
        "invoice_date": "2026-10-15",
        "contract_no": "HT-EU-2026-0918",
        "platform": "AliExpress",
        "order_id": "AE-2026-10-7719",
        "logistics_no": "YT9876543210987",
        "clear_date": "2026-10-28",
        "trade_terms": "CIF HAMBURG",
        "origin": "义乌 Yiwu",
        "dest": "HAMBURG, GERMANY",
        "dest_cn": "汉堡 · 德国",
        "payment": "L/C at sight",
        "currency": "USD",
        "items": [
            {"no": 1, "desc": "锂离子聚合物充电宝 10000mAh (UN3480)", "desc_en": "Li-ion Polymer Power Bank 10000mAh",
             "hs": "8507.60", "qty": 6000, "unit": "PCS", "price": 4.20, "amount": 25200.00},
            {"no": 2, "desc": "蓝牙耳机 含锂电池 (UN3481)", "desc_en": "Bluetooth Earphone with Li-ion Battery",
             "hs": "8518.30", "qty": 8000, "unit": "PCS", "price": 2.35, "amount": 18800.00},
        ],
        "total_qty": 14000, "total_amount": 44000.00,
        "net_weight": "5,200 KGS", "gross_weight": "6,000 KGS", "ctns": "20 CTNS",
        "net_kg": 5200, "gross_kg": 6000, "pkgs": 20,
        "remark": "货物含锂电池，属危险品（UN3480 / UN3481），须提供 UN38.3 测试报告与 MSDS。\nMarks: N/M  " + "20 CTNS  Made in China",
        "priority": "time", "month": 11,
        "risk_tag": "危险品风险（UN3480 / UN3481 锂电池）",
    },
    {
        "id": "scene3_green",
        "name": "场景三 · 合规直通（A级）",
        "subtitle": "运动鞋出口 · 宁波 → 洛杉矶",
        "doc_type": "商业发票",
        "seller": "宁波市步远鞋业有限公司",
        "seller_en": "NINGBO BUYUAN FOOTWEAR CO., LTD.",
        "seller_addr": "浙江省宁波市鄞州区姜山镇科技路 99 号",
        "seller_tel": "Tel: +86-574-8822 3366",
        "seller_id": "统一社会信用代码: 91330212MA2JXXXX8Q",
        "buyer": "GreenLeaf Apparel LLC",
        "buyer_addr": "1200 S Santa Fe Ave, Los Angeles, CA 90021, USA",
        "buyer_tel": "Tel: +1-213-8800 5517",
        "invoice_no": "BY-EXP-20261020-03",
        "invoice_date": "2026-10-20",
        "contract_no": "BY-GL-2026-1012",
        "platform": "Amazon",
        "order_id": "AMZ-2026-10-9901",
        "logistics_no": "SF1122334455667",
        "clear_date": "2026-10-30",
        "trade_terms": "FOB NINGBO",
        "origin": "宁波港 Ningbo",
        "dest": "LOS ANGELES, USA",
        "dest_cn": "洛杉矶 · 美国",
        "payment": "T/T 100% before shipment",
        "currency": "USD",
        "items": [
            {"no": 1, "desc": "纺织材料制面运动鞋（无品牌）", "desc_en": "Textile Upper Sports Shoes",
             "hs": "6404.11", "qty": 6000, "unit": "PCS", "price": 8.50, "amount": 51000.00},
        ],
        "total_qty": 6000, "total_amount": 51000.00,
        "net_weight": "1,200 KGS", "gross_weight": "1,350 KGS", "ctns": "30 CTNS",
        "net_kg": 1200, "gross_kg": 1350, "pkgs": 30,
        "remark": "无品牌、无电池、无第三方知识产权；随附装箱单与订舱确认。\nMarks: N/M  " + "30 CTNS  Made in China",
        "priority": "time", "month": 11,
        "risk_tag": "合规直通（无品牌/无电/单证齐全）",
    },
    {
        "id": "scene4_lowvalue",
        "name": "场景四 · 低报预警（B级）",
        "subtitle": "硅胶手机壳出口 · 东莞 → 洛杉矶",
        "doc_type": "商业发票",
        "seller": "东莞市柔盾塑胶制品有限公司",
        "seller_en": "DONGGUAN ROUDUN PLASTIC PRODUCTS CO., LTD.",
        "seller_addr": "广东省东莞市长安镇乌沙工业区兴发路 18 号",
        "seller_tel": "Tel: +86-769-8123 6677",
        "seller_id": "统一社会信用代码: 91441900MA4WXXXX3R",
        "buyer": "SoftCase Direct Inc.",
        "buyer_addr": "3850 S Alameda St, Los Angeles, CA 90058, USA",
        "buyer_tel": "Tel: +1-323-6600 1188",
        "invoice_no": "RD-EXP-20261022-04",
        "invoice_date": "2026-10-22",
        "contract_no": "RD-SC-2026-1016",
        "platform": "Shopify",
        "order_id": "SHF-2026-10-5521",
        "logistics_no": "YT5566778899001",
        "clear_date": "2026-10-27",
        "trade_terms": "FOB SHENZHEN",
        "origin": "深圳港 Shenzhen",
        "dest": "LOS ANGELES, USA",
        "dest_cn": "洛杉矶 · 美国",
        "payment": "T/T 70% / 30% B/L",
        "currency": "USD",
        "items": [
            {"no": 1, "desc": "硅胶手机保护壳（无品牌）", "desc_en": "Silicone Phone Case (No Brand)",
             "hs": "3926.90", "qty": 20000, "unit": "PCS", "price": 0.18, "amount": 3600.00},
        ],
        "total_qty": 20000, "total_amount": 3600.00,
        "net_weight": "760 KGS", "gross_weight": "850 KGS", "ctns": "15 CTNS",
        "net_kg": 760, "gross_kg": 850, "pkgs": 15,
        "remark": "无品牌、无电池；申报单价明显低于同类商品，需准备价格构成说明。\nMarks: N/M  " + "15 CTNS  Made in China",
        "priority": "cost", "month": 11,
        "risk_tag": "货值异常（申报单价低于同类阈值）",
    },
]


# ------------------------------------------------------------------ #
# 绘制辅助
# ------------------------------------------------------------------ #
def _font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_BOLD if bold else FONT_REG, size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    """按像素宽度折行（兼容中英文混排）。"""
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if draw.textlength(cur + ch, font=font) <= max_w:
            cur += ch
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines or [""]


def _draw_text(draw, xy, text, font, fill=INK, anchor="la"):
    draw.text(xy, text, font=font, fill=fill, anchor=anchor)


def render_invoice(sc: dict) -> Image.Image:
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)

    f_title = _font(True, 40)
    f_company = _font(True, 28)
    f_company_en = _font(False, 17)
    f_small = _font(False, 16)
    f_label = _font(True, 18)
    f_body = _font(False, 19)
    f_body_b = _font(True, 20)
    f_table_h = _font(True, 19)

    y = MARGIN

    # ---- 抬头：公司名（左） + 单据类型（右） ----
    d.rectangle([MARGIN, y, MARGIN + 8, y + 92], fill=ACCENT)
    _draw_text(d, (MARGIN + 24, y + 4), sc["seller"], f_company, INK)
    _draw_text(d, (MARGIN + 24, y + 42), sc["seller_en"], f_company_en, MUTED)
    _draw_text(d, (MARGIN + 24, y + 66), sc["seller_addr"], f_small, MUTED)

    _draw_text(d, (W - MARGIN, y + 4), "商业发票", f_title, ACCENT, anchor="ra")
    _draw_text(d, (W - MARGIN, y + 52), "COMMERCIAL INVOICE", _font(True, 18), MUTED, anchor="ra")

    y += 108
    d.line([MARGIN, y, W - MARGIN, y], fill=ACCENT, width=3)
    y += 24

    # ---- 卖方 / 买方 ----
    col_w = (W - 2 * MARGIN - 30) // 2
    left_x, right_x = MARGIN, MARGIN + col_w + 30

    _draw_text(d, (left_x, y), "出口商 / 发货人  Seller / Shipper", f_label, ACCENT)
    _draw_text(d, (right_x, y), "买方 / 收货人  Consignee", f_label, ACCENT)
    y += 28
    for i, ln in enumerate([sc["seller"], sc["seller_en"], sc["seller_addr"], sc["seller_tel"], sc["seller_id"]]):
        _draw_text(d, (left_x, y + i * 26), ln, f_body if i == 0 else f_small, INK if i == 0 else MUTED)
    for i, ln in enumerate([sc["buyer"], sc["buyer_addr"], sc["buyer_tel"]]):
        _draw_text(d, (right_x, y + i * 26), ln, f_body if i == 0 else f_small, INK if i == 0 else MUTED)
    y += 5 * 26 + 14

    # ---- 单据要素 meta 行 ----
    meta = [
        ("发票号 Invoice No", sc["invoice_no"]),
        ("发票日期 Date", sc["invoice_date"]),
        ("合同号 Contract No", sc["contract_no"]),
        ("贸易条款 Trade Terms", sc["trade_terms"]),
        ("支付方式 Payment", sc["payment"]),
        ("币种 Currency", sc["currency"]),
    ]
    box_h = 40
    cw = (W - 2 * MARGIN) // 6
    for i, (k, v) in enumerate(meta):
        bx = MARGIN + i * cw
        d.rectangle([bx, y, bx + cw, y + box_h], outline=LINE, width=1)
        _draw_text(d, (bx + 8, y + 4), k, _font(True, 13), MUTED)
        _draw_text(d, (bx + 8, y + 21), v, _font(True, 15), INK)
    y += box_h + 20

    # ---- 航程 ----
    route = [
        ("起运港 From", sc["origin"]),
        ("目的港 To", sc["dest"]),
        ("目的国 Country", sc["dest_cn"]),
        ("装运月份 Ship Month", f"2026-{sc['month']:02d}"),
    ]
    rw = (W - 2 * MARGIN) // 4
    for i, (k, v) in enumerate(route):
        bx = MARGIN + i * rw
        d.rectangle([bx, y, bx + rw, y + 44], fill=BG_SOFT, outline=LINE, width=1)
        _draw_text(d, (bx + 10, y + 5), k, _font(True, 14), MUTED)
        _draw_text(d, (bx + 10, y + 24), v, _font(True, 17), ACCENT)
    y += 60

    # ---- 跨境电商订单信息（9610 清单必填）----
    order_row = [
        ("平台 Platform", sc["platform"]),
        ("订单编号 Order No", sc["order_id"]),
        ("物流运单号 Tracking No", sc["logistics_no"]),
        ("结关日 Clear Date", sc["clear_date"]),
    ]
    ow = (W - 2 * MARGIN) // 4
    for i, (k, v) in enumerate(order_row):
        bx = MARGIN + i * ow
        d.rectangle([bx, y, bx + ow, y + 46], outline=LINE, width=1)
        _draw_text(d, (bx + 8, y + 4), k, _font(True, 12), MUTED)
        _draw_text(d, (bx + 8, y + 24), v, _font(True, 15), INK)
    y += 62

    # ---- 商品明细表 ----
    cols = [
        ("序号\nNo.", 60, "c"),
        ("品名 / 规格\nDescription", 430, "l"),
        ("HS编码\nHS Code", 110, "c"),
        ("数量\nQty", 96, "r"),
        ("单位\nUnit", 80, "c"),
        ("单价(USD)\nUnit Price", 120, "r"),
        ("金额(USD)\nAmount", 154, "r"),
    ]
    table_top = y
    # 表头
    x = MARGIN
    hh = 52
    for name, cwid, align in cols:
        d.rectangle([x, y, x + cwid, y + hh], fill=ACCENT)
        _draw_text(d, (x + cwid / 2, y + hh / 2), name, f_table_h, "white", anchor="mm")
        x += cwid
    y += hh

    # 行
    for it in sc["items"]:
        desc_lines = _wrap(d, it["desc"], f_body, cols[1][1] - 16)
        desc_lines += _wrap(d, it["desc_en"], f_small, cols[1][1] - 16)
        row_h = max(64, 26 * len(desc_lines) + 16)
        x = MARGIN
        vals = [
            (str(it["no"]), cols[0][2]),
            (None, cols[1][2]),
            (it["hs"], cols[2][2]),
            (f"{it['qty']:,}", cols[3][2]),
            (it["unit"], cols[4][2]),
            (f"{it['price']:.2f}", cols[5][2]),
            (f"{it['amount']:,.2f}", cols[6][2]),
        ]
        for ci, (name, cwid, align) in enumerate(cols):
            d.rectangle([x, y, x + cwid, y + row_h], outline=LINE, width=1)
            val, al = vals[ci]
            if val is None:
                for li, ln in enumerate(desc_lines):
                    _draw_text(d, (x + 8, y + 8 + li * 26), ln,
                               f_body if li == 0 else f_small, INK if li == 0 else MUTED)
            else:
                ax = {"c": x + cwid / 2, "r": x + cwid - 10, "l": x + 8}[align]
                a = {"c": "mm", "r": "rm", "l": "lm"}[align]
                _draw_text(d, (ax, y + row_h / 2), val, f_body, INK, anchor=a)
            x += cwid
        y += row_h

    # ---- 合计行 ----
    tot_h = 46
    x = MARGIN
    span_left = sum(c[1] for c in cols[:3])
    d.rectangle([x, y, x + span_left, y + tot_h], fill=BG_SOFT, outline=LINE, width=1)
    _draw_text(d, (x + span_left - 12, y + tot_h / 2), f"合计 Total  {sc['total_qty']:,} PCS",
               f_body_b, INK, anchor="rm")
    x += span_left
    d.rectangle([x, y, x + cols[3][1] + cols[4][1] + cols[5][1], y + tot_h], fill=BG_SOFT, outline=LINE, width=1)
    x += cols[3][1] + cols[4][1] + cols[5][1]
    d.rectangle([x, y, x + cols[6][1], y + tot_h], fill=ACCENT)
    _draw_text(d, (x + cols[6][1] - 10, y + tot_h / 2), f"USD {sc['total_amount']:,.2f}",
               f_body_b, "white", anchor="rm")
    y += tot_h + 18

    # ---- 重量 ----
    wrow = [
        ("件数 Packages", sc["ctns"]),
        ("净重 N.W.", sc["net_weight"]),
        ("毛重 G.W.", sc["gross_weight"]),
    ]
    ww = (W - 2 * MARGIN) // 3
    for i, (k, v) in enumerate(wrow):
        bx = MARGIN + i * ww
        d.rectangle([bx, y, bx + ww, y + 42], outline=LINE, width=1)
        _draw_text(d, (bx + 10, y + 4), k, _font(True, 14), MUTED)
        _draw_text(d, (bx + 10, y + 22), v, _font(True, 17), INK)
    y += 58

    # ---- 备注 ----
    _draw_text(d, (MARGIN, y), "备注 Remarks", f_label, ACCENT)
    y += 26
    rem_lines = []
    for seg in sc["remark"].split("\n"):
        rem_lines += _wrap(d, seg, f_small, W - 2 * MARGIN - 24)
    rh = 26 * len(rem_lines) + 18
    d.rectangle([MARGIN, y, W - MARGIN, y + rh], fill=(255, 251, 240), outline=(220, 180, 90), width=2)
    for li, ln in enumerate(rem_lines):
        _draw_text(d, (MARGIN + 12, y + 9 + li * 26), ln, f_small, (140, 90, 20))
    y += rh + 20

    # ---- 签署 ----
    d.line([MARGIN, y, W - MARGIN, y], fill=LINE, width=1)
    y += 14
    _draw_text(d, (MARGIN, y), "We hereby certify that the information above is true and correct.",
               f_small, MUTED)
    _draw_text(d, (W - MARGIN, y + 2), "Authorized Signature / 授权签字", f_small, MUTED, anchor="ra")
    _draw_text(d, (W - MARGIN, y + 44), sc["seller"], _font(True, 20), ACCENT, anchor="ra")
    _draw_text(d, (W - MARGIN, y + 72), "（公司盖章 Company Chop）", _font(False, 14), MUTED, anchor="ra")

    return img


def to_json_doc(sc: dict) -> dict:
    """把场景数据转成电子单据 JSON（结构化识别路径）。"""
    return {
        "doc_type": "商业发票",
        "seller": sc["seller"],
        "seller_en": sc["seller_en"],
        "buyer": sc["buyer"],
        "invoice_no": sc["invoice_no"],
        "invoice_date": sc["invoice_date"],
        "contract_no": sc["contract_no"],
        "platform": sc["platform"],
        "order_id": sc["order_id"],
        "logistics_no": sc["logistics_no"],
        "clear_date": sc["clear_date"],
        "trade_terms": sc["trade_terms"],
        "origin": sc["origin"],
        "dest": sc["dest_cn"].split("·")[0].strip(),
        "dest_country": sc["dest_cn"].split("·")[-1].strip(),
        "cargo_name": sc["items"][0]["desc"],
        "tonnage": float(sc["gross_weight"].replace(",", "").split()[0]) / 1000.0,
        "gross_weight": float(sc["gross_kg"]),
        "net_weight": float(sc["net_kg"]),
        "package_count": int(sc["pkgs"]),
        "quantity": sc["total_qty"],
        "unit": "PCS",
        "unit_price_usd": sc["items"][0]["price"],
        "total_amount_usd": sc["total_amount"],
        "currency": sc["currency"],
        "priority": sc["priority"],
        "month": sc["month"],
        "special_notes": sc["risk_tag"],
        "items": [
            {"desc": f"{it['desc']} / {it['desc_en']}", "hs": it["hs"],
             "qty": it["qty"], "unit": it["unit"], "price": it["price"], "amount": it["amount"]}
            for it in sc["items"]
        ],
    }


def main() -> None:
    SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
    index = []
    for sc in SCENARIOS:
        d = SCENARIOS_DIR / sc["id"]
        d.mkdir(parents=True, exist_ok=True)
        png = d / f"{sc['invoice_no']}_商业发票.png"
        render_invoice(sc).save(png)
        jpath = d / f"{sc['invoice_no']}_电子单据.json"
        jpath.write_text(json.dumps(to_json_doc(sc), ensure_ascii=False, indent=2), encoding="utf-8")
        index.append({
            "id": sc["id"],
            "name": sc["name"],
            "subtitle": sc["subtitle"],
            "risk_tag": sc["risk_tag"],
            "doc_type": sc["doc_type"],
            "invoice_no": sc["invoice_no"],
            "seller": sc["seller"],
            "buyer": sc["buyer"],
            "origin": sc["origin"],
            "dest": sc["dest_cn"],
            "month": sc["month"],
            "gross_weight": sc["gross_weight"],
            "total_amount": sc["total_amount"],
            "png": f"{sc['id']}/{png.name}",
            "json": f"{sc['id']}/{jpath.name}",
        })
        print(f"[生成] {png}")
        print(f"[生成] {jpath}")
    (SCENARIOS_DIR / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[生成] {SCENARIOS_DIR / 'index.json'}")


if __name__ == "__main__":
    main()

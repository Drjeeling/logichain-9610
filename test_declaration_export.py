"""制单链路回归测试：组装、缺口分级、补填、Excel 导出。"""
import io
import sys

sys.path.insert(0, ".")

from customs.declaration_export import build_payload, export_xlsx


COMPLETE = {
    "trade_mode": "9610",
    "platform": "Amazon",
    "order_id": "AMZ-2026-001",
    "logistics_no": "SF1234567890",
    "cargo_spec": "蓝牙耳机",
    "cargo_common": "蓝牙耳机",
    "hs_code": "8518300000",
    "hs_desc": "耳机",
    "tariff_rate": "0%",
    "supervision_code": "无",
    "material": "塑料",
    "usage": "听音乐",
    "quantity": 1000,
    "unit": "件",
    "unit_price": 14.0,
    "total_price": 14000.0,
    "currency": "USD",
    "shipper": "深圳市某某科技有限公司",
    "consignee": "US Buyer Inc",
    "production_sales_unit": "深圳市某某科技有限公司",
    "from_port": "深圳",
    "dest_port": "美国",
    "origin_country": "中国",
    "package_type": "纸箱",
    "gross_weight": 120.0,
    "net_weight": 100.0,
    "package_count": 10,
}


def test_complete_is_ready():
    p = build_payload(COMPLETE)
    assert p["blocked"] is False
    assert p["ready"] is True
    assert p["counts"]["BLOCK"] == 0
    assert p["counts"]["NEED_INPUT"] == 0
    assert p["totals"]["total_price"] == 14000.0


def test_missing_block_field():
    decl = {k: v for k, v in COMPLETE.items() if k != "production_sales_unit"}
    p = build_payload(decl)
    assert p["blocked"] is True
    assert p["ready"] is False
    gap = [g for g in p["gaps"] if g["key"] == "production_sales_unit"][0]
    assert gap["status"] == "BLOCK"
    assert gap["action"] == "block"


def test_edits_fill_gap():
    decl = {k: v for k, v in COMPLETE.items()
            if k not in ("production_sales_unit", "gross_weight", "net_weight")}
    before = build_payload(decl)
    assert before["blocked"] is True
    after = build_payload(decl, edits={
        "production_sales_unit": "深圳市某某科技有限公司",
        "gross_weight": 120,
        "net_weight": 100,
    })
    assert after["blocked"] is False
    assert after["ready"] is True
    assert after["counts"]["NEED_INPUT"] == 0


def test_edits_do_not_clear_existing():
    p = build_payload(COMPLETE, edits={"platform": "", "order_id": "   "})
    assert p["declaration"]["platform"] == "Amazon"
    assert p["declaration"]["order_id"] == "AMZ-2026-001"


def test_every_field_has_status_and_source():
    p = build_payload(COMPLETE)
    for row in p["header"] + p["items"][0]["rows"]:
        assert row["status"] in (
            "FILLED", "DERIVED", "DEFAULTED", "NEED_INPUT", "BLOCK", "EMPTY", "WARN"
        )
        assert row["label"]
        assert row["key"]
        assert row["source"] is not None


def test_xlsx_export():
    from openpyxl import load_workbook

    p = build_payload(COMPLETE)
    data = export_xlsx(p)
    assert len(data) > 4000
    wb = load_workbook(io.BytesIO(data))
    assert wb.sheetnames == ["申报清单", "字段溯源", "缺口与依据"]
    ws = wb["申报清单"]
    texts = [str(c.value) for row in ws.iter_rows() for c in row if c.value]
    assert any("申报清单" in t for t in texts)
    assert any("8518300000" in t for t in texts)


if __name__ == "__main__":
    test_complete_is_ready()
    test_missing_block_field()
    test_edits_fill_gap()
    test_edits_do_not_clear_existing()
    test_every_field_has_status_and_source()
    test_xlsx_export()
    print("declaration export: 6/6 passed")

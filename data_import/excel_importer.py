"""Excel 批量导入：支持从 .xlsx 文件批量更新图边数据。

模板列：
| from | to | mode | distance_km | base_price_per_km | hours | frequency | transfer_cost | transfer_hours | carbon_g_per_ton_km |

使用方式：
1. 调用 generate_template() 生成空白模板
2. 在模板里填入新边数据
3. 调用 import_edges(path) 把新边合并到 edges.json（已存在的 from+to+mode 三元组会被覆盖）

也支持命令行：
    python -m data_import.excel_importer --template      # 生成模板
    python -m data_import.excel_importer --import xx.xlsx  # 导入数据
"""
from __future__ import annotations

import json
import sys
import argparse
from pathlib import Path
from typing import List, Dict

from openpyxl import Workbook, load_workbook


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
EDGES_PATH = DATA_DIR / "edges.json"

COLUMNS = [
    "from", "to", "mode", "distance_km", "base_price_per_km",
    "hours", "frequency", "transfer_cost", "transfer_hours",
    "carbon_g_per_ton_km",
]


def generate_template(out_path: Path | None = None) -> Path:
    """生成空白导入模板（含表头与示例行）。"""
    out_path = out_path or (DATA_DIR.parent / "templates" / "运输边数据模板.xlsx")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    ws = wb.active
    ws.title = "edges"
    ws.append(COLUMNS)

    # 示例行
    ws.append([
        "义乌", "宁波港", "truck", 200, 0.55, 4, "随时", 0, 0, 62
    ])
    ws.append([
        "义乌西", "宁波北仑", "rail", 250, 0.15, 7, "每周2班(三五)", 0, 0, 21
    ])

    # 备注表
    ws2 = wb.create_sheet("说明")
    ws2.append(["字段", "说明", "示例"])
    notes = [
        ("from", "起点节点ID", "义乌"),
        ("to", "终点节点ID", "宁波港"),
        ("mode", "运输方式", "rail/river/truck"),
        ("distance_km", "距离（公里）", 200),
        ("base_price_per_km", "基准运价（元/吨·公里）", 0.55),
        ("hours", "单段运输时效（小时）", 4),
        ("frequency", "班次描述", "随时 / 每周3班(一三五)"),
        ("transfer_cost", "起点换装成本（元/吨）", "0 或 200"),
        ("transfer_hours", "起点换装时长（小时）", "0 或 12"),
        ("carbon_g_per_ton_km", "碳排放（g/吨·公里）", "rail=21 / river=30 / truck=62"),
    ]
    for row in notes:
        ws2.append(row)

    wb.save(out_path)
    return out_path


def import_edges(xlsx_path: Path, output_path: Path | None = None, mode: str = "merge") -> Dict:
    """从 Excel 导入边数据，合并/覆盖到 edges.json。

    Args:
        xlsx_path: Excel 文件路径
        output_path: 输出路径，默认覆盖 data/edges.json
        mode: merge=合并（同 from+to+mode 覆盖）/ replace=全量替换
    """
    output_path = output_path or EDGES_PATH

    wb = load_workbook(xlsx_path, data_only=True)
    ws = wb["edges"]

    # 读取表头
    headers = [cell.value for cell in ws[1]]
    new_edges: List[Dict] = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not any(row):
            continue
        edge = {h: v for h, v in zip(headers, row)}
        # 类型转换
        edge["distance_km"] = float(edge["distance_km"])
        edge["base_price_per_km"] = float(edge["base_price_per_km"])
        edge["hours"] = float(edge["hours"])
        edge["transfer_cost"] = float(edge.get("transfer_cost") or 0)
        edge["transfer_hours"] = float(edge.get("transfer_hours") or 0)
        edge["carbon_g_per_ton_km"] = float(edge.get("carbon_g_per_ton_km") or 0)
        new_edges.append(edge)

    if mode == "replace":
        all_edges = new_edges
    else:  # merge
        with open(EDGES_PATH, "r", encoding="utf-8") as f:
            current = json.load(f)
        current_edges = current["edges"]
        # 用 (from, to, mode) 作主键去重
        key_set = {(e["from"], e["to"], e["mode"]) for e in new_edges}
        kept = [e for e in current_edges
                if (e["from"], e["to"], e["mode"]) not in key_set]
        all_edges = kept + new_edges

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({"edges": all_edges}, f, ensure_ascii=False, indent=2)

    return {
        "imported": len(new_edges),
        "total": len(all_edges),
        "mode": mode,
        "output": str(output_path),
    }


def main():
    parser = argparse.ArgumentParser(description="运输边数据 Excel 批量导入工具")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--template", action="store_true", help="生成空白导入模板")
    g.add_argument("--import", dest="import_path", help="从 Excel 导入边数据")
    parser.add_argument("--mode", choices=["merge", "replace"], default="merge")
    args = parser.parse_args()

    if args.template:
        out = generate_template()
        print(f"模板已生成: {out}")
    else:
        result = import_edges(Path(args.import_path), mode=args.mode)
        print(f"导入完成：新增 {result['imported']} 条，"
              f"当前共 {result['total']} 条，模式={result['mode']}")


if __name__ == "__main__":
    main()

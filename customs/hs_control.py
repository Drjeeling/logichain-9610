"""HS 税号管制状态查询（自动触发 E 板块要素校验）。

数据源：customs/data/hs_control_map.json（6,812 个受管制税号，
CusComplex 官方 control_mark + 识别码表连接的申报类别码）。

自动判断口径：
- 查询税号在表中 → 受管制（control_mark）→ 若带申报类别码，
  自动要求该类别的必答参数问题（无需场景显式声明）；
- 不在表中 → 无管制标记（源表 12,089 行中其余 5,277 行均无标记）；
- 受管制但申报类别码缺失 → 警告并要求人工核对，不猜测类别。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional


DATA_PATH = Path(__file__).resolve().parent / "data" / "hs_control_map.json"


class HSControlMap:
    def __init__(self, path: Path = DATA_PATH):
        self._codes: Dict[str, Dict] = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self._codes = data.get("codes", {}) or {}

    def check(self, code_ts: str) -> Dict:
        """查询税号管制状态。

        返回 {controlled, control_mark, g_name, declaration_categories,
        classify_codes, category_known}。
        """
        code = (code_ts or "").strip()
        row = self._codes.get(code)
        if not row:
            return {
                "controlled": False, "control_mark": None, "g_name": None,
                "declaration_categories": [], "classify_codes": [],
                "category_known": False,
            }
        cats = row.get("declaration_categories") or []
        return {
            "controlled": True,
            "control_mark": row.get("control_mark"),
            "g_name": row.get("g_name"),
            "declaration_categories": cats,
            "classify_codes": row.get("classify_codes") or [],
            "category_known": bool(cats),
        }

    def is_controlled(self, code_ts: str) -> bool:
        return self.check(code_ts)["controlled"]


if __name__ == "__main__":
    m = HSControlMap()
    for c in ["3926909010", "3926909090", "8518220000", "8545900000", "8801009090"]:
        r = m.check(c)
        print(c, "→", "受管制" if r["controlled"] else "无标记",
              f"mark={r['control_mark']}", f"类别={r['declaration_categories']}")

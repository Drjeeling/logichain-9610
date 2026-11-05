"""禁限管制申报要素参数查询（E 板块）。

数据源：customs/data/control_params.json（单一窗口 CusMerchElement 参数，
2026-10-27 抓取，128 条 / 16 类）。两用物项与出口管制商品在申报时须逐项
回答类别下的参数问题（如石墨密度/纯度阈值、无人机续航与视距控制等）。

映射边界（重要）：
HS 编码 → 管制类别的自动映射依赖"识别码参数数据"（707 行，未交付）。
该数据到位前，本模块只做"类别 → 必答问题"查询；调用方须显式提供
control_category（4 位申报类别码），不得由商品名猜测映射。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional


DATA_PATH = Path(__file__).resolve().parent / "data" / "control_params.json"


class ControlParamsKB:
    def __init__(self, path: Path = DATA_PATH):
        self._categories: Dict[str, Dict] = {}
        self._meta: Dict = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self._categories = data.get("categories", {}) or {}
            self._meta = data.get("_meta", {}) or {}

    # ----------------------------------------------------------------- #
    # 类别查询
    # ----------------------------------------------------------------- #
    def lookup_category(self, category_code: str) -> Optional[Dict]:
        """按 4 位申报类别码查询，返回 {code, name, elements}；未命中返回 None。"""
        code = (category_code or "").strip()
        cat = self._categories.get(code)
        if not cat:
            return None
        return {
            "code": code,
            "name": cat.get("name", ""),
            "elements": cat.get("elements", []),
            "source": self._meta.get("source"),
        }

    @property
    def categories(self) -> List[Dict]:
        """全部管制类别概览（code + name + 问题数）。"""
        return [
            {"code": code, "name": cat.get("name", ""),
             "question_count": len(cat.get("elements", []))}
            for code, cat in sorted(self._categories.items())
        ]

    # ----------------------------------------------------------------- #
    # 申报校验：类别必答问题是否已回答
    # ----------------------------------------------------------------- #
    def validate_answers(
        self,
        category_code: str,
        answers: Optional[Dict[str, str]],
    ) -> Dict:
        """校验某管制类别的必答问题回答情况。

        answers: {问题原文或 s_num 字符串: 回答值}。回答非空即视为已答，
        本层不做阈值判定（如密度是否超限属专业裁量，交人工/后续规则）。

        返回 {hit, name, total, answered, unanswered[]}：
        - hit=False 表示类别码未命中数据表（警告，不阻断）
        """
        hit = self.lookup_category(category_code)
        if not hit:
            return {"hit": False, "name": "", "total": 0,
                    "answered": 0, "unanswered": []}

        answers = answers or {}
        answered_keys = {str(k) for k in answers}
        unanswered = []
        answered = 0
        for item in hit["elements"]:
            keys = {item["element"], str(item.get("s_num"))}
            val = next((answers[k] for k in keys if k in answers), None)
            if val is None or not str(val).strip():
                unanswered.append(item["element"])
            else:
                answered += 1
        return {
            "hit": True,
            "name": hit["name"],
            "total": len(hit["elements"]),
            "answered": answered,
            "unanswered": unanswered,
        }


if __name__ == "__main__":
    kb = ControlParamsKB()
    print(f"已加载 {len(kb.categories)} 类管制参数")
    for c in kb.categories:
        print(f"  {c['code']} {c['name']}: {c['question_count']} 问")
    # 碳棒案例（E 板块）：石墨及其制品 3014
    v = kb.validate_answers("3014", None)
    print("\n3014 未作答校验:", v["total"], "问，未答", len(v["unanswered"]))

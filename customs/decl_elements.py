"""申报要素（涉税规范申报目录）查询：按税号最长前缀匹配。

数据源：customs/data/decl_elements.json（单一窗口 CusMerchElement 参数，
2026-10-27 抓取）。该目录按"最细可用前缀"组织，例如 3926 章级共享一套
8 项要素，851822 细化到 6 位才有独立清单——因此不能假设 10 位码必有独立行。

适用范围：报关单（含 9610 汇总申报）的申报要素校验。
9610 清单使用 CEB603 栏目体系，不得用本表代替清单栏目校验。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional


DATA_PATH = Path(__file__).resolve().parent / "data" / "decl_elements.json"


class DeclElementsKB:
    def __init__(self, path: Path = DATA_PATH):
        self._elements: Dict[str, List[Dict]] = {}
        self._meta: Dict = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self._elements = data.get("elements", {}) or {}
            self._meta = data.get("_meta", {}) or {}

    def lookup(self, hs_code: str) -> Optional[Dict]:
        """返回 {key, elements}；键为命中的最长前缀，未命中返回 None。"""
        code = (hs_code or "").strip()
        if not code:
            return None
        for length in range(len(code), 3, -1):
            key = code[:length]
            if key in self._elements:
                return {
                    "key": key,
                    "elements": self._elements[key],
                    "source": self._meta.get("source"),
                    "fetched_at": self._meta.get("fetched_at"),
                }
        return None

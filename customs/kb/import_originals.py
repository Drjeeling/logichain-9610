"""把 docs/regulations 下的官方原文导入法规知识库。

用法：
    python -m customs.kb.import_originals          # 只报告
    python -m customs.kb.import_originals --write  # 写回 index.json

解析策略（按优先级自动选择）：
1. 行首「第X条」             —— 法规、规章
2. 行首「一、」               —— 公告类（含 2012 年 24 号办法的特殊编号）
3. 「规则一/二/…」            —— 归类总规则
4. 全文单条                  —— 只有一段执行性表述的公告

清洗规则：
- 从「公告如下」「为深入贯彻」首次出现处开始，丢弃网页导航
- 到「特此公告」为止，丢弃页脚
- 识别「来信收悉/答复」页面并标为不可用，不导入
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent.parent
DOCS_DIR = ROOT / "docs" / "regulations"
INDEX_PATH = ROOT / "customs" / "data" / "regulations" / "index.json"

FILE_MAP = {
    "CUSTOMS-LAW-2021": "LAW-CUSTOMS-2021",
    "DECREE-236-2017": "ADVANCE-RULING-CLASSIFICATION",
    "DECREE-277-2025": "DECREE-277-2025",
    "ANN-24-2012-TAXREBATE": "SAT-MGT-EXPORT-TAX",
    "ANN-12-2014-9610": "CROSSBORDER-9610-MODE",
    "ANN-4-2023-RETURN": "ANN-4-2023-RETURN",
    "ANN-34-2023-RETURN-EXT": "ANN-34-2023-RETURN-EXT",
    "ANN-16-2026-RETURN": "ANN-16-2026-RETURN",
    "ANN-167-2024": "ANN-167-2024",
    "ANN-260-2025-TARIFF": "ANN-260-2025",
    "TARIFF-COMMITTEE-11-2025": "CUSTOMS-TARIFF-2026",
    "ANN-40-2026-DECL": "ANN-40-2026",
    "RULES-GENERAL-INTERP": "HARMONIZED-RULES",
}

# 这些文件不导入：内容不是公告正文（问答页 / 空文件）
SKIP_FILES = {
    "ANN-40-2026-DECL": "页面是 12360 问答，不含公告正文",
    "CATALOG-DECLARATION-2026": "文件为空，申报要素目录未采到",
}

ARTICLE_RE = re.compile(r"^第([一二三四五六七八九十百零〇]+)条\s*(.*)$")
ITEM_RE = re.compile(r"^([一二三四五六七八九十]+)、\s*(.*)$")
RULE_RE = re.compile(r"^规则([一二三四五六七八九十]+)\s*(.*)$")
FOOTER_KEYS = ("打印本页", "关闭窗口", "京ICP", "公网安备", "网站标识码", "本站热词", "责任编辑")


def _read_meta(path: Path) -> Dict[str, str]:
    meta: Dict[str, str] = {}
    if not path.exists():
        return meta
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta


def _clean_lines(lines: List[str]) -> List[str]:
    """去掉网页导航与页脚，只留正文区间。"""
    start = None
    anchors = ("公告如下", "现就", "为深入贯彻", "为支持", "为促进")
    for i, line in enumerate(lines):
        s = line.strip()
        if any(a in s for a in anchors):
            start = i
            break
    if start is None:
        for i, line in enumerate(lines):
            s = line.strip()
            if ARTICLE_RE.match(s) or ITEM_RE.match(s) or RULE_RE.match(s):
                start = i
                break
    if start is None:
        for i, line in enumerate(lines):
            if re.match(r"^=+$", line.strip()):
                start = i + 1
                break
    if start is None:
        start = 0

    out: List[str] = []
    for line in lines[start:]:
        s = line.strip()
        if not s:
            continue
        if any(k in s for k in FOOTER_KEYS):
            continue
        out.append(s)
        if "特此公告" in s:
            break
    return out


def _parse(lines: List[str]) -> Tuple[List[Dict], str]:
    """返回 (条款列表, 使用的解析策略)。"""
    # 策略 1：第X条
    arts: List[Dict] = []
    cur: Optional[Dict] = None
    for s in lines:
        m = ARTICLE_RE.match(s)
        if m:
            if cur:
                arts.append(cur)
            cur = {"article": f"第{m.group(1)}条", "text": m.group(2)}
            continue
        if re.match(r"^第[一二三四五六七八九十百零〇]+章", s):
            continue
        if cur:
            cur["text"] += s
    if cur:
        arts.append(cur)
    if len(arts) >= 2:
        return _finalize(arts), "第X条"

    # 策略 2：一、
    arts, cur = [], None
    for s in lines:
        m = ITEM_RE.match(s)
        if m:
            if cur:
                arts.append(cur)
            cur = {"article": f"第{m.group(1)}条", "text": m.group(2)}
            continue
        if cur:
            cur["text"] += s
    if cur:
        arts.append(cur)
    if len(arts) >= 2:
        return _finalize(arts), "一、→第X条"

    # 策略 3：规则
    arts, cur = [], None
    for s in lines:
        m = RULE_RE.match(s)
        if m:
            if cur:
                arts.append(cur)
            cur = {"article": f"规则{m.group(1)}", "text": m.group(2)}
            continue
        if cur:
            cur["text"] += s
    if cur:
        arts.append(cur)
    if len(arts) >= 2:
        return _finalize(arts), "规则X"

    # 策略 4：全文
    text = "".join(lines)
    if len(text) >= 30:
        return [{"article": "全文", "text": text}], "全文"
    return [], "不可用"


def _finalize(arts: List[Dict]) -> List[Dict]:
    for a in arts:
        a["text"] = re.sub(r"\s+", "", a["text"]).strip()
    return [a for a in arts if len(a["text"]) >= 6]


def _is_qa_page(text: str) -> bool:
    return ("来信收悉" in text or "您好！" in text) and "公告如下" not in text


def _load_cards() -> Dict:
    return json.loads(INDEX_PATH.read_text(encoding="utf-8"))


def import_all(write: bool = False) -> Dict:
    data = _load_cards()
    cards = data["cards"]
    report: List[Dict] = []

    for path in sorted(DOCS_DIR.glob("*.txt")):
        prefix = path.name.split("_")[0]
        # 兼容 ANN-40-2026-DECL 这类多段前缀
        for known in list(FILE_MAP) + list(SKIP_FILES):
            if path.name.startswith(known + "_"):
                prefix = known
                break

        if prefix in SKIP_FILES:
            report.append({"file": path.name, "card": "-", "status": "SKIP", "note": SKIP_FILES[prefix]})
            continue
        card_id = FILE_MAP.get(prefix)
        if not card_id:
            report.append({"file": path.name, "card": "-", "status": "SKIP", "note": "无卡片映射"})
            continue

        raw = path.read_text(encoding="utf-8", errors="ignore")
        if _is_qa_page(raw):
            report.append({"file": path.name, "card": card_id, "status": "SKIP", "note": "问答页，非公告正文"})
            continue

        lines = _clean_lines(raw.splitlines())
        arts, strategy = _parse(lines)
        if not arts:
            report.append({"file": path.name, "card": card_id, "status": "SKIP", "note": "未解析出条款"})
            continue

        card = cards.setdefault(card_id, {
            "card_id": card_id,
            "title": prefix,
            "doc_no": "",
            "issuer": "",
            "status": "effective",
            "clauses": [],
        })
        old = card.get("clauses", [])
        used_by = {c.get("article"): c.get("used_by", []) for c in old if c.get("used_by")}
        extra = [c for c in old if not ARTICLE_RE.match(str(c.get("article", "")) + " ")]

        new_clauses = []
        for a in arts:
            clause = {"article": a["article"], "text": a["text"]}
            if used_by.get(a["article"]):
                clause["used_by"] = used_by[a["article"]]
            new_clauses.append(clause)
        card["clauses"] = new_clauses + extra

        meta = _read_meta(path.with_name(path.name.replace(".txt", ".meta.yaml")))
        for k in ("doc_no", "issuer"):
            if meta.get(k):
                card[k] = meta[k]
        if meta.get("publish_date"):
            card["publish_date"] = meta["publish_date"]
        card["original_text"] = str(path.relative_to(ROOT))
        card["verified_at"] = meta.get("fetched_at") or str(date.today())
        card["import_strategy"] = strategy

        total = sum(len(c.get("text") or c.get("text_summary") or "") for c in card["clauses"])
        report.append({
            "file": path.name,
            "card": card_id,
            "status": "OK",
            "articles": len(arts),
            "extra_kept": len(extra),
            "chars": total,
            "strategy": strategy,
        })

    # 版本链：4 号 → 34 号 → 16 号
    if "ANN-4-2023-RETURN" in cards:
        cards["ANN-4-2023-RETURN"]["status"] = "superseded"
        cards["ANN-4-2023-RETURN"]["superseded_by"] = "ANN-34-2023-RETURN-EXT"
        cards["ANN-4-2023-RETURN"]["version_note"] = "2023年第34号延续其适用期，后被2026年第16号替代"
    if "ANN-34-2023-RETURN-EXT" in cards:
        cards["ANN-34-2023-RETURN-EXT"].update({
            "card_id": "ANN-34-2023-RETURN-EXT",
            "title": "关于延续实施跨境电子商务出口退运商品税收政策的公告",
            "doc_no": "财政部 海关总署 税务总局公告2023年第34号",
            "issuer": "财政部 海关总署 税务总局",
            "status": "superseded",
            "superseded_by": "ANN-16-2026-RETURN",
            "version_note": "适用窗口 2023-01-30 至 2025-12-31，已被 2026 年第 16 号替代",
        })

    data["_meta"] = data.get("_meta", {})
    data["_meta"]["last_original_import"] = str(date.today())
    data["_meta"]["originals_dir"] = "docs/regulations"

    if write:
        INDEX_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"report": report, "cards": cards}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    result = import_all(write=args.write)
    for r in result["report"]:
        if r["status"] == "OK":
            print(f"[OK]   {r['card']:34s} {r['articles']:3d} 条 {r['chars']:6d} 字  ({r['strategy']})")
        else:
            print(f"[SKIP] {r['file'][:34]:36s} {r['note']}")
    total = sum(len(c.get("clauses", [])) for c in result["cards"].values())
    print(f"\n条款总数 {total}；写入：{'是' if args.write else '否（加 --write 才保存）'}")


if __name__ == "__main__":
    main()

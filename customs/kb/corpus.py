"""法规知识库语料构建：结构优先切块 + 上下文回填 + 交叉引用解析。

解决"单纯切块丢失语义"的三个具体手段：

1. 结构优先切块
   - 只在条款 / 事实条目 / 规则条目边界切，绝不在句子中间截断；
   - 条款号、文号、发布机关随块保存，引用时可回溯。

2. 双文本
   - `text`        = 原文或摘要，一字不改，用于引用展示与答辩核对；
   - `retrieval_text` = 标题 + 文号 + 机关 + 条款号 + 前文上下文 + 正文，
     只用于建索引，因此不会污染引用原文。

3. 指代回填 + 交叉引用
   - 检测 "上述 / 前述 / 本条 / 两项 / 该等" 等指代词，回填前一条正文作为 context；
   - 解析 "《某法》第X条" / "本办法第X条"，建立引用边，检索命中时自动带出被引条款。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

# 指代词：出现时说明该块依赖前文，需要回填 context
ANAPHORA_RE = re.compile(
    r"上述|前述|前款|本条|该等|该项|这些|两项|三类|四项|以上(?:情况|企业|货物|规定)|此条"
)

# 跨文档引用：《中华人民共和国海关法》第四十四条
CROSS_DOC_RE = re.compile(r"《(?P<title>[^》]{2,45})》\s*第\s*(?P<art>[一二三四五六七八九十百零〇0-9]+)\s*条")
# 同文档引用：本办法第七条规定
CROSS_SELF_RE = re.compile(
    r"(?:本|该)(?:规定|办法|公告|条例|细则|法)\s*第\s*(?P<art>[一二三四五六七八九十百零〇0-9]+)\s*条"
)

# 定义抽取：本规定中的"申报"是指……
DEF_RE = re.compile(
    r'本(?:规定|办法|公告|条例|细则|法)(?:中|所称)?(?:的)?["“]?(?P<term>[^"，。”\n]{2,20})["”]?是指'
    r'(?P<definition>[^。\n]{4,300})'
)
ALIAS_RE = re.compile(r"（以下简称(?P<alias>[^）)]{2,15})）")

TEXT_KIND_CN = {
    "original": "原文",
    "summary": "摘要",
    "editorial": "编者注",
    "rule": "规则说明",
    "hs_note": "归类参考",
}


def build_chunks(root: Path) -> List[Dict]:
    """从结构化源文件生成全部检索块（幂等，源文件改动后重建即可）。"""
    data_dir = root / "customs" / "data"
    cards = _load_json(data_dir / "regulations" / "index.json").get("cards", {})
    rules = _load_json(data_dir / "rules.json").get("rules", [])
    hs = _load_json(data_dir / "hs_codes.json").get("hs_codes", [])

    chunks: List[Dict] = []
    for source_id, card in cards.items():
        chunks.extend(_card_chunks(source_id, card))
    for rule in rules:
        chunks.append(_rule_chunk(rule))
    for entry in hs:
        chunks.append(_hs_chunk(entry))

    _resolve_cross_refs(chunks, cards)
    return chunks


# ------------------------------------------------------------------ #
# 各类来源的切块
# ------------------------------------------------------------------ #
def _card_chunks(source_id: str, card: Dict) -> List[Dict]:
    meta = {k: card.get(k) for k in _CARD_META_FIELDS}
    glossary = _extract_glossary(card)
    out: List[Dict] = []

    prev_text = ""
    for i, clause in enumerate(card.get("clauses", [])):
        article = clause.get("article") or clause.get("false") or f"条款{i + 1}"
        if clause.get("text"):
            body, kind = clause["text"], "original"
        else:
            body, kind = clause.get("text_summary", ""), "summary"
        if not body:
            continue
        context = _build_context(body, prev_text, glossary)
        out.append(_make_chunk(
            chunk_id=f"{source_id}#art{article}",
            source_id=source_id, source_type="law_card", article=article,
            text=body, text_kind=kind, context=context,
            used_by=clause.get("used_by", []), glossary=glossary, meta=meta,
        ))
        prev_text = body

    prev_text = ""
    for i, fact in enumerate(card.get("key_facts", [])):
        if not fact:
            continue
        context = _build_context(fact, prev_text, glossary)
        out.append(_make_chunk(
            chunk_id=f"{source_id}#fact{i + 1}",
            source_id=source_id, source_type="law_card", article=None,
            text=fact, text_kind="summary", context=context,
            used_by=card.get("used_by", []), glossary=glossary, meta=meta,
        ))
        prev_text = fact

    if card.get("notes"):
        out.append(_make_chunk(
            chunk_id=f"{source_id}#note",
            source_id=source_id, source_type="law_card", article=None,
            text=card["notes"].strip(), text_kind="editorial",
            context="编者注：非法规原文，仅作理解线索，不得作为引用依据。",
            used_by=[], glossary=glossary, meta=meta,
        ))
    return out


def _rule_chunk(rule: Dict) -> Dict:
    rule_id = rule.get("rule_id", "")
    parts = [rule.get("description") or "", rule.get("risk") or "", rule.get("advice") or ""]
    body = "；".join(dict.fromkeys(p for p in parts if p))  # 去重：部分规则 description 与 risk 同文
    doc_id = rule.get("source_document_id")
    meta = {
        "doc_no": None,
        "issuer": None,
        "official_url": None,
        "effective_date": None,
        "status": rule.get("status", "active"),
        "verified_at": rule.get("last_reviewed"),
    }
    chunk = _make_chunk(
        chunk_id=f"RULE#{rule_id}",
        source_id=f"RULE-{rule_id}",
        source_type="rule",
        article=None,
        text=body,
        text_kind="rule",
        context=f"规则 {rule_id} · {rule.get('rule_name', '')}（业务域：{'/'.join(rule.get('business_type', []))}）",
        used_by=rule.get("business_type", []),
        glossary=[],
        meta=meta,
    )
    chunk["source_title"] = rule.get("rule_name", rule_id)
    chunk["source_document_id"] = doc_id
    chunk["severity"] = rule.get("severity")
    return chunk


def _hs_chunk(entry: Dict) -> Dict:
    code = entry.get("code", "")
    names = [entry.get("official_name") or entry.get("description", "")]
    if entry.get("common_names"):
        names.append("俗称：" + "、".join(entry["common_names"]))
    facts = []
    for key, label in (("tariff_rate", "税率"), ("export_rebate_rate", "出口退税率"),
                       ("vat_rate", "增值税率"), ("unit", "计量单位"),
                       ("regulatory_code", "监管条件")):
        if entry.get(key):
            facts.append(f"{label}{entry[key]}")
    body = f"{'｜'.join(names)}｜HS {code}" + ("｜" + "｜".join(facts) if facts else "")
    meta = {
        "doc_no": None, "issuer": "海关税则", "official_url": None,
        "effective_date": None, "status": "effective", "verified_at": None,
    }
    chunk = _make_chunk(
        chunk_id=f"HS#{code}",
        source_id=f"HS-{code}",
        source_type="hs_note",
        article=None,
        text=body,
        text_kind="hs_note",
        context=f"归类参考：{entry.get('category', '')}",
        used_by=[],
        glossary=[],
        meta=meta,
    )
    chunk["source_title"] = f"HS {code} {entry.get('description', '')}".strip()
    return chunk


# ------------------------------------------------------------------ #
# 组装
# ------------------------------------------------------------------ #
_CARD_META_FIELDS = (
    "title", "issuer", "doc_no", "official_url", "effective_date",
    "status", "verified_at", "superseded_by",
)


def _make_chunk(*, chunk_id, source_id, source_type, article, text, text_kind,
                context, used_by, glossary, meta) -> Dict:
    title = meta.get("title") or ""
    head = " ".join(x for x in [title, meta.get("doc_no") or "", meta.get("issuer") or "",
                                article or ""] if x)
    return {
        "chunk_id": chunk_id,
        "source_id": source_id,
        "source_type": source_type,
        "source_title": title,
        "doc_no": meta.get("doc_no"),
        "issuer": meta.get("issuer"),
        "official_url": meta.get("official_url"),
        "effective_date": meta.get("effective_date"),
        "status": meta.get("status") or "effective",
        "superseded_by": meta.get("superseded_by"),
        "verified_at": meta.get("verified_at"),
        "article": article,
        "text": text,
        "text_kind": text_kind,
        "text_kind_cn": TEXT_KIND_CN.get(text_kind, text_kind),
        "context": context,
        "used_by": used_by,
        "glossary": glossary,
        # 只用于建索引，绝不用于引用展示
        "retrieval_text": " ".join(x for x in [head, context, text] if x),
        "cross_refs": [],
    }


def _build_context(body: str, prev_text: str, glossary: List[Dict]) -> str:
    """指代回填：把"上述企业"这类依赖前文的表述，补上前一条正文与本文定义。"""
    if not ANAPHORA_RE.search(body):
        return ""
    parts: List[str] = []
    if prev_text:
        parts.append(f"前文：{prev_text[:160]}")
    for g in glossary:
        if g.get("term") and g["term"] in body:
            parts.append(f"定义：{g['term']}是指{g['definition']}")
    return "；".join(parts)


def _extract_glossary(card: Dict) -> List[Dict]:
    glossary: List[Dict] = []
    for clause in card.get("clauses", []):
        body = clause.get("text") or clause.get("text_summary") or ""
        for m in DEF_RE.finditer(body):
            term = m.group("term").strip('"“” ')
            definition = m.group("definition").strip()
            if term and definition:
                glossary.append({"term": term, "definition": definition,
                                 "article": clause.get("article")})
        for m in ALIAS_RE.finditer(body):
            glossary.append({"term": m.group("alias").strip(), "definition": body[:200],
                             "article": clause.get("article")})
    return glossary


# ------------------------------------------------------------------ #
# 交叉引用
# ------------------------------------------------------------------ #
def _resolve_cross_refs(chunks: List[Dict], cards: Dict) -> None:
    title_to_source = {c.get("title"): sid for sid, c in cards.items() if c.get("title")}
    # 条款号归一化：卡片里存 "第七条"，正则捕获的是 "七"，统一成纯数字串再匹配
    by_key = {(c["source_id"], _art_num(c["article"])): c["chunk_id"]
              for c in chunks if c.get("article")}

    for ch in chunks:
        refs: List[Dict] = []
        text = ch["text"]
        for m in CROSS_DOC_RE.finditer(text):
            target_title, art = m.group("title"), m.group("art")
            target_sid = title_to_source.get(target_title)
            refs.append({
                "raw": m.group(0),
                "target_source_id": target_sid,
                "target_article": art,
                "target_chunk_id": by_key.get((target_sid, _art_num(art))) if target_sid else None,
                "relation": "referenced_doc",
            })
        for m in CROSS_SELF_RE.finditer(text):
            art = m.group("art")
            refs.append({
                "raw": m.group(0),
                "target_source_id": ch["source_id"],
                "target_article": art,
                "target_chunk_id": by_key.get((ch["source_id"], _art_num(art))),
                "relation": "referenced_same_doc",
            })
        # 去重
        seen = set()
        ch["cross_refs"] = [r for r in refs if not (r["raw"] in seen or seen.add(r["raw"]))]


def _art_num(article: str) -> str:
    """'第七条' -> '七'；已是纯数字/数字串则原样返回。"""
    return re.sub(r"^第|条$", "", (article or "").strip())


def _load_json(path: Path) -> Dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

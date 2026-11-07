"""知识库检索：BM25 打分 + 状态过滤 + 交叉引用展开。

返回结构对前端稳定，后续若换向量检索，只改本文件，接口不变。
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Dict, List

from .bm25 import BM25
from .corpus import build_chunks
from .normalize import expand_query
from .tokenizer import backend, tokenize

# 只有这些状态可被检索；expired / superseded 一律过滤
_LIVE_STATUS = {"effective", "active", "expiring_soon"}

# 片段类型权重：编者注只是理解线索，不该压过法规原文与摘要
_KIND_WEIGHT = {"original": 1.0, "rule": 1.0, "summary": 0.9, "hs_note": 0.85, "editorial": 0.45}

# 相关性下限：低于此分数视为"未找到依据"。
# 由 test_kb_eval.py 在 22 个可回答问题 + 3 个无依据问题上校准。
# 原文入库后（260 个可检索片段）复测：可回答问题最低 top1 6.61，
# 无依据问题最高 top1 5.07，取中间值 5.8。
# 语料规模显著变化后必须重新校准，不要当成永久常数。
MIN_SCORE = 5.8

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_date(value) -> "date | None":
    if not value or not _DATE_RE.match(str(value)):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def is_live_chunk(chunk: Dict, today: date) -> bool:
    """是否可作为"当前依据"检索。

    三类排除：
    1. 状态为 expired / superseded（已废止）；
    2. 存在 superseded_by（被新文件替代）；
    3. effective_date 晚于今天（已发布但尚未生效）。
    """
    if chunk.get("status") not in _LIVE_STATUS:
        return False
    if chunk.get("superseded_by"):
        return False
    effective = _parse_date(chunk.get("effective_date"))
    if effective and effective > today:
        return False
    return True


class KnowledgeBase:
    def __init__(self, root: Path, verbose: bool = False, today: "date | None" = None,
                 min_score: float = MIN_SCORE):
        self.root = Path(root)
        self.verbose = verbose
        self.today = today or date.today()
        self.min_score = min_score
        self.chunks: List[Dict] = build_chunks(self.root)
        self._by_id: Dict[str, Dict] = {c["chunk_id"]: c for c in self.chunks}

        self._live = [c for c in self.chunks if is_live_chunk(c, self.today)]
        docs = [tokenize(c["retrieval_text"]) for c in self._live]
        self._bm25 = BM25(docs)
        if self.verbose:
            print(f"[KB] 切块 {len(self.chunks)} 条，可检索 {len(self._live)} 条，分词 {backend()}")

    # ---------------------------------------------------------------- #
    def stats(self) -> Dict:
        return {
            "chunks": len(self.chunks),
            "searchable": len(self._live),
            "excluded": len(self.chunks) - len(self._live),
            "today": self.today.isoformat(),
            "backend": backend(),
            "by_kind": _count(self._live, "text_kind"),
            "by_source_type": _count(self.chunks, "source_type"),
        }

    def search(self, query: str, top_k: int = 3, include_expired: bool = False) -> Dict:
        q = (query or "").strip()
        if not q:
            return self._empty(q, "empty_query")

        if include_expired:
            pool, bm25 = self.chunks, BM25([tokenize(c["retrieval_text"]) for c in self.chunks])
        else:
            pool, bm25 = self._live, self._bm25

        exp = expand_query(q)
        tokens = exp["tokens"]
        scores = bm25.score(tokens)
        ranked = sorted(
            ((i, scores[i] * _KIND_WEIGHT.get(pool[i]["text_kind"], 1.0) * _exact_boost(tokens, pool[i]["text"]))
             for i in range(len(pool)) if scores[i] > 0),
            key=lambda x: -x[1],
        )

        hits: List[Dict] = []
        for i, final in ranked[:top_k]:
            if final < self.min_score:
                break
            hits.append(self._hit(pool[i], scores[i], final))

        if not hits:
            return self._empty(q, "no_hit", exp["expanded"])

        # 交叉引用展开：命中"本办法第X条"时，把被引条款一起带出
        hits = self._expand(hits, pool)

        return {
            "query": q,
            "coverage": "hit",
            "backend": backend(),
            "expanded_terms": exp["expanded"],
            "hits": hits,
        }

    # ---------------------------------------------------------------- #
    def _hit(self, chunk: Dict, score: float, final: float) -> Dict:
        return {
            "chunk_id": chunk["chunk_id"],
            "source_id": chunk["source_id"],
            "source_type": chunk["source_type"],
            "source_title": chunk["source_title"],
            "doc_no": chunk.get("doc_no"),
            "issuer": chunk.get("issuer"),
            "article": chunk.get("article"),
            "text": chunk["text"],
            "text_kind": chunk["text_kind"],
            "text_kind_cn": chunk["text_kind_cn"],
            "context": chunk.get("context") or "",
            "official_url": chunk.get("official_url"),
            "effective_date": chunk.get("effective_date"),
            "verified_at": chunk.get("verified_at"),
            "status": chunk.get("status"),
            "used_by": chunk.get("used_by") or [],
            "score": round(final, 3),
            "via": "query",
            "cross_refs": [r for r in chunk.get("cross_refs", []) if r.get("target_chunk_id")],
        }

    def _expand(self, hits: List[Dict], pool: List[Dict]) -> List[Dict]:
        """带出被引条款（最多 2 条），标记 via=cross_ref，分数衰减。"""
        existing = {h["chunk_id"] for h in hits}
        extra: List[Dict] = []
        for h in hits:
            for ref in h.get("cross_refs", []):
                cid = ref.get("target_chunk_id")
                if not cid or cid in existing:
                    continue
                target = self._by_id.get(cid)
                if not target or not is_live_chunk(target, self.today):
                    continue
                item = self._hit(target, 0.0, h["score"] * 0.6)
                item["via"] = "cross_ref"
                item["via_ref"] = ref["raw"]
                item["referenced_by"] = h["chunk_id"]
                extra.append(item)
                existing.add(cid)
                if len(extra) >= 2:
                    return hits + extra
        return hits + extra

    @staticmethod
    def _empty(query: str, reason: str, expanded: List[str] | None = None) -> Dict:
        return {
            "query": query,
            "coverage": "no_hit",
            "reason": reason,
            "backend": backend(),
            "expanded_terms": expanded or [],
            "hits": [],
        }


def _exact_boost(tokens: List[str], text: str) -> float:
    """原词命中加权：中文分词偶尔会切错，整词命中是强信号。"""
    terms = {t for t in tokens if len(t) >= 2}
    if not terms:
        return 1.0
    hit = sum(1 for t in terms if t in text)
    return 1.0 + min(hit, 3) * 0.15


def _count(items: List[Dict], key: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for it in items:
        k = str(it.get(key))
        out[k] = out.get(k, 0) + 1
    return out

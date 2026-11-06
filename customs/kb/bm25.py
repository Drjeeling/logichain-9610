"""纯 Python BM25 实现（Okapi 变体，k1=1.5, b=0.75）。

语料只有数千字，倒排索引足够快，不引入 rank_bm25 / 向量模型等额外依赖。
"""
from __future__ import annotations

import math
from typing import Dict, List, Sequence, Tuple


class BM25:
    def __init__(self, docs: Sequence[Sequence[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.n = len(docs)
        self.doc_len = [len(d) for d in docs]
        self.avg_len = (sum(self.doc_len) / self.n) if self.n else 0.0

        # 倒排索引：term -> [(doc_idx, tf), ...]
        self.postings: Dict[str, List[Tuple[int, int]]] = {}
        for i, doc in enumerate(docs):
            tf: Dict[str, int] = {}
            for t in doc:
                tf[t] = tf.get(t, 0) + 1
            for t, c in tf.items():
                self.postings.setdefault(t, []).append((i, c))
        self.df = {t: len(p) for t, p in self.postings.items()}
        self.idf = {
            t: math.log(1 + (self.n - df + 0.5) / (df + 0.5))
            for t, df in self.df.items()
        }

    def score(self, query: Sequence[str]) -> List[float]:
        scores = [0.0] * self.n
        for t in query:
            idf = self.idf.get(t)
            if idf is None:
                continue
            for i, tf in self.postings[t]:
                denom = tf + self.k1 * (
                    1 - self.b + self.b * self.doc_len[i] / (self.avg_len or 1.0)
                )
                scores[i] += idf * tf * (self.k1 + 1) / denom
        return scores

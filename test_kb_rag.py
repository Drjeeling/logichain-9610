"""法规知识库 RAG 回归测试。

覆盖四类风险：
1. 切块完整性（空块 / 重复 id / 条款号丢失）
2. 状态过滤（失效法规不得被检索）
3. 指代回填（"上述/两项"这类表述必须带上前文）
4. 引用与拒答边界（无命中不生成、无 Key 降级、编者注不参与）

运行：python3 test_kb_rag.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from customs.kb import KnowledgeBase, answer_question
from customs.kb.answer import REFUSAL
from customs.kb.corpus import _resolve_cross_refs
from customs.kb.retriever import is_live_chunk
from customs.kb.tokenizer import backend, tokenize


class TestCorpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase(_ROOT, verbose=False)
        cls.by_id = {c["chunk_id"]: c for c in cls.kb.chunks}

    def test_chunk_integrity(self):
        ids = [c["chunk_id"] for c in self.kb.chunks]
        self.assertEqual(len(ids), len(set(ids)), "chunk_id 必须唯一")
        for c in self.kb.chunks:
            self.assertTrue(c["text"].strip(), f"{c['chunk_id']} 正文为空")
            self.assertIn(c["text_kind"], {"original", "summary", "editorial", "rule", "hs_note"})
            self.assertTrue(c["retrieval_text"].strip())

    def test_clause_keeps_article(self):
        clause = self.by_id["DECREE-277-2025#art第六条"]
        self.assertEqual(clause["article"], "第六条")
        self.assertEqual(clause["text_kind"], "original")

    def test_anaphora_backfilled(self):
        """ANN-40-2026 的"两项均为有条件必填"依赖前一条事实，context 必须回填。"""
        target = next(c for c in self.kb.chunks
                      if c["source_id"] == "ANN-40-2026" and "均为有条件必填" in c["text"])
        self.assertIn("禁限管制", target["context"])

    def test_cross_ref_resolution(self):
        cards = {"DOC-A": {"title": "测试办法"}}
        chunks = [
            {"source_id": "DOC-A", "article": "第七条", "chunk_id": "DOC-A#art第七条",
             "text": "……", "cross_refs": []},
            {"source_id": "DOC-A", "article": "第三条", "chunk_id": "DOC-A#art第三条",
             "text": "依照本办法第七条规定办理。", "cross_refs": []},
        ]
        _resolve_cross_refs(chunks, cards)
        refs = chunks[1]["cross_refs"]
        self.assertTrue(refs)
        self.assertEqual(refs[0]["target_chunk_id"], "DOC-A#art第七条")


class TestRetrieval(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase(_ROOT, verbose=False)

    def test_receipt_question(self):
        r = self.kb.search("9610出口还要不要传收款单", top_k=3)
        self.assertEqual(r["coverage"], "hit")
        self.assertEqual(r["hits"][0]["source_id"], "ANN-167-2024")
        self.assertIn("收款单", r["hits"][0]["text"])

    def test_expired_filtered(self):
        r = self.kb.search("跨境电子商务出口退运商品税收政策", top_k=5)
        sids = [h["source_id"] for h in r["hits"]]
        self.assertNotIn("ANN-4-2023-RETURN", sids, "失效法规不得被检索")

    def test_no_hit(self):
        r = self.kb.search("今天天气怎么样", top_k=3)
        self.assertEqual(r["coverage"], "no_hit")
        self.assertEqual(r["hits"], [])

    def test_editorial_downweighted(self):
        """编者注不应排在法规原文/摘要之前。"""
        r = self.kb.search("收款单", top_k=3)
        self.assertEqual(r["hits"][0]["text_kind"], "summary")

    def test_backend_available(self):
        self.assertIn(backend(), {"jieba", "bigram"})
        self.assertTrue(tokenize("9610 出口申报"))

    def test_synonym_expansion(self):
        """换说法：用户说"收款信息"，法规写"收款单"，必须能召回。"""
        r = self.kb.search("收款信息是不是必须上传", top_k=3)
        self.assertEqual(r["coverage"], "hit")
        self.assertEqual(r["hits"][0]["source_id"], "ANN-167-2024")
        self.assertIn("收款单", r["expanded_terms"])

    def test_low_relevance_refused(self):
        """相关性低于阈值时应判为无依据，而不是硬凑一条结果。"""
        r = self.kb.search("海关周六上班吗", top_k=3)
        self.assertEqual(r["coverage"], "no_hit")
        self.assertEqual(r["hits"], [])

    def test_version_filter(self):
        from datetime import date
        base = {"status": "effective", "superseded_by": None}
        future = dict(base, effective_date="2026-12-01")
        past = dict(base, effective_date="2025-05-01")
        superseded = dict(base, effective_date="2025-05-01", superseded_by="新版")
        text_date = dict(base, effective_date="随税则年度更新")
        today = date(2026, 11, 6)
        self.assertFalse(is_live_chunk(future, today), "未生效法规不应可检索")
        self.assertTrue(is_live_chunk(past, today))
        self.assertFalse(is_live_chunk(superseded, today), "被替代法规不应可检索")
        self.assertTrue(is_live_chunk(text_date, today), "非日期型版本标记应保留")


class TestAnswerBoundary(unittest.TestCase):
    def test_refuse_without_hits(self):
        out = answer_question("今天天气怎么样", [], api_key="sk-fake")
        self.assertFalse(out["grounded"])
        self.assertTrue(out["insufficient"])
        self.assertEqual(out["answer"], REFUSAL)
        self.assertEqual(out["citations"], [])

    def test_refuse_when_only_editorial(self):
        out = answer_question("测试", [{"text_kind": "editorial", "text": "编者注"}], api_key="sk-fake")
        self.assertFalse(out["grounded"])
        self.assertEqual(out["answer"], REFUSAL)

    def test_degrade_without_key(self):
        r = KnowledgeBase(_ROOT).search("收款单", top_k=2)
        out = answer_question("要不要传收款单", r["hits"], api_key="")
        self.assertTrue(out["degraded"])
        self.assertTrue(out["grounded"])
        self.assertTrue(out["citations"])

    def test_advisory_mode(self):
        r = KnowledgeBase(_ROOT).search("收款单", top_k=2)
        out = answer_question("要不要传收款单", r["hits"], api_key="")
        self.assertEqual(out["mode"], "ADVISORY")


if __name__ == "__main__":
    unittest.main(verbosity=2)

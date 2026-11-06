"""法规知识库 RAG（最小可用版）。

对外只有三个入口：
    KnowledgeBase(root).search(query, top_k)     检索
    KnowledgeBase(root).stats()                  规模统计
    answer_question(question, hits, api_key)     受约束生成
"""
from .answer import answer_question
from .retriever import KnowledgeBase

__all__ = ["KnowledgeBase", "answer_question"]

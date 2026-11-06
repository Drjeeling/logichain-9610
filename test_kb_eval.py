"""RAG 固定评测集：用数字回答"直接搜够不够用"。

指标：
    Recall@1 / Recall@3   正确依据是否进入前 1 / 前 3
    MRR                   正确依据的平均倒数排名
    拒答准确率            无依据问题是否被正确拒答

同时输出"关闭同义词扩展"的对照结果，用来衡量查询归一化的实际收益。

运行：python3 test_kb_eval.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from customs.kb import KnowledgeBase
from customs.kb import normalize

# (问题, 可接受的依据 chunk_id / source_id 列表)
CASES = [
    ("9610 出口还要不要传收款单", ["ANN-167-2024"]),
    ("收款信息是不是必须上传", ["ANN-167-2024"]),
    ("支付凭证要不要一起提交", ["ANN-167-2024"]),
    ("海外仓企业还需要备案吗", ["ANN-167-2024"]),
    ("汇总申报最晚什么时候", ["CROSSBORDER-9610-MODE", "RULE-R018"]),
    ("未按时汇总会怎么样", ["CROSSBORDER-9610-MODE"]),
    ("迟了报关有什么后果", ["DECREE-277-2025"]),
    ("超期申报会不会产生滞报金", ["DECREE-277-2025"]),
    ("申报需要交哪些随附单证", ["DECREE-277-2025#art第八条"]),
    ("装货前多久必须报关", ["DECREE-277-2025#art第七条"]),
    ("充电宝出口需要什么资料", ["HS-8507600090", "RULE-R001"]),
    ("电池出口要 UN38.3 吗", ["RULE-R001"]),
    ("蓝牙音箱的 HS 编码是多少", ["HS-8518220000"]),
    ("棉 T 恤出口退税率多少", ["HS-6109100021"]),
    ("出口退运多久内可以免税", ["RULE-R008", "ANN-16-2026-RETURN"]),
    ("食品可以适用退运免税吗", ["RULE-R010"]),
    ("出口退税按什么价格计算", ["SAT-MGT-EXPORT-TAX"]),
    ("预裁定决定有效期多久", ["ADVANCE-RULING-CLASSIFICATION"]),
    ("禁限管制识别码是什么", ["ANN-40-2026"]),
    ("归类从后原则是什么", ["HARMONIZED-RULES"]),
    ("B2B 批量出口能报 9610 吗", ["RULE-R012"]),
    ("出口美国需要原产地证吗", ["RULE-R002"]),
]

UNANSWERABLE = ["今天天气怎么样", "海关周六上班吗", "亚美尼亚的进口关税是多少"]


def evaluate(kb: KnowledgeBase) -> dict:
    r1 = r3 = 0
    rr = 0.0
    misses = []
    for q, expected in CASES:
        hits = kb.search(q, top_k=5)["hits"]
        ids = [h["chunk_id"] for h in hits]
        rank = None
        for i, h in enumerate(hits):
            if any(h["chunk_id"] == e or h["source_id"] == e for e in expected):
                rank = i + 1
                break
        if rank == 1:
            r1 += 1
        if rank and rank <= 3:
            r3 += 1
        if rank:
            rr += 1 / rank
        else:
            misses.append(q)

    refusals = sum(1 for q in UNANSWERABLE if kb.search(q, top_k=3)["coverage"] == "no_hit")
    n = len(CASES)
    return {
        "recall@1": r1 / n, "recall@3": r3 / n, "mrr": rr / n,
        "refusal": refusals / len(UNANSWERABLE), "misses": misses,
    }


def report(name: str, m: dict) -> None:
    print(f"{name:14s} Recall@1 {m['recall@1']:.1%}  Recall@3 {m['recall@3']:.1%}  "
          f"MRR {m['mrr']:.3f}  拒答 {m['refusal']:.0%}")
    if m["misses"]:
        print("   未命中：" + " / ".join(m["misses"]))


def main() -> int:
    kb = KnowledgeBase(_ROOT)
    print(f"语料：{kb.stats()['searchable']} 个可检索片段，{len(CASES)} 个问题，"
          f"{len(UNANSWERABLE)} 个无依据问题\n")

    with_expansion = evaluate(kb)

    # 对照组：临时关闭同义词扩展，量化查询归一化的收益
    saved = dict(normalize._ALIAS_INDEX)
    normalize._ALIAS_INDEX.clear()
    try:
        without = evaluate(kb)
    finally:
        normalize._ALIAS_INDEX.update(saved)

    report("关闭同义词扩展", without)
    report("当前（含扩展）", with_expansion)

    print("\n结论：", end="")
    gain = with_expansion["recall@3"] - without["recall@3"]
    if gain > 0:
        print(f"同义词扩展把 Recall@3 提升 {gain:.1%}，"
              f"MRR 提升 {with_expansion['mrr'] - without['mrr']:.3f}。")
    else:
        print("同义词扩展在当前测试集上无额外收益，需检查同义词表覆盖。")

    # 回归门槛：低于阈值直接失败
    ok = with_expansion["recall@3"] >= 0.85 and with_expansion["refusal"] >= 0.99
    print("阈值检查 Recall@3>=85% 且 拒答>=99%：", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

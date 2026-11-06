"""受约束生成层：只依据检索片段作答，强制引用，无依据拒答。

硬边界（与 PRD 9.3 一致）：
- 输出标记 ADVISORY，不写 risk_flags、不改 tier、不参与任何硬校验；
- 没有命中片段时直接拒答，不调用模型；
- 引用必须来自本次检索结果，模型编造的编号会被丢弃；
- "摘要""编者注"不得被表述为法条原文。
"""
from __future__ import annotations

import json
import re
from typing import Dict, List

REFUSAL = "未在现有法规库中找到明确依据，建议人工核对海关最新公告或咨询报关行。"

_SYSTEM = """你是海关业务法规知识库问答助手。你只能使用用户提供的【片段】回答，禁止使用片段以外的任何知识。

严格规则：
1. 片段不足以回答时，insufficient=true，answer 固定为"未在现有法规库中找到明确依据，建议人工核对海关最新公告或咨询报关行。"。
2. 回答中每个结论后面必须标注片段编号，如 [1]、[2]。
3. 片段类型为"摘要"时，不得写成"第X条规定"，只能写"据《…》摘要"；类型为"编者注"的片段不得引用。
4. 不得给出申报结论、分流等级、风险定性、归类最终结论，只做解释与依据说明。
5. 只输出 JSON：{"answer": "...", "citations": [1, 2], "insufficient": false}"""


def answer_question(question: str, hits: List[Dict], api_key: str,
                    model: str = "deepseek-chat", timeout: float = 30.0) -> Dict:
    """基于检索片段回答。api_key 为空时降级为"只检索不生成"。"""
    usable = [h for h in hits if h.get("text_kind") != "editorial"]

    if not usable:
        return _result(question, REFUSAL, [], grounded=False, insufficient=True,
                       degraded=False, degraded_reason="无可引用片段")

    if not api_key:
        return _result(
            question,
            "已检索到相关依据（未启用生成，请直接查看下方片段）。",
            usable, grounded=True, insufficient=False,
            degraded=True, degraded_reason="未配置 DEEPSEEK_API_KEY",
        )

    prompt = _build_prompt(question, usable)
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com", timeout=timeout)
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": _SYSTEM},
                      {"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=700,
        )
        raw = resp.choices[0].message.content or ""
        parsed = _parse_json(raw)
    except Exception as e:  # 网络/超时/解析失败统一降级
        return _result(question, "已检索到相关依据（生成失败，请查看下方片段）。",
                       usable, grounded=True, insufficient=False,
                       degraded=True, degraded_reason=f"生成失败：{type(e).__name__}")

    if parsed.get("insufficient") or not parsed.get("answer"):
        return _result(question, REFUSAL, [], grounded=False, insufficient=True,
                       degraded=False, degraded_reason="模型判断片段不足")

    cited = _valid_citations(parsed.get("citations") or [], len(usable))
    if not cited:
        return _result(question, REFUSAL, [], grounded=False, insufficient=True,
                       degraded=False, degraded_reason="回答未给出有效引用")

    return _result(question, parsed["answer"].strip(), [usable[i - 1] for i in cited],
                   grounded=True, insufficient=False, degraded=False)


# ------------------------------------------------------------------ #
def _build_prompt(question: str, hits: List[Dict]) -> str:
    blocks = []
    for i, h in enumerate(hits, 1):
        head = f"[{i}] 《{h['source_title']}》"
        if h.get("article"):
            head += f" {h['article']}"
        if h.get("doc_no"):
            head += f"（{h['doc_no']}）"
        head += f"｜类型：{h.get('text_kind_cn', h.get('text_kind'))}"
        if h.get("context"):
            head += f"｜上下文：{h['context']}"
        blocks.append(f"{head}\n内容：{h['text']}")
    return "【片段】\n" + "\n\n".join(blocks) + f"\n\n【问题】\n{question}\n\n请输出 JSON。"


def _parse_json(text: str) -> Dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {}


def _valid_citations(citations, n: int) -> List[int]:
    out = []
    for c in citations:
        try:
            i = int(c)
        except (TypeError, ValueError):
            continue
        if 1 <= i <= n and i not in out:
            out.append(i)
    return out


def _result(question: str, answer: str, sources: List[Dict], *,
            grounded: bool, insufficient: bool, degraded: bool,
            degraded_reason: str = "") -> Dict:
    return {
        "question": question,
        "answer": answer,
        "grounded": grounded,
        "insufficient": insufficient,
        "citations": [_citation(s) for s in sources],
        "mode": "ADVISORY",
        "degraded": degraded,
        "degraded_reason": degraded_reason,
    }


def _citation(hit: Dict) -> Dict:
    return {
        "chunk_id": hit["chunk_id"],
        "source_title": hit["source_title"],
        "doc_no": hit.get("doc_no"),
        "article": hit.get("article"),
        "quote": hit["text"],
        "text_kind": hit["text_kind"],
        "text_kind_cn": hit.get("text_kind_cn"),
        "official_url": hit.get("official_url"),
        "verified_at": hit.get("verified_at"),
        "via": hit.get("via", "query"),
    }

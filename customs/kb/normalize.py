"""查询归一化与同义词扩展。

解决的问题（实测数据支撑）：
    问："收款信息是不是必须上传"      法规原文："不再要求传输收款单"
    → BM25 按词项匹配，两个词面不重叠，直接搜召回为 0。

这叫词面鸿沟（lexical gap），在法规这种术语固定的文本里尤其明显。
4,000 字的小语料上，补齐同义词表的成本远低于引入 Embedding 模型，
而且效果可解释、可审计、可写进答辩材料。

注意：扩展只作用于检索，不改变用户问题，也不改变引用原文。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Dict, List

from .tokenizer import tokenize

# 同义词组：第一个是规范词，其余是业务口语 / 简称 / 英文写法
SYNONYM_GROUPS: Dict[str, List[str]] = {
    "收款单": ["收款信息", "支付凭证", "收款凭证", "收汇凭证", "支付单证", "收款回执", "收款资料"],
    "汇总申报": ["汇总报送", "月度汇总", "次月汇总", "汇总清单"],
    "滞报金": ["超期申报", "逾期申报", "报关逾期", "迟报关", "迟了报关", "报关迟了", "报关晚了", "晚报关", "超期报关"],
    "出口退运": ["退运", "退货", "退运货物"],
    "锂电池": ["充电宝", "移动电源", "power bank", "锂电", "电池"],
    "禁限管制": ["管制", "禁限", "出口管制"],
    "申报要素": ["申报项", "要素填报", "要素申报"],
    "申报": ["报关", "通关申报", "报验"],
    "装货": ["装货前", "装货时", "装船前", "装箱前"],
    "归类": ["税号", "hs编码", "hs code", "商品编码", "归类编码"],
    "三单": ["订单", "支付单", "物流单"],
    "海外仓": ["海外仓储", "境外仓"],
    "知识产权": ["品牌授权", "商标授权", "ip授权"],
    "危险品": ["危险货物", "危品", "dg货物"],
    "原产地证": ["产地证", "原产地证明", "form e", "co证书"],
    "清单核放": ["清单放行", "核放"],
    "单一窗口": ["国际贸易单一窗口"],
    "规范申报": ["规范填制", "申报规范"],
    "计税依据": ["按什么价格", "计税价格", "价格计算", "计价基础", "离岸价", "fob", "到岸价", "cif"],
}

# 反向索引：别名 -> 整组词
_ALIAS_INDEX: Dict[str, List[str]] = {}
for _canonical, _aliases in SYNONYM_GROUPS.items():
    for _m in [_canonical] + _aliases:
        _ALIAS_INDEX.setdefault(_m.lower(), [])
        if _canonical not in _ALIAS_INDEX[_m.lower()]:
            _ALIAS_INDEX[_m.lower()].append(_canonical)

_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """全角转半角、统一小写、压缩空白。"""
    s = unicodedata.normalize("NFKC", text or "")
    return _WS_RE.sub(" ", s).strip().lower()


def expand_query(query: str) -> Dict[str, object]:
    """返回扩展后的检索词与新增词，供接口透出（可解释性）。"""
    q = normalize(query)
    base = tokenize(q)

    added: List[str] = []
    seen = set(base)
    for alias, canonicals in _ALIAS_INDEX.items():
        if alias and alias in q:
            for canonical in canonicals:
                for t in tokenize(canonical):
                    if t not in seen:
                        seen.add(t)
                        added.append(t)

    return {"tokens": base + added, "expanded": added}

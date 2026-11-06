"""中文分词层：jieba 优先，缺失时降级为字符 bigram。

为什么单独做一层：
- 中文 BM25 必须先分词，否则 "三单" "汇总申报" 会被拆成单字，检索质量崩掉；
- jieba 是可选的，没装时不能阻断启动，所以保留零依赖的 bigram 降级路径；
- 业务术语加入自定义词典，避免领域词被错误切分。
"""
from __future__ import annotations

import re
from typing import List

# 业务术语：不加词典时 "三单"→"三"/"单"、"汇总申报"→"汇总"/"申报" 会丢语义
DOMAIN_WORDS = [
    "9610", "9710", "9810", "跨境电商", "三单", "收款单", "报关单", "报关企业",
    "收发货人", "进出口货物收发货人", "受委托的报关企业", "清单核放", "汇总申报",
    "汇总统计", "离岸价", "到岸价", "出口退税", "出口退运", "海外仓", "滞报金",
    "禁限管制", "管制识别码", "申报要素", "规范申报", "归类总规则", "从后归类",
    "知识产权", "预裁定", "危险品", "锂电池", "UN38.3", "MSDS", "单一窗口",
    "监管条件", "检验检疫", "成交方式", "原产地证", "许可证件", "载货清单",
    "舱单", "提运单", "装箱清单", "代理报关", "风险预判", "分流等级",
]

# 极简停用词：只去掉无检索价值的虚词，保留"不""未""禁止"等否定词
_STOP = set("的 了 和 与 及 或 等 在 对 为 是 有 把 被 就 而 之 及其".split())

_CJK_RUN = re.compile(r"[a-z0-9\.]+|[^\x00-\x7f]+", re.I)
_CJK_ONLY = re.compile(r"[^\x00-\x7f]")

_HAS_JIEBA = False
try:  # pragma: no cover - 环境相关
    import jieba

    jieba.setLogLevel(20)
    for _w in DOMAIN_WORDS:
        jieba.add_word(_w)
    _HAS_JIEBA = True
except ImportError:  # 降级：字符 bigram
    jieba = None  # type: ignore


def backend() -> str:
    """当前分词后端，供 /api/health 与答辩说明使用。"""
    return "jieba" if _HAS_JIEBA else "bigram"


def tokenize(text: str) -> List[str]:
    """把文本切成检索词。jieba 缺失时使用汉字二元组。"""
    if not text:
        return []
    text = text.lower()
    if _HAS_JIEBA:
        tokens = [t.strip() for t in jieba.lcut(text)]  # type: ignore[union-attr]
        return [t for t in tokens if t and t not in _STOP and not t.isspace()]
    return _bigram(text)


def _bigram(text: str) -> List[str]:
    """零依赖降级：英文/数字整体保留，中文按二字滑窗切分。"""
    out: List[str] = []
    for run in _CJK_RUN.findall(text):
        if run.isascii():
            out.append(run)
            continue
        if len(run) == 1:
            out.append(run)
            continue
        out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return [t for t in out if t not in _STOP]


def has_cjk(text: str) -> bool:
    return bool(_CJK_ONLY.search(text or ""))

"""BM25 排序：中文分词、型号与版本号保留、稳定同分。

只对正文计分。BM25 使用正 IDF：

    idf = log(1 + (N - df + 0.5) / (df + 0.5))

因此常见词权重仍为正，不会把命中结果压到未命中结果之后。
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

import jieba

K1 = 1.5
B = 0.75

# 仅删除已识别的表情标签。技术讨论里的 [ROCm]、[显存] 也需要参与检索。
_EMOTICON = re.compile(
    r"\[(?:doge|笑哭|星星眼|滑稽|呲牙|捂脸|吃瓜|囧|大哭|点赞|打call|妙啊|辣眼睛|喜极而泣"
    r"|脱单doge|热词系列_[^\[\]\n]{1,24})(?:_[^\[\]\n]{1,16})?\]"
)
# 中文片段与英文/数字片段分开切；型号 PAM4、版本 6.3.1 先作为整体保留。
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_ALNUM_RUN = re.compile(r"[a-z0-9]+(?:[._]\d+)*", re.IGNORECASE)
_ALNUM_PARTS = re.compile(r"([a-z]*)(\d+(?:[._]\d+)*)([a-z]*)", re.IGNORECASE)
# “RTX 4090”额外生成 rtx4090，用户省略空格时也能命中。
_MODEL_PAIR = re.compile(r"([a-z]{2,10})[\s._-]*(\d{2,5}[a-z]{0,3})", re.IGNORECASE)

_STOPWORDS = frozenset(
    {
        "的",
        "了",
        "是",
        "在",
        "我",
        "你",
        "他",
        "她",
        "它",
        "们",
        "这",
        "那",
        "吗",
        "呢",
        "吧",
        "啊",
        "就",
        "都",
        "而",
        "及",
        "与",
        "或",
        "和",
        "也",
        "还",
        "有",
        "个",
        "一个",
        "这个",
        "那个",
        "什么",
        "怎么",
        "the",
        "a",
        "an",
        "of",
        "to",
        "and",
        "or",
        "is",
        "are",
    }
)
_PUNCTUATION = frozenset("，。！？；：、,.!?;:()（）[]【】\"'“”‘’")


def _is_noise(token: str) -> bool:
    return token in _STOPWORDS or all(ch in _PUNCTUATION for ch in token)


def _split_alnum(chunk: str) -> list[str]:
    """保留整体型号，只在必要时补充可检索的字母/数字片段。

    PAM4 保留为 pam4 和 pam，不产出单字符 4，避免“4”命中所有带 4 的文本。
    ROCm6.3.1 则补充 rocm 与 6.3.1；RTX4090 补充 rtx 与 4090。
    """
    tokens = [chunk]
    match = _ALNUM_PARTS.fullmatch(chunk)
    if match is None:
        return tokens
    prefix, number, suffix = match.groups()
    if not number:
        return tokens

    def add(token: str) -> None:
        if token and token not in tokens:
            tokens.append(token)

    if len(prefix) >= 2:
        add(prefix)
    digits_only = number.replace(".", "").replace("_", "")
    # 单字符数字片段不单独入索引，否则 PAM4 会让查询 “4” 命中所有含 4 的文本。
    if len(digits_only) >= 2 or "." in number or "_" in number:
        add(number)
    if len(suffix) >= 2:
        add(suffix)
    return tokens


def tokenize(text: str) -> tuple[str, ...]:
    """返回归一化词元；正文由服务层按候选集缓存，不做全局正文缓存。"""
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    normalized = _EMOTICON.sub(" ", normalized)

    tokens: list[str] = []
    for match in re.finditer(f"(?:{_CJK_RUN.pattern})|(?:{_ALNUM_RUN.pattern})", normalized):
        piece = match.group(0)
        if _CJK_RUN.fullmatch(piece):
            for word in jieba.lcut(piece):
                word = word.strip()
                if word and not _is_noise(word):
                    tokens.append(word)
        else:
            for token in _split_alnum(piece):
                if token and not _is_noise(token):
                    tokens.append(token)

    for match in _MODEL_PAIR.finditer(normalized):
        joined = match.group(1) + match.group(2)
        if not _is_noise(joined) and joined not in tokens:
            tokens.append(joined)
    return tuple(tokens)


def query_terms(query: str) -> list[str]:
    """查询词按首次出现顺序去重，用于输出 ranking.query_terms。"""
    return list(dict.fromkeys(tokenize(query)))


@dataclass(frozen=True)
class Match:
    index: int
    score: float
    terms: tuple[str, ...]


class BM25:
    """一次构建、多次查询的 BM25 索引。``documents`` 为已分词正文。"""

    def __init__(self, documents: Sequence[Sequence[str]]):
        self.documents = [tuple(doc) for doc in documents]
        self.lengths = [len(doc) for doc in self.documents]
        self.total = len(self.documents)
        self.avg_length = (sum(self.lengths) / self.total) if self.total else 0.0
        self.frequencies: list[dict[str, int]] = []
        document_frequency: dict[str, int] = {}
        for doc in self.documents:
            counts: dict[str, int] = {}
            for token in doc:
                counts[token] = counts.get(token, 0) + 1
            self.frequencies.append(counts)
            for token in counts:
                document_frequency[token] = document_frequency.get(token, 0) + 1
        self.document_frequency = document_frequency

    def _idf(self, term: str) -> float:
        df = self.document_frequency.get(term, 0)
        if df == 0:
            return 0.0
        return math.log(1.0 + (self.total - df + 0.5) / (df + 0.5))

    def search(self, query: str, *, limit: int = 20, offset: int = 0) -> tuple[list[Match], int]:
        """返回当前页命中与命中总数；原始索引顺序用于稳定同分排序。"""
        terms = query_terms(query)
        if not terms or not self.documents:
            return [], 0
        matches: list[Match] = []
        for index, counts in enumerate(self.frequencies):
            length = self.lengths[index]
            score = 0.0
            matched: list[str] = []
            for term in terms:
                tf = counts.get(term, 0)
                if not tf:
                    continue
                idf = self._idf(term)
                if idf <= 0:
                    continue
                denominator = tf + K1 * (
                    1.0 - B + B * (length / self.avg_length if self.avg_length else 0.0)
                )
                score += idf * (tf * (K1 + 1.0)) / denominator
                matched.append(term)
            if score > 0:
                matches.append(Match(index=index, score=score, terms=tuple(matched)))
        matches.sort(key=lambda item: (-item.score, item.index))
        total = len(matches)
        if limit is None:
            return matches[offset:], total
        return matches[offset : offset + limit], total

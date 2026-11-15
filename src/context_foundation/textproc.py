"""keyword 検索(BM25)のための語の分け方。索引と質問で同じ分け方を使う。

- 識別子は、そのままの語と、camelCase・snake_case・数字の境目で分けた語の両方を出す
  (`GetBufferPool` → getbufferpool, get, buffer, pool)
- 英語の語は小文字にし、複数形の s だけを落とす(buffers → buffer)
- 日本語など区切りの無い文字は、2 文字ずつの組(bigram)にする(「描画処理」→ 描画, 画処, 処理)
- 質問の側では、英語のよくある語(the, where など)と、ひらがなを含む組を落とす。残るのは、漢字と
  カタカナだけの組(「処理」「バッ」など)。ひらがなを含む組は、助詞や語尾がほとんどで、日本語の文書の
  どこにでも当たってしまうため

出す語は、英小文字・数字・_ と、区切りの無い文字だけでできている。FTS5 の索引には、語を空白で
区切った文字列として渡す(tokenize は unicode61 に `_` を足したもの)。
"""
from __future__ import annotations

import re

# 区切りの無い文字: ひらがな・カタカナ・漢字・半角カタカナ・々(コードポイントの範囲で書く)
_CJK_RANGES = "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f\u3005"
# 語のもと: 識別子(英数字と _)か、区切りの無い文字の連なりか、そのほかの文字の語
_RUN = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+(?:\.[0-9]+)?[A-Za-z0-9_]*|[" + _CJK_RANGES + r"]+|[^\W\d_]+")
_CJK = re.compile("[" + _CJK_RANGES + "]")
_HIRAGANA = re.compile("[\u3041-\u309f]")
# 識別子を分ける: 大文字の連なり(略語)・先頭大文字の語・小文字の語・数字
_PARTS = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")

_STOPWORD_LIST = """
a an the of in on at to for from by with and or not is are was were be been do does did how what where which who
when why can could should would it its this that these those i we you they there here as if then than so such
into over under about any all each some no yes via per vs
""".split()

MAX_TOKEN_LEN = 64


def _stem(word: str) -> str:
    """英語の語尾を少しだけそろえる(複数形の s、-ing、-ed、語末の e)。

    initialize / initializes / initialized / initializing を同じ語にするためのもので、語形の解析ではない。
    索引と質問で同じ規則を使うので、切り方が荒くても一致は保たれる。
    """
    if not word.isascii() or not word.isalpha():
        return word
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-3] + "y" if word.endswith("ies") and len(word) > 4 else word[:-1]
    if word.endswith("ing") and len(word) >= 7:
        word = word[:-3]
    elif word.endswith("ed") and len(word) >= 6 and not word.endswith("eed"):
        word = word[:-2]
    if word.endswith("e") and len(word) > 4:
        word = word[:-1]
    return word


# 語尾をそろえた後の形(where → wher)でも落とせるように、両方を入れておく
STOPWORDS = frozenset(_STOPWORD_LIST) | frozenset(_stem(w) for w in _STOPWORD_LIST)


def _identifier_tokens(run: str) -> list[str]:
    out: list[str] = []
    whole = run.strip("_").lower()
    if not whole:
        return out
    pieces: list[str] = []
    for seg in run.split("_"):
        pieces.extend(m.group(0).lower() for m in _PARTS.finditer(seg))
    if len(pieces) > 1:
        if len(whole) <= MAX_TOKEN_LEN:
            out.append(whole)                       # 識別子そのまま(完全一致を強く効かせる)
        out.extend(_stem(p) for p in pieces if len(p) >= 2 or p.isdigit())
    else:
        out.append(_stem(whole) if len(whole) <= MAX_TOKEN_LEN else whole[:MAX_TOKEN_LEN])
    return out


def _cjk_tokens(run: str) -> list[str]:
    if len(run) == 1:
        return [run]
    return [run[i:i + 2] for i in range(len(run) - 1)]


def tokenize(text: str) -> list[str]:
    """索引に入れる語の並び(重なりあり・出現順)。"""
    out: list[str] = []
    for m in _RUN.finditer(text):
        run = m.group(0)
        if _CJK.match(run):
            out.extend(_cjk_tokens(run))
        elif run[0].isascii():
            out.extend(_identifier_tokens(run))
        else:
            out.append(run.lower()[:MAX_TOKEN_LEN])
    return out


def query_tokens(text: str) -> list[str]:
    """質問から、検索に使う語を取り出す(重なりを除く・出現順)。"""
    seen: set[str] = set()
    out: list[str] = []
    for t in tokenize(text):
        if t in STOPWORDS or (len(t) < 2 and t.isascii()) or _HIRAGANA.search(t):
            continue
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def fts_text(text: str) -> str:
    """FTS5 に渡す、空白で区切った語の並び。"""
    return " ".join(tokenize(text))


def fts_query(tokens: list[str]) -> str:
    """FTS5 の MATCH に渡す式(どれかの語を含む)。語は英数字と _ と CJK だけなので、引用符で囲めば安全。"""
    return " OR ".join('"' + t.replace('"', "") + '"' for t in tokens)

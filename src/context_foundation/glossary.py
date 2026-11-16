"""用語の対応表: 質問に出てくる語に、コードと文書で使われている語を足して、検索を広げる。

日本語だけの質問では、英語で書かれたコードに keyword がほとんど当たらず、意味検索も弱い(日本語に英語の語を
足すと、どちらも大きく良くなる。docs/design.md の「日本語の質問」)。そこで、検索の前に、対応表で見つかった
語の訳を質問の後ろに足す。質問そのものは変えない(足した語は、結果の note で知らせる)。

対応表は 2 つあり、作業フォルダのものを先に当てる。

  作業フォルダの対応表  .cf/glossary.tsv(設定の [search] glossary で場所を変えられる)。エンジンに固有の語や、
                        別のエンジンの用語との対応(例: `AudioMixer<TAB>mixer bus volume`)を、チームで書く
  既定の対応表          このパッケージの glossary_ja.tsv。日本語の一般の語(描画 → render draw など)だけ。
                        [search] builtin_glossary = false で使わない

書き方は 1 行 1 語: `<語><TAB><足す語(空白区切り)>`。`#` で始まる行は注釈。書き方の誤った行は飛ばし、
結果の note で知らせる。照合の前に、質問と語の両方を NFKC で正規化する(半角カナ・全角英数をそろえる)。

- 長い語を先に当て、当たった所に重なる短い語は当てない
- 英字の語は、単語の切れ目でだけ当たり、大文字と小文字を区別しない
- カタカナだけの語は、前後がカタカナでないときだけ当たる(「ロック」は「ブロック」に、「ログ」は
  「ダイアログ」に当たらない)。語の後ろの長音「ー」だけは許す(「スケジューラ」は「スケジューラー」に当たる)
- 漢字を含む語は、質問の中のどこにあっても当たる
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

MAX_ADDED_TERMS = 24           # 足す語の上限(多すぎると、質問の意味が薄まる)
_BOM = chr(0xFEFF)
_LONG_VOWEL = chr(0x30FC)      # ー


class GlossaryError(Exception):
    """対応表を読めない(ファイルが無い・読めない)。文面は利用者に見せる。"""


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def parse(text: str, where: str, problems: list[str] | None = None) -> list[tuple[str, list[str]]]:
    """対応表を読む。書き方の誤った行(タブが無い・足す語が無い)は飛ばし、problems に場所を足す。"""
    entries: list[tuple[str, list[str]]] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip(_BOM).rstrip("\r\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        term, tab, adds = line.partition("\t")
        term, words = normalize(term.strip()), normalize(adds).split()
        if not tab or not term or not words:
            if problems is not None:
                problems.append(f"{where}:{n}")
            continue
        entries.append((term, words))
    return entries


def _is_kana(c: str) -> bool:
    return "゠" <= c <= "ヿ"          # カタカナと長音(ー)・中黒(・)


def _all_kana(term: str) -> bool:
    return bool(term) and all(_is_kana(c) for c in term)


def _kana_bounded(query: str, s: int, e: int) -> bool:
    """カタカナだけの語が、前後のカタカナに続いていないか(語の後ろの長音 ー は許す)。"""
    if s > 0 and _is_kana(query[s - 1]):
        return False
    while e < len(query) and query[e] == _LONG_VOWEL:
        e += 1
    return e >= len(query) or not _is_kana(query[e])


# 既定の対応表を当てるのは、質問の英語の語がこれより少ないときだけ(エージェントがすでに英語の語を足した
# 質問に、さらに一般の語を足すと、かえって順位が下がった。docs/design.md の「日本語の質問」)。
# 数えるのは、空白で区切られた、小文字を含む英字の語だけ(GPU のような略語、std::vector や
# src/render/buffer.cpp のような名前やパスは数えない)
BUILTIN_MAX_ASCII_WORDS = 3
_ENGLISH_WORD = re.compile(r"^[A-Za-z][a-z]+(?:[-'][a-z]+)?$")


def english_words(query: str) -> list[str]:
    from .textproc import STOPWORDS
    words = (w.strip(".,;:!?()[]{}\"'`") for w in query.split())       # 文の区切りの記号は外して数える
    return [w for w in words if _ENGLISH_WORD.match(w) and w.lower() not in STOPWORDS]


@dataclass
class Glossary:
    entries: list[tuple[str, list[str]]] = field(default_factory=list)     # 先に当てるものから
    sources: list[str] = field(default_factory=list)
    builtin_from: int = -1        # entries のうち、既定の対応表の始まり(無ければ -1)
    problems: list[str] = field(default_factory=list)       # 飛ばした行の場所(ファイル:行)

    @classmethod
    def load(cls, workspace_file: Path | None, builtin: bool) -> "Glossary":
        g = cls()
        if workspace_file is not None and workspace_file.is_file():
            try:
                text = workspace_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                raise GlossaryError(f"用語の対応表を読めません({workspace_file}): {e}") from None
            g.entries += parse(text, str(workspace_file), g.problems)
            g.sources.append(str(workspace_file))
        if builtin:
            text = resources.files(__package__).joinpath("glossary_ja.tsv").read_text(encoding="utf-8")
            g.builtin_from = len(g.entries)
            g.entries += parse(text, "glossary_ja.tsv", g.problems)
            g.sources.append("builtin")
        return g

    def expand(self, query: str) -> list[str]:
        """質問に足す語(重なりを除く・当たった順)。当たらなければ空。"""
        query = normalize(query)
        lower = query.lower()
        taken: list[tuple[int, int]] = []
        found: list[tuple[int, list[str]]] = []
        n = len(self.entries)
        if self.builtin_from >= 0 and len(english_words(query)) >= BUILTIN_MAX_ASCII_WORDS:
            n = self.builtin_from                     # 英語の語が足りている質問には、既定の対応表を当てない
        # 長い語を先に当てる(同じ長さなら、作業フォルダの対応表を先に)。ただし見つかった順に並べ直す
        order = sorted(range(n), key=lambda i: (-len(self.entries[i][0]), i))
        for i in order:
            term, words = self.entries[i]
            if term.isascii():
                pattern = r"(?<![A-Za-z0-9_])" + re.escape(term.lower()) + r"(?![A-Za-z0-9_])"
                spans = [m.span() for m in re.finditer(pattern, lower)]
            else:
                kana = _all_kana(term)
                spans, start = [], 0
                while (pos := query.find(term, start)) >= 0:
                    if not kana or _kana_bounded(query, pos, pos + len(term)):
                        spans.append((pos, pos + len(term)))
                    start = pos + 1
            for s, e in spans:
                if any(s < te and ts < e for ts, te in taken):
                    continue
                taken.append((s, e))
                found.append((s, words))
                break
        present = set(re.findall(r"[a-z0-9_]+", lower))
        out: list[str] = []
        for _, words in sorted(found, key=lambda x: x[0]):
            for w in words:
                lw = w.lower()
                if lw not in present and w not in out:
                    out.append(w)
                    present.add(lw)
        return out[:MAX_ADDED_TERMS]


def resolve_workspace_file(cf_dir: Path, setting: str) -> Path | None:
    """作業フォルダの対応表の場所。設定が空なら .cf/glossary.tsv(無ければ None)。"""
    if setting:
        p = Path(setting)
        p = p if p.is_absolute() else cf_dir / p
        if not p.is_file():
            raise GlossaryError(f"[search] glossary のファイルがありません: {p}")
        return p
    p = cf_dir / "glossary.tsv"
    return p if p.is_file() else None

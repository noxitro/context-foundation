"""用語の対応表: 質問に出てくる語に、コードと文書で使われている語を足して、検索を広げる。

日本語だけの質問では、英語で書かれたコードに keyword がほとんど当たらず、意味検索も弱い(日本語に英語の語を
足すと、どちらも大きく良くなる。docs/design.md の「日本語の質問」)。そこで、検索の前に、対応表で見つかった
語の訳を質問の後ろに足す。質問そのものは変えない(足した語は、結果の note で知らせる)。

対応表は 2 つあり、作業フォルダのものを先に当てる。

  作業フォルダの対応表  .cf/glossary.tsv(設定の [search] glossary で場所を変えられる)。エンジンに固有の語や、
                        別のエンジンの用語との対応(例: `AudioMixer<TAB>mixer bus volume`)を、チームで書く
  既定の対応表          このパッケージの glossary_ja.tsv。日本語の一般の語(描画 → render draw など)だけ。
                        [search] builtin_glossary = false で使わない

書き方は 1 行 1 語: `<語><TAB><足す語(空白区切り)>`。`#` で始まる行は注釈。日本語の語は、質問の中のどこに
あっても当たる(長い語を先に当て、当たった所に重なる短い語は当てない)。英字の語は、単語の切れ目でだけ当たり、
大文字と小文字を区別しない。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

MAX_ADDED_TERMS = 24           # 足す語の上限(多すぎると、質問の意味が薄まる)


class GlossaryError(Exception):
    """対応表の誤り。文面は利用者に見せる。"""


def parse(text: str, where: str) -> list[tuple[str, list[str]]]:
    entries: list[tuple[str, list[str]]] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip("﻿").rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "\t" not in line:
            raise GlossaryError(f"{where} の {n} 行目: 語と足す語を、タブで区切ってください: {line[:60]!r}")
        term, _, adds = line.partition("\t")
        term, words = term.strip(), adds.split()
        if term and words:
            entries.append((term, words))
    return entries


# 既定の対応表を当てるのは、質問の英字の語がこれより少ないときだけ(エージェントがすでに英語の語を足した
# 質問に、さらに一般の語を足すと、かえって順位が下がった。docs/design.md の「日本語の質問」)
BUILTIN_MAX_ASCII_WORDS = 3


@dataclass
class Glossary:
    entries: list[tuple[str, list[str]]] = field(default_factory=list)     # 先に当てるものから
    sources: list[str] = field(default_factory=list)
    builtin_from: int = -1        # entries のうち、既定の対応表の始まり(無ければ -1)

    @classmethod
    def load(cls, workspace_file: Path | None, builtin: bool) -> "Glossary":
        g = cls()
        if workspace_file is not None and workspace_file.is_file():
            try:
                text = workspace_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                raise GlossaryError(f"用語の対応表を読めません({workspace_file}): {e}") from None
            g.entries += parse(text, str(workspace_file))
            g.sources.append(str(workspace_file))
        if builtin:
            text = resources.files(__package__).joinpath("glossary_ja.tsv").read_text(encoding="utf-8")
            g.builtin_from = len(g.entries)
            g.entries += parse(text, "glossary_ja.tsv")
            g.sources.append("builtin")
        return g

    def expand(self, query: str) -> list[str]:
        """質問に足す語(重なりを除く・当たった順)。当たらなければ空。"""
        lower = query.lower()
        taken: list[tuple[int, int]] = []
        found: list[tuple[int, list[str]]] = []
        n = len(self.entries)
        if self.builtin_from >= 0:
            from .textproc import STOPWORDS
            ascii_words = [w for w in re.findall(r"[a-z][a-z0-9_]+", lower) if w not in STOPWORDS]
            if len(ascii_words) >= BUILTIN_MAX_ASCII_WORDS:
                n = self.builtin_from                 # 英語の語が足りている質問には、既定の対応表を当てない
        # 長い語を先に当てる(同じ長さなら、作業フォルダの対応表を先に)。ただし見つかった順に並べ直す
        order = sorted(range(n), key=lambda i: (-len(self.entries[i][0]), i))
        for i in order:
            term, words = self.entries[i]
            if term.isascii():
                pattern = r"(?<![A-Za-z0-9_])" + re.escape(term.lower()) + r"(?![A-Za-z0-9_])"
                spans = [m.span() for m in re.finditer(pattern, lower)]
            else:
                spans, start = [], 0
                while (pos := query.find(term, start)) >= 0:
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

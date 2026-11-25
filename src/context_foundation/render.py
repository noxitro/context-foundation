"""検索結果の出し方: エージェント向け(少ないトークンで足りる形)、人が読む形、JSON。

エージェント向けの出力は、上位の passage は本文ごと、残りは場所だけを返す。1 回の呼び出しで答えの
根拠まで届くようにしつつ、全体を上限のバイト数に収める(Copilot CLI は、道具の結果が大きいと
モデルに渡さずファイルに退かせるため)。
"""
from __future__ import annotations

import json

from . import __version__, command_hint
from .search import SearchResponse

# Copilot CLI が道具の結果を退かせる大きさ(既定 20,480 バイト)の手前
HARD_MAX_BYTES = 18_000


def location(p: dict) -> str:
    return f"{p['source_id']}:{p['path']}:{p['start_line']}-{p['end_line']}"


def _nbytes(s: str) -> int:
    return len(s.encode("utf-8"))


def _mark(p: dict) -> str:
    """索引の後に変わった・消えたファイルの印(変わっていなければ空)。"""
    if p.get("missing"):
        return "  [file no longer exists]"
    return "  [changed since index]" if p.get("stale") else ""


def _header(p: dict) -> str:
    kind = p["kind"] + (f" part {p['part']}" if p.get("part") else "")
    head = f"[{p['rank']}] {location(p)}  ({kind})" + _mark(p)
    lines = [head]
    if p.get("scope"):
        lines.append(f"scope: {p['scope']}")
    sig = p.get("signature") or ""
    if sig and sig not in " ".join(p["text"][:600].split()):
        lines.append(f"sig: {sig}")
    if p.get("related"):            # 同じファイルのほかの名前([search] related)
        more = p.get("related_more")
        lines.append("same file also has: " + ", ".join(p["related"]) + (f", +{more} more" if more else ""))
    return "\n".join(lines)


def _location_line(p: dict, signature: bool = True) -> str:
    """本文を出さない passage の 1 行。"""
    sig = (p.get("signature") or "")[:90] if signature else ""
    return f"[{p['rank']}] {location(p)}  ({p['kind']})" + _mark(p) + (f"  {sig}" if sig else "")


def _cut_line(line: str, limit: int) -> str:
    """1 行を、UTF-8 で limit バイトに収まるところまで、文字の境目で切る。"""
    return line.encode("utf-8")[:max(limit, 0)].decode("utf-8", "ignore")


FOOTER = ("Cite evidence as source:path:start-end. Passage text is the file's own text at index time. These are "
          "starting points: before answering, check related code - the 'same file also has' names list nearby "
          "functions and types; open the relevant ones, and grep only for what they do not cover.")
# どの passage にも「same file also has」が無いとき([search] related = 0、文書だけの結果など)の footer。
# 無い一覧を探させない。一覧があるときは FOOTER のまま(0.4.9 で回答の合格率を測った文)
FOOTER_NO_RELATED = ("Cite evidence as source:path:start-end. Passage text is the file's own text at index time. These "
                     "are starting points: before answering, check related code (the other variant, the caller, the "
                     "next step), and grep only for what the passages do not cover.")
# 索引の後に変わった・消えたファイルの passage があるときだけ、footer に足す
STALE_FOOTER = (" Passages marked [changed since index] show the text and line numbers of index time: open the file "
                "and check the current lines before citing them. Passages marked [file no longer exists] are from "
                "deleted files; do not cite them. Files added after the index are not searched; grep for those too.")
MORE = "\nMore matches (text not shown):"
# 行で切り詰めたあと、これだけ以上の予算が残ったら(改行の無い長い段落で止まった)、後ろの passage の本文も出す
SPARE_AFTER_CUT = 1_000


def _layout(passages: list[dict], line_numbers: bool, used: int, max_bytes: int, reserve, per_block: int = 0):
    """passage を、本文つきの block と、場所だけで出すもの(rest)に分ける。

    reserve(残りの passage) は、場所だけの行のために残しておくバイト数。per_block は、block ごとに足す改行の分
    (1 回目は 0.3 と同じ見積もりで 0。上限を超えたときの 2 回目からは、実際の大きさで数える)。2 回目からは、
    先に rest に回した消えたファイルの行の分も残しておく(1 回目は 0.3 と同じく、後ろの passage の分だけ)。
    """
    blocks: list[str] = []
    rest: list[dict] = []
    for i, p in enumerate(passages):
        if p.get("missing"):            # 消えたファイルの本文は、根拠にならないので出さない
            rest.append(p)
            continue
        header = _header(p)
        text = p["text"]
        if line_numbers:
            text = "\n".join(f"{p['start_line'] + n}| {line}" for n, line in enumerate(text.split("\n")))
        block = f"\n{header}\n```\n{text}\n```"
        res = reserve((rest if per_block else []) + passages[i + 1:])
        if used + _nbytes(block) + per_block + res <= max_bytes:
            blocks.append(block)
            used += _nbytes(block) + per_block
            continue
        room = max_bytes - used - res - _nbytes(header) - 80
        if room >= 400:
            lines = text.split("\n")
            kept, size = [], 0
            for line in lines:
                if size + _nbytes(line) + 1 > room:
                    break
                kept.append(line)
                size += _nbytes(line) + 1
            if kept:
                block = (f"\n{header}\n```\n" + "\n".join(kept)
                         + f"\n```\n(+{len(lines) - len(kept)} more lines, not shown)")
            else:                       # 最初の 1 行だけで入らない(改行の無い長い段落など): 文字の境目で切る
                part = _cut_line(lines[0], room - 60)
                size = _nbytes(part) + 1
                block = f"\n{header}\n```\n{part}\n```\n(line truncated, +{len(lines) - 1} more lines, not shown)"
            blocks.append(block)
            used += _nbytes(block) + per_block
            if room - size >= SPARE_AFTER_CUT:
                continue
            rest.extend(passages[i + 1:])
            break
        rest.extend(passages[i:])
        break
    return blocks, rest


def to_agent(resp: SearchResponse, max_bytes: int = 12_000, line_numbers: bool = False) -> str:
    """エージェントが読む出力(英語)。"""
    max_bytes = max(1_000, min(max_bytes, HARD_MAX_BYTES))
    idx = resp.index
    head = (f'context-foundation search: {len(resp.passages)} passages, mode {resp.mode}, '
            f'index of {idx.get("created_at", "?")} ({idx.get("chunks", 0):,} passages)')
    out = [head]
    out += [f"note: {n}" for n in resp.notes]
    if not resp.passages:
        out.append("No passage matched. Try other words (English terms, identifiers) or search with grep.")
        return "\n".join(out) + "\n"
    footer = FOOTER if any(p.get("related") for p in resp.passages) else FOOTER_NO_RELATED
    stale = sum(1 for p in resp.passages if p.get("stale") or p.get("missing"))
    if stale:
        out.append(f"stale: {stale} of {len(resp.passages)} passages are from files changed or deleted after the "
                   f"index was built ({idx.get('created_at', '?')}).")
        footer += STALE_FOOTER
    if resp.roots:                      # 作業フォルダそのものではない source のルート(ファイルを開けるように)
        footer = ("roots: " + ", ".join(f"{k} = {v}" for k, v in resp.roots.items())
                  + " (open source:path as <root>/<path>; relative roots are under the workspace)\n" + footer)
    used = _nbytes("\n".join(out)) + _nbytes(footer) + 2

    def assemble(blocks: list[str], rest: list[dict], sigs: int | None = None, dropped: int = 0) -> str:
        """出力を組み立てる。場所だけの行は、先頭の sigs 件だけにシグネチャを付ける(None なら全部)。"""
        lines = out + blocks
        if rest or dropped:
            lines.append(MORE)
            lines += [_location_line(p, sigs is None or n < sigs) for n, p in enumerate(rest)]
            if dropped:
                lines.append(f"(+{dropped} more matches, not shown)")
        lines.append("\n" + footer)
        return "\n".join(lines) + "\n"

    def fit(blocks: list[str], rest: list[dict]) -> str | None:
        """上限に収まるまで、後ろの行からシグネチャを落とす。それでも収まらなければ None。"""
        for sigs in range(len(rest), -1, -1):
            text = assemble(blocks, rest, sigs if sigs < len(rest) else None)
            if _nbytes(text) <= max_bytes:
                return text
        return None

    def last_resort(blocks: list[str], rest: list[dict]) -> str:
        """後ろの場所の行から落とす(件数は出す。消えたファイルの行も、ほかの行と同じく落とす)。本文の block は
        落とさない: 3 回目は block を 1 バイトまで数えて入れるので、固定の部分に `More matches` の見出しと件数の行
        (合わせて 60 バイトほど)を足した大きさが上限を超えるとき(上限の最小近くで、ルートの行がとても長い)の
        ほかは収まる。"""
        dropped = 0
        text = assemble(blocks, rest, 0)
        while _nbytes(text) > max_bytes and rest:
            rest, dropped = rest[:-1], dropped + 1
            text = assemble(blocks, rest, 0, dropped)
        return text

    # 1 回目は 0.3 と同じ見積もり(場所だけの行を 1 件 150 バイトとみる)。パスが長い・日本語のパスやシグネチャで
    # 上限を超えたら、まず場所だけの行のシグネチャを後ろから落とす(本文は 0.3 と同じまま)
    blocks, rest = _layout(resp.passages, line_numbers, used, max_bytes, lambda r: 150 * len(r))
    text = fit(blocks, rest)
    if text is not None:
        return text
    # それでも超えるなら、場所だけの行の実際の大きさを残して並べ直し、最後は後ろの場所から落とす(件数は出す)。
    # used は、末尾の改行の分だけ実際の固定の部分より 1 少ない。2 回目はこの値のまま(今まで収まっていた出力を
    # 変えないため)で、それでも上限を超えたときだけ、3 回目は 1 バイトまで正しく数えて並べ直す
    def exact(r: list[dict]) -> int:
        return sum(_nbytes(_location_line(q)) + 1 for q in r) + (_nbytes(MORE) + 1 if r else 0)

    for extra in (0, 1):
        blocks, rest = _layout(resp.passages, line_numbers, used + extra, max_bytes, exact, per_block=1)
        text = fit(blocks, rest)
        if text is None and not extra and blocks and any(q.get("missing") for q in rest):
            # 2 回目の 1 バイトの数え違いで入りきらないとき、後ろの行(消えたファイルの行のことがある)を落とす前に、
            # 3 回目で正しく数えて並べ直す(本文の block のほうが 1 バイト分縮む)
            continue
        if text is None:
            text = last_resort(blocks, rest)
        if _nbytes(text) <= max_bytes:
            break
    return text


def to_human(resp: SearchResponse, max_lines: int = 14) -> str:
    """人が読む出力(日本語)。"""
    idx = resp.index
    out = [f"検索: {resp.query}",
           f"方法: {resp.mode}  索引: {idx.get('created_at', '?')}({idx.get('chunks', 0):,} passage)  "
           f"時間: {resp.timings.get('search_ms', 0):.0f} ms"
           + (f"(読み込み {resp.timings['load_ms']:.0f} ms)" if resp.timings.get("load_ms") else "")]
    out += [f"注意: {n}" for n in resp.notes]
    if not resp.passages:
        out.append("見つかりませんでした。別の言葉(英語の用語や識別子)でも試してください。"
                   f"索引({idx.get('created_at', '?')})の後に増えたファイルは、作り直すまで検索に出ません。")
        return "\n".join(out) + "\n"
    for p in resp.passages:
        kind = p["kind"] + (f" {p['part']}" if p.get("part") else "")
        ranks = p.get("ranks") or {}
        via = " / ".join(f"{k} {v} 位" for k, v in ranks.items() if v)
        out.append("")
        mark = "  ※索引の後に消えたファイル" if p.get("missing") else "  ※索引の後に変更あり" if p.get("stale") else ""
        out.append(f"{p['rank']:>2}. {location(p)}  [{kind}]" + mark)
        if p.get("scope"):
            out.append(f"    スコープ: {p['scope']}")
        if p.get("signature"):
            out.append(f"    {p['signature']}")
        out.append(f"    点: {p['score']}" + (f"({via})" if via else ""))
        lines = p["text"].split("\n")
        for n, line in enumerate(lines[:max_lines]):
            out.append(f"    {p['start_line'] + n:>6}| {line}")
        if len(lines) > max_lines:
            out.append(f"          …(あと {len(lines) - max_lines} 行)")
    return "\n".join(out) + "\n"


def to_json(resp: SearchResponse) -> str:
    return json.dumps(resp.to_dict(), ensure_ascii=False, indent=1) + "\n"


def status_agent(st: dict) -> str:
    """索引の状態(英語・短い形)。MCP の status と、エージェントからの `status` で使う。"""
    stats, emb = st.get("stats", {}), st.get("embedding") or {}
    out = [f"context-foundation index: created {st.get('created_at')}, schema v{st.get('schema_version')}, "
           f"tool {st.get('tool_version')}, {st.get('index_bytes', 0) / 1e6:.1f} MB",
           f"passages: {stats.get('chunks', 0):,} from {stats.get('files', 0):,} files "
           f"({stats.get('lines', 0):,} lines)",
           # source のフォルダも出す(source:path のファイルを開けるように。相対なら作業フォルダの中)
           "sources: " + ", ".join(f"{s['id']}" + (f" = {s['folder']}" if s.get("folder") else "")
                                   + f" ({s['files']:,} files, {s['chunks']:,} passages)"
                                   for s in st.get("sources", []))
           + (". Open source:path as <root>/<path>; relative roots are under the workspace."
              if any(s.get("folder") for s in st.get("sources", [])) else ""),
           "modes: " + ", ".join(st.get("modes", []))
           + (f"; embedding model {emb.get('model')} ({emb.get('dim')} dims)" if emb else "; no embeddings")]
    # 索引に入らなかったフォルダ(0.4.0 から meta にある項目。無ければ何も出さない)
    ed = stats.get("excluded_dirs") or {}
    if ed.get("files"):
        out.append(f"not indexed: {ed['folders']:,} folders excluded by default folder names (build, target, "
                   f"packages, ...) hold {ed['files']:,} indexable files, e.g. "
                   + ", ".join(f"{e['path']} ({e['files']:,})" for e in ed.get("examples", [])[:5])
                   + ". Search those with grep; to index one, the user adds a [[source]] for it.")
    dirs = stats.get("skipped_dirs") or {}
    if dirs:
        out.append("folders not walked: " + ", ".join(f"{k} {v:,}" for k, v in sorted(dirs.items()))
                   + (" (to index a linked folder, the user adds a [[source]] rooted at it)"
                      if dirs.get("link_dir") else ""))
    cf = stats.get("chunk_failed") or {}
    if cf.get("count"):
        out.append(f"split by lines after a chunking error: {cf['count']:,} files")
    fr = st.get("freshness")
    if fr:                              # `status --check` のときだけ
        line = f"since the index: {fr['changed']:,} files changed, {fr['added']:,} added, {fr['deleted']:,} deleted"
        ex = [e for k in ("changed", "added", "deleted") for e in fr["examples"][k]][:5]
        if ex:
            line += f" (e.g. {', '.join(ex)}); search shows the index-time state until the index is rebuilt"
        if fr["missing_roots"]:
            line += f"; not checked, source root not found: {', '.join(fr['missing_roots'])}"
        if _other_version(st) and (fr["added"] or fr["deleted"]):
            line += (f"; the index was built by tool {st.get('tool_version')} (this is {__version__}), whose rules for "
                     "which files to index may differ: \"added\" and \"deleted\" may include files that did not change, "
                     "until the index is rebuilt")
        out.append(line)
    return "\n".join(out) + "\n"


def _other_version(st: dict) -> bool:
    """索引を、今と違う版の道具で作ったか(取り込む規則が違うことがある)。"""
    return bool(st.get("tool_version")) and st.get("tool_version") != __version__


def _freshness_human(fr: dict, st: dict) -> list[str]:
    out = [f"  索引の後の変化(今 確かめた。{fr['seconds']} 秒): 変わった {fr['changed']:,}、増えた {fr['added']:,}、"
           f"消えた {fr['deleted']:,}"]
    for key, name in (("changed", "変わった"), ("added", "増えた"), ("deleted", "消えた")):
        if fr["examples"][key]:
            out.append(f"    {name}: {', '.join(fr['examples'][key])}")
    if fr["missing_roots"]:
        out.append(f"    ルートが無いので確かめていない source: {', '.join(fr['missing_roots'])}")
    if _other_version(st) and (fr["added"] or fr["deleted"]):
        out.append(f"    索引は道具の版 {st.get('tool_version')} で作ったものです(今は {__version__})。取り込む規則が版で"
                   "違うことがあるので、増えた・消えたには、変えていないファイルも入ることがあります(作り直すと消えます)")
    if fr["changed"] or fr["added"] or fr["deleted"]:
        out.append(f"    検索に反映するには、もう一度 `{command_hint('index')}`。2 回目からは、変わった分だけ埋め込みます")
    return out


def status_human(st: dict) -> str:
    stats, emb, tm = st.get("stats", {}), st.get("embedding") or {}, st.get("timings", {})
    out = [f"索引: {st.get('index_path')}",
           f"  作成: {st.get('created_at')}  形式の版: {st.get('schema_version')}  道具の版: {st.get('tool_version')}"
           + (f"  コーパスの版: {st['revision']}" if st.get("revision") else ""),
           f"  大きさ: {st.get('index_bytes', 0) / 1e6:.1f} MB",
           f"  ファイル: {stats.get('files', 0):,}  行: {stats.get('lines', 0):,}  passage: {stats.get('chunks', 0):,}",
           f"  使える検索: {' / '.join(st.get('modes', []))}",
           "  埋め込みモデル: " + (f"{emb.get('model')}({emb.get('dim')} 次元、{emb.get('vector_dtype')})" if emb
                              else "なし(keyword だけ)")]
    if emb.get("provider_fallback"):
        # 索引づくりの途中の知らせは、--quiet では出ない。終わりの要約と status で、後からも分かるようにする
        out.append(f"  埋め込みの計算: GPU({emb.get('provider')})の実行が途中で誤りになり、残りを CPU で計算しました")
    for s in st.get("sources", []):
        out.append(f"  source {s['id']}: {s['files']:,} ファイル / {s['chunks']:,} passage  ({s.get('root')})"
                   + (f"(索引を作ったときは {s['indexed_root']})" if s.get("indexed_root") else ""))
    if tm:
        out.append(f"  作成の時間: 合計 {tm.get('total_seconds', 0)} 秒(取り込みと分割 {tm.get('ingest_chunk_seconds', 0)} 秒、"
                   f"埋め込み {tm.get('embed_seconds', 0)} 秒)")
        if tm.get("unembedded_passages"):
            out.append(f"  中身が無く、埋め込んでいない passage: {tm['unembedded_passages']:,}"
                       "(意味検索には出ない。keyword では引ける)")
    if stats.get("by_language"):
        out.append("  言語ごと:")
        for lang, b in sorted(stats["by_language"].items()):
            fb = f"、行で分けた行 {b['fallback_lines']:,}({b['fallback_lines'] / b['lines'] * 100:.1f}%)" \
                if b.get("fallback_lines") and b.get("lines") else ""
            out.append(f"    {lang:<9} {b['files']:>7,} ファイル  {b['lines']:>10,} 行  {b['chunks']:>8,} passage{fb}")
        out.append(f"  構文で分ける言語のうち、解析できず行で分けた割合: {stats.get('syntax_fallback_ratio', 0) * 100:.1f}%")
    if st.get("fallback_files"):
        out.append("  行で分けた行が多いファイル(マクロで文法が崩れていることが多い。設定の [chunk] attribute_macros に"
                   "名前を足すと、下がることがある):")
        for f in st["fallback_files"]:
            out.append(f"    {f['source_id']}:{f['path']}  {f['fallback_lines']:,} / {f['lines']:,} 行")
    if stats.get("skipped"):
        out.append("  除いたファイル: " + "、".join(f"{k} {v:,}" for k, v in sorted(stats["skipped"].items())))
    out += _ingest_notes_human(stats)
    if st.get("freshness"):             # `status --check` のときだけ
        out += _freshness_human(st["freshness"], st)
    return "\n".join(out) + "\n"


# 入らなかったフォルダの理由(人が読む名前)
_DIR_REASONS_JA = {"link_dir": "たどらなかったフォルダのリンク・ジャンクション", "unreadable_dir": "一覧できなかったフォルダ"}


def _ingest_notes_human(stats: dict) -> list[str]:
    """取り込みで、索引に入らなかったものの知らせ(0.4.0 から meta にある項目。無ければ何も出さない)。"""
    out = []
    ed = stats.get("excluded_dirs") or {}
    if ed.get("files"):
        out.append(f"  既定の名前(build・target・packages など)で除いたフォルダ: {ed['folders']:,} 個。"
                   f"中に、索引に入りうるファイルが {ed['files']:,}")
        for e in ed.get("examples", [])[:5]:
            out.append(f"    {e['path']}  {e['files']:,} ファイル")
        out.append("    入れるには、そのフォルダを root にした [[source]] を足すか、"
                   "source に default_excludes = false を書いて、要らないものを exclude に並べます")
    for reason, n in sorted((stats.get("skipped_dirs") or {}).items()):
        ex = (stats.get("skipped_dir_examples") or {}).get(reason, [])
        out.append(f"  {_DIR_REASONS_JA.get(reason, reason)}: {n:,}" + (f"(例: {', '.join(ex[:3])})" if ex else ""))
        if reason == "link_dir":
            out.append("    入れるには、そのフォルダを root にした [[source]] を足します")
    for n in stats.get("nested_roots") or []:
        outer, _, inner = n.partition(" → ")
        out.append(f"  {outer} は source {inner} のルートなので、{inner} の側だけで取り込んだ")
    cf = stats.get("chunk_failed") or {}
    if cf.get("count"):
        out.append(f"  構文で分ける途中で落ちて、行で分けたファイル: {cf['count']:,}(例: {', '.join(cf.get('examples', [])[:3])})")
    return out

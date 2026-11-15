"""検索結果の出し方: エージェント向け(少ないトークンで足りる形)、人が読む形、JSON。

エージェント向けの出力は、上位の passage は本文ごと、残りは場所だけを返す。1 回の呼び出しで答えの
根拠まで届くようにしつつ、全体を上限のバイト数に収める(Copilot CLI は、道具の結果が大きいと
モデルに渡さずファイルに退かせるため)。
"""
from __future__ import annotations

import json

from .search import SearchResponse

# Copilot CLI が道具の結果を退かせる大きさ(既定 20,480 バイト)の手前
HARD_MAX_BYTES = 18_000


def location(p: dict) -> str:
    return f"{p['source_id']}:{p['path']}:{p['start_line']}-{p['end_line']}"


def _nbytes(s: str) -> int:
    return len(s.encode("utf-8"))


def _header(p: dict) -> str:
    kind = p["kind"] + (f" part {p['part']}" if p.get("part") else "")
    head = f"[{p['rank']}] {location(p)}  ({kind})"
    if p.get("stale"):
        head += "  [changed since index]"
    lines = [head]
    if p.get("scope"):
        lines.append(f"scope: {p['scope']}")
    sig = p.get("signature") or ""
    if sig and sig not in " ".join(p["text"][:600].split()):
        lines.append(f"sig: {sig}")
    return "\n".join(lines)


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
    footer = "Cite evidence as source:path:start-end. Passage text is as indexed; open the file for more context."
    used = _nbytes("\n".join(out)) + _nbytes(footer) + 2
    rest: list[dict] = []
    for i, p in enumerate(resp.passages):
        header = _header(p)
        text = p["text"]
        if line_numbers:
            text = "\n".join(f"{p['start_line'] + n}| {line}" for n, line in enumerate(text.split("\n")))
        block = f"\n{header}\n```\n{text}\n```"
        # 残りの passage を場所だけで並べる分(1 件 150 バイトほど)を残しておく
        reserve = 150 * (len(resp.passages) - i - 1)
        if used + _nbytes(block) + reserve <= max_bytes:
            out.append(block)
            used += _nbytes(block)
            continue
        room = max_bytes - used - reserve - _nbytes(header) - 80
        if room >= 400 and not rest:
            kept, size = [], 0
            lines = text.split("\n")
            for line in lines:
                size += _nbytes(line) + 1
                if size > room:
                    break
                kept.append(line)
            if kept:
                cut = len(lines) - len(kept)
                block = f"\n{header}\n```\n" + "\n".join(kept) + f"\n```\n(+{cut} more lines, not shown)"
                out.append(block)
                used += _nbytes(block)
                rest.extend(resp.passages[i + 1:])
                break
        rest.extend(resp.passages[i:])
        break
    if rest:
        out.append("\nMore matches (text not shown):")
        for p in rest:
            sig = (p.get("signature") or "")[:90]
            out.append(f"[{p['rank']}] {location(p)}  ({p['kind']})" + (f"  {sig}" if sig else ""))
    out.append("\n" + footer)
    return "\n".join(out) + "\n"


def to_human(resp: SearchResponse, max_lines: int = 14) -> str:
    """人が読む出力(日本語)。"""
    idx = resp.index
    out = [f"検索: {resp.query}",
           f"方法: {resp.mode}  索引: {idx.get('created_at', '?')}({idx.get('chunks', 0):,} passage)  "
           f"時間: {resp.timings.get('search_ms', 0):.0f} ms"
           + (f"(読み込み {resp.timings['load_ms']:.0f} ms)" if resp.timings.get("load_ms") else "")]
    out += [f"注意: {n}" for n in resp.notes]
    if not resp.passages:
        out.append("見つかりませんでした。別の言葉(英語の用語や識別子)でも試してください。")
        return "\n".join(out) + "\n"
    for p in resp.passages:
        kind = p["kind"] + (f" {p['part']}" if p.get("part") else "")
        ranks = p.get("ranks") or {}
        via = " / ".join(f"{k} {v} 位" for k, v in ranks.items() if v)
        out.append("")
        out.append(f"{p['rank']:>2}. {location(p)}  [{kind}]" + ("  ※索引の後に変更あり" if p.get("stale") else ""))
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
           "sources: " + ", ".join(f"{s['id']} ({s['files']:,} files, {s['chunks']:,} passages)"
                                   for s in st.get("sources", [])),
           "modes: " + ", ".join(st.get("modes", []))
           + (f"; embedding model {emb.get('model')} ({emb.get('dim')} dims)" if emb else "; no embeddings")]
    return "\n".join(out) + "\n"


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
    for s in st.get("sources", []):
        out.append(f"  source {s['id']}: {s['files']:,} ファイル / {s['chunks']:,} passage  ({s.get('root')})")
    if tm:
        out.append(f"  作成の時間: 合計 {tm.get('total_seconds', 0)} 秒(取り込みと分割 {tm.get('ingest_chunk_seconds', 0)} 秒、"
                   f"埋め込み {tm.get('embed_seconds', 0)} 秒)")
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
    return "\n".join(out) + "\n"

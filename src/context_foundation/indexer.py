"""索引づくり: 取り込み → 分割 → keyword の索引 → 埋め込み → 1 ファイルに保存。

最初は作り直しの方式(毎回すべてを作る)。途中のファイルに書き、できあがってから置き換えるので、
作り直しの最中も前の索引で検索できる。所要時間と統計は、索引の meta に残す。
"""
from __future__ import annotations

import os
import re
import sys
import time
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from . import __version__
from .chunking import chunk_file
from .config import Config
from .embed import EmbedPool, Embedder, HashEmbedder, ModelUnavailable, make_embedder
from .ingest import IngestStats, iter_source_files
from .store import IndexWriter, open_readonly, unpack_text
from .sysinfo import peak_memory_bytes
from .textproc import fts_text

EMBED_TASK = 128       # 1 つの仕事として埋め込む passage の数(DB から読む単位でもある)


_LICENSE_WORDS = re.compile(r"copyright|licen[sc]e|spdx|著作権", re.IGNORECASE)
_DIRECTIVES = ("#include", "#pragma", "#if", "#define", "#!")


def strip_license_header(text: str) -> str:
    """ファイルの先頭にある、著作権やライセンスのコメントを外す(埋め込みの文だけ。passage の本文は変えない)。

    どのファイルにも同じ文が付いていると、ファイルの頭の passage どうしが似てしまい、どの質問にも
    上位に出やすくなるため。
    """
    lines = text.split("\n")
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines):
        return text
    first = lines[i].lstrip()
    end = i
    if first.startswith("/*"):
        while end < len(lines) and "*/" not in lines[end]:
            end += 1
        end += 1
    elif first.startswith(("//", "#")) and not first.startswith(_DIRECTIVES):
        mark = "//" if first.startswith("//") else "#"
        while end < len(lines) and lines[end].lstrip().startswith(mark) \
                and not lines[end].lstrip().startswith(_DIRECTIVES):
            end += 1
    else:
        return text
    if end > len(lines) or not _LICENSE_WORDS.search("\n".join(lines[i:end])):
        return text
    rest = "\n".join(lines[end:]).lstrip("\n")
    return rest if rest.strip() else text


def embedding_text(path: str, scope: str, signature: str, text: str, start_line: int = 0) -> str:
    """埋め込みに渡す文。passage の頭に、場所・外側のスコープ・シグネチャを付ける
    (関数の途中の部分だけでも、どのクラスのどの関数かが伝わるように)。"""
    if start_line == 1:
        text = strip_license_header(text)
    head = path
    if scope:
        head += " | " + scope
    if signature and signature not in text[:400]:
        head += " | " + signature
    return head + "\n" + text


def _percentile(sorted_values: list[int], p: float) -> int:
    if not sorted_values:
        return 0
    return sorted_values[min(len(sorted_values) - 1, int(len(sorted_values) * p))]


class _Progress:
    def __init__(self, enabled: bool, stream=None):
        self.enabled, self.stream, self.last = enabled, stream or sys.stderr, 0.0

    def __call__(self, text: str, force: bool = False) -> None:
        now = time.time()
        if self.enabled and (force or now - self.last >= 2.0):
            self.last = now
            print(text, file=self.stream, flush=True)


def _replace(tmp: Path, dest: Path) -> None:
    """できあがった索引に置き換える。Windows では、検索が開いている間は置き換えられないので、少し待って繰り返す。"""
    for attempt in range(40):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            time.sleep(0.25)
    raise PermissionError(f"索引を置き換えられません(ほかのプログラムが開いています): {dest}")


def build_index(cfg: Config, *, use_embedding: bool | None = None, embedder: Embedder | None = None,
                require_embedding: bool = False, progress: bool = True, log=None) -> dict:
    """索引を作り直し、統計(meta に入れたものと同じ)を返す。

    use_embedding: None なら設定([embedding] enabled)に従う。
    モデルが手元に無いときは、keyword だけの索引を作る(require_embedding なら ModelUnavailable を投げる)。
    """
    say = _Progress(progress)
    log = log or (lambda s: say(s, force=True))
    t_start = time.time()
    dest = cfg.index_path
    tmp = dest.with_name(dest.name + f".tmp-{os.getpid()}")
    writer = IndexWriter(tmp)
    try:
        ingest_stats = IngestStats()
        per_source: dict[str, dict] = {}
        by_lang: dict[str, dict] = defaultdict(lambda: {"files": 0, "lines": 0, "chunks": 0, "fallback_lines": 0})
        sizes: list[int] = []
        methods: dict[str, int] = defaultdict(int)
        total_bytes = total_lines = total_chunks = total_files = inner_error_units = 0

        for src in cfg.sources:
            s_files = s_lines = s_chunks = 0
            for f in iter_source_files(src, cfg.index, ingest_stats):
                chunks, cs = chunk_file(f.text, f.language, cfg.chunk)
                if not chunks:
                    ingest_stats.skip("empty", f.path)
                    ingest_stats.included -= 1
                    continue
                nlines = f.text.count("\n") + (0 if f.text.endswith("\n") else 1)
                file_id = writer.add_file(f.source_id, f.path, f.size, f.mtime_ns, nlines, f.language, f.encoding,
                                          len(chunks), cs.fallback_lines)
                fts_path = fts_text(f.path)
                for c in chunks:
                    writer.add_chunk(file_id, f.source_id, f.path, c, fts_text(c.text),
                                     fts_text(" ".join([c.scope, c.signature] + c.symbols)), fts_path)
                    sizes.append(len(c.text))
                    methods[c.method] += 1
                b = by_lang[f.language]
                b["files"] += 1
                b["lines"] += cs.lines
                b["chunks"] += len(chunks)
                b["fallback_lines"] += cs.fallback_lines
                inner_error_units += cs.inner_error_units
                s_files += 1
                s_lines += nlines
                s_chunks += len(chunks)
                total_bytes += f.size
                total_files += 1
                total_lines += nlines
                total_chunks += len(chunks)
                if total_files % 500 == 0:
                    writer.commit()
                say(f"  取り込み中: {total_files:,} ファイル / {total_chunks:,} passage")
            per_source[src.id] = {"id": src.id, "root": str(src.root), "files": s_files, "lines": s_lines,
                                  "chunks": s_chunks}
        writer.commit()
        t_chunk = time.time()
        log(f"取り込みと分割: {total_files:,} ファイル / {total_lines:,} 行 / {total_chunks:,} passage"
            f"({t_chunk - t_start:.1f} 秒)")
        if not total_chunks:
            raise RuntimeError("索引に入るファイルが 1 つもありません。設定の root・include・exclude を確かめてください")

        # 埋め込み
        embedding_meta = None
        want = cfg.embedding.enabled if use_embedding is None else use_embedding
        t_embed0 = time.time()
        if want:
            try:
                emb = embedder or make_embedder(cfg.embedding)
            except ModelUnavailable as e:
                if require_embedding:
                    raise
                log(f"注意: {e}\n      → keyword(BM25)だけの索引を作ります")
                emb = None
            if emb is not None:
                workers = 1
                if embedder is None and not isinstance(emb, HashEmbedder):
                    workers = cfg.embedding.workers or 1
                    workers = max(1, min(workers, -(-total_chunks // EMBED_TASK)))
                pool = EmbedPool(cfg.embedding, workers) if workers > 1 else None
                done = 0

                def batches(rdb):
                    for first in range(1, total_chunks + 1, EMBED_TASK):
                        rows = rdb.execute(
                            "SELECT path, scope, signature, text, start_line FROM chunks "
                            "WHERE id >= ? AND id < ? ORDER BY id", (first, first + EMBED_TASK)).fetchall()
                        yield [embedding_text(r[0], r[1], r[2], unpack_text(r[3]), r[4]) for r in rows]

                try:
                    with closing(open_readonly(tmp)) as rdb:
                        results = pool.map(batches(rdb)) if pool else (emb.embed_passages(t) for t in batches(rdb))
                        for vecs in results:
                            writer.add_vectors(done + 1, vecs, cfg.embedding.vector_dtype)
                            done += len(vecs)
                            if done % (EMBED_TASK * 16) < EMBED_TASK:
                                writer.commit()
                            el = time.time() - t_embed0
                            rate = done / el if el > 0 else 0
                            eta = (total_chunks - done) / rate if rate else 0
                            say(f"  埋め込み中: {done:,} / {total_chunks:,} passage"
                                f"({rate:.0f} 件/秒、残り 約 {eta:.0f} 秒)")
                finally:
                    if pool:
                        pool.close()
                writer.commit()
                embedding_meta = dict(emb.info(), vector_dtype=cfg.embedding.vector_dtype, workers=workers)
                log(f"埋め込み: {total_chunks:,} passage({time.time() - t_embed0:.1f} 秒、モデル {emb.name}、"
                    f"{workers} プロセス)")
        t_embed = time.time()

        sizes.sort()
        code_lines = sum(v["lines"] for k, v in by_lang.items() if k in ("c", "cpp", "csharp"))
        code_fallback = sum(v["fallback_lines"] for k, v in by_lang.items() if k in ("c", "cpp", "csharp"))
        stats = {
            "files": total_files, "lines": total_lines, "chunks": total_chunks, "source_bytes": total_bytes,
            "by_language": dict(by_lang),
            "chunk_methods": dict(methods),
            "chunk_chars": {"p10": _percentile(sizes, 0.1), "p50": _percentile(sizes, 0.5),
                            "p90": _percentile(sizes, 0.9), "max": sizes[-1] if sizes else 0},
            # 構文で分ける言語(C・C++・C#)の行のうち、解析できずに行で分けた割合
            "syntax_fallback_ratio": round(code_fallback / code_lines, 4) if code_lines else 0.0,
            "inner_error_units": inner_error_units,
            "scanned": ingest_stats.scanned, "skipped": dict(ingest_stats.skipped),
            "skipped_examples": ingest_stats.examples,
        }
        writer.set_meta("stats", stats)
        writer.set_meta("sources", list(per_source.values()))
        writer.set_meta("embedding", embedding_meta)
        writer.set_meta("chunk_config", {"max_bytes": cfg.chunk.max_bytes, "min_bytes": cfg.chunk.min_bytes,
                                         "line_window": cfg.chunk.line_window, "macro_hints": cfg.chunk.macro_hints})
        writer.set_meta("tool_version", __version__)
        writer.set_meta("created_at", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        timings = {"ingest_chunk_seconds": round(t_chunk - t_start, 2),
                   "embed_seconds": round(t_embed - t_embed0, 2) if embedding_meta else 0.0}
        writer.set_meta("timings", timings)        # finalize と total は、下で書き足す
        t_fin0 = time.time()
        writer.finish()
        # 仕上げ(FTS の最適化)の時間は、閉じた後で分かるので、開き直して書き足す
        import sqlite3
        timings["finalize_seconds"] = round(time.time() - t_fin0, 2)
        timings["total_seconds"] = round(time.time() - t_start, 2)
        timings["peak_memory_bytes"] = peak_memory_bytes()     # このプロセスの分(埋め込みを別プロセスに分けた分は入らない)
        with closing(sqlite3.connect(str(tmp))) as db:
            import json
            db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('timings', ?)", (json.dumps(timings),))
            db.commit()
        _replace(tmp, dest)
    except BaseException:
        writer.abort()
        raise
    result = {"index_path": str(dest), "index_bytes": dest.stat().st_size, "stats": stats, "timings": timings,
              "embedding": embedding_meta, "sources": list(per_source.values())}
    log(f"索引ができました: {dest}({result['index_bytes'] / 1e6:.1f} MB、合計 {timings['total_seconds']:.1f} 秒)")
    return result

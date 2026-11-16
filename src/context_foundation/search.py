"""検索: keyword(BM25)、semantic(手元の埋め込みの総当たり)、hybrid(2 つを RRF でまとめる)。

検索だけをする(回答は作らない)。返すのは、順位つきの passage(場所・スコープ・シグネチャ・本文)。
reranker は後段に差し込める(既定は無効)。
"""
from __future__ import annotations

import importlib
import os
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .config import MODES, Config, ConfigError, EmbeddingConfig, SearchConfig
from .embed import Embedder, ModelUnavailable, make_embedder
from .glossary import Glossary, GlossaryError, resolve_workspace_file
from .store import IndexError_, fetch_passages, load_vectors, open_readonly, read_meta
from .textproc import fts_query, query_tokens


@dataclass
class SearchResponse:
    query: str
    mode: str                      # 実際に使った方法
    requested_mode: str
    passages: list[dict]
    notes: list[str] = field(default_factory=list)       # 利用者・エージェントに伝えること(切り替えた理由など)
    timings: dict = field(default_factory=dict)          # ミリ秒。load_ms は、モデルとベクトルの読み込み
    index: dict = field(default_factory=dict)            # 索引の作成日時と件数
    added_terms: list[str] = field(default_factory=list)  # 用語の対応表で、質問に足した語

    def to_dict(self) -> dict:
        return {"query": self.query, "added_terms": self.added_terms, "mode": self.mode,
                "requested_mode": self.requested_mode, "notes": self.notes, "timings": self.timings,
                "index": self.index, "passages": self.passages}


def load_reranker(spec: str):
    """`モジュール:関数` の形で指定した reranker を読み込む。関数は (query, passages) を受けて、並べ直した passages を返す。"""
    mod, _, attr = spec.partition(":")
    if not mod or not attr:
        raise ValueError(f"reranker は モジュール:関数 の形で書いてください: {spec!r}")
    return getattr(importlib.import_module(mod), attr)


def rrf(rankings: list[list[int]], k: int, weights: list[float] | None = None) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion。順位の並び(id のリスト)をまとめて、(id, 点) を点の高い順に返す。
    weights を渡すと、並びごとに重みを掛ける。"""
    score: dict[int, float] = {}
    for i, ranking in enumerate(rankings):
        w = weights[i] if weights else 1.0
        for rank, cid in enumerate(ranking, 1):
            score[cid] = score.get(cid, 0.0) + w / (k + rank)
    return sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))


def keyword_coverage(db, tokens: list[str]) -> float:
    """質問の語のうち、索引に 1 回でも出てくる語の割合(0〜1)。

    日本語の質問で英語のコードを探すときは、keyword に当たる語がほとんど無く、たまたま当たった 1 語
    (`gpu` など)の結果が上位を占める。hybrid では、この割合が低いほど keyword の重みを下げる。
    """
    if not tokens:
        return 0.0
    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.cf_vocab USING fts5vocab(main, chunks_fts, 'row')")
    marks = ",".join("?" * len(tokens))
    found = db.execute(f"SELECT count(*) FROM temp.cf_vocab WHERE term IN ({marks})", tokens).fetchone()[0]
    return found / len(tokens)


class Searcher:
    """索引 1 つに対する検索。作ったまま使い回すと、モデルとベクトルを読み込んだまま保つ(MCP サーバーと評価で使う)。"""

    def __init__(self, index_path: Path, search_cfg: SearchConfig | None = None,
                 embedding_cfg: EmbeddingConfig | None = None, embedder: Embedder | None = None,
                 glossary: Glossary | None = None):
        self.index_path = Path(index_path)
        self.search_cfg = search_cfg or SearchConfig()
        self.embedding_cfg = embedding_cfg or EmbeddingConfig()
        self._embedder = embedder
        self._embedder_error: str | None = None
        self._sig: tuple | None = None           # 読み込んだ索引ファイルの (更新日時, 大きさ)
        self._meta: dict = {}
        self._vectors: np.ndarray | None = None
        self._reranker = load_reranker(self.search_cfg.reranker) if self.search_cfg.reranker else None
        self._glossary = glossary

    @classmethod
    def from_config(cls, cfg: Config, embedder: Embedder | None = None) -> "Searcher":
        try:
            glossary = Glossary.load(resolve_workspace_file(cfg.cf_dir, cfg.search.glossary),
                                     cfg.search.builtin_glossary)
        except GlossaryError as e:
            raise ConfigError(str(e)) from None
        return cls(cfg.index_path, cfg.search, cfg.embedding, embedder, glossary)

    # --- 索引の読み込み ---
    def _refresh(self) -> None:
        try:
            st = self.index_path.stat()
        except OSError:
            raise IndexError_(f"索引がありません: {self.index_path}\n作るには `ctxf index`(index.bat でも作れます)") from None
        sig = (st.st_mtime_ns, st.st_size)
        if sig != self._sig:
            with closing(open_readonly(self.index_path)) as db:
                self._meta = read_meta(db)
            self._sig, self._vectors = sig, None

    def meta(self) -> dict:
        self._refresh()
        return self._meta

    def has_vectors(self) -> bool:
        return bool(self.meta().get("embedding"))

    def _load_vectors(self) -> np.ndarray:
        if self._vectors is None:
            emb = self._meta["embedding"]
            with closing(open_readonly(self.index_path)) as db:
                self._vectors = load_vectors(db, emb["dim"], emb.get("vector_dtype", "float16"))
        return self._vectors

    def _get_embedder(self) -> Embedder:
        if self._embedder is None:
            if self._embedder_error:
                raise ModelUnavailable(self._embedder_error)
            try:
                self._embedder = make_embedder(self.embedding_cfg)
            except ModelUnavailable as e:
                self._embedder_error = str(e)
                raise
        return self._embedder

    def index_info(self) -> dict:
        m = self.meta()
        emb = m.get("embedding") or {}
        return {"created_at": m.get("created_at"), "files": m["stats"]["files"], "chunks": m["stats"]["chunks"],
                "embedding_model": emb.get("model"), "sources": [s["id"] for s in m.get("sources", [])]}

    # --- 検索 ---
    def _keyword(self, db, query: str, limit: int, sources: list[str] | None) -> list[tuple[int, float]]:
        tokens = query_tokens(query)
        if not tokens:
            return []
        c = self.search_cfg
        args: list = [c.weight_body, c.weight_symbols, c.weight_path, fts_query(tokens)]
        sql = "SELECT chunks_fts.rowid, bm25(chunks_fts, ?, ?, ?) AS s FROM chunks_fts"
        if sources:
            sql += (" JOIN chunks ON chunks.id = chunks_fts.rowid WHERE chunks_fts MATCH ? AND chunks.source_id IN ("
                    + ",".join("?" * len(sources)) + ")")
            args += sources
        else:
            sql += " WHERE chunks_fts MATCH ?"
        sql += " ORDER BY s LIMIT ?"
        args.append(limit)
        return [(r[0], -r[1]) for r in db.execute(sql, args)]

    def _semantic(self, db, qvec: np.ndarray, limit: int, sources: list[str] | None) -> list[tuple[int, float]]:
        mat = self._load_vectors()
        if not len(mat):
            return []
        scores = mat @ qvec
        if sources:
            marks = ",".join("?" * len(sources))
            ids = np.fromiter((r[0] for r in db.execute(f"SELECT id FROM chunks WHERE source_id IN ({marks})", sources)),
                              dtype=np.int64)
            mask = np.zeros(len(scores), dtype=bool)
            mask[ids - 1] = True
            scores = np.where(mask, scores, -np.inf)
        n = min(limit, len(scores))
        top = np.argpartition(-scores, n - 1)[:n]
        top = top[np.argsort(-scores[top], kind="stable")]
        return [(int(i) + 1, float(scores[i])) for i in top if np.isfinite(scores[i])]

    def search(self, query: str, mode: str | None = None, top_k: int | None = None,
               source_ids: list[str] | None = None, check_stale: bool = True) -> SearchResponse:
        t0 = time.perf_counter()
        c = self.search_cfg
        requested = mode or c.mode
        if requested not in MODES:
            raise ValueError(f"mode は {' / '.join(MODES)} のどれかです: {requested!r}")
        top_k = max(1, min(int(top_k or c.top_k), 50))
        self._refresh()
        meta = self._meta
        notes: list[str] = []
        timings: dict = {"load_ms": 0.0}
        known = [s["id"] for s in meta.get("sources", [])]
        sources = [s for s in (source_ids or []) if s]
        unknown = [s for s in sources if s not in known]
        if unknown:
            raise ValueError(f"知らない source_id です: {', '.join(unknown)}(索引にあるのは {', '.join(known)})")

        # 用語の対応表で、質問に語を足す(日本語だけの質問で、英語のコードと文書を引くため)
        added = self._glossary.expand(query) if self._glossary else []
        q = query + " " + " ".join(added) if added else query
        if added:
            notes.append("added terms from the glossary: " + " ".join(added))

        effective = requested
        qvec = None
        if requested in ("semantic", "hybrid"):
            emb_meta = meta.get("embedding")
            reason = None
            if not emb_meta:
                reason = "the index has no embeddings (built keyword-only)"
            else:
                t = time.perf_counter()
                loaded_before = self._embedder is not None and self._vectors is not None
                try:
                    emb = self._get_embedder()
                    if emb.name != emb_meta["model"] or emb.dim != emb_meta["dim"]:
                        reason = (f"the index was embedded with {emb_meta['model']} but the configured model is "
                                  f"{emb.name}; rebuild the index")
                    else:
                        self._load_vectors()
                        if not loaded_before:
                            timings["load_ms"] = round((time.perf_counter() - t) * 1000, 1)
                        t = time.perf_counter()
                        qvec = emb.embed_query(q)
                        timings["embed_ms"] = round((time.perf_counter() - t) * 1000, 1)
                except ModelUnavailable as e:
                    reason = "the embedding model is not available locally (" + str(e).splitlines()[0] + ")"
            if reason:
                effective = "keyword"
                notes.append(f"mode {requested} is unavailable: {reason}. Fell back to keyword.")

        n_cand = max(c.candidates, top_k)
        kw: list[tuple[int, float]] = []
        sem: list[tuple[int, float]] = []
        with closing(open_readonly(self.index_path)) as db:
            if effective in ("keyword", "hybrid"):
                t = time.perf_counter()
                kw = self._keyword(db, q, n_cand, sources or None)
                timings["keyword_ms"] = round((time.perf_counter() - t) * 1000, 1)
            if effective in ("semantic", "hybrid") and qvec is not None:
                t = time.perf_counter()
                sem = self._semantic(db, qvec, n_cand, sources or None)
                timings["semantic_ms"] = round((time.perf_counter() - t) * 1000, 1)

            kw_rank = {cid: r for r, (cid, _) in enumerate(kw, 1)}
            sem_rank = {cid: r for r, (cid, _) in enumerate(sem, 1)}
            if effective == "keyword":
                ranked = kw
            elif effective == "semantic":
                ranked = sem
            else:
                w_kw = 1.0
                if c.hybrid_weighting == "coverage":
                    # 用語の対応表で語を足したときは、足した語と元の質問の英字の語で割合を見る(日本語の質問の
                    # 2 文字の組は、英語のコードにはほとんど無いので、それで割合を下げると、足した語が効かない)
                    cov_tokens = query_tokens(q)
                    if added:
                        cov_tokens = [t for t in cov_tokens if t.isascii()] or cov_tokens
                    w_kw = keyword_coverage(db, cov_tokens) ** 0.5
                timings["keyword_weight"] = round(w_kw, 3)
                ranked = rrf([[cid for cid, _ in kw], [cid for cid, _ in sem]], c.rrf_k, [w_kw, 1.0])

            t = time.perf_counter()
            keep = ranked[:max(top_k, c.rerank_candidates) if self._reranker else top_k]
            found = fetch_passages(db, [cid for cid, _ in keep])
            passages = []
            for cid, score in keep:
                p = found.get(cid)
                if p is None:
                    continue
                p["score"] = round(float(score), 6)
                p["ranks"] = {"keyword": kw_rank.get(cid), "semantic": sem_rank.get(cid)}
                passages.append(p)
            if self._reranker and passages:
                passages = list(self._reranker(query, passages))[:top_k]
            if check_stale and passages:
                self._mark_stale(db, passages, meta)
            timings["fetch_ms"] = round((time.perf_counter() - t) * 1000, 1)

        for rank, p in enumerate(passages, 1):
            p["rank"] = rank
            p.pop("file_id", None)
        timings["total_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        timings["search_ms"] = round(timings["total_ms"] - timings["load_ms"], 1)
        return SearchResponse(query=query, mode=effective, requested_mode=requested, passages=passages, notes=notes,
                              timings=timings, index=self.index_info(), added_terms=added)

    def _mark_stale(self, db, passages: list[dict], meta: dict) -> None:
        """索引の後に変わった(か、無くなった)ファイルの passage に印を付ける。見るのは大きさと更新日時だけ。"""
        roots = {s["id"]: s.get("root") for s in meta.get("sources", [])}
        ids = sorted({p["file_id"] for p in passages})
        rows = {r[0]: (r[1], r[2]) for r in db.execute(
            f"SELECT id, size, mtime_ns FROM files WHERE id IN ({','.join('?' * len(ids))})", ids)}
        cache: dict[int, bool] = {}
        for p in passages:
            fid = p["file_id"]
            if fid not in cache:
                root = roots.get(p["source_id"])
                stale = False
                if root and os.path.isdir(root):
                    try:
                        st = os.stat(os.path.join(root, p["path"]))
                        stale = (st.st_size, st.st_mtime_ns) != rows.get(fid)
                    except OSError:
                        stale = True
                cache[fid] = stale
            if cache[fid]:
                p["stale"] = True

    def fallback_files(self, limit: int = 5) -> list[dict]:
        """構文で分けられずに行で分けた行が多いファイル(マクロで文法が崩れている場所を探す手がかり)。"""
        with closing(open_readonly(self.index_path)) as db:
            rows = db.execute("SELECT source_id, path, lines, fallback_lines FROM files WHERE fallback_lines > 0 "
                              "ORDER BY fallback_lines DESC, source_id, path LIMIT ?", (limit,)).fetchall()
        return [{"source_id": s, "path": p, "lines": n, "fallback_lines": fb} for s, p, n, fb in rows]

    def status(self) -> dict:
        m = self.meta()
        return {
            "fallback_files": self.fallback_files(),
            "index_path": str(self.index_path), "index_bytes": self.index_path.stat().st_size,
            "schema_version": m.get("schema_version"), "tool_version": m.get("tool_version"),
            "created_at": m.get("created_at"), "revision": m.get("revision", ""),
            "sources": m.get("sources", []), "stats": m.get("stats", {}),
            "embedding": m.get("embedding"), "timings": m.get("timings", {}),
            "chunk_config": m.get("chunk_config", {}),
            "modes": list(MODES) if m.get("embedding") else ["keyword"],
        }

"""検索: keyword(BM25)、semantic(手元の埋め込みの総当たり)、hybrid(2 つを RRF でまとめる)。

検索だけをする(回答は作らない)。返すのは、順位つきの passage(場所・スコープ・シグネチャ・本文)。
reranker は後段に差し込める(Python から Searcher(reranker=関数) で渡したときだけ。設定ファイルからは読まない)。
"""
from __future__ import annotations

import json
import os
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

import numpy as np

from . import command_hint
from .config import MODES, Config, ConfigError, EmbeddingConfig, SearchConfig
from .embed import Embedder, ModelUnavailable, make_embedder
from .glossary import Glossary, GlossaryError, resolve_workspace_file
from .indexer import query_signature, signature_diff
from .ingest import IngestStats, _long_path, iter_source_files
from .store import IndexError_, fetch_passages, load_vectors, open_readonly, read_meta
from .textproc import MAX_QUERY_TOKENS, fts_query, query_tokens, query_tokens_cut


class UnknownSourceError(ValueError):
    """索引に無い source_id を渡された。文は人が読む形(CLI が、そのまま出す)。エージェントに返す英語の文は、
    MCP サーバーが unknown と known から作る。"""

    def __init__(self, unknown: list[str], known: list[str]):
        super().__init__(f"知らない source_id です: {', '.join(unknown)}(索引にあるのは {', '.join(known)})")
        self.unknown, self.known = list(unknown), list(known)


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
    # 結果に出た source のルート(id → 作業フォルダからの相対か、絶対のパス)。どれかが作業フォルダそのもので
    # ないときだけ入れる(エージェントが source:path のファイルを開けるように)
    roots: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"query": self.query, "added_terms": self.added_terms, "mode": self.mode,
                "requested_mode": self.requested_mode, "notes": self.notes, "timings": self.timings,
                "index": self.index, "passages": self.passages}


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


# 同じファイルのほかの名前を添えるとき、名前を取る passage の種類(名前が中身を表すもの)
RELATED_KINDS = ("function", "class", "struct", "enum", "macro", "typedef", "union", "declaration")
RELATED_FIRST = 3          # 名前を添えるのは、上位のこの件数の passage だけ(出力が膨らまないように)
# 同じファイルの passage を、id の範囲(主キー)で読む(_add_related)
RELATED_SQL = "SELECT kind, symbols FROM chunks WHERE id BETWEEN ? AND ? AND file_id = ? ORDER BY start_line"


def _add_related(db, passages: list[dict], limit: int) -> None:
    """上位の passage に、同じファイルにあるほかの関数・クラスの名前を、行の順に limit 個まで添える(p["related"])。
    検索が返した passage の近くにある関連の処理(別の版、呼び出し元、次の手順)を、grep の往復なしで見せるため。
    名前は索引の symbols から取る(ファイルは読まない)。上位に出ている passage の名前は除く。
    limit を超えた数は p["related_more"] に入れる(名前の並びに "+N more" を混ぜない。JSON の出力で名前に見えるため)。

    chunks には file_id の索引が無い(足すと、今ある索引で効かない)。passage はファイルごとに続いた id で書いて
    あるので(indexer の _ChunkReuse と同じ前提)、その passage の id からファイルの passage の数(files.chunks)の
    範囲だけを、主キーで読む(file_id だけで探すと、検索のたびに chunks を全部読む)。"""
    shown = {s for p in passages for s in (p.get("symbols") or [])}
    for p in passages[:RELATED_FIRST]:
        fid, cid = p.get("file_id"), p.get("chunk_id")
        if fid is None or cid is None:
            continue
        row = db.execute("SELECT chunks FROM files WHERE id = ?", (fid,)).fetchone()
        span = max(int(row[0]) if row else 0, 1) - 1
        names: list[str] = []
        rows = db.execute(RELATED_SQL, (cid - span, cid + span, fid))
        for kind, symbols in rows:
            if kind not in RELATED_KINDS:
                continue
            for s in json.loads(symbols):
                if s and s not in shown and s not in names:
                    names.append(s)
        if names:
            p["related"] = names[:limit]
            if len(names) > limit:
                p["related_more"] = len(names) - limit


class Searcher:
    """索引 1 つに対する検索。作ったまま使い回すと、モデルとベクトルを読み込んだまま保つ(MCP サーバーと評価で使う)。"""

    def __init__(self, index_path: Path, search_cfg: SearchConfig | None = None,
                 embedding_cfg: EmbeddingConfig | None = None, embedder: Embedder | None = None,
                 glossary: Glossary | None = None, *, reranker=None, rerank_candidates: int = 30,
                 source_roots: dict[str, Path] | None = None, workspace: Path | None = None):
        """reranker: (query, passages) を受けて、並べ直した passages を返す関数(評価・テスト用。既定は使わない)。
        source_roots: source の id → 今の設定のルート。索引の後に変わったファイルを、ここで見る
        (無い id は、索引を作ったときのルートで見る)。
        workspace: 作業フォルダ。ルートを、作業フォルダからの相対で見せるのに使う(無ければ絶対のパスで見せる)。"""
        self.index_path = Path(index_path)
        self.search_cfg = search_cfg or SearchConfig()
        self.embedding_cfg = embedding_cfg or EmbeddingConfig()
        self._embedder = embedder
        self._embedder_error: str | None = None
        self._sig: tuple | None = None           # 読み込んだ索引ファイルの (更新日時, 大きさ)
        self._meta: dict = {}
        self._vectors: np.ndarray | None = None
        self._unembedded: np.ndarray | None = None     # 埋め込みの無い passage(行がすべて 0)の位置。無ければ None
        self._reranker = reranker
        self._rerank_candidates = rerank_candidates
        self._source_roots = {k: str(v) for k, v in (source_roots or {}).items()}
        self._workspace = str(workspace) if workspace else None
        self._glossary = glossary

    @classmethod
    def from_config(cls, cfg: Config, embedder: Embedder | None = None) -> "Searcher":
        try:
            glossary = Glossary.load(resolve_workspace_file(cfg.cf_dir, cfg.search.glossary),
                                     cfg.search.builtin_glossary)
        except GlossaryError as e:
            raise ConfigError(str(e)) from None
        return cls(cfg.index_path, cfg.search, cfg.embedding, embedder, glossary,
                   source_roots={s.id: s.root for s in cfg.sources}, workspace=cfg.workspace)

    # --- 索引の読み込み ---
    def _refresh(self) -> None:
        try:
            st = self.index_path.stat()
        except OSError:
            raise IndexError_(f"索引がありません: {self.index_path}\n作るには `{command_hint('index')}`") from None
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

    def _same_build(self, db) -> bool:
        """開いた索引(db)が、読み込んである meta とベクトルの索引と同じものか(検索の途中で作り直されていないか)。"""
        row = db.execute("SELECT value FROM meta WHERE key = 'build_id'").fetchone()
        if row is not None or "build_id" in self._meta:
            return row is not None and json.loads(row[0]) == self._meta.get("build_id")
        return read_meta(db) == self._meta        # 0.3 までの索引(印が無い)は、meta の全体で比べる

    def _embedding_mismatch(self, emb: Embedder, emb_meta: dict) -> str | None:
        """索引の埋め込みと、今の埋め込みの作り方が合わなければ、その理由(英語。エージェントに見せる)。"""
        if emb.name != emb_meta["model"] or emb.dim != emb_meta["dim"]:
            return (f"the index was embedded with {emb_meta['model']} but the configured model is "
                    f"{emb.name}; rebuild the index")
        diff = signature_diff(emb_meta, query_signature(emb), self.embedding_cfg)
        if diff:
            return f"the index was embedded with different settings ({', '.join(diff)}); rebuild the index"
        return None

    def _load_vectors(self) -> np.ndarray:
        if self._vectors is None:
            emb = self._meta["embedding"]
            with closing(open_readonly(self.index_path)) as db:
                self._vectors = load_vectors(db, emb["dim"], emb.get("vector_dtype", "float16"))
            # 中身が無いので埋め込まなかった passage(indexer.has_content)は、意味検索の結果に入れない
            unembedded = ~self._vectors.any(axis=1)
            self._unembedded = unembedded if unembedded.any() else None
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

    def warm_up(self) -> bool:
        """索引のベクトルと埋め込みモデルを読み込んでおく(MCP サーバーが、最初の検索の前に呼ぶ)。

        埋め込みの無い索引(keyword だけ)なら、何もしない。読み込めたら True。モデルが手元に無いなどで
        読み込めなければ False(検索のときに keyword に切り替わる。ここでは知らせない)。
        """
        self._refresh()
        emb_meta = self._meta.get("embedding")
        if not emb_meta:
            return False
        try:
            emb = self._get_embedder()
        except ModelUnavailable:
            return False
        if self._embedding_mismatch(emb, emb_meta):
            return False
        self._load_vectors()
        emb.embed_query("warm up")       # 最初の計算でだけ起きる用意も、済ませておく
        return True

    def index_info(self) -> dict:
        m = self.meta()
        emb = m.get("embedding") or {}
        return {"created_at": m.get("created_at"), "files": m["stats"]["files"], "chunks": m["stats"]["chunks"],
                "embedding_model": emb.get("model"), "sources": [s["id"] for s in m.get("sources", [])]}

    # --- 検索 ---
    def _keyword(self, db, tokens: list[str], limit: int, sources: list[str] | None) -> list[tuple[int, float]]:
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
        if self._unembedded is not None:
            scores[self._unembedded] = -np.inf
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
        requested = mode or self.search_cfg.mode
        if requested not in MODES:
            raise ValueError(f"mode は {' / '.join(MODES)} のどれかです: {requested!r}")
        top_k = max(1, min(int(top_k or self.search_cfg.top_k), 50))
        # 質問を埋め込んでいる間に索引が作り直されると、前の索引のベクトルの id で、新しい索引の passage を
        # 引いてしまう(作り直すと id が振り直される)。開いた索引が違えば、読み込み直してやり直す
        # (3 回目は確かめずに進める。続けて作り直されることは、まず無い)
        for attempt in range(3):
            resp = self._search_once(t0, query, requested, top_k, source_ids, check_stale, verify=attempt < 2)
            if resp is not None:
                return resp
            self._sig = None
        raise AssertionError("unreachable")

    def _search_once(self, t0: float, query: str, requested: str, top_k: int, source_ids: list[str] | None,
                     check_stale: bool, verify: bool) -> SearchResponse | None:
        """1 回の検索。verify で、開いた索引が読み込んだものと違うと分かったら None(呼び出し側がやり直す)。"""
        c = self.search_cfg
        self._refresh()
        meta = self._meta
        notes: list[str] = []
        timings: dict = {"load_ms": 0.0}
        known = [s["id"] for s in meta.get("sources", [])]
        sources = [s for s in (source_ids or []) if s]
        unknown = [s for s in sources if s not in known]
        if unknown:
            raise UnknownSourceError(unknown, known)

        # 用語の対応表で、質問に語を足す(日本語だけの質問で、英語のコードと文書を引くため)
        added = self._glossary.expand(query) if self._glossary else []
        q = query + " " + " ".join(added) if added else query
        if added:
            notes.append("added terms from the glossary: " + " ".join(added))
        if self._glossary and self._glossary.problems:
            notes.append("glossary lines skipped (need <term><TAB><words>): "
                         + ", ".join(self._glossary.problems[:5]))
        q_tokens, cut = query_tokens_cut(q)
        if cut and added:
            # 語の上限に当たると、後ろに足した対応表の語から落ちる。足した語の場所を先に取る(上限より少ない質問は、
            # 語の並びも順も今までどおり)
            extra = query_tokens(" ".join(added))
            if any(t not in q_tokens for t in extra):
                base = [t for t in query_tokens(query) if t not in extra]
                q_tokens = base[:MAX_QUERY_TOKENS - len(extra)] + extra

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
                    reason = self._embedding_mismatch(emb, emb_meta)
                    if reason is None:
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
            if verify and not self._same_build(db):
                return None
            if effective in ("keyword", "hybrid"):
                if cut:
                    notes.append(f"long query: only the first {MAX_QUERY_TOKENS} distinct terms were used for "
                                 "keyword matching")
                t = time.perf_counter()
                kw = self._keyword(db, q_tokens, n_cand, sources or None)
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
                    # ただし日本語の文書が主の索引では、足した英語の語が無く、元の語のほうがよく当たるので、
                    # 元の質問の語での割合との大きい方を使う(対応表が無いときと同じ重みを下回らない)
                    cov = keyword_coverage(db, query_tokens(query))
                    ascii_tokens = [t for t in q_tokens if t.isascii()]
                    if added and ascii_tokens:
                        cov = max(cov, keyword_coverage(db, ascii_tokens))
                    w_kw = cov ** 0.5
                timings["keyword_weight"] = round(w_kw, 3)
                ranked = rrf([[cid for cid, _ in kw], [cid for cid, _ in sem]], c.rrf_k, [w_kw, 1.0])

            t = time.perf_counter()
            keep = ranked[:max(top_k, self._rerank_candidates) if self._reranker else top_k]
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
                self._mark_stale(db, passages, meta, notes)
            if self.search_cfg.related > 0 and passages:
                _add_related(db, passages, self.search_cfg.related)
            timings["fetch_ms"] = round((time.perf_counter() - t) * 1000, 1)

        for rank, p in enumerate(passages, 1):
            p["rank"] = rank
            p.pop("file_id", None)
        roots = {sid: self._folder(sid, meta) for sid in dict.fromkeys(p["source_id"] for p in passages)}
        timings["total_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        timings["search_ms"] = round(timings["total_ms"] - timings["load_ms"], 1)
        return SearchResponse(query=query, mode=effective, requested_mode=requested, passages=passages, notes=notes,
                              timings=timings, index=self.index_info(), added_terms=added,
                              roots=roots if any(v != "." for v in roots.values()) else {})

    def _root(self, sid: str, meta: dict) -> str | None:
        """source のルート(今の設定のもの。設定に無い id なら、索引を作ったときのもの)。"""
        if sid in self._source_roots:
            return self._source_roots[sid]
        return next((s.get("root") for s in meta.get("sources", []) if s["id"] == sid), None)

    def _folder(self, sid: str, meta: dict) -> str:
        """エージェントに見せる source のルート: 作業フォルダそのものなら "."、その中なら相対(/ 区切り)、
        外なら絶対のパス。"""
        root = self._root(sid, meta)
        if not root:
            return "?"
        if self._workspace and _same_drive(root, self._workspace):
            rel = os.path.relpath(root, self._workspace)
            if rel != ".." and not rel.startswith(".." + os.sep):
                return Path(rel).as_posix()
        return root

    def _mark_stale(self, db, passages: list[dict], meta: dict, notes: list[str]) -> None:
        """索引の後に変わった(か、無くなった)ファイルの passage に印を付ける。見るのは大きさと更新日時だけ。

        ルートは今の設定のものを使う(作業フォルダの名前を変えた・移した・写したときも、今の木を見る)。
        ルートが見つからない source があれば、変わったかを確かめられないことを notes に書く。"""
        missing: list[str] = []
        ids = sorted({p["file_id"] for p in passages})
        rows = {r[0]: (r[1], r[2]) for r in db.execute(
            f"SELECT id, size, mtime_ns FROM files WHERE id IN ({','.join('?' * len(ids))})", ids)}
        cache: dict[int, str] = {}         # file_id → "" / "stale" / "missing"
        for p in passages:
            fid = p["file_id"]
            if fid not in cache:
                root = self._root(p["source_id"], meta)
                state = ""
                if _outside_root(p["path"]):
                    state = "stale"             # 索引の path は信じない。ルートの外(UNC など)には触れない
                elif root and os.path.isdir(root):
                    try:
                        # 260 文字を超えるパスも、取り込みと同じ形で見る
                        st = os.stat(_long_path(os.path.join(root, p["path"])))
                        state = "stale" if (st.st_size, st.st_mtime_ns) != rows.get(fid) else ""
                    except (FileNotFoundError, NotADirectoryError):
                        state = "missing"
                    except OSError:
                        state = "stale"
                elif p["source_id"] not in missing:
                    missing.append(p["source_id"])
                cache[fid] = state
            if cache[fid]:
                p["stale"] = True               # 消えたファイルも stale(0.3 と同じ)。そのうえで missing を付ける
            if cache[fid] == "missing":
                p["missing"] = True
        if missing:
            notes.append("cannot check whether files changed since the index: source root of "
                         + ", ".join(missing) + " not found; rebuild the index")

    def fallback_files(self, limit: int = 5) -> list[dict]:
        """構文で分けられずに行で分けた行が多いファイル(マクロで文法が崩れている場所を探す手がかり)。"""
        with closing(open_readonly(self.index_path)) as db:
            rows = db.execute("SELECT source_id, path, lines, fallback_lines FROM files WHERE fallback_lines > 0 "
                              "ORDER BY fallback_lines DESC, source_id, path LIMIT ?", (limit,)).fetchall()
        return [{"source_id": s, "path": p, "lines": n, "fallback_lines": fb} for s, p, n, fb in rows]

    def status(self) -> dict:
        m = self.meta()
        sources = []
        for s in m.get("sources", []):
            # root は今の設定のもの(作業フォルダを移した後も、今の場所)。索引を作ったときと違えば indexed_root に
            now = dict(s, root=self._root(s["id"], m), folder=self._folder(s["id"], m))
            if now["root"] != s.get("root"):
                now["indexed_root"] = s.get("root")
            sources.append(now)
        return {
            "fallback_files": self.fallback_files(),
            "index_path": str(self.index_path), "index_bytes": self.index_path.stat().st_size,
            "schema_version": m.get("schema_version"), "tool_version": m.get("tool_version"),
            "created_at": m.get("created_at"), "revision": m.get("revision", ""),
            "sources": sources, "stats": m.get("stats", {}),
            "embedding": m.get("embedding"), "timings": m.get("timings", {}),
            "chunk_config": m.get("chunk_config", {}),
            "modes": list(MODES) if m.get("embedding") else ["keyword"],
        }


def _same_drive(a: str, b: str) -> bool:
    """os.path.relpath が使えるか(Windows で、ドライブが違うと ValueError になる)。"""
    return os.path.splitdrive(os.path.abspath(a))[0].lower() == os.path.splitdrive(os.path.abspath(b))[0].lower()


def _outside_root(path: str) -> bool:
    """索引から来た path が、ルートの外を指しうるか(絶対パス・ドライブつき・UNC・/ か \\ で始まる・.. を含む)。
    索引のファイルは、取ってきたリポジトリに入っていることもあるので、そういう path ではファイルを見ない。
    取り込みが書く path は、ルートからの相対(/ 区切り)なので当たらない。"""
    pure = PureWindowsPath(path) if os.name == "nt" else PurePosixPath(path)
    return not path or bool(pure.anchor) or ".." in pure.parts


def freshness(cfg: Config, examples: int = 5) -> dict:
    """索引の後に、変わった・増えた・消えたファイルを数える(`ctxf status --check`)。

    取り込みと同じ規則でフォルダを歩いてファイルを読む(索引に入るかは、中身を見ないと決まらないものがある)ので、
    大きいコーパスでは数十秒かかる。検索の経路では使わない。消えたファイルには、索引に入らなくなったもの
    (大きくなりすぎた、など)と、設定から外した source のものも入る。ルートが無い source は数えず、名前を返す。
    """
    t = time.perf_counter()
    with closing(open_readonly(cfg.index_path)) as db:
        indexed = {(sid, path): (size, mtime) for sid, path, size, mtime in
                   db.execute("SELECT source_id, path, size, mtime_ns FROM files")}
    changed: list[str] = []
    added: list[str] = []
    seen: set[tuple[str, str]] = set()
    missing_roots = [s.id for s in cfg.sources if not s.root.is_dir()]
    for source in cfg.sources:
        if source.id in missing_roots:
            continue
        for f in iter_source_files(source, cfg.index, IngestStats(), cfg.sources):
            seen.add((source.id, f.path))
            old = indexed.get((source.id, f.path))
            if old is None:
                added.append(f"{source.id}:{f.path}")
            elif old != (f.size, f.mtime_ns):
                changed.append(f"{source.id}:{f.path}")
    deleted = sorted(f"{sid}:{path}" for sid, path in indexed if (sid, path) not in seen and sid not in missing_roots)
    return {"changed": len(changed), "added": len(added), "deleted": len(deleted),
            "examples": {"changed": changed[:examples], "added": added[:examples], "deleted": deleted[:examples]},
            "missing_roots": missing_roots, "seconds": round(time.perf_counter() - t, 1)}

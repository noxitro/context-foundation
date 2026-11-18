"""索引づくり: 取り込み → 分割 → keyword の索引 → 埋め込み → 1 ファイルに保存。

毎回、取り込みと分割はすべてのファイルについてやり直し、新しいファイルに書いて、できあがってから
置き換える(作り直しの最中も、前の索引で検索できる)。時間のほとんどを占める埋め込みだけは、前の索引に
同じ文の埋め込みがあれば、それを使い回す(差分更新)。使い回すかどうかは、埋め込みに渡す文そのもの
(場所・スコープ・シグネチャ・本文)のハッシュで決めるので、変更の検知に版管理(git など)は使わない。
変わったファイル・増えたファイルの passage だけが埋め込み直され、消えたファイルの passage は新しい索引に
入らない。passage は作り直したときと同じになり、同じ文には同じ埋め込みを使う(埋め込みの計算は、まとめて
計算する相手によって浮動小数の末尾が変わりうるので、作り直しとの厳密な一致は保証しない。実測では一致した)。
中身の無い passage(include だけのファイルの頭など。下の has_content)は埋め込まない(意味検索に出さない)。
所要時間と統計は、索引の meta に残す。
途中で止められても、計算した埋め込みは <索引>.partial に書き足してあるので、次の索引づくりは続きから計算する。
前の索引を置き換えられなかったときは、できた索引を <索引>.new に残す(次の索引づくりが、そこから使い回す)。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import uuid
import zlib
from collections import defaultdict
from contextlib import closing
from pathlib import Path

import numpy as np

from . import __version__, command_hint
from .chunking import chunk_file
from .chunking.base import chunk_lines
from .config import CONFIG_DIR_NAME, Config, ConfigError
from .embed import CPU_PROVIDER, EmbedPool, Embedder, HashEmbedder, ModelUnavailable, make_embedder
from .ingest import IngestStats, SourceFile, iter_source_files, shown_path
from .store import IndexError_, IndexWriter, load_vectors, open_readonly, read_meta, unpack_text
from .sysinfo import peak_memory_bytes, pid_alive
from .textproc import fts_text

EMBED_TASK = 128       # 1 つの仕事として埋め込む passage の数(DB から読む単位でもある)
RESUME_EVERY = 1024    # 計算した埋め込みを、続きから作るためのファイルに書き足す間隔(passage の数)


class BuildError(RuntimeError):
    """索引を作れない(設定に合うファイルが 1 つも無いなど)。文面は利用者に見せる。"""


class ReplaceError(BuildError):
    """できあがった索引で、前の索引を置き換えられない。できた索引は <索引>.new に残してある。"""


_LICENSE_WORDS = re.compile(r"copyright|licen[sc]e|spdx|著作権", re.IGNORECASE)
_DIRECTIVES = ("#include", "#pragma", "#if", "#define", "#!")


def _after_license_header(text: str) -> str | None:
    """先頭の、著作権やライセンスのコメントを外した残り(空のこともある)。そういうコメントが無ければ None。"""
    lines = text.split("\n")
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines):
        return None
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
        return None
    if end > len(lines) or not _LICENSE_WORDS.search("\n".join(lines[i:end])):
        return None
    return "\n".join(lines[end:]).lstrip("\n")


def strip_license_header(text: str) -> str:
    """ファイルの先頭にある、著作権やライセンスのコメントを外す(埋め込みの文だけ。passage の本文は変えない)。

    どのファイルにも同じ文が付いていると、ファイルの頭の passage どうしが似てしまい、どの質問にも
    上位に出やすくなるため。
    """
    rest = _after_license_header(text)
    return rest if rest is not None and rest.strip() else text


# 中身の無い行(C・C++・C#): 前処理の行(値のある #define は除く)、using、名前空間の開き閉じ、extern "C"、
# 前方宣言、括弧だけの行。後ろに付いた短いコメント(`}  // namespace Foo`)は、あってもよい
_FORWARD_DECLARATION = r"(?:class|struct|union|enum(?:\s+class)?)\s+(?:[A-Z][A-Z0-9_]*\s+)?[\w:]+\s*;"
_NO_CONTENT_LINE = re.compile(r"""^(?:
      \#\s*(?:include|import|pragma|if|ifdef|ifndef|else|elif|endif|undef|region|endregion|line|error|warning
              |nullable)\b.*
    | \#\s*define\s+\w+                                    # 値の無い #define(include ガード)
    | (?:global\s+)?using\s+(?:namespace\s+|static\s+)?[\w:.]+\s*;
    | (?:inline\s+)?namespace(?:\s+[\w:.]+)?\s*[{;]?        # 開く行と、C# の `namespace Foo.Bar;`
    | (?:inline\s+)?namespace\s+[\w:.]+\s*\{\s*(?:%(fwd)s\s*)*\}   # `namespace Foo { class Bar; }`
    | extern\s+"C(?:\+\+)?"\s*\{?
    | %(fwd)s                                              # 前方宣言(`class FOO_API Bar;` も)
    | [{}()\[\];,]+
    )\s*(?://.*|/\*.*\*/)?$""" % {"fwd": _FORWARD_DECLARATION}, re.VERBOSE)
_SYNTAX_LANGUAGES = ("c", "cpp", "csharp")


def has_content(text: str, language: str, start_line: int = 0) -> bool:
    """passage に、意味で探す中身があるか。無いものは埋め込まない(keyword では引ける)。

    ファイルの頭の「ライセンスのコメント + include の並び」や、名前空間の開き閉じ・前方宣言だけの passage は、
    埋め込みに渡す文がほとんど場所(パス)だけになる。パスにはクラスの名前が入っているので、どの質問にも
    上位に出て、答えの passage を押し出す(公開の C++ のコードで測ると、日本語の質問の意味検索の上位 10 件の
    37% を占めていた。docs/design.md)。判定するのは、構文で分ける言語(C・C++・C#)だけ。
    """
    if language not in _SYNTAX_LANGUAGES:
        return True
    if start_line == 1:
        rest = _after_license_header(text)
        if rest is not None:
            text = rest
    return any(s and not _NO_CONTENT_LINE.match(s) for s in (line.strip() for line in text.splitlines()))


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


def text_key(text: str) -> bytes:
    """埋め込みに渡す文の鍵(使い回すかどうかを決める)。"""
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).digest()


# 埋め込みに渡す文(embedding_text)の作り方の版。embedding_text や strip_license_header を変えたら上げる
# (前の索引の鍵は、前の索引の passage から今の embedding_text で作り直すので、作り方が変わったら使い回せない)
EMBED_TEXT_VERSION = 1

# 前の索引の埋め込みを使い回してよいか、の判定に使う項目(どれかが違えば、すべて埋め込み直す)
_REUSE_KEYS = ("model", "revision", "dim", "onnx_file", "onnx_bytes", "pooling", "max_tokens", "passage_prefix",
               "vector_dtype", "embed_text_version")
# 0.1 で作った索引の meta に無い項目。無いときは、今の設定が既定のままなら同じとみなす(下の _missing_ok)
_ADDED_IN_0_2 = ("onnx_bytes", "passage_prefix", "embed_text_version")


def embedding_signature(emb: Embedder, vector_dtype: str) -> dict:
    sig = dict(emb.info(), vector_dtype=vector_dtype, embed_text_version=EMBED_TEXT_VERSION)
    spec = getattr(emb, "spec", None)
    sig["passage_prefix"] = getattr(spec, "passage_prefix", "") if spec is not None else ""
    return {k: sig.get(k) for k in _REUSE_KEYS}


def query_signature(emb: Embedder) -> dict:
    """検索のときに、索引の埋め込みと今の設定で同じでなければならない項目(保存の形は違ってもよい)。
    query_prefix は 0.4.0 から meta に書く。"""
    sig = embedding_signature(emb, "")
    sig.pop("vector_dtype")
    spec = getattr(emb, "spec", None)
    sig["query_prefix"] = getattr(spec, "query_prefix", "") if spec is not None else ""
    return sig


def signature_diff(old: dict, signature: dict, ecfg) -> list[str]:
    """索引の meta の埋め込み(old)と、今の作り方(signature)で違う項目。meta に無い項目は _missing_ok で決める。"""
    return [k for k in signature
            if (old.get(k) != signature.get(k) if k in old
                else signature.get(k) is not None and not _missing_ok(k, ecfg))]


def _missing_ok(key: str, ecfg) -> bool:
    """前の索引の meta に無い項目を、同じとみなしてよいか(0.1 の索引は、既定の設定で作ったものとして扱う)。"""
    if key == "query_prefix":
        return True                                    # 0.3 までの索引には無い。分からないので同じとみなす
    if key == "embed_text_version":
        return EMBED_TEXT_VERSION == 1                 # 0.1 の embedding_text は版 1 と同じ
    if key == "passage_prefix":
        return ecfg is None or ecfg.passage_prefix is None
    if key == "onnx_bytes":
        return ecfg is None or (not ecfg.model_dir and not ecfg.onnx_file)
    return False


def _previous_vectors(dest: Path, signature: dict, log, ecfg=None) -> tuple[dict[bytes, int], np.ndarray | None]:
    """前の索引から、使い回せる埋め込みを読む。返すのは (文の鍵 → 行, ベクトルの行列)。

    前の索引が無い・壊れている・埋め込みの作り方(モデル・版・次元・保存の形・文の作り方など)が違うときは、
    何も使い回さない(空の辞書)。索引の形式の版が違っても、表が読めて埋め込みの作り方が同じなら使い回す
    (鍵は埋め込みに渡す文そのもののハッシュなので、鍵が合えば同じ埋め込みになる)。"""
    if not dest.is_file():
        return {}, None
    try:
        with closing(open_readonly(dest)) as db:
            meta = read_meta(db, check_version=False)
            old = meta.get("embedding") or {}
            if not old:
                return {}, None
            diff = signature_diff(old, signature, ecfg)
            if diff:
                log(f"  前の索引とは埋め込みの作り方が違う({', '.join(diff)})ので、すべて埋め込み直します")
                return {}, None
            mat = load_vectors(db, old["dim"], old.get("vector_dtype", "float16"))
            n_chunks = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
            if len(mat) != n_chunks:
                return {}, None
            embedded = mat.any(axis=1)        # すべて 0 の行は、埋め込まなかった passage(使い回さない)
            keys: dict[bytes, int] = {}
            for cid, path, scope, signature_, text, start_line in db.execute(
                    "SELECT id, path, scope, signature, text, start_line FROM chunks ORDER BY id"):
                if embedded[cid - 1]:
                    keys.setdefault(text_key(embedding_text(path, scope, signature_, unpack_text(text), start_line)),
                                    cid - 1)
            return keys, mat
    except (IndexError_, sqlite3.Error, ValueError, KeyError, IndexError, zlib.error) as e:
        log(f"  前の索引を読めないので、すべて埋め込み直します({str(e).splitlines()[0]})")
        return {}, None


def _had_embeddings(dest: Path) -> bool:
    if not dest.is_file():
        return False
    try:
        with closing(open_readonly(dest)) as db:
            return bool(read_meta(db, check_version=False).get("embedding"))
    except (IndexError_, sqlite3.Error, ValueError):
        return False


def _pending_path(dest: Path) -> Path:
    """置き換えられなかった、できあがった索引の置き場(次の索引づくりが、ここから埋め込みを使い回す)。"""
    return dest.with_name(dest.name + ".new")


def _partial_path(dest: Path) -> Path:
    """途中までの埋め込みの置き場(_Resume)。"""
    return dest.with_name(dest.name + ".partial")


def _previous_index(dest: Path) -> Path:
    """埋め込みを使い回す前の索引。置き換えられずに残った <索引>.new があれば、そちらのほうが新しい。"""
    pending = _pending_path(dest)
    return pending if pending.is_file() else dest


def previous_had_embeddings(dest: Path) -> bool:
    """前の索引(置き換えられずに残った <索引>.new があれば、そちら)に埋め込みがあるか。"""
    return _had_embeddings(_previous_index(dest))


def _remove_dead_tmp(dest: Path, log) -> None:
    """止められた索引づくりが残した途中のファイル(<索引>.tmp-<pid>)のうち、そのプロセスがもう無いものを消す。"""
    head = dest.name + ".tmp-"
    try:
        names = [p for p in dest.parent.iterdir() if p.name.startswith(head)]
    except OSError:
        return
    for p in names:
        pid = p.name[len(head):]
        # 末尾は、この道具が書く ASCII の数字だけ(「²」なども isdigit だが int にできない)
        if not (pid.isascii() and pid.isdigit()) or len(pid) > 10:
            continue
        if int(pid) == os.getpid() or pid_alive(int(pid)) is not False:
            continue
        try:
            p.unlink()
            log(f"  止められた索引づくりの途中のファイルを消しました: {p.name}")
        except OSError:
            pass


class _Resume:
    """計算した埋め込みを、索引とは別のファイル(<索引>.partial)に、RESUME_EVERY 件ごとに書き足す。

    索引づくりが途中で止められても(端末を閉じた・再起動した・時間切れで止められた)、次の索引づくりは、ここに
    ある分を計算し直さない。使うのは埋め込みの作り方(embedding_signature)がまったく同じときだけ。埋め込みつきの
    索引ができあがったら消す。`--no-embed` で作ったときは残し、次に埋め込みつきで作るときに使う(設定で埋め込みを
    切った([embedding] enabled = false)索引ができあがったときは消す)。鍵は、前の索引の使い回しと同じ text_key。"""

    def __init__(self, dest: Path, signature: dict):
        self.path = _partial_path(dest)
        self.signature = json.dumps(signature, sort_keys=True, ensure_ascii=False)
        self.dim, self.dtype = signature["dim"], signature["vector_dtype"]
        self.db: sqlite3.Connection | None = None
        self.pending: list[tuple[bytes, bytes]] = []
        self.broken = False
        if os.path.islink(self.path):
            # 置かれたリンクをたどって、作業フォルダの外にファイルを作らない。リンクだけを消す
            try:
                self.path.unlink()
            except OSError:
                self.broken = True

    def load(self, log) -> tuple[dict[bytes, int], np.ndarray | None]:
        """前の、止められた索引づくりが計算した分。返すのは (文の鍵 → 行, ベクトルの行列)。"""
        if self.broken or not self.path.is_file():
            return {}, None
        try:
            with closing(sqlite3.connect(str(self.path))) as db:
                row = db.execute("SELECT value FROM meta WHERE key = 'signature'").fetchone()
                if row is None or row[0] != self.signature:
                    raise ValueError("埋め込みの作り方が違う")
                rows = db.execute("SELECT key, data FROM vectors").fetchall()
            # この道具が書く形(鍵もベクトルも BLOB、長さは次元 × 要素の大きさ)でない行は使わない
            size = self.dim * np.dtype(self.dtype).itemsize
            rows = [(k, d) for k, d in rows if isinstance(k, bytes) and isinstance(d, bytes) and len(d) == size]
            mat = np.empty((len(rows), self.dim), dtype=np.float32)
            keys: dict[bytes, int] = {}
            for i, (key, data) in enumerate(rows):
                mat[i] = np.frombuffer(data, dtype=self.dtype)
                keys[key] = i
        except (sqlite3.Error, ValueError, TypeError, OverflowError, MemoryError) as e:
            log(f"  前回の途中までの埋め込みは使えないので消します({str(e).splitlines()[0] if str(e) else e})")
            self.discard()
            return {}, None
        if keys:
            log(f"  前回の途中までの埋め込みが {len(keys):,} passage あります(続きから計算します)")
        return keys, mat

    def add(self, keys: list[bytes], vecs: np.ndarray) -> None:
        if self.broken:
            return
        self.pending.extend(zip(keys, (np.ascontiguousarray(v).tobytes() for v in vecs.astype(self.dtype))))
        if len(self.pending) >= RESUME_EVERY:
            try:
                self.flush()
            except sqlite3.Error:
                # 書けなくても(場所が足りないなど)、索引づくりは続ける。次回に続きから作れないだけ
                self.broken, self.pending = True, []

    def flush(self) -> None:
        if not self.pending:
            return
        if self.db is None:
            self.db = sqlite3.connect(str(self.path), timeout=30)
            self.db.executescript("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                                  "CREATE TABLE IF NOT EXISTS vectors(key BLOB PRIMARY KEY, data BLOB NOT NULL);")
            self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('signature', ?)", (self.signature,))
        self.db.executemany("INSERT OR REPLACE INTO vectors(key, data) VALUES(?,?)", self.pending)
        self.db.commit()
        self.pending = []

    def close(self) -> None:
        """残りを書いて閉じる(中断されたときにも呼ぶ)。"""
        try:
            if not self.broken:
                self.flush()
        except sqlite3.Error:
            pass
        if self.db is not None:
            self.db.close()
            self.db = None

    def discard(self) -> None:
        self.pending = []
        self.close()
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


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
    # できあがった索引は捨てない。<索引>.new に移しておき、次の索引づくりがそこから埋め込みを使い回す
    keep = _pending_path(dest)
    try:
        os.replace(tmp, keep)
        kept = f"できあがった索引は {keep} に残したので、次の索引づくりは埋め込みをそこから使い回します"
    except OSError:
        kept = f"できあがった索引は {tmp} に残しました"
    raise ReplaceError(f"索引を置き換えられません: {dest}\n"
                       "この索引で検索しているプログラム(Copilot のセッションなど)を閉じてから、もう一度流してください。"
                       "それでも直らなければ、このファイルを消してから作り直してください。" + kept)


def _chunk_one(f: SourceFile, cfg: Config, stats: IngestStats, log):
    """1 つのファイルを分ける。分割の途中で落ちたら(深すぎる入れ子など)、そのファイルだけを行で分け直す
    (1 つのファイルのために、索引づくり全体を止めないため)。"""
    try:
        return chunk_file(f.text, f.language, cfg.chunk)
    except Exception as e:
        where = f"{f.source_id}:{f.path}"
        first = str(e).splitlines()[0] if str(e) else ""
        log(f"  注意: {shown_path(where)} を構文で分けられなかったので、行で分けます({type(e).__name__}: {first})")
        stats.chunk_failure(where)
        chunks, cs = chunk_lines(f.text, cfg.chunk)
        cs.parse_failed = True
        cs.fallback_lines = cs.lines
        return chunks, cs


def build_index(cfg: Config, *, use_embedding: bool | None = None, embedder: Embedder | None = None,
                require_embedding: bool = False, progress: bool = True, log=None, revision: str = "",
                reuse: bool = True) -> dict:
    """索引を作り直し、統計(meta に入れたものと同じ)を返す。

    use_embedding: None なら設定([embedding] enabled)に従う。
    モデルが手元に無いときは、keyword だけの索引を作る。ただし require_embedding のときと、前の索引に埋め込みが
    あるとき(keyword だけの索引に置き換えると、埋め込みを捨てることになる)は ModelUnavailable を投げる。
    keyword だけの索引でよければ use_embedding=False(`--no-embed`)を渡す。
    reuse: 前の索引に同じ文の埋め込みがあれば使い回す(差分更新)。止められた索引づくりが計算した分
    (<索引>.partial)も使う。False なら、前の索引の分も <索引>.partial の分も使わず、すべて埋め込み直す
    (<索引>.partial は、埋め込みを始める前に消す)。
    source のルートが無いときは ConfigError、索引に入るファイルが無いときは BuildError、できた索引で前の索引を
    置き換えられないときは ReplaceError(できた索引は <索引>.new に残す)。
    """
    say = _Progress(progress)
    log = log or (lambda s: say(s, force=True))
    t_start = time.time()
    dest = cfg.index_path
    previous = _previous_index(dest)
    # 1 つでも source のルートが無ければ、何も作らずに止める(前の索引はそのまま使える)
    missing_roots = [s for s in cfg.sources if not s.root.is_dir()]
    if missing_roots:
        raise ConfigError("source のルートがありません:\n"
                          + "".join(f"  {s.id}: {s.root}\n" for s in missing_roots)
                          + "設定の root を直すか、ドライブをつないでから、もう一度流してください"
                            "(前の索引はそのまま使えます)")
    _remove_dead_tmp(dest, log)

    # 埋め込みモデルは、取り込みの前に確かめる(取り込みの後で止まると、その時間が無駄になる)
    want = cfg.embedding.enabled if use_embedding is None else use_embedding
    emb = None
    if want:
        try:
            emb = embedder or make_embedder(cfg.embedding, for_index=True, log=log)
        except ModelUnavailable as e:
            if require_embedding:
                raise
            if previous_had_embeddings(dest):
                raise ModelUnavailable(
                    f"{e}\n前の索引には埋め込みがあるので、keyword だけの索引に置き換えずに止めました"
                    "(置き換えると、次に埋め込みつきで作るときに、すべての passage を埋め込み直すことになります)。\n"
                    f"モデルを用意するなら `{command_hint('model download')}`。"
                    f"keyword だけの索引でよければ `{command_hint('index --no-embed')}`") from None
            log(f"注意: {e}\n      → keyword(BM25)だけの索引を作ります")

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
            for f in iter_source_files(src, cfg.index, ingest_stats, cfg.sources):
                chunks, cs = _chunk_one(f, cfg, ingest_stats, log)
                if not chunks:
                    ingest_stats.skip("empty", f.path)
                    ingest_stats.included -= 1
                    continue
                nlines = f.text.count("\n") + (0 if f.text.endswith("\n") else 1)
                try:
                    file_id = writer.add_file(f.source_id, f.path, f.size, f.mtime_ns, nlines, f.language,
                                              f.encoding, len(chunks), cs.fallback_lines)
                    fts_path = fts_text(f.path)
                    for c in chunks:
                        writer.add_chunk(file_id, f.source_id, f.path, c, fts_text(c.text),
                                         fts_text(" ".join([c.scope, c.signature] + c.symbols)), fts_path)
                        sizes.append(len(c.text))
                        methods[c.method] += 1
                except Exception as e:
                    e.add_note(f"索引に書いている途中のファイル: {src.id}:{shown_path(f.path)}")
                    raise
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
        for n in ingest_stats.nested_roots:
            outer, inner = n.split(" → ")
            log(f"  {outer} は source {inner} のルートなので、{inner} の側だけで取り込みました")
        t_chunk = time.time()
        log(f"取り込みと分割: {total_files:,} ファイル / {total_lines:,} 行 / {total_chunks:,} passage"
            f"({t_chunk - t_start:.1f} 秒)")
        if not total_chunks:
            raise BuildError("索引に入るファイルが 1 つもありません。設定の root・include・exclude を確かめてください")

        # 埋め込み
        embedding_meta = None
        resume = None
        embed_counts = {"reused": 0, "embedded": 0, "unembedded": 0}
        t_embed0 = time.time()
        if emb is not None:
            dtype = cfg.embedding.vector_dtype
            signature = embedding_signature(emb, dtype)
            old_keys, old_mat = _previous_vectors(previous, signature, log, cfg.embedding) if reuse else ({}, None)
            # 止められた索引づくりが計算した分も、前の索引の分と同じように使い回す(後ろに並べる)
            resume = _Resume(dest, signature)
            if reuse:
                part_keys, part_mat = resume.load(log)
            else:                   # --full: 途中までの埋め込みも使わない(すべて埋め込み直す)
                resume.discard()
                part_keys, part_mat = {}, None
            if part_keys:
                base = 0 if old_mat is None else len(old_mat)
                for k, r in part_keys.items():
                    old_keys.setdefault(k, base + r)
                old_mat = part_mat if old_mat is None else np.vstack([old_mat, part_mat])
            del part_keys, part_mat

            # 1. 新しい passage ごとに、使い回せる前の行を決める(無ければ -1。中身が無く、埋め込まないものは -2)
            src_rows = np.full(total_chunks, -1, dtype=np.int64)
            new_keys: list[bytes | None] = [None] * total_chunks     # 埋め込み直す passage の鍵(続きから作るため)
            with closing(open_readonly(tmp)) as rdb:
                for cid, path, scope, sig, text, start_line, language in rdb.execute(
                        "SELECT c.id, c.path, c.scope, c.signature, c.text, c.start_line, f.language "
                        "FROM chunks c JOIN files f ON f.id = c.file_id ORDER BY c.id"):
                    body = unpack_text(text)
                    if not has_content(body, language, start_line):
                        src_rows[cid - 1] = -2
                        continue
                    key = text_key(embedding_text(path, scope, sig, body, start_line))
                    row = old_keys.get(key)
                    if row is not None:
                        src_rows[cid - 1] = row
                    else:
                        new_keys[cid - 1] = key
            missing = np.flatnonzero(src_rows == -1)     # 埋め込み直す passage(0 始まりの位置)
            unembedded = int((src_rows == -2).sum())
            reused = int((src_rows >= 0).sum())
            if reused:
                log(f"  前の索引の埋め込みを使い回します: {reused:,} passage、"
                    f"埋め込み直すのは {len(missing):,} passage")

            # 2. 使い回せない passage だけを埋め込む
            # 保存の形(既定は float16)で持つ。float16 → float32 → float16 は値が変わらない
            vectors = np.zeros((total_chunks, emb.dim), dtype=dtype)    # 埋め込まない passage は 0 のまま
            if reused:
                vectors[src_rows >= 0] = old_mat[src_rows[src_rows >= 0]].astype(dtype)
            del old_mat, old_keys
            workers = 1
            provider = getattr(emb, "provider", "")
            on_gpu = bool(provider) and provider != CPU_PROVIDER
            # GPU では、プロセスを分けない(1 つの GPU を取り合うだけで、速くならない)
            if len(missing) and embedder is None and not isinstance(emb, HashEmbedder) and not on_gpu:
                workers = cfg.embedding.workers or 1
                workers = max(1, min(workers, -(-len(missing) // EMBED_TASK)))
            pool = EmbedPool(cfg.embedding, workers) if workers > 1 else None

            def batches(rdb):
                for s in range(0, len(missing), EMBED_TASK):
                    ids = [int(i) + 1 for i in missing[s:s + EMBED_TASK]]
                    marks = ",".join("?" * len(ids))
                    rows = {r[0]: r[1:] for r in rdb.execute(
                        f"SELECT id, path, scope, signature, text, start_line FROM chunks WHERE id IN ({marks})",
                        ids)}
                    yield [embedding_text(rows[i][0], rows[i][1], rows[i][2], unpack_text(rows[i][3]), rows[i][4])
                           for i in ids]

            done = 0
            try:
                with closing(open_readonly(tmp)) as rdb:
                    results = pool.map(batches(rdb)) if pool else (emb.embed_passages(t) for t in batches(rdb))
                    for vecs in results:
                        part = missing[done:done + len(vecs)]
                        vectors[part] = vecs.astype(dtype)
                        resume.add([new_keys[int(i)] for i in part], vecs)
                        done += len(vecs)
                        el = time.time() - t_embed0
                        rate = done / el if el > 0 else 0
                        eta = (len(missing) - done) / rate if rate else 0
                        say(f"  埋め込み中: {done:,} / {len(missing):,} passage"
                            f"({rate:.0f} 件/秒、残り 約 {eta:.0f} 秒)")
            finally:
                resume.close()
                if pool:
                    pool.close()
            del new_keys

            # 3. id の順に書く
            writer.add_vectors(1, vectors, dtype)
            writer.commit()
            del vectors
            embedding_meta = dict(emb.info(), vector_dtype=dtype, workers=workers, provider=provider,
                                  passage_prefix=signature["passage_prefix"],
                                  query_prefix=query_signature(emb)["query_prefix"],
                                  embed_text_version=EMBED_TEXT_VERSION)
            embed_counts = {"reused": reused, "embedded": int(len(missing)), "unembedded": unembedded}
            log(f"埋め込み: {len(missing):,} passage を計算、{reused:,} passage を使い回し、"
                f"中身の無い {unembedded:,} passage は埋め込まず"
                f"({time.time() - t_embed0:.1f} 秒、モデル {emb.name}、"
                + (f"GPU({provider})" if on_gpu else f"CPU、{workers} プロセス") + ")")
        t_embed = time.time()
        if embedding_meta is None and reuse and _had_embeddings(previous):
            if cfg.embedding.enabled and os.path.isfile(_partial_path(dest)):
                log("注意: 前の索引には埋め込みがありましたが、今回は keyword だけの索引になります。"
                    f"途中までの埋め込み({_partial_path(dest).name})は残してあり、次に埋め込みつきで作るときに"
                    "使います(そこに無い passage は埋め込み直します)")
            else:
                log("注意: 前の索引には埋め込みがありましたが、今回は keyword だけの索引になります。"
                    "次に埋め込みつきで作るときは、すべての passage を埋め込み直します(時間がかかります)")

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
            **ingest_stats.extra_meta(),
        }
        writer.set_meta("stats", stats)
        writer.set_meta("sources", list(per_source.values()))
        writer.set_meta("embedding", embedding_meta)
        writer.set_meta("chunk_config", {"max_bytes": cfg.chunk.max_bytes, "min_bytes": cfg.chunk.min_bytes,
                                         "line_window": cfg.chunk.line_window, "macro_hints": cfg.chunk.macro_hints})
        writer.set_meta("tool_version", __version__)
        writer.set_meta("created_at", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        writer.set_meta("revision", revision)     # コーパスの版(コミットやチェンジリストの番号。利用者が渡す)
        # 作るたびに変わる印。検索の途中で作り直されたかを見分ける(0.4.0 から。0.3 までの索引には無い)
        writer.set_meta("build_id", uuid.uuid4().hex)
        timings = {"ingest_chunk_seconds": round(t_chunk - t_start, 2),
                   "embed_seconds": round(t_embed - t_embed0, 2) if embedding_meta else 0.0,
                   "embedded_passages": embed_counts["embedded"], "reused_passages": embed_counts["reused"],
                   # 中身が無いので埋め込まなかった passage(意味検索には出ない。keyword では引ける)
                   "unembedded_passages": embed_counts["unembedded"]}
        writer.set_meta("timings", timings)        # finalize と total は、下で書き足す
        t_fin0 = time.time()
        writer.finish()
        # 仕上げ(FTS の最適化)の時間は、閉じた後で分かるので、開き直して書き足す
        timings["finalize_seconds"] = round(time.time() - t_fin0, 2)
        timings["total_seconds"] = round(time.time() - t_start, 2)
        timings["peak_memory_bytes"] = peak_memory_bytes()     # このプロセスの分(埋め込みを別プロセスに分けた分は入らない)
        with closing(sqlite3.connect(str(tmp))) as db:
            db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('timings', ?)", (json.dumps(timings),))
            db.commit()
        _replace(tmp, dest)
    except ReplaceError:
        raise                   # できた索引は <索引>.new に残した(消さない)
    except BaseException:
        writer.abort()
        raise
    # 置き換えたので、続きから作るための埋め込みと、前に置き換えられずに残った索引は要らない
    if resume is not None:
        resume.discard()
    elif not cfg.embedding.enabled:
        # 設定で埋め込みを切ったときは、途中までの埋め込みを残しても使われない(--no-embed のときは残して、次に使う)
        try:
            _partial_path(dest).unlink(missing_ok=True)
        except OSError:
            pass
    try:
        _pending_path(dest).unlink(missing_ok=True)
    except OSError:
        pass
    # .cf の中身(設定と索引)を git に入れないように、全部を無視する .gitignore を置く
    ignore = cfg.cf_dir / ".gitignore"
    if cfg.cf_dir.name == CONFIG_DIR_NAME and not os.path.lexists(ignore):      # リンクの先には書かない
        try:
            ignore.write_text("*\n", encoding="utf-8")
        except OSError:
            pass
    result = {"index_path": str(dest), "index_bytes": dest.stat().st_size, "stats": stats, "timings": timings,
              "embedding": embedding_meta, "sources": list(per_source.values())}
    log(f"索引ができました: {dest}({result['index_bytes'] / 1e6:.1f} MB、合計 {timings['total_seconds']:.1f} 秒)")
    return result

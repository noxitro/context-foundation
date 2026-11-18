"""索引の保存: 外部のサービスなしの 1 ファイル(SQLite)。

  files       索引に入れたファイル(大きさ・更新日時・行数。索引の後に変わったかを見分けるため)
  chunks      passage(場所・種類・スコープ・シグネチャ・本文。本文は zlib で縮めて持つ)
  chunks_fts  keyword 検索の索引(FTS5。語は textproc で分けてから入れる。本文は持たない)
  vectors     埋め込み(意味検索)。passage の id の順に並べ、まとまりごとに 1 行で持つ(検索は総当たり)
  file_chunking  ファイルごとの分割の控え(中身のハッシュと分割の統計)。次の索引づくりが、中身の変わらない
              ファイルの passage を分け直さずに写すため(0.4.5 から。検索では使わない)
  skipped_files  名前の決まりは通ったが、大きさや中身で除いたファイル(大きさ・更新日時・理由)。セッションの
              始まりの差分更新が、ファイルを読まずに、索引の後に変わったかを見分けるため(0.4.6 から。検索では使わない)
  meta        索引の版・作成日時・統計・埋め込みモデル

読むときは読み取り専用で開き、呼び出しの間は開いたままにしない(作り直しで置き換えられるように)。
"""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.parse
import zlib
from contextlib import closing
from pathlib import Path

import numpy as np

from . import INDEX_SCHEMA_VERSION, command_hint

VECTOR_BLOCK = 4096    # 1 行にまとめるベクトルの数

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE files(
    id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL, lines INTEGER NOT NULL, language TEXT NOT NULL, encoding TEXT NOT NULL,
    chunks INTEGER NOT NULL, fallback_lines INTEGER NOT NULL, UNIQUE(source_id, path));
CREATE TABLE chunks(
    id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, source_id TEXT NOT NULL, path TEXT NOT NULL,
    start_line INTEGER NOT NULL, end_line INTEGER NOT NULL, kind TEXT NOT NULL, method TEXT NOT NULL,
    scope TEXT NOT NULL, signature TEXT NOT NULL, symbols TEXT NOT NULL, part TEXT NOT NULL, text BLOB NOT NULL);
CREATE INDEX chunks_source ON chunks(source_id);
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    body, symbols, path, content='', tokenize="unicode61 remove_diacritics 0 tokenchars '_'");
CREATE TABLE vectors(block INTEGER PRIMARY KEY, first_id INTEGER NOT NULL, count INTEGER NOT NULL, data BLOB NOT NULL);
CREATE TABLE file_chunking(file_id INTEGER PRIMARY KEY, digest BLOB NOT NULL, lines INTEGER NOT NULL,
    inner_error_units INTEGER NOT NULL);
CREATE TABLE skipped_files(source_id TEXT NOT NULL, path TEXT NOT NULL, size INTEGER, mtime_ns INTEGER,
    reason TEXT NOT NULL);
"""


class IndexError_(Exception):
    """索引が無い・版が合わない・壊れている。文面は利用者に見せる。"""


def pack_text(text: str) -> bytes:
    return zlib.compress(text.encode("utf-8"), 6)


def unpack_text(blob: bytes) -> str:
    return zlib.decompress(blob).decode("utf-8")


class IndexWriter:
    """新しい索引ファイルに書く(作り直しの方式。途中のファイルに書いて、最後に置き換える)。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(self.path):          # リンクなら、リンクだけを消す(指す先に索引を書かない)
            self.path.unlink()
        self.db = sqlite3.connect(str(self.path))
        self.db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA page_size=8192;")
        self.db.executescript(SCHEMA)
        self.next_chunk_id = 1
        self._vec_block = 0

    def add_file(self, source_id: str, path: str, size: int, mtime_ns: int, lines: int, language: str,
                 encoding: str, chunks: int, fallback_lines: int) -> int:
        cur = self.db.execute(
            "INSERT INTO files(source_id, path, size, mtime_ns, lines, language, encoding, chunks, fallback_lines) "
            "VALUES(?,?,?,?,?,?,?,?,?)", (source_id, path, size, mtime_ns, lines, language, encoding, chunks,
                                         fallback_lines))
        return cur.lastrowid

    def add_file_chunking(self, file_id: int, digest: bytes, lines: int, inner_error_units: int) -> None:
        """分割の控え(次の索引づくりが、中身の同じファイルの passage を写すため)。"""
        self.db.execute("INSERT INTO file_chunking(file_id, digest, lines, inner_error_units) VALUES(?,?,?,?)",
                        (file_id, digest, lines, inner_error_units))

    def add_skipped(self, rows: list[tuple]) -> None:
        """大きさや中身で除いたファイル((source の id, パス, 大きさ, 更新日時, 理由)。stat できなければ大きさは None)。"""
        self.db.executemany("INSERT INTO skipped_files(source_id, path, size, mtime_ns, reason) VALUES(?,?,?,?,?)",
                            rows)

    def add_chunk(self, file_id: int, source_id: str, path: str, chunk, fts_body: str, fts_symbols: str,
                  fts_path: str) -> int:
        cid = self.next_chunk_id
        self.next_chunk_id += 1
        self.db.execute(
            "INSERT INTO chunks(id, file_id, source_id, path, start_line, end_line, kind, method, scope, signature, "
            "symbols, part, text) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, file_id, source_id, path, chunk.start_line, chunk.end_line, chunk.kind, chunk.method, chunk.scope,
             chunk.signature, json.dumps(chunk.symbols, ensure_ascii=False), chunk.part, pack_text(chunk.text)))
        self.db.execute("INSERT INTO chunks_fts(rowid, body, symbols, path) VALUES(?,?,?,?)",
                        (cid, fts_body, fts_symbols, fts_path))
        return cid

    def add_vectors(self, first_id: int, vectors: np.ndarray, dtype: str) -> None:
        """id が first_id から連続する passage のベクトルを足す(id の順に呼ぶ)。"""
        for s in range(0, len(vectors), VECTOR_BLOCK):
            part = np.ascontiguousarray(vectors[s:s + VECTOR_BLOCK].astype(dtype))
            self.db.execute("INSERT INTO vectors(block, first_id, count, data) VALUES(?,?,?,?)",
                            (self._vec_block, first_id + s, len(part), part.tobytes()))
            self._vec_block += 1

    def set_meta(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)",
                        (key, json.dumps(value, ensure_ascii=False)))

    def commit(self) -> None:
        self.db.commit()

    def finish(self) -> None:
        self.set_meta("schema_version", INDEX_SCHEMA_VERSION)
        self.db.commit()
        self.db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
        self.db.commit()
        self.db.close()

    def abort(self) -> None:
        try:
            self.db.close()
        finally:
            self.path.unlink(missing_ok=True)


def open_readonly(path: Path) -> sqlite3.Connection:
    path = Path(path)
    if not path.is_file():
        raise IndexError_(f"索引がありません: {path}\n作るには `{command_hint('index')}`")
    try:
        db = sqlite3.connect(readonly_uri(path), uri=True)
    except sqlite3.Error as e:
        raise IndexError_(f"索引を開けません({path}): {e}") from None
    try:
        db.execute("SELECT 1 FROM meta LIMIT 1")
    except sqlite3.Error as e:
        # 開いたままにすると、Windows では同じプロセスがこのファイルを置き換えられない(作り直しが失敗する)
        db.close()
        raise IndexError_(f"索引を開けません({path}): {e}") from None
    return db


def readonly_uri(path: Path) -> str:
    """読み取り専用で開く SQLite の URI。パスの % ? # などは %XX にする(フォルダ名の %20 を空白と読まないように)。
    UNC のパス(//server/share/...)は、権限の部分を空にした file:////server/share/... にする。"""
    posix = Path(path).as_posix()
    quoted = urllib.parse.quote(posix, safe="/:")
    if posix.startswith("//"):
        quoted = "//" + quoted
    return "file:" + quoted + "?mode=ro"


def read_meta(db: sqlite3.Connection, check_version: bool = True) -> dict:
    """索引の meta。check_version が偽なら、形式の版が違っても読む(前の索引の埋め込みを使い回すとき)。"""
    meta = {k: json.loads(v) for k, v in db.execute("SELECT key, value FROM meta")}
    if check_version and meta.get("schema_version") != INDEX_SCHEMA_VERSION:
        raise IndexError_(
            f"索引の形式の版が合いません(索引 {meta.get('schema_version')}、この道具 {INDEX_SCHEMA_VERSION})。"
            f"`{command_hint('index')}` で作り直してください")
    return meta


def load_meta(path: Path) -> dict:
    with closing(open_readonly(path)) as db:
        return read_meta(db)


def load_vectors(db: sqlite3.Connection, dim: int, dtype: str) -> np.ndarray:
    """全 passage のベクトルを、id の順(行 i が id i+1)の float32 の行列にする。無ければ 0 行。"""
    rows = db.execute("SELECT first_id, count, data FROM vectors ORDER BY block").fetchall()
    if not rows:
        return np.zeros((0, dim), dtype=np.float32)
    total = sum(r[1] for r in rows)
    out = np.empty((total, dim), dtype=np.float32)
    pos = 0
    for first_id, count, data in rows:
        if first_id != pos + 1:
            raise IndexError_(f"索引のベクトルの並びが壊れています。`{command_hint('index')}` で作り直してください")
        out[pos:pos + count] = np.frombuffer(data, dtype=dtype).reshape(count, dim)
        pos += count
    return out


CHUNK_COLUMNS = ("id, source_id, path, start_line, end_line, kind, method, scope, signature, symbols, part, text, "
                 "file_id")


def row_to_passage(row) -> dict:
    return {
        "chunk_id": row[0], "source_id": row[1], "path": row[2], "start_line": row[3], "end_line": row[4],
        "kind": row[5], "method": row[6], "scope": row[7], "signature": row[8], "symbols": json.loads(row[9]),
        "part": row[10], "text": unpack_text(row[11]), "file_id": row[12],
    }


def fetch_passages(db: sqlite3.Connection, ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    return {r[0]: row_to_passage(r)
            for r in db.execute(f"SELECT {CHUNK_COLUMNS} FROM chunks WHERE id IN ({marks})", ids)}

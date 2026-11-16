"""設定ファイル(.cf/config.toml)の読み込みと、置き場所の探し方。

設定と索引は、作業フォルダの `.cf` フォルダに置く。探し方は次の順:
  1. 引数で渡されたパス(`--config`。ファイルか、`.cf` フォルダか、その親フォルダ)
  2. 環境変数 `CF_CONFIG`
  3. 作業フォルダから親へさかのぼって、最初に見つかった `.cf/config.toml`

git には頼らない(Perforce などの作業フォルダでも同じ)。
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR_NAME = ".cf"
CONFIG_FILE_NAME = "config.toml"
INDEX_FILE_NAME = "index.db"

SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MODES = ("hybrid", "keyword", "semantic")


class ConfigError(Exception):
    """設定の誤り。利用者に見せる文面をそのまま持つ。"""


@dataclass
class SourceConfig:
    id: str
    root: Path
    include: list[str] = field(default_factory=list)   # 空なら既定の拡張子
    exclude: list[str] = field(default_factory=list)   # 既定の除外に足す
    default_excludes: bool = True


@dataclass
class IndexConfig:
    path: Path = Path(INDEX_FILE_NAME)
    max_file_bytes: int = 1_000_000
    max_line_chars: int = 5_000        # これより長い行があるファイルは、生成物(minify など)として除く
    skip_generated: bool = True


@dataclass
class ChunkConfig:
    # 大きさは UTF-8 のバイト数で数える(英数字は 1 文字 1、日本語は 1 文字 3)。埋め込みモデルが一度に読める量
    # (512 トークン)に、英語のコードでも日本語の文書でも、だいたい収まるようにするため
    max_bytes: int = 1500              # 1 つの passage の上限(これを超える単位は分ける)
    min_bytes: int = 300               # これより短い単位は、隣とまとめる
    line_window: int = 60              # 行で分けるときの 1 つの上限(行)
    macro_hints: bool = True           # C・C++ の解析の前に、属性のようなマクロを伏せる
    attribute_macros: list[str] = field(default_factory=list)  # 伏せる名前を足す(正規表現)


@dataclass
class EmbeddingConfig:
    enabled: bool = True
    model: str = ""                    # 空なら既定のモデル(embed.DEFAULT_MODEL)
    model_dir: str = ""                # 手元に置いたモデルのフォルダ(指定すると取得しない)
    cache_dir: str = ""                # 取得したモデルの置き場(空なら既定)
    onnx_file: str = ""                # 既定と違う ONNX を使うとき(量子化したものなど)
    revision: str = ""
    pooling: str = ""                  # cls / mean(既定のモデル以外で指定する)
    query_prefix: str | None = None
    passage_prefix: str | None = None
    max_tokens: int = 512
    batch_size: int = 8                # 1 回に計算する passage の数(小さいほうが、埋め草が減って速い)
    threads: int = 0                   # 1 プロセスのスレッド数。0 なら自動
    workers: int = 0                   # 索引づくりで埋め込みを計算するプロセスの数。0 と 1 は 1 プロセス
    vector_dtype: str = "float16"      # 保存の形(float16 / float32)


@dataclass
class SearchConfig:
    mode: str = "hybrid"
    top_k: int = 8
    candidates: int = 50               # keyword・semantic それぞれから取る候補の数
    rrf_k: int = 60
    hybrid_weighting: str = "coverage"  # coverage: 質問の語が索引にある割合で keyword の重みを決める / equal
    weight_body: float = 1.0           # BM25 の列ごとの重み
    weight_symbols: float = 4.0
    weight_path: float = 2.0
    reranker: str = ""                 # "モジュール:関数"。空なら使わない
    rerank_candidates: int = 30
    max_output_bytes: int = 12_000     # エージェント向けの出力の上限


@dataclass
class Config:
    config_path: Path
    workspace: Path                    # .cf の親フォルダ
    sources: list[SourceConfig]
    index: IndexConfig = field(default_factory=IndexConfig)
    chunk: ChunkConfig = field(default_factory=ChunkConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    search: SearchConfig = field(default_factory=SearchConfig)

    @property
    def cf_dir(self) -> Path:
        return self.config_path.parent

    @property
    def index_path(self) -> Path:
        p = self.index.path
        return p if p.is_absolute() else self.cf_dir / p

    def source(self, source_id: str) -> SourceConfig | None:
        for s in self.sources:
            if s.id == source_id:
                return s
        return None


def _resolve_explicit(explicit: str) -> Path:
    p = Path(explicit).expanduser()
    if p.is_file():
        return p.resolve()
    if p.is_dir():
        for cand in (p / CONFIG_FILE_NAME, p / CONFIG_DIR_NAME / CONFIG_FILE_NAME):
            if cand.is_file():
                return cand.resolve()
    raise ConfigError(f"設定ファイルが見つかりません: {explicit}")


def find_config(explicit: str | None = None, start: str | os.PathLike | None = None) -> Path | None:
    """設定ファイルの場所を返す。見つからなければ None(explicit を渡して見つからなければ ConfigError)。"""
    if explicit:
        return _resolve_explicit(explicit)
    env = os.environ.get("CF_CONFIG")
    if env:
        return _resolve_explicit(env)
    d = Path(start or os.getcwd()).resolve()
    while True:
        cand = d / CONFIG_DIR_NAME / CONFIG_FILE_NAME
        if cand.is_file():
            return cand
        if d.parent == d:
            return None
        d = d.parent


def _take(table: dict, name: str, typ, default):
    """table から name を取り出す。型が違えば ConfigError。"""
    if name not in table:
        return default
    v = table[name]
    if typ is float and isinstance(v, int) and not isinstance(v, bool):
        v = float(v)
    if typ is int and isinstance(v, bool):
        raise ConfigError(f"設定 {name} は整数で書いてください")
    if not isinstance(v, typ):
        raise ConfigError(f"設定 {name} の型が違います(期待: {typ.__name__})")
    return v


def _str_list(table: dict, name: str) -> list[str]:
    v = table.get(name, [])
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ConfigError(f"設定 {name} は文字列の配列で書いてください")
    return list(v)


def _check_keys(table: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"設定 [{where}] に知らない項目があります: {', '.join(unknown)}")


def load_config(path: str | os.PathLike) -> Config:
    path = Path(path).resolve()
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"設定ファイルがありません: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"設定ファイルを読めません({path}): {e}") from None

    _check_keys(data, {"source", "index", "chunk", "embedding", "search"}, "全体")
    cf_dir = path.parent
    workspace = cf_dir.parent if cf_dir.name == CONFIG_DIR_NAME else cf_dir

    raw_sources = data.get("source", [])
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ConfigError("設定に [[source]] が 1 つも無いので、検索の対象がありません")
    sources: list[SourceConfig] = []
    seen: set[str] = set()
    for i, s in enumerate(raw_sources, 1):
        if not isinstance(s, dict):
            raise ConfigError(f"{i} 番目の [[source]] の書き方が違います")
        _check_keys(s, {"id", "root", "include", "exclude", "default_excludes"}, "source")
        sid = _take(s, "id", str, "")
        if not SOURCE_ID_RE.match(sid):
            raise ConfigError(f"{i} 番目の [[source]] の id が使えません: {sid!r}(英数字と . _ - だけ。先頭は英数字)")
        if sid in seen:
            raise ConfigError(f"[[source]] の id が重なっています: {sid}")
        seen.add(sid)
        root_s = _take(s, "root", str, "")
        if not root_s:
            raise ConfigError(f"[[source]] {sid} に root がありません")
        root = Path(root_s).expanduser()
        if not root.is_absolute():
            root = workspace / root
        sources.append(SourceConfig(
            id=sid, root=Path(os.path.normpath(root)),
            include=_str_list(s, "include"), exclude=_str_list(s, "exclude"),
            default_excludes=_take(s, "default_excludes", bool, True)))

    cfg = Config(config_path=path, workspace=workspace, sources=sources)

    t = data.get("index", {})
    _check_keys(t, {"path", "max_file_bytes", "max_line_chars", "skip_generated"}, "index")
    cfg.index = IndexConfig(
        path=Path(_take(t, "path", str, INDEX_FILE_NAME)),
        max_file_bytes=_take(t, "max_file_bytes", int, 1_000_000),
        max_line_chars=_take(t, "max_line_chars", int, 5_000),
        skip_generated=_take(t, "skip_generated", bool, True))

    t = data.get("chunk", {})
    _check_keys(t, {"max_bytes", "min_bytes", "line_window", "macro_hints", "attribute_macros"}, "chunk")
    cfg.chunk = ChunkConfig(
        max_bytes=_take(t, "max_bytes", int, 1500), min_bytes=_take(t, "min_bytes", int, 300),
        line_window=_take(t, "line_window", int, 60), macro_hints=_take(t, "macro_hints", bool, True),
        attribute_macros=_str_list(t, "attribute_macros"))
    if cfg.chunk.max_bytes < 200 or cfg.chunk.min_bytes < 0 or cfg.chunk.min_bytes >= cfg.chunk.max_bytes:
        raise ConfigError("[chunk] は max_bytes >= 200、0 <= min_bytes < max_bytes にしてください")

    t = data.get("embedding", {})
    _check_keys(t, {"enabled", "model", "model_dir", "cache_dir", "onnx_file", "revision", "pooling", "query_prefix",
                    "passage_prefix", "max_tokens", "batch_size", "threads", "workers", "vector_dtype"}, "embedding")
    cfg.embedding = EmbeddingConfig(
        enabled=_take(t, "enabled", bool, True), model=_take(t, "model", str, ""),
        model_dir=_take(t, "model_dir", str, ""), cache_dir=_take(t, "cache_dir", str, ""),
        onnx_file=_take(t, "onnx_file", str, ""), revision=_take(t, "revision", str, ""),
        pooling=_take(t, "pooling", str, ""),
        query_prefix=_take(t, "query_prefix", str, None), passage_prefix=_take(t, "passage_prefix", str, None),
        max_tokens=_take(t, "max_tokens", int, 512), batch_size=_take(t, "batch_size", int, 8),
        threads=_take(t, "threads", int, 0), workers=_take(t, "workers", int, 0),
        vector_dtype=_take(t, "vector_dtype", str, "float16"))
    if cfg.embedding.vector_dtype not in ("float16", "float32"):
        raise ConfigError("[embedding] vector_dtype は float16 か float32 です")
    if cfg.embedding.pooling not in ("", "cls", "mean"):
        raise ConfigError("[embedding] pooling は cls か mean です")

    t = data.get("search", {})
    _check_keys(t, {"mode", "top_k", "candidates", "rrf_k", "hybrid_weighting", "weight_body", "weight_symbols",
                    "weight_path",
                    "reranker", "rerank_candidates", "max_output_bytes"}, "search")
    cfg.search = SearchConfig(
        mode=_take(t, "mode", str, "hybrid"), top_k=_take(t, "top_k", int, 8),
        candidates=_take(t, "candidates", int, 50), rrf_k=_take(t, "rrf_k", int, 60),
        hybrid_weighting=_take(t, "hybrid_weighting", str, "coverage"),
        weight_body=_take(t, "weight_body", float, 1.0), weight_symbols=_take(t, "weight_symbols", float, 4.0),
        weight_path=_take(t, "weight_path", float, 2.0), reranker=_take(t, "reranker", str, ""),
        rerank_candidates=_take(t, "rerank_candidates", int, 30),
        max_output_bytes=_take(t, "max_output_bytes", int, 12_000))
    if cfg.search.hybrid_weighting not in ("coverage", "equal"):
        raise ConfigError("[search] hybrid_weighting は coverage か equal です")
    if cfg.search.mode not in MODES:
        raise ConfigError(f"[search] mode は {' / '.join(MODES)} のどれかです")
    return cfg


def source_id_for(path: Path, used: set[str]) -> str:
    """フォルダの名前から、source の呼び名(英数字と . _ -)を作る。使った名前とは重ならないようにする。"""
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", path.name).strip("-._") or "source"
    if not base[0].isalnum():
        base = "s" + base
    sid, n = base[:40], 2
    while sid in used:
        sid = f"{base[:36]}-{n}"
        n += 1
    return sid


def toml_string(s: str) -> str:
    """TOML の文字列にする。Windows のパスを読みやすく書くため、使えるなら '…'(中をそのまま読む形)にする。"""
    if "'" not in s and "\n" not in s and "\r" not in s:
        return f"'{s}'"
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r") + '"'


def config_template(sources: list[tuple[str, str]], embedding_enabled: bool = True) -> str:
    """新しい設定ファイルの中身を作る。sources は (id, ルートのパス) の並び。"""
    out = [
        "# context-foundation の設定(このフォルダ .cf は、索引と一緒に手元だけに置く)",
        "# 書き方: https://github.com/noxitro/context-foundation の README.ja.md「設定ファイル」",
        "",
    ]
    for sid, root in sources:
        out += [
            "[[source]]",
            f"id = {toml_string(sid)}          # 検索結果に出る名前(英数字と . _ -)",
            f"root = {toml_string(root)}",
            "# include = ['**/*.cpp', '**/*.h']   # 省くと、既定の拡張子(C・C++・C#・Markdown・その他の文字のファイル)",
            "# exclude = ['**/ThirdParty/**']     # 既定の除外(build・.git など)に足す",
            "",
        ]
    out += [
        "[embedding]",
        f"enabled = {'true' if embedding_enabled else 'false'}        # false にすると keyword(BM25)だけで動く",
        "",
        "[search]",
        'mode = "hybrid"       # hybrid / keyword / semantic',
        "top_k = 8",
        "",
    ]
    return "\n".join(out)

"""埋め込み(意味検索のベクトル)を、手元の CPU で計算する。

- モデルは ONNX の形のものを onnxruntime で動かす(PyTorch は使わない)。外部の API は呼ばない
- モデルの取得は、明示的に頼まれたときだけ(`ctxf model download`、索引づくりの `--download`)。
  検索のときには取得しない。モデルが無ければ、呼び出し側が keyword だけに切り替える
- モデルは設定で差し替えられる。既定の候補は、下の MODEL_PRESETS(入手先・版・ライセンス・大きさを固定)
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .config import EmbeddingConfig


class ModelUnavailable(Exception):
    """モデルが手元に無い・読めない。文面は利用者に見せる。"""


@dataclass(frozen=True)
class ModelPreset:
    repo: str                      # 配布元(Hugging Face)のリポジトリ
    revision: str                  # 固定するコミット
    onnx_file: str
    pooling: str                   # cls / mean
    dim: int
    license: str
    query_prefix: str = ""
    passage_prefix: str = ""
    tokenizer_file: str = "tokenizer.json"
    # ファイル名 → (バイト数, sha256)。取得したものを確かめる
    files: dict[str, tuple[int, str]] = field(default_factory=dict)
    note: str = ""


MODEL_PRESETS: dict[str, ModelPreset] = {
    "ibm-granite/granite-embedding-97m-multilingual-r2": ModelPreset(
        repo="ibm-granite/granite-embedding-97m-multilingual-r2",
        revision="835ad14087e140460703cf0fae09f97d469d65c2",
        onnx_file="onnx/model.onnx", pooling="cls", dim=384, license="Apache-2.0",
        files={
            "onnx/model.onnx": (390004608, "68e592b160673d30250824c1116bc6ab33f70efb22b97c9e1d7ce1e69c1c9d70"),
            "onnx/model_quint8_avx2.onnx": (98247878,
                                            "a6022dd8220ea6f6595562a1328ee216f4a94faa55362f2f4747c80f1e78772e"),
            "tokenizer.json": (25301672, "4f2842d568e2724370aec203652a42ac783c7937f8347a1a2cc7506d71f1582f"),
        },
        note="多言語(日本語を含む)とプログラムのコード(C・C++ など)で学習。97M パラメータ・384 次元"),
    "intfloat/multilingual-e5-small": ModelPreset(
        repo="intfloat/multilingual-e5-small",
        revision="614241f622f53c4eeff9890bdc4f31cfecc418b3",
        onnx_file="onnx/model.onnx", pooling="mean", dim=384, license="MIT",
        query_prefix="query: ", passage_prefix="passage: ",
        files={
            "onnx/model.onnx": (470268510, "ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665"),
            "onnx/model_qint8_avx512_vnni.onnx": (118346824,
                                                  "dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88"),
            "tokenizer.json": (17082730, "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39"),
        },
        note="多言語の汎用モデル。118M パラメータ・384 次元。入力は 512 トークンまで"),
}
# 既定は multilingual-e5-small。公開の C++ のコードで 2 つを比べると、英語の質問と、日本語に英語の語を足した
# 質問で、こちらのほうが上位に当たった。日本語だけの質問では、どちらも弱く、差は小さかった(docs/design.md)。
DEFAULT_MODEL = "intfloat/multilingual-e5-small"


@dataclass
class ModelSpec:
    """設定と既定値を合わせた、実際に使うモデルの指定。"""
    name: str
    repo: str
    revision: str
    onnx_file: str
    tokenizer_file: str
    pooling: str
    query_prefix: str
    passage_prefix: str
    max_tokens: int
    dim: int                       # 0 なら読み込んでから決まる
    license: str
    files: dict[str, tuple[int, str]]
    model_dir: str                 # 利用者が置いたフォルダ(空なら取得先のキャッシュ)
    cache_dir: str


def default_cache_dir() -> Path:
    env = os.environ.get("CF_MODEL_CACHE")
    if env:
        return Path(env)
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "context-foundation" / "models"
    return Path.home() / ".cache" / "context-foundation" / "models"


def resolve_model(cfg: EmbeddingConfig) -> ModelSpec:
    name = cfg.model or DEFAULT_MODEL
    p = MODEL_PRESETS.get(name)
    if p is None and not (cfg.pooling and (cfg.model_dir or cfg.onnx_file)):
        raise ModelUnavailable(
            f"モデル {name} は既定の候補にありません。[embedding] に pooling(cls / mean)と、"
            "onnx_file か model_dir を書いてください")
    return ModelSpec(
        name=name, repo=p.repo if p else name, revision=cfg.revision or (p.revision if p else "main"),
        onnx_file=cfg.onnx_file or (p.onnx_file if p else "onnx/model.onnx"),
        tokenizer_file=p.tokenizer_file if p else "tokenizer.json",
        pooling=cfg.pooling or (p.pooling if p else "mean"),
        query_prefix=cfg.query_prefix if cfg.query_prefix is not None else (p.query_prefix if p else ""),
        passage_prefix=cfg.passage_prefix if cfg.passage_prefix is not None else (p.passage_prefix if p else ""),
        max_tokens=cfg.max_tokens, dim=p.dim if p else 0, license=p.license if p else "",
        files=dict(p.files) if p else {}, model_dir=cfg.model_dir, cache_dir=cfg.cache_dir)


def model_folder(spec: ModelSpec) -> Path:
    if spec.model_dir:
        return Path(spec.model_dir).expanduser()
    root = Path(spec.cache_dir).expanduser() if spec.cache_dir else default_cache_dir()
    return root / spec.repo.replace("/", "__") / spec.revision[:12]


def model_files(spec: ModelSpec) -> tuple[Path, Path]:
    """(ONNX, tokenizer.json) の場所。どちらかが無ければ ModelUnavailable。"""
    folder = model_folder(spec)
    onnx, tok = folder / spec.onnx_file, folder / spec.tokenizer_file
    if not onnx.is_file() and (folder / Path(spec.onnx_file).name).is_file():
        onnx = folder / Path(spec.onnx_file).name     # フォルダの直下に置かれた場合
    missing = [str(x) for x in (onnx, tok) if not x.is_file()]
    if missing:
        raise ModelUnavailable(
            "埋め込みモデルが手元にありません: " + ", ".join(missing) +
            "\n取得するには `ctxf model download`(setup.bat でも取得できます)。"
            "モデル無しでも keyword(BM25)の検索は使えます")
    return onnx, tok


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download_model(spec: ModelSpec, log=print) -> Path:
    """モデルを配布元(Hugging Face)から取得して、sha256 を確かめる。取得先のフォルダを返す。

    送るのは、取得するファイルの名前だけ(利用者のソースや質問は送らない)。
    """
    if spec.model_dir:
        raise ModelUnavailable("[embedding] model_dir を指定しているときは、取得しません(そのフォルダに置いてください)")
    from huggingface_hub import hf_hub_download

    folder = model_folder(spec)
    folder.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    for name in (spec.onnx_file, spec.tokenizer_file):
        target = folder / name
        want = spec.files.get(name)
        if target.is_file() and (want is None or target.stat().st_size == want[0]):
            log(f"あります: {target}")
        else:
            size = f"(約 {want[0] / 1e6:.0f} MB)" if want else ""
            log(f"取得します: {spec.repo} {name} {size}")
            hf_hub_download(repo_id=spec.repo, filename=name, revision=spec.revision, local_dir=str(folder))
        if want is not None:
            got = _sha256(target)
            if got != want[1]:
                target.unlink(missing_ok=True)
                raise ModelUnavailable(f"取得した {name} の sha256 が、控えてある値と違います(消しました)。取り直してください")
    (folder / "cf-model.json").write_text(json.dumps({
        "repo": spec.repo, "revision": spec.revision, "onnx_file": spec.onnx_file, "license": spec.license,
        "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, ensure_ascii=False, indent=1), encoding="utf-8")
    return folder


class Embedder:
    """埋め込みを計算するものの共通の形。ベクトルは float32 で、長さ 1 にそろえて返す。"""
    name = ""
    dim = 0
    revision = ""

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        raise NotImplementedError

    def embed_query(self, text: str) -> np.ndarray:
        raise NotImplementedError

    def info(self) -> dict:
        return {"model": self.name, "revision": self.revision, "dim": self.dim}


class OnnxEmbedder(Embedder):
    def __init__(self, spec: ModelSpec, threads: int = 0, batch_size: int = 16):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        onnx_path, tok_path = model_files(spec)
        self.spec, self.name, self.revision, self.batch_size = spec, spec.name, spec.revision, max(1, batch_size)
        self.onnx_file = onnx_path.name
        tok = Tokenizer.from_file(str(tok_path))
        tok.enable_truncation(max_length=spec.max_tokens)
        pad_id = next((tok.token_to_id(t) for t in ("<pad>", "[PAD]", "<|pad|>") if tok.token_to_id(t) is not None), 0)
        self.pad_id = pad_id
        tok.no_padding()
        self.tok = tok
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        if threads > 0:
            opts.intra_op_num_threads = threads
        self.session = ort.InferenceSession(str(onnx_path), sess_options=opts, providers=["CPUExecutionProvider"])
        self.input_names = [i.name for i in self.session.get_inputs()]
        outs = [o.name for o in self.session.get_outputs()]
        self.output_name = "last_hidden_state" if "last_hidden_state" in outs else outs[0]
        self.dim = spec.dim or len(self._run(["x"])[0])

    def _run(self, texts: list[str]) -> np.ndarray:
        encs = self.tok.encode_batch(texts)
        width = max(len(e.ids) for e in encs)
        ids = np.full((len(encs), width), self.pad_id, dtype=np.int64)
        mask = np.zeros((len(encs), width), dtype=np.int64)
        for i, e in enumerate(encs):
            ids[i, :len(e.ids)] = e.ids
            mask[i, :len(e.ids)] = 1
        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.input_names:
            feeds["token_type_ids"] = np.zeros_like(ids)
        out = self.session.run([self.output_name], {k: v for k, v in feeds.items() if k in self.input_names})[0]
        if out.ndim == 3:
            if self.spec.pooling == "cls":
                vec = out[:, 0, :]
            else:
                m = mask[:, :, None].astype(np.float32)
                vec = (out * m).sum(axis=1) / np.maximum(m.sum(axis=1), 1.0)
        else:
            vec = out                                   # すでに文のベクトルを出すモデル
        vec = vec.astype(np.float32)
        return vec / np.maximum(np.linalg.norm(vec, axis=1, keepdims=True), 1e-12)

    def _embed(self, texts: list[str], progress=None) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        # 長さの近いものを同じ束にして、埋め草を減らす
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        done = 0
        for s in range(0, len(order), self.batch_size):
            idx = order[s:s + self.batch_size]
            out[idx] = self._run([texts[i] for i in idx])
            done += len(idx)
            if progress:
                progress(done, len(texts))
        return out

    def embed_passages(self, texts: list[str], progress=None) -> np.ndarray:
        return self._embed([self.spec.passage_prefix + t for t in texts], progress)

    def embed_query(self, text: str) -> np.ndarray:
        return self._embed([self.spec.query_prefix + text])[0]

    def info(self) -> dict:
        return {"model": self.name, "revision": self.revision, "dim": self.dim, "onnx_file": self.onnx_file,
                "pooling": self.spec.pooling, "max_tokens": self.spec.max_tokens, "license": self.spec.license}


class HashEmbedder(Embedder):
    """テスト用: 語をハッシュで 256 次元に散らすだけのもの(意味は見ない)。モデル無しで通しの動作を確かめる。"""
    name = "hash-256-test-only"
    dim = 256

    def _vec(self, text: str) -> np.ndarray:
        from .textproc import tokenize
        v = np.zeros(self.dim, dtype=np.float32)
        for t in tokenize(text):
            h = int.from_bytes(hashlib.blake2b(t.encode("utf-8"), digest_size=4).digest(), "little")
            v[h % self.dim] += 1.0 if (h >> 16) & 1 else -1.0
        n = float(np.linalg.norm(v))
        return v / n if n else v

    def embed_passages(self, texts: list[str], progress=None) -> np.ndarray:
        return np.stack([self._vec(t) for t in texts]) if texts else np.zeros((0, self.dim), dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        return self._vec(text)


# --- 索引づくり用: 複数のプロセスで計算する(既定では使わない) ---
# [embedding] workers を 2 以上にしたときだけ使う。計測した PC(16 コア)では、1 プロセスで毎秒 45〜50 件、
# 3 プロセスに分けても合計で毎秒 60 件ほどで、プロセスの数には比例しなかった(スレッドを増やしても同じ)。
# ほかの PC で効くかは確かめていないので、既定は 1 プロセスにしている。
WORKER_THREADS = 4
_worker: Embedder | None = None


def _worker_init(cfg: EmbeddingConfig) -> None:
    global _worker
    _worker = make_embedder(cfg)


def _worker_embed(texts: list[str]) -> np.ndarray:
    assert _worker is not None
    return _worker.embed_passages(texts)


class EmbedPool:
    """passage の束を、複数のプロセスで埋め込む。結果は、渡した順に返す。"""

    def __init__(self, cfg: EmbeddingConfig, workers: int):
        from concurrent.futures import ProcessPoolExecutor
        from dataclasses import replace
        self.workers = workers
        self.cfg = replace(cfg, threads=cfg.threads or WORKER_THREADS)
        self.pool = ProcessPoolExecutor(max_workers=workers, initializer=_worker_init, initargs=(self.cfg,))

    def map(self, batches):
        """batches(文のリストを順に出すもの)を埋め込み、同じ順でベクトルの配列を返す。"""
        from collections import deque
        pending: deque = deque()
        for texts in batches:
            pending.append(self.pool.submit(_worker_embed, texts))
            if len(pending) >= self.workers * 2:
                yield pending.popleft().result()
        while pending:
            yield pending.popleft().result()

    def close(self) -> None:
        self.pool.shutdown(wait=True, cancel_futures=True)


def make_embedder(cfg: EmbeddingConfig) -> Embedder:
    """設定に合う埋め込み器を作る。モデルが無ければ ModelUnavailable。"""
    if os.environ.get("CF_TEST_EMBEDDER") == "hash" or cfg.model == HashEmbedder.name:
        return HashEmbedder()
    spec = resolve_model(cfg)
    try:
        return OnnxEmbedder(spec, threads=cfg.threads, batch_size=cfg.batch_size)
    except ModelUnavailable:
        raise
    except Exception as e:   # onnxruntime が入っていない・モデルが壊れている など
        raise ModelUnavailable(f"埋め込みモデルを読み込めません({type(e).__name__}: {e})") from e


def _main_selftest() -> None:   # pragma: no cover - 手で確かめるためのもの
    from .config import EmbeddingConfig as EC
    e = make_embedder(EC())
    v = e.embed_passages(["hello world", "こんにちは 世界"])
    print(e.info(), v.shape, float(v[0] @ v[1]), file=sys.stderr)

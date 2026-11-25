"""埋め込みの計算(モデルの実行)が途中で誤りになったときの続け方。

GPU とモデルの実物は使わない(偽のセッションで、誤りの道筋だけを通す)。確かめること:
- 誤りのわけを、読める 1 行にする(onnxruntime が UnicodeDecodeError にした、OS の文字コードの文も)
- GPU の実行の誤りは、同じ束をもう一度 → GPU のセッションを作り直す → CPU に替える、の順で続ける
- `device = "gpu"` と決めたときは、CPU に替えずに、1 つの文面で止まる
- 止まっても、計算した分は残り、次の索引づくりが続きから計算する(終了コードは 5)
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import context_foundation.embed as embed
from context_foundation import indexer
from context_foundation.cli import main as cli_main
from context_foundation.config import EmbeddingConfig, load_config
from context_foundation.embed import CPU_PROVIDER, EmbedRunError, HashEmbedder, error_text
from context_foundation.indexer import BuildError, EmbedError, build_index
from context_foundation.render import status_human
from context_foundation.store import load_meta

from conftest import write_config

GPU = "DmlExecutionProvider"


def test_error_text_is_one_readable_line():
    assert error_text(RuntimeError("the device\n  was removed")) == "RuntimeError: the device was removed"
    # 長い文は、頭と末尾を残す(DirectML の誤りの番号と OS の文は、末尾に来る)
    long = error_text(ValueError("x" * 2000 + " 887A0006 the end"))
    assert len(long) == embed.ERROR_TEXT_LIMIT + 1 and long.startswith("ValueError: xxx") and "x…x" in long
    assert long.endswith(" 887A0006 the end")
    assert error_text(EmbedRunError("887A0006 already one line")) == "887A0006 already one line"   # 型の名前を足さない
    # onnxruntime は、OS の文字コード(日本語の Windows なら cp932)の文を UTF-8 として読もうとして、こうなる
    raw = b"Status Message: 887A0005 " + "GPU デバイスが中断されました。".encode("cp932") + b"\r\n"
    with pytest.raises(UnicodeDecodeError) as caught:
        raw.decode("utf-8")
    text = error_text(caught.value)
    assert text.startswith("Status Message: 887A0005 ") and "\n" not in text and "\r" not in text
    assert "UnicodeDecodeError" not in text and "codec" not in text      # もとのわけを出す(読めなかった、ではなく)
    if embed.os.name == "nt":
        import ctypes
        if ctypes.windll.kernel32.GetACP() == 932:
            assert text == "Status Message: 887A0005 GPU デバイスが中断されました。"


class _Tok:
    def encode_batch(self, texts):
        return [SimpleNamespace(ids=list(range(1, len(t) + 2))) for t in texts]


class _Session:
    """onnxruntime のセッションの代わり。failures の数だけ、実行が誤りになる(None なら、いつも誤り)。"""

    def __init__(self, provider: str, failures: int | None = 0):
        self.provider, self.failures, self.runs = provider, failures, 0

    def run(self, outputs, feeds):
        self.runs += 1
        if self.failures is None or self.failures > 0:
            if self.failures:
                self.failures -= 1
            raise RuntimeError(f"887A0005 device removed\n(run {self.runs})")
        ids = feeds["input_ids"]
        return [np.ones((ids.shape[0], ids.shape[1], 4), dtype=np.float32)]

    # 下は、本物の OnnxEmbedder.__init__ と _open_session が呼ぶもの
    def get_providers(self):
        return [self.provider]

    def get_inputs(self):
        return [SimpleNamespace(name="input_ids"), SimpleNamespace(name="attention_mask")]

    def get_outputs(self):
        return [SimpleNamespace(name="last_hidden_state")]

    def disable_fallback(self):
        self.fallback_disabled = True


def _embedder(first: _Session, later: list, cpu_batch_size: int = 8):
    """GPU で動いている OnnxEmbedder(モデルは読まない)。later は、作り直しで渡すセッション(誤りなら、作れない)。"""
    emb = object.__new__(embed.OnnxEmbedder)
    emb.spec = SimpleNamespace(pooling="mean", passage_prefix="", query_prefix="")
    emb.tok, emb.pad_id = _Tok(), 0
    emb.input_names, emb.output_name, emb.dim = ["input_ids", "attention_mask"], "last_hidden_state", 4
    emb.batch_size, emb.cpu_batch_size = 32, cpu_batch_size
    emb.notes = []
    emb.log = emb.notes.append
    emb.gpu_reopens, emb.gpu_fallback, emb._started, emb._dead = 0, "", True, ""
    emb.session, emb.provider = first, first.provider
    emb.opened = []

    def open_session(provider):
        emb.opened.append(provider)
        made = later.pop(0)
        if isinstance(made, Exception):
            raise made
        assert made.provider == provider
        return made
    emb._open_session = open_session
    return emb


def test_a_passing_gpu_error_is_retried_on_the_same_session():
    gpu = _Session(GPU, failures=1)
    emb = _embedder(gpu, [])
    vecs = emb.embed_passages(["a", "bcd"])
    assert vecs.shape == (2, 4) and np.allclose(np.linalg.norm(vecs, axis=1), 1.0)
    assert gpu.runs == 2 and emb.opened == [] and emb.provider == GPU and emb.gpu_fallback == ""
    assert len(emb.notes) == 1 and "やり直して続けます" in emb.notes[0] and "887A0005 device removed (run 1)" in emb.notes[0]
    assert "\n" not in emb.notes[0]
    emb.embed_passages(["e"])
    assert gpu.runs == 3 and len(emb.notes) == 1                 # 通っている間は、何も言わない


def test_a_gpu_error_that_stays_reopens_the_gpu_session():
    broken, fresh = _Session(GPU, failures=None), _Session(GPU)
    emb = _embedder(broken, [fresh])
    assert emb.embed_passages(["a"]).shape == (1, 4)
    assert broken.runs == 2 and fresh.runs == 1 and emb.opened == [GPU]
    assert emb.provider == GPU and emb.session is fresh and emb.gpu_fallback == "" and emb.batch_size == 32
    assert len(emb.notes) == 1 and "GPU の用意をやり直して続けます" in emb.notes[0]


def test_a_gpu_that_keeps_failing_hands_the_rest_to_the_cpu():
    cpu = _Session(CPU_PROVIDER)
    emb = _embedder(_Session(GPU, failures=None), [_Session(GPU, failures=None), cpu])
    # 束(32)より多い文を渡す。替えた後は束が小さく(8)なるが、どの文も計算から漏れない(漏れると、0 のまま残る)
    vecs = emb.embed_passages(["x" * (i % 7 + 1) for i in range(70)])
    assert vecs.shape == (70, 4) and np.allclose(np.linalg.norm(vecs, axis=1), 1.0)
    assert emb.opened == [GPU, CPU_PROVIDER] and emb.session is cpu
    assert cpu.runs == 1 + -(-(70 - 32) // 8)                     # 誤りになった束(32)と、残りを 8 ずつ
    assert emb.provider == CPU_PROVIDER and emb.batch_size == 8
    # わけは、起きた順に残す(始めのわけを、やり直しのわけで消さない)。作り直した後の誤りは、始めと同じ文なので 1 度
    assert emb.gpu_fallback == ("RuntimeError: 887A0005 device removed (run 1)"
                                " → RuntimeError: 887A0005 device removed (run 2)")
    assert len(emb.notes) == 1 and "残りは CPU で計算します" in emb.notes[0] and GPU in emb.notes[0]
    assert emb.gpu_fallback in emb.notes[0]
    before = cpu.runs
    emb.embed_passages(["c"])                                     # 替えた後は、CPU でそのまま計算する
    assert cpu.runs == before + 1 and emb.opened == [GPU, CPU_PROVIDER] and len(emb.notes) == 1


def test_the_gpu_session_is_reopened_only_a_few_times():
    emb = _embedder(_Session(GPU, failures=None),
                    [_Session(GPU) for _ in range(embed.GPU_REOPEN_LIMIT)] + [_Session(CPU_PROVIDER)])
    for _ in range(embed.GPU_REOPEN_LIMIT):                       # 作り直すと 1 度は通るが、次の束でまた誤りになる
        emb.session.failures = None
        emb.embed_passages(["a"])
        assert emb.provider == GPU
    emb.session.failures = None
    emb.embed_passages(["a"])                                     # 上限を超えたら、作り直さずに CPU に替える
    assert emb.opened == [GPU] * embed.GPU_REOPEN_LIMIT + [CPU_PROVIDER] and emb.provider == CPU_PROVIDER


def test_a_gpu_session_that_cannot_be_reopened_also_ends_on_the_cpu():
    emb = _embedder(_Session(GPU, failures=None), [OSError("no adapter"), _Session(CPU_PROVIDER)])
    assert emb.embed_passages(["a"]).shape == (1, 4)
    assert emb.provider == CPU_PROVIDER
    assert emb.gpu_fallback.startswith("RuntimeError: 887A0005 device removed (run 1) → ")   # 始めのわけも残す
    assert emb.gpu_fallback.endswith(" → GPU の用意のやり直し: OSError: no adapter")


def test_device_gpu_stops_instead_of_moving_to_the_cpu():
    emb = _embedder(_Session(GPU, failures=None), [_Session(GPU, failures=None)], cpu_batch_size=0)
    with pytest.raises(EmbedRunError, match="やり直しても通りません.*887A0005") as caught:
        emb.embed_passages(["a"])
    assert "\n" not in str(caught.value)
    assert "(run 1)" in str(caught.value) and "(run 2)" in str(caught.value)
    assert emb.opened == [GPU] and emb.provider == GPU and emb.notes == []
    with pytest.raises(EmbedRunError) as again:                   # 止まった後に呼ばれても、同じ文面で止まる
        emb.embed_passages(["b"])
    assert str(again.value) == str(caught.value) and emb.opened == [GPU]


def test_a_stop_is_remembered_when_no_session_can_be_made():
    emb = _embedder(_Session(GPU, failures=None), [OSError("no adapter")], cpu_batch_size=0)
    with pytest.raises(EmbedRunError, match="やり直しても通りません.*GPU の用意のやり直し: OSError: no adapter") as caught:
        emb.embed_passages(["a"])
    assert emb.session is None
    with pytest.raises(EmbedRunError) as again:                   # 無いセッションで計算しようとしない
        emb.embed_passages(["b"])
    assert str(again.value) == str(caught.value) and "NoneType" not in str(again.value)

    emb = _embedder(_Session(GPU, failures=None), [_Session(GPU, failures=None), OSError("no memory")])
    with pytest.raises(EmbedRunError, match=r"CPU にも替えられません\(OSError: no memory\)") as caught:
        emb.embed_passages(["a"])
    assert emb.provider == GPU and emb.gpu_fallback == "" and emb.notes == []
    with pytest.raises(EmbedRunError) as again:
        emb.embed_passages(["b"])
    assert str(again.value) == str(caught.value) and emb.opened == [GPU, CPU_PROVIDER]


def test_an_error_on_the_cpu_is_one_line_and_is_not_retried():
    cpu = _Session(CPU_PROVIDER, failures=None)
    emb = _embedder(cpu, [])
    with pytest.raises(EmbedRunError, match=r"^RuntimeError: 887A0005 device removed \(run 1\)$"):
        emb.embed_passages(["a"])
    assert cpu.runs == 1 and emb.opened == []


def test_only_auto_lets_a_running_gpu_fall_back(monkeypatch):
    made = []

    class Fake:
        def __init__(self, spec, threads=0, batch_size=16, provider=CPU_PROVIDER, cpu_batch_size=0, log=None):
            self.provider, self.batch_size = provider, batch_size
            made.append((provider, cpu_batch_size, log))

    monkeypatch.delenv("CF_TEST_EMBEDDER")
    monkeypatch.setattr(embed, "OnnxEmbedder", Fake)
    monkeypatch.setattr(embed, "gpu_provider", lambda: GPU)
    notes: list[str] = []
    embed.make_embedder(EmbeddingConfig(), for_index=True, log=notes.append)
    embed.make_embedder(EmbeddingConfig(batch_size=4), for_index=True, log=notes.append)
    embed.make_embedder(EmbeddingConfig(device="gpu"), for_index=True, log=notes.append)
    embed.make_embedder(EmbeddingConfig(batch_size=0), for_index=True, log=notes.append)   # 0 は「替えない」ではない
    assert made == [(GPU, 8, notes.append), (GPU, 4, notes.append), (GPU, 0, notes.append), (GPU, 1, notes.append)]


class _Stopping(HashEmbedder):
    """GPU で動いているふりをして、fail_at 回目の呼び出しで、モデルの実行の誤りになる。"""
    provider = GPU

    def __init__(self, fail_at: int | None = None, fall_back_at: int | None = None):
        self.calls = self.passages = 0
        self.fail_at, self.fall_back_at, self.gpu_fallback = fail_at, fall_back_at, ""

    def embed_passages(self, texts, progress=None):
        self.calls += 1
        if self.calls == self.fail_at:
            raise EmbedRunError("887A0005 the device was removed")
        if self.calls == self.fall_back_at:
            self.gpu_fallback = "887A0005 the device was removed"
        self.passages += len(texts)
        return super().embed_passages(texts)


def _build(ws: Path, corpus: Path, log=None, **kw) -> dict:
    return build_index(load_config(write_config(ws, corpus)), progress=False, log=log or (lambda s: None), **kw)


def test_a_failed_embedding_run_keeps_what_was_computed(tmp_path, corpus_copy, monkeypatch, capsys):
    monkeypatch.setattr(indexer, "EMBED_TASK", 2)
    ws = tmp_path / "ws"
    index = ws / ".cf" / "index.db"
    with pytest.raises(EmbedError) as caught:
        _build(ws, corpus_copy, embedder=_Stopping(fail_at=3))
    text = str(caught.value)
    assert isinstance(caught.value, BuildError)
    assert "埋め込みの計算が途中で止まりました(887A0005 the device was removed)" in text
    assert "今回計算した 4 passage の埋め込みは残してある" in text and "続きから計算します" in text
    assert '[embedding] device を "cpu"' in text                  # GPU で止まったときだけ、CPU での作り方を添える
    assert not index.exists() and not list(index.parent.glob("index.db.tmp-*"))
    assert index.with_name("index.db.partial").exists()

    again = _Stopping()
    result = _build(ws, corpus_copy, embedder=again)
    assert result["timings"]["reused_passages"] == 4             # 止まる前に計算した分は、計算し直さない
    assert again.passages == result["timings"]["embedded_passages"] > 0
    assert not index.with_name("index.db.partial").exists()

    # コマンドでは、traceback を出さずに 5 で終わる(前の索引は、そのまま)
    before = index.read_bytes()
    (corpus_copy / "docs" / "added.md").write_text("# added\n\nnew text to embed\n", encoding="utf-8")
    monkeypatch.setattr(indexer, "make_embedder", lambda *a, **kw: _Stopping(fail_at=1))
    assert cli_main(["index", "--config", str(ws), "--quiet"]) == 5
    err = capsys.readouterr().err
    assert "Traceback" not in err and "埋め込みの計算が途中で止まりました" in err
    assert index.read_bytes() == before


def test_an_error_on_the_cpu_does_not_suggest_the_cpu(tmp_path, corpus_copy):
    stopping = _Stopping(fail_at=1)
    stopping.provider = CPU_PROVIDER
    with pytest.raises(EmbedError) as caught:
        _build(tmp_path / "ws", corpus_copy, embedder=stopping)
    text = str(caught.value)
    assert "計算し直します" in text and "0 passage" not in text and "[embedding]" not in text


def test_an_error_after_the_cpu_took_over_does_not_suggest_the_cpu(tmp_path, corpus_copy, monkeypatch):
    monkeypatch.setattr(indexer, "EMBED_TASK", 2)

    class Switching(_Stopping):
        def embed_passages(self, texts, progress=None):
            vecs = super().embed_passages(texts)
            if self.gpu_fallback:
                self.provider = CPU_PROVIDER                      # OnnxEmbedder は、替えたときに実行先も替える
            return vecs

    with pytest.raises(EmbedError) as caught:
        _build(tmp_path / "ws", corpus_copy, embedder=Switching(fail_at=3, fall_back_at=1))
    assert "今回計算した 4 passage" in str(caught.value) and "[embedding]" not in str(caught.value)


def test_the_index_says_when_the_cpu_finished_what_the_gpu_started(tmp_path, corpus_copy, monkeypatch):
    monkeypatch.setattr(indexer, "EMBED_TASK", 2)
    ws = tmp_path / "ws"
    lines: list[str] = []
    _build(ws, corpus_copy, log=lines.append, embedder=_Stopping(fall_back_at=2))
    done = [s for s in lines if s.startswith("埋め込み: ")]
    assert len(done) == 1 and done[0].endswith(f"GPU({GPU})、途中から CPU)")
    meta = load_meta(ws / ".cf" / "index.db")["embedding"]
    assert meta["provider"] == GPU and meta["provider_fallback"] == CPU_PROVIDER
    # 途中の知らせを見ていなくても(--quiet)、終わりの要約と status で分かる
    assert f"埋め込みの計算: GPU({GPU})の実行が途中で誤りになり、残りを CPU で計算しました" in status_human({"embedding": meta})

    lines.clear()
    (corpus_copy / "docs" / "added.md").write_text("# added\n\nnew text to embed\n", encoding="utf-8")
    _build(ws, corpus_copy, log=lines.append, embedder=_Stopping())
    done = [s for s in lines if s.startswith("埋め込み: ")]
    assert len(done) == 1 and done[0].endswith(f"GPU({GPU}))")
    meta = load_meta(ws / ".cf" / "index.db")["embedding"]
    assert "provider_fallback" not in meta and "埋め込みの計算" not in status_human({"embedding": meta})


class _FakeTokenizer(_Tok):
    @staticmethod
    def from_file(path):
        return _FakeTokenizer()

    def enable_truncation(self, max_length):
        self.max_length = max_length

    def token_to_id(self, token):
        return None

    def no_padding(self):
        pass


@pytest.fixture
def fake_runtime(monkeypatch, tmp_path):
    """onnxruntime と tokenizers を偽物にして、本物の OnnxEmbedder を作れるようにする。返すのは、これから作られる
    セッションの並び(作るたびに、頭から 1 つ使う)と、作ったときに頼まれた実行先の並び。"""
    import sys
    onnx, tok = tmp_path / "model.onnx", tmp_path / "tokenizer.json"
    onnx.write_bytes(b"onnx")
    tok.write_text("{}", encoding="utf-8")
    sessions: list[_Session] = []
    asked: list[list[str]] = []

    def inference_session(path, sess_options=None, providers=None):
        asked.append(list(providers))
        return sessions.pop(0)

    ort = SimpleNamespace(SessionOptions=lambda: SimpleNamespace(), InferenceSession=inference_session,
                          ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL=0),
                          get_available_providers=lambda: [GPU, CPU_PROVIDER])
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    monkeypatch.setitem(sys.modules, "tokenizers", SimpleNamespace(Tokenizer=_FakeTokenizer))
    monkeypatch.setattr(embed, "model_files", lambda spec: (onnx, tok))
    spec = SimpleNamespace(name="org/model", revision="r1", max_tokens=16, dim=0, pooling="mean", license="",
                           passage_prefix="", query_prefix="")
    monkeypatch.setattr(embed, "resolve_model", lambda cfg: spec)
    monkeypatch.delenv("CF_TEST_EMBEDDER")
    return SimpleNamespace(sessions=sessions, asked=asked, spec=spec)


def test_a_real_embedder_checks_the_provider_and_turns_off_the_silent_fallback(fake_runtime):
    gpu = _Session(GPU)
    fake_runtime.sessions[:] = [gpu]
    emb = embed.OnnxEmbedder(fake_runtime.spec, provider=GPU, cpu_batch_size=8)
    assert emb.provider == GPU and emb.dim == 4 and gpu.runs == 1 and gpu.fallback_disabled
    assert fake_runtime.asked == [[GPU, CPU_PROVIDER]]

    # onnxruntime は、GPU の実行先を始められないと、何も言わずに CPU のセッションを返す。GPU のものとして使わない
    fake_runtime.sessions[:] = [_Session(CPU_PROVIDER)]
    with pytest.raises(RuntimeError, match="DmlExecutionProvider を使えませんでした"):
        embed.OnnxEmbedder(fake_runtime.spec, provider=GPU)

    # 作り直すときも同じ(黙って CPU になったセッションで続けず、知らせを出して CPU に替える)
    notes: list[str] = []
    cpu = _Session(CPU_PROVIDER)
    fake_runtime.sessions[:] = [_Session(GPU), _Session(CPU_PROVIDER), cpu]
    emb = embed.OnnxEmbedder(fake_runtime.spec, provider=GPU, cpu_batch_size=8, log=notes.append)
    emb.session.failures = None
    assert emb.embed_passages(["a"]).shape == (1, 4)
    assert emb.provider == CPU_PROVIDER and emb.session is cpu and emb.batch_size == 8
    assert len(notes) == 1 and "残りは CPU で計算します" in notes[0] and "使えませんでした" in notes[0]


def test_an_error_in_the_first_run_is_a_gpu_that_cannot_start(fake_runtime, monkeypatch):
    monkeypatch.setattr(embed, "gpu_provider", lambda: GPU)
    notes: list[str] = []
    broken, cpu = _Session(GPU, failures=None), _Session(CPU_PROVIDER)
    fake_runtime.sessions[:] = [broken, cpu]
    emb = embed.make_embedder(EmbeddingConfig(), for_index=True, log=notes.append)
    # 始めの 1 回は、やり直しも作り直しもしない(作れなかったものとして、前からの形で CPU に替える)
    assert broken.runs == 1 and fake_runtime.asked == [[GPU, CPU_PROVIDER], [CPU_PROVIDER]]
    assert emb.provider == CPU_PROVIDER and emb.batch_size == 8 and emb.gpu_fallback == ""
    assert notes == [f"注意: GPU({GPU})で埋め込みを始められないので、CPU で計算します"
                     "(RuntimeError: 887A0005 device removed (run 1))"]                # 内部の型の名前は出さない

    fake_runtime.sessions[:] = [_Session(GPU, failures=None)]
    with pytest.raises(embed.ModelUnavailable, match=r"読み込めません\(RuntimeError: 887A0005 device removed"):
        embed.make_embedder(EmbeddingConfig(device="gpu"), for_index=True)

"""examples/o3de と examples/godot の評価例が、仕様(docs/evaluation-data-spec.md)の形式に合っていること。

題材のコーパス(公開のゲームエンジン)は、このリポジトリには無い。ここで確かめるのは、コーパスが無くても
分かる範囲(形式・件数・手元のパスが入っていないこと)。行の範囲がコーパスに合っているかは、コーパスを
取得して `ctxf validate --cases examples/o3de/cases.jsonl --check-corpus` で確かめる。
"""
from __future__ import annotations

import re
import tomllib
from collections import Counter
from pathlib import Path

from context_foundation.evalkit.data import load_cases

EXAMPLE = Path(__file__).parent.parent / "examples" / "o3de"
SOURCES = {"atom", "framework"}


def test_o3de_cases_follow_the_spec():
    cases, issues = load_cases(EXAMPLE / "cases.jsonl")
    assert not [i.format() for i in issues if i.level in ("error", "warning")]
    held_out = [c for c in cases.values() if c.get("split", "eval") == "eval"]
    tune = [c for c in cases.values() if c.get("split") == "tune"]
    assert 20 <= len(held_out) <= 30 and tune
    cats = Counter(c["category"] for c in held_out)
    assert all(cats[k] >= 3 for k in ("identifier_known", "natural_language_only", "doc_to_code", "code_to_doc",
                                      "similar_implementation")) and cats["unanswerable"] >= 2
    langs = Counter(c["question_language"] for c in held_out)
    assert langs["ja"] >= 5 and langs["en"] >= 5
    for c in cases.values():
        assert len(c["expected_evidence"]) <= 10
        for ev in c["expected_evidence"]:
            assert ev["source_id"] in SOURCES
            assert not Path(ev["path"]).is_absolute() and ".." not in ev["path"].split("/")


def test_o3de_cases_do_not_carry_local_paths():
    text = (EXAMPLE / "cases.jsonl").read_text(encoding="utf-8")
    assert "\r" not in text and not text.startswith("\ufeff")
    # ドライブ名つきのパス(`C:\\...`・`E:/...`)、利用者のフォルダ、一時フォルダの名前が入っていないこと
    assert not re.search(r"\b[a-z]:[\\/]|/users/|\\users\\|appdata|/home/", text, re.IGNORECASE)


def test_o3de_example_config_names_the_same_sources():
    cfg = tomllib.loads((EXAMPLE / "config.example.toml").read_text(encoding="utf-8"))
    assert {s["id"] for s in cfg["source"]} == SOURCES
    assert all(s["root"].startswith("<o3de>") for s in cfg["source"])        # 手元のパスを書かない


GODOT = Path(__file__).parent.parent / "examples" / "godot"


def test_godot_cases_follow_the_spec():
    cases, issues = load_cases(GODOT / "cases.jsonl")
    assert not [i.format() for i in issues if i.level in ("error", "warning")]
    held_out = [c for c in cases.values() if c.get("split", "eval") == "eval"]
    assert 20 <= len(held_out) <= 30 and [c for c in cases.values() if c.get("split") == "tune"]
    cats = Counter(c["category"] for c in held_out)
    assert all(cats[k] >= 3 for k in ("identifier_known", "natural_language_only", "doc_to_code", "code_to_doc",
                                      "similar_implementation")) and cats["unanswerable"] >= 2
    langs = Counter(c["question_language"] for c in held_out)
    assert langs["ja"] >= 5 and langs["en"] >= 5
    # 区域(case_id の最後の - より前。compare.py summary --by area)は、描画・音などの servers、scene、core の 3 つ
    assert {cid.rsplit("-", 1)[0] for cid in cases} == {"GDS", "GDV", "GDC"}
    for c in cases.values():
        assert len(c["expected_answer_points"]) == 3 and len(c["expected_evidence"]) <= 10
        assert (c["category"] == "unanswerable") == (not c["expected_evidence"])
        for ev in c["expected_evidence"]:
            assert ev["source_id"] == "godot" and not Path(ev["path"]).is_absolute()
            assert not ev["path"].startswith(("thirdparty/", "platform/")) and "/tests/" not in "/" + ev["path"]


def test_godot_cases_do_not_carry_local_paths_and_config_matches():
    text = (GODOT / "cases.jsonl").read_text(encoding="utf-8")
    assert "\r" not in text and not text.startswith("\ufeff")
    assert not re.search(r"\b[a-z]:[\\/]|/users/|\\users\\|appdata|/home/|/tmp/", text, re.IGNORECASE)
    cfg = tomllib.loads((GODOT / "config.example.toml").read_text(encoding="utf-8"))
    assert [s["id"] for s in cfg["source"]] == ["godot"] and cfg["source"][0]["root"] == "<godot>"

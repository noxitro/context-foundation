"""取り込みで、索引に入るもの・入らないものと、その数え方(0.4.0 で直したもの)。

既定の名前で除いたフォルダの数え方、生成物の印、読めないバイトが混ざった文字コード、長いパス、
秘密を持ちやすい名前、分割の失敗、UTF-8 にできない名前、リンクとジャンクション、重なった source。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

from context_foundation import ingest
from context_foundation.config import IndexConfig, SourceConfig, load_config
from context_foundation.indexer import build_index
from context_foundation.ingest import IngestStats, iter_source_files, looks_generated
from context_foundation.render import status_agent, status_human
from context_foundation.search import Searcher


def write(path: Path, text: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def ingest_all(root: Path, sources: list[SourceConfig] | None = None, **kw) -> tuple[dict, IngestStats]:
    stats = IngestStats()
    src = SourceConfig(id="s", root=root, **kw)
    files = {f.path: f for f in iter_source_files(src, IndexConfig(), stats, sources)}
    return files, stats


def make_workspace(ws: Path, sources: str = "[[source]]\nid = 'src'\nroot = '.'\n") -> Path:
    (ws / ".cf").mkdir(parents=True, exist_ok=True)
    path = ws / ".cf" / "config.toml"
    path.write_text(sources + "\n[embedding]\nenabled = false\n", encoding="utf-8")
    return path


def indexed_paths(cfg) -> set[tuple[str, str]]:
    with closing(sqlite3.connect(str(cfg.index_path))) as db:
        return set(db.execute("SELECT source_id, path FROM files"))


def try_symlink(target: Path, link: Path, is_dir: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"ここではリンクを作れない: {e}")


# --- 既定の名前で除いたフォルダ(CRIT-1)

def test_default_named_folders_are_counted_and_reported(tmp_path):
    ws = tmp_path / "ws"
    write(ws / "src/Game/Player.cpp", "int player_health() { return 1; }\n")
    write(ws / "src/Game/Target/LockOnTargetSelector.cpp", "int select_lock_on_target() { return 2; }\n")
    write(ws / "src/Game/Target/Notes.md", "# lock-on\n")
    write(ws / "Packages/com.studio.combat/Runtime/ComboSystem.cs", "class ComboSystem {}\n")
    write(ws / "Packages/com.studio.combat/icon.png", b"\x89PNG")
    write(ws / "node_modules/lib/index.js", "module.exports = 1;\n")      # 依存物の置き場は数えない
    write(ws / ".git/hooks/pre-commit.sh", "exit 0\n")
    files, stats = ingest_all(ws)
    assert set(files) == {"src/Game/Player.cpp"}
    meta = stats.extra_meta()["excluded_dirs"]
    assert (meta["folders"], meta["files"]) == (2, 3)
    assert meta["examples"] == [{"path": "s:src/Game/Target", "files": 2},
                                {"path": "s:Packages", "files": 1}]

    cfg = load_config(make_workspace(ws))
    build_index(cfg, progress=False, log=lambda s: None)
    st = Searcher.from_config(cfg).status()
    human, agent = status_human(st), status_agent(st)
    assert "既定の名前" in human and "src:src/Game/Target  2 ファイル" in human and "[[source]]" in human
    assert "src:src/Game/Target (2)" in agent and "src:Packages (1)" in agent


@pytest.mark.parametrize("include, expected", [(None, 1), (["**/*"], 2)])
def test_count_in_default_named_folders_applies_every_name_rule(tmp_path, include, expected):
    """除いたフォルダの中で数えるのは、名前の決まり(拡張子・exclude・秘密の名前・include)で入りうるものだけ。"""
    for name in (".env", "a.generated.h", "x.png", "b.cpp"):
        write(tmp_path / "build" / name, "x\n")
    _, stats = ingest_all(tmp_path, include=include) if include else ingest_all(tmp_path)
    assert stats.extra_meta()["excluded_dirs"]["examples"] == [{"path": "s:build", "files": expected}]


def test_default_excludes_off_walks_every_folder(tmp_path):
    write(tmp_path / "Target/a.cpp", "int a;\n")
    files, stats = ingest_all(tmp_path, default_excludes=False)
    assert set(files) == {"Target/a.cpp"} and "excluded_dirs" not in stats.extra_meta()


def test_status_without_new_fields_is_unchanged():
    """0.3 の索引の meta には、新しい項目が無い。そのときは何も足さない。"""
    st = {"stats": {"files": 1, "chunks": 1, "lines": 1, "skipped": {"binary": 1}}, "sources": [], "modes": ["keyword"]}
    assert "folders" not in status_agent(st) and "既定の名前" not in status_human(st)
    assert status_agent(st).count("\n") == 4


# --- 重なった source(IDX-7)と、除いたフォルダを source にして戻すこと

def test_nested_source_root_is_indexed_once_by_the_inner_source(tmp_path):
    ws = tmp_path / "ws"
    write(ws / "Engine/a.cpp", "int async_loader_detail;\n")
    write(ws / "Engine/Docs/load.md", "# async_loader_detail\n")
    write(ws / "src/Game/Target/Lock.cpp", "int lock_on;\n")
    cfg = load_config(make_workspace(ws, "[[source]]\nid = 'engine'\nroot = 'Engine'\n\n"
                                         "[[source]]\nid = 'docs'\nroot = 'Engine/Docs'\n\n"
                                         "[[source]]\nid = 'src'\nroot = 'src'\n\n"
                                         "[[source]]\nid = 'target'\nroot = 'src/Game/Target'\n"))
    logs: list[str] = []
    build_index(cfg, progress=False, log=logs.append)
    assert indexed_paths(cfg) == {("engine", "a.cpp"), ("docs", "load.md"), ("target", "Lock.cpp")}
    st = Searcher.from_config(cfg).status()
    assert "excluded_dirs" not in st["stats"]          # Target は target の側で入ったので、除いたことにしない
    assert any("engine:Docs" in line and "docs" in line for line in logs)
    assert "engine:Docs は source docs のルート" in status_human(st)


# --- 生成物の印(CRIT-2)

@pytest.mark.parametrize("name, text", [
    ("docs/dungeon.md", "# ダンジョン自動生成の仕様\n\n部屋を自動生成する。\n"),
    ("docs/notes.txt", "This file was generated by hand. DO NOT EDIT\n"),
    ("src/DamageCalculator.cpp", "#pragma once\n// Computes the damage generated by a weapon hit, then clamps it.\n"
                                 "int damage();\n"),
    ("src/Loader.cpp", '#include "../BulletFileLoader/autogenerated/bullet.h"\nint load();\n'),
    ("src/noise.py", '"""Noise helpers."""\n# Adds noise generated by _get_noise\nx = 1\n'),
    ("src/id.cs", "// ID は自動生成される\nclass Id {}\n"),
    ("src/edit.cpp", "// do not edit this order unless the table changes\nint order;\n"),
    # 文書には当てない(見出しの # はコメントの行の形なので、この決まりだけが守る)
    ("docs/gen.md", "# 自動生成されるダンジョンの仕様\n"),
    ("docs/build.md", "# Generated by the build: what to expect\n"),
    ("docs/notes.md", "<!-- @generated sections are marked below -->\n# Notes\n"),
    ("docs/todo.txt", "-- DO NOT EDIT the table below by hand; ask the planner\n"),
    # 説明のコメントの書き出し(0.3.1 は入れていた)。弱い形は、最初のコードの行より後ろでは見ない
    ("src/Ids.cpp", '#include "Ids.h"\n\n// Auto-generated ids start at 1000 and are never reused.\nint NextId();\n'),
    ("src/SeededRandom.h", "#pragma once\n// Generated from the world seed, so that server and client agree.\n"),
    ("src/WidgetProxy.h", "#pragma once\n// This class is generated at runtime by FWidgetFactory; do not construct it.\n"),
    ("src/ShaderCache.cpp", "void g();\n/**\n * Code generated shaders are cached per platform.\n */\nvoid f();\n"),
    ("src/LodBuilder.cpp", "int a;\n/*\n * Auto generated LODs are built here when the mesh has none.\n */\n"),
    ("src/pbkdf_test.cc", "#include <x.h>\n// Note that the expectation was\n"
                          "// generated using OpenSSL itself, and hence is not verified.\n"),
    # 文字列の中の行(' と <!-- は、VB・マークアップのときだけコメントの始まり)
    ("src/export_format.py", 'X = """\n    <!-- Generated with Rich https://example -->\n"""\n'),
    ("src/gen_tables.py", "HEADER = (\n    '// Generated by gen.py. DO NOT EDIT.\\n'\n)\n"),
    # 書き出しに前置きがあっても、印の形でないもの
    ("src/asdl.h", "/* It would be nice if the code generated by asdl_c.py was completely\n   independent */\nint x;\n"),
    ("src/strings.h", "// All field names generated by ASCII_STR() have a common prefix,\nint x;\n"),
    ("src/phix.py", "# Note these lists are auto-generated by p2js.exw, when the keywords change\nx = 1\n"),
    ("src/test_overlap.py", "# Test automatically generated assignments which overlap in memory.\nx = 1\n"),
    ("src/note_id.cs", "// 注意: ID は自動生成される\nclass Id {}\n"),
    ("src/cache.cpp", "// Generated code is cached in the shader cache folder.\nint cache;\n"),
    ("src/mesh.cpp", "// Note: the mesh is generated by the terrain tool at load time\nint mesh;\n"),
    # 表の出どころを書いたコメント(0.3.1 の「generated by 」は、直後に空白が要った)
    ("src/Color.cpp", '#include "T.h"\n\n// Generated by: https://example.org/colors (distinct colours)\n'
                      "static const int k[] = {1, 2, 3};\n"),
    # <!-- は、マークアップの拡張子のときだけコメントの始まり(ここでは文字列の中の行)
    ("src/embed.py", 'X = """\n<!-- Generated by Rich -->\n"""\n'),
    # C 系では、# の行はプリプロセッサの行(コメントとみなさない)
    ("src/a.cpp", "#define GENERATED_BY 1 // Generated by tool\nint x;\n"),
    ("src/b.cpp", "#warning This file was generated by tool\nint x;\n"),
])
def test_handwritten_files_are_not_taken_for_generated(name, text):
    assert not looks_generated(text, Path(name).name)


@pytest.mark.parametrize("name, text", [
    ("a.g.cs", "// <auto-generated>\n//     This code was generated by a tool.\n// </auto-generated>\nclass A {}\n"),
    ("x_pb2.py", "# -*- coding: utf-8 -*-\n# Generated by the protocol buffer compiler.  DO NOT EDIT!\nx = 1\n"),
    ("y.h", "/*\n * This file was automatically generated by tool.py\n */\nint y;\n"),
    ("z.go", "// Code generated by protoc-gen-go. DO NOT EDIT.\npackage z\n"),
    ("w.cs", "// このファイルはツールで自動生成されています\nclass W {}\n"),
    ("v.ts", "/** @generated */\nexport const v = 1;\n"),
    # 書き出しに前置き(ラベル・飾り・日本語の主語)があるバナー
    ("pycore_ast.h", "// File automatically generated by Parser/asdl_c.py.\nint x;\n"),
    ("a.h", "// NOTE: This file is auto generated by build tool\nint x;\n"),
    ("b.h", "// Generated Code - Do Not Edit\nint x;\n"),
    ("c.h", "// WARNING: This file was auto-generated. Changes will be lost.\nint x;\n"),
    ("d.h", "/* WARNING: this file is automatically generated; do not edit */\nint x;\n"),
    ("e.cs", "// Do not edit! This file was generated by tools/gen.py\nclass E {}\n"),
    ("f.h", "/* !!! AUTOGENERATED FILE !!! */\nint x;\n"),
    ("g.cpp", "// Copyright Foo Corp.\n//\n// *** This file is auto-generated by codegen. ***\nint x;\n"),
    ("h.py", "# NOTE: autogenerated from schema.json\nx = 1\n"),
    ("_version.py", "# file generated by setuptools-scm\n# don't change, don't track in version control\n"),
    ("i.cs", "// 注意: このファイルは自動生成されています。編集しないでください。\nclass I {}\n"),
    ("j.cs", "// 本ファイルは MasterConverter により自動生成されています。\nclass J {}\n"),
    ("k.cs", "// ※このファイルはコンバータで自動生成されます。\nclass K {}\n"),
    ("l.cs", "// これは自動生成されたコードです\nclass L {}\n"),
    ("SoundBank_IDs.h", "////////////////////\n//\n// Sound bank generated include file. Do not edit.\n//\n"
                        "////////////////////\n\n#ifndef SOUND_BANK_IDS_H\n"),
    # ファイルの頭のブロックコメント・docstring の中の、記号の無い行
    ("m.cpp", "/*\n    This file was generated by tools/gen.py. DO NOT EDIT.\n*/\nint x;\n"),
    ("n.cpp", "/*\n    This file was generated by tools/gen.py.\n*/\nint x;\n"),
    ("o.cs", "/*\n\tこのファイルは自動生成されたファイルです\n*/\nclass O {}\n"),
    ("_css_builtins.py", '"""\n    pygments.lexers._css_builtins\n\n    This file is autogenerated by scripts/get_css.py\n"""\n'),
    # ファイルの頭のコメントのかたまりにある、弱い形
    ("ares_config.h", "/* Generated from ares_config.h.cmake*/\n#define X 1\n"),
    ("bullet.h", "// Auto generated from tools/gen.py\nint x;\n"),
    ("gl.h", "/**\n * Loader generated by glad 2.0.4 on Sun Jan  1 00:00:00 2023\n */\n"),
    ("p.h", "// NOTE: this file is auto-generated from messages.xml\nint x;\n"),
    # インクルードガードの後ろでも、強い形は見る
    ("q.h", "#ifndef Q_H\n#define Q_H\n// This file was generated by tools/gen.py\n"),
    # ' と <!-- は、VB・マークアップではコメントの始まり
    ("r.vb", "' <auto-generated>\n'     This code was generated by a tool.\nModule R\n"),
    ("s.natvis", '<?xml version="1.0"?>\n<!-- This file was generated by gen.py -->\n<AutoVisualizer/>\n'),
    # <?xml の行は、頭のコメントのかたまりを終わらせない(弱い形も見る)
    ("t.natvis", '<?xml version="1.0"?>\n<!-- Generated from types.yaml -->\n<AutoVisualizer/>\n'),
    # `#` がコメントの言語では、# WARNING: や # If ... で始まる行もコメントの行
    ("gen_cfg.py", "# WARNING: This file is auto-generated by tools/gen_cfg.py. Do not edit by hand.\nX = 1\n"),
    ("values.yaml", "# WARNING: DO NOT EDIT. This file was generated by helm-docs.\nkey: 1\n"),
    ("schema.py", "# If you need to change this, edit schema.json.\n# Generated from schema.json\nX = 1\n"),
    ("Makefile", "# Warning: this file was generated by configure\nall:\n"),
])
def test_generated_headers_are_still_detected(name, text):
    assert looks_generated(text, name)


@pytest.mark.parametrize("text", ["// note" + "-" * 1990, "// warning" + ":" * 1990, "# note" + "_-" * 995])
def test_generated_check_stays_fast_on_a_long_run_of_separators(text):
    """前置きの後ろに区切りの記号が長く続く 1 行でも、正規表現の分け方が増えない(前は 1 行で 10 秒ほどかかった)。"""
    t0 = time.perf_counter()
    assert not looks_generated(text, "a.cpp")
    assert not looks_generated(text, "a.py")
    assert time.perf_counter() - t0 < 0.25          # 直したものは 1 ケース数ミリ秒。片方の正規表現を戻すと 0.5 秒を超える


def test_generated_check_in_the_walk(tmp_path):
    write(tmp_path / "docs/dungeon.md", "# ダンジョン自動生成の仕様\n")
    write(tmp_path / "gen.cpp", "// <auto-generated>\nint g;\n")
    files, stats = ingest_all(tmp_path)
    assert set(files) == {"docs/dungeon.md"} and stats.skipped == {"generated": 1}


# --- 読めないバイトが混ざった文字コード(CRIT-7)

def test_file_with_a_few_undecodable_bytes_is_indexed(tmp_path):
    write(tmp_path / "Parry.cpp", b"int parry();\n// Author: Ren\xe9 Dupont\nint parry_window = 3;\n")
    write(tmp_path / "Stamina.cpp", "// スタミナの回復\n".encode("cp932") + b"// J\xf6rg M\xfcller\nint stamina;\n")
    write(tmp_path / "Mixed.cpp", "// UTF-8 のコメント\n".encode("utf-8") + "// Shift_JIS のコメント\n".encode("cp932")
          + b"int mixed;\n")
    write(tmp_path / "noise.cpp", bytes(range(0x80, 0x100)) * 40)       # NUL は無いが、文字ではない
    files, stats = ingest_all(tmp_path)
    assert set(files) == {"Parry.cpp", "Stamina.cpp", "Mixed.cpp"}
    assert "parry_window" in files["Parry.cpp"].text and files["Parry.cpp"].encoding == "mixed"
    assert "スタミナの回復" in files["Stamina.cpp"].text
    assert "UTF-8 のコメント" in files["Mixed.cpp"].text and "Shift_JIS のコメント" in files["Mixed.cpp"].text
    assert stats.skipped == {"undecodable": 1}


# --- 長いパス(CRIT-8)と、一覧できないフォルダ

def write_long(path: Path, text: str) -> None:
    """260 文字を超えるパスにファイルを作る。Windows では拡張の形(\\\\?\\)を、確かめる対象の関数を使わずに書く
    (長いパスが有効かどうかに関わらず作れる。作れなければ skip ではなく失敗にする)。"""
    target = "\\\\?\\" + os.path.abspath(path) if sys.platform == "win32" else str(path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(text)


def test_files_under_long_paths_are_indexed(tmp_path):
    deep = tmp_path
    for i in range(6):
        deep = deep / (f"{i}_" + "d" * 48)
    write_long(deep / "Deep.cpp", "int deep_function() { return 7; }\n")
    write(tmp_path / "short.cpp", "int s;\n")
    assert len(str(deep / "Deep.cpp")) > 300
    files, stats = ingest_all(tmp_path)
    rel = str((deep / "Deep.cpp").relative_to(tmp_path)).replace("\\", "/")
    assert set(files) == {"short.cpp", rel} and "deep_function" in files[rel].text
    assert stats.skipped == {}


@pytest.mark.parametrize("path, expected", [
    ("C:\\a\\b.cpp", "\\\\?\\C:\\a\\b.cpp"),
    ("\\\\server\\share\\x.cpp", "\\\\?\\UNC\\server\\share\\x.cpp"),
    ("\\\\?\\C:\\a\\b.cpp", "\\\\?\\C:\\a\\b.cpp"),
])
def test_long_path_form_on_windows(monkeypatch, path, expected):
    """拡張の形への書き換え(ファイルシステムの設定に頼らない。どの OS でも、Windows の形として確かめる)。"""
    import ntpath
    from types import SimpleNamespace
    monkeypatch.setattr(ingest, "os", SimpleNamespace(name="nt", path=ntpath))     # この関数が見る os だけを替える
    assert ingest._long_path(path) == expected


def test_long_path_form_elsewhere_is_unchanged(monkeypatch):
    import posixpath
    from types import SimpleNamespace
    monkeypatch.setattr(ingest, "os", SimpleNamespace(name="posix", path=posixpath))
    assert ingest._long_path("/a/b.cpp") == "/a/b.cpp"


@pytest.mark.skipif(sys.platform != "win32", reason="拡張の形は Windows だけ")
def test_walk_and_stale_check_use_the_long_path_form(tmp_path, monkeypatch):
    """長いパスが有効な Windows(CI)でも、歩きと stale の確かめが拡張の形を使うことを確かめる。"""
    from context_foundation import search

    ws = tmp_path / "ws"
    write(ws / "sub/a.cpp", "int long_form_function() { return 1; }\n")
    cfg = load_config(make_workspace(ws))
    build_index(cfg, progress=False, log=lambda s: None)
    seen: list[str] = []
    real_scandir, real_stat = os.scandir, os.stat

    def scandir(path):
        seen.append(str(path))
        return real_scandir(path)

    def stat(path, *a, **kw):
        if str(path).endswith("a.cpp"):
            seen.append(str(path))
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(ingest.os, "scandir", scandir)
    monkeypatch.setattr(search.os, "stat", stat)
    files, _ = ingest_all(ws)
    assert "sub/a.cpp" in files
    resp = Searcher.from_config(cfg).search("long_form_function", mode="keyword", top_k=1)
    assert resp.passages and not resp.passages[0].get("stale")
    assert any(p.endswith("a.cpp") for p in seen) and any(p.endswith("sub") for p in seen)
    assert all(p.startswith("\\\\?\\") for p in seen), seen


def test_folder_that_cannot_be_listed_is_counted(tmp_path, monkeypatch):
    write(tmp_path / "ok/a.cpp", "int a;\n")
    write(tmp_path / "locked/b.cpp", "int b;\n")
    real = os.scandir

    def scandir(path):
        if os.path.basename(str(path)) == "locked":
            raise PermissionError("denied")
        return real(path)

    monkeypatch.setattr(ingest.os, "scandir", scandir)
    files, stats = ingest_all(tmp_path)
    assert set(files) == {"ok/a.cpp"}
    assert stats.skipped_dirs == {"unreadable_dir": 1} and stats.dir_examples["unreadable_dir"] == ["locked"]


# --- 秘密を持ちやすい名前(SEC-2)

def test_default_excludes_secret_named_files(tmp_path):
    names = [".env", ".env.local", "deploy/server.pem", "home/id_rsa", "config/secrets.yaml",
             "deploy/credentials.ini", "config/app-secret.json", ".npmrc", "infra/prod.tfvars"]
    for n in names:
        write(tmp_path / n, "password = hunter2\n")
    write(tmp_path / "src/SecretManager.cpp", "int secret_manager();\n")
    write(tmp_path / "src/CredentialProvider.cs", "class CredentialProvider {}\n")
    write(tmp_path / "config/game.yaml", "speed: 3\n")
    files, stats = ingest_all(tmp_path, include=["**/*"])
    assert set(files) == {"src/SecretManager.cpp", "src/CredentialProvider.cs", "config/game.yaml"}
    assert stats.skipped == {"secret_name": len(names)}
    files, _ = ingest_all(tmp_path, include=["**/*"], default_excludes=False)
    assert ".env" in files and "config/secrets.yaml" in files


# --- 分割の失敗(IDX-1)

def deep_else_if(n: int) -> str:
    return ("int Dispatch(const char* s) {\n  if (strcmp(s, \"cmd0\") == 0) { return 0; }\n"
            + "".join(f"  else if (strcmp(s, \"cmd{i}\") == 0) {{ return {i}; }}\n" for i in range(1, n))
            + "  return -1;\n}\n")


@pytest.mark.parametrize("lang", ["cpp", "csharp"])
def test_deep_else_if_chain_does_not_recurse(lang):
    from context_foundation.chunking.code import chunk_code
    from context_foundation.config import ChunkConfig

    text = deep_else_if(2000)
    if lang == "csharp":
        text = "class C {\n" + text.replace("const char* s", "string s").replace("strcmp(s, ", "Eq(s, ") + "}\n"
    chunks, stats = chunk_code(text, lang, ChunkConfig())
    covered = {n for c in chunks for n in range(c.start_line, c.end_line + 1)}
    assert covered == set(range(1, text.count("\n") + 1)) and not stats.parse_failed


def test_chunking_error_falls_back_to_lines_and_names_the_file(tmp_path, monkeypatch):
    from context_foundation import indexer

    ws = tmp_path / "ws"
    write(ws / "src/dispatch.cpp", deep_else_if(50))
    write(ws / "src/other.cpp", "int other_function() { return 1; }\n")
    real = indexer.chunk_file

    def chunk_file(text, language, cfg):
        if "Dispatch" in text:
            raise RecursionError("maximum recursion depth exceeded")
        return real(text, language, cfg)

    monkeypatch.setattr(indexer, "chunk_file", chunk_file)
    cfg = load_config(make_workspace(ws))
    logs: list[str] = []
    result = build_index(cfg, progress=False, log=logs.append)
    assert indexed_paths(cfg) == {("src", "src/dispatch.cpp"), ("src", "src/other.cpp")}
    assert result["stats"]["chunk_failed"] == {"count": 1, "examples": ["src:src/dispatch.cpp"]}
    assert any("src:src/dispatch.cpp" in line and "RecursionError" in line for line in logs)
    assert "行で分けたファイル: 1" in status_human(Searcher.from_config(cfg).status())


# --- UTF-8 にできない名前(IDX-2)

def test_undecodable_filename_is_skipped_not_fatal(tmp_path):
    ws = tmp_path / "ws"
    write(ws / "a.cpp", "int a_function();\n")
    try:
        if sys.platform == "win32":
            bad = str(ws / "b\udc80.cpp")
        else:
            bad = os.fsdecode(os.path.join(os.fsencode(ws), b"\x93\xfa.cpp"))
        with open(bad, "w", encoding="utf-8") as f:
            f.write("int b;\n")
    except (OSError, UnicodeError) as e:
        pytest.skip(f"ここではその名前のファイルを作れない: {e}")
    cfg = load_config(make_workspace(ws))
    result = build_index(cfg, progress=False, log=lambda s: None)
    assert indexed_paths(cfg) == {("src", "a.cpp")}
    assert result["stats"]["skipped"]["bad_name"] == 1
    example = result["stats"]["skipped_examples"]["bad_name"][0]
    example.encode("utf-8")                              # meta に書けて、読める形
    assert example.endswith(".cpp") and "\\x" in example


# --- リンクとジャンクション(IDX-3・IDX-4)

def test_file_symlinks_are_not_read(tmp_path):
    outside = write(tmp_path / "outside/secret.txt", "SECRET_TOKEN_OUTSIDE_ROOT=xyz\n")
    root = tmp_path / "src"
    write(root / "real.txt", "inside text\n")
    try_symlink(outside, root / "link.txt")
    try_symlink(root / "real.txt", root / "alias.txt")         # ルートの中を指すものも、指す先の場所で入る
    files, stats = ingest_all(root)
    assert set(files) == {"real.txt"}
    assert stats.skipped == {"symlink_outside": 1, "symlink_inside": 1}
    assert stats.examples["symlink_outside"] == ["link.txt"] and stats.examples["symlink_inside"] == ["alias.txt"]


@pytest.mark.parametrize("include", [None, ["**/*"]])
def test_file_links_to_secret_or_excluded_files_inside_the_root_are_not_read(tmp_path, include):
    root = tmp_path / "ws"
    write(root / ".env", "API_TOKEN=example-secret-value\n")
    write(root / ".git/config", "[remote]\n\turl = https://user:token@example.invalid/r.git\n")
    write(root / "a.cpp", "int a;\n")
    (root / "docs").mkdir()
    try_symlink(root / ".env", root / "docs/setup-notes.txt")
    try_symlink(root / ".git/config", root / "docs/remote.ini")
    files, stats = ingest_all(root, include=include) if include else ingest_all(root)
    assert set(files) == {"a.cpp"}
    assert stats.skipped["symlink_inside"] == 2
    assert all("example-secret-value" not in f.text and "token@" not in f.text for f in files.values())


def test_file_symlink_into_another_source_is_read_there(tmp_path):
    other = tmp_path / "shared"
    write(other / "s.txt", "shared text\n")
    root = tmp_path / "src"
    root.mkdir()
    try_symlink(other / "s.txt", root / "s.txt")
    sources = [SourceConfig(id="s", root=root), SourceConfig(id="shared", root=other)]
    files, stats = ingest_all(root, sources=sources)
    assert files == {} and stats.skipped == {"symlink_inside": 1}
    inner = {f.path for f in iter_source_files(sources[1], IndexConfig(), IngestStats(), sources)}
    assert inner == {"s.txt"}


def test_dir_symlink_is_counted_not_followed(tmp_path):
    write(tmp_path / "shared/s.cpp", "int shared_symlinked_func();\n")
    root = tmp_path / "src"
    write(root / "a.cpp", "int a;\n")
    try_symlink(root, root / "loop", is_dir=True)
    try_symlink(tmp_path / "shared", root / "SharedLink", is_dir=True)
    files, stats = ingest_all(root)
    assert set(files) == {"a.cpp"}
    assert stats.skipped_dirs == {"link_dir": 2} and sorted(stats.dir_examples["link_dir"]) == ["SharedLink", "loop"]


def _linked_folder_as_a_source(tmp_path: Path, make_link) -> None:
    """フォルダのリンクを root にした source を足すと、中のファイルはその source で入り、外側では数えない。"""
    ws = tmp_path / "ws"
    write(tmp_path / "shared/Plugin/Shared.cpp", "int shared_plugin_func();\n")
    write(ws / "Source/a.cpp", "int a;\n")
    make_link(tmp_path / "shared/Plugin", ws / "Source/SharedPlugin")
    cfg = load_config(make_workspace(ws, "[[source]]\nid = 'src'\nroot = '.'\n"))
    build_index(cfg, progress=False, log=lambda s: None)
    st = Searcher.from_config(cfg).status()
    assert st["stats"]["skipped_dirs"] == {"link_dir": 1}
    assert "入れるには、そのフォルダを root にした [[source]] を足します" in status_human(st)
    assert "link_dir 1 (to index a linked folder, the user adds a [[source]] rooted at it)" in status_agent(st)

    cfg = load_config(make_workspace(ws, "[[source]]\nid = 'src'\nroot = '.'\n\n"
                                         "[[source]]\nid = 'shared'\nroot = 'Source/SharedPlugin'\n"))
    build_index(cfg, progress=False, log=lambda s: None)
    assert indexed_paths(cfg) == {("src", "Source/a.cpp"), ("shared", "Shared.cpp")}
    st = Searcher.from_config(cfg).status()
    assert "skipped_dirs" not in st["stats"]
    assert "src:Source/SharedPlugin は source shared のルート" in status_human(st)


def test_dir_symlink_used_as_a_source_root(tmp_path):
    _linked_folder_as_a_source(tmp_path, lambda target, link: try_symlink(target, link, is_dir=True))


@pytest.mark.skipif(sys.platform != "win32", reason="ジャンクションは Windows だけ")
def test_junction_used_as_a_source_root(tmp_path):
    import _winapi

    def junction(target, link):
        try:
            _winapi.CreateJunction(str(target), str(link))
        except OSError as e:
            pytest.skip(f"ジャンクションを作れない: {e}")

    _linked_folder_as_a_source(tmp_path, junction)


@pytest.mark.skipif(sys.platform != "win32", reason="ジャンクションは Windows だけ")
def test_junction_loop_not_reindexed(tmp_path):
    import _winapi

    root = tmp_path / "src"
    write(root / "sub/g.cpp", "int gamma_func();\n")
    try:
        _winapi.CreateJunction(str(root), str(root / "sub" / "loop"))
    except OSError as e:
        pytest.skip(f"ジャンクションを作れない: {e}")
    files, stats = ingest_all(root)
    assert set(files) == {"sub/g.cpp"}
    assert stats.skipped_dirs == {"link_dir": 1} and stats.dir_examples["link_dir"] == ["sub/loop"]

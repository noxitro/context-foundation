"""Copilot CLI・アプリへの登録と解除。

既定で登録するのは、次の 2 つ(どちらも Copilot CLI の利用者設定のフォルダ。GitHub Copilot のデスクトップ
アプリは、Copilot CLI に設定した skill と MCP サーバーをそのまま使う。公式文書の場所は docs/copilot.md)。

  skill   `copilot skill add <このリポジトリの skills フォルダ>`。フォルダを写さずに、その場で登録する
          (settings.json の skillDirectories に 1 行入る)
  hook    hooks/context-foundation.json を 1 つ置く。セッションの始めに、索引(.cf)のある作業フォルダで
          だけ、skill の使いどころを 1 文で知らせる。説明文だけでは、skill を読まずに grep から始める
          モデルがあるため(Claude Haiku 5.5 で実測)。索引の無いフォルダでは、何も足さない

`--mcp` を付けると、読み取り専用の MCP サーバーも `copilot mcp add` で登録する(mcp-config.json)。
MCP の形は、ツールの定義を使わない回でも毎回モデルに送るので、既定にはしていない。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO_ROOT / "skills"
LAUNCHER = REPO_ROOT / "ctxf.py"
SKILL_NAME = "context-search"
MCP_NAME = "context-foundation"
HOOK_FILE_NAME = "context-foundation.json"


def find_copilot() -> str | None:
    # npm で入れたものは copilot.cmd。.exe があればそちらを使う(cmd を経ると、引数の記号が解釈されうる)
    return shutil.which("copilot.exe") or shutil.which("copilot")


def copilot_home() -> Path:
    return Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot")


def hook_file(home: Path | None = None) -> Path:
    return (home or copilot_home()) / "hooks" / HOOK_FILE_NAME


def hook_config(launcher: Path = LAUNCHER) -> dict:
    """sessionStart の hook の定義。shell を通さずに、python で入口(ctxf.py)を直接動かす。"""
    return {"version": 1, "hooks": {"sessionStart": [
        {"type": "command", "exec": "python", "args": ["-I", str(launcher), "session-hint"], "timeoutSec": 10}]}}


def write_hook(home: Path | None = None) -> Path:
    path = hook_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(hook_config(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _run(exe: str, *args: str) -> tuple[int, str]:
    p = subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def skill_registered(exe: str) -> tuple[bool, str]:
    code, out = _run(exe, "skill", "list", "--json")
    if code != 0:
        return False, out.strip()
    try:
        skills = json.loads(out[out.index("["):])
    except ValueError:
        return False, out.strip()
    for s in skills:
        if s.get("name") == SKILL_NAME:
            return True, f"{s.get('path')}({s.get('source')})"
    return False, ""


def mcp_registered(exe: str) -> tuple[bool, str]:
    code, out = _run(exe, "mcp", "get", MCP_NAME)
    return code == 0, out.strip()


def mcp_command() -> list[str]:
    """MCP サーバーを起動するコマンド(この仮想環境の python で、このパッケージを動かす)。"""
    return [sys.executable, "-I", "-m", "context_foundation", "serve"]


def _confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return not input(f"{question} (Y/n) ").strip().lower().startswith("n")
    except EOFError:
        return False


def show_status(exe: str) -> None:
    ok, where = skill_registered(exe)
    print(f"skill {SKILL_NAME}: " + (f"登録済み {where}" if ok else "未登録"))
    hook = hook_file()
    print(f"hook(セッションの始めの 1 文): " + (f"あり {hook}" if hook.is_file() else "なし"))
    ok, info = mcp_registered(exe)
    print(f"MCP サーバー {MCP_NAME}: " + ("登録済み" if ok else "未登録"))
    if ok:
        print("  " + info.replace("\n", "\n  "))


def main(args) -> int:
    exe = find_copilot()
    if exe is None:
        sys.stderr.write("Copilot CLI(copilot)が見つかりません。入れてから、もう一度実行してください\n"
                         "  https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli\n")
        return 2
    if not (SKILLS_DIR / SKILL_NAME / "SKILL.md").is_file() or not LAUNCHER.is_file():
        sys.stderr.write(f"skill のフォルダか入口(ctxf.py)がありません: {REPO_ROOT}\n"
                         "このリポジトリを取得したフォルダで setup.bat を実行した環境から使ってください\n")
        return 2

    if args.action == "status":
        show_status(exe)
        return 0

    if args.action == "register":
        print("Copilot CLI の利用者設定に、次のものを登録します(アプリも同じ設定を使います):")
        print(f"  skill のフォルダ: {SKILLS_DIR}")
        print(f"  hook のファイル: {hook_file()}")
        if args.mcp:
            print(f"  MCP サーバー {MCP_NAME}: {' '.join(mcp_command())}")
        if not _confirm("登録しますか", args.yes):
            print("登録しませんでした")
            return 0
        code, out = _run(exe, "skill", "add", str(SKILLS_DIR))
        print(out.strip())
        if code != 0:
            sys.stderr.write("skill の登録に失敗しました\n")
            return 1
        print(f"hook を置きました: {write_hook()}")
        if args.mcp:
            _run(exe, "mcp", "remove", MCP_NAME)
            code, out = _run(exe, "mcp", "add", MCP_NAME, "--", *mcp_command())
            print(out.strip())
            if code != 0:
                sys.stderr.write("MCP サーバーの登録に失敗しました\n")
                return 1
        print()
        show_status(exe)
        print("\n使うには: 索引を作った作業フォルダで Copilot CLI(かアプリ)の新しいセッションを始めます。"
              "\n解除するには: unregister-copilot.bat(`ctxf copilot unregister`)")
        return 0

    # unregister
    code, out = _run(exe, "skill", "remove", str(SKILLS_DIR))
    print(out.strip() or "skill のフォルダは登録されていませんでした")
    hook = hook_file()
    if hook.is_file():
        hook.unlink()
        print(f"hook を消しました: {hook}")
    ok, _ = mcp_registered(exe)
    if ok:
        code, out = _run(exe, "mcp", "remove", MCP_NAME)
        print(out.strip())
    print()
    show_status(exe)
    return 0

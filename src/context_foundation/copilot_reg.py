"""Copilot CLI・アプリへの登録と解除(このリポジトリを取得して使う人向け。register-copilot.bat から呼ぶ)。

登録は、このリポジトリのフォルダを Copilot の Marketplace として足し、そこからプラグイン
`context-foundation` を入れる(公式の仕組み。公式文書の場所は docs/copilot.md)。

  copilot plugin marketplace add <このフォルダ>
  copilot plugin install context-foundation@context-foundation

手元のフォルダの Marketplace から入れたプラグインは、写されずに、そのフォルダから直接読まれる
(Copilot CLI 1.0.112 で実測。「nothing was copied」と出る)。そのため、このフォルダの .venv を
そのまま使い、フォルダを更新すれば次のセッションから新しい中身で動く。プラグインには、skill
`context-search` と、索引のある作業フォルダでだけ skill の使いどころを 1 文で知らせる hook が入る。

GitHub から入れる人(アプリの Customize など)は、この登録を使わない(docs/copilot.md)。

0.1 の登録の形(`copilot skill add` と hooks フォルダのファイル)が残っていれば、登録と解除のときに外す。
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
PLUGIN_NAME = "context-foundation"
SKILL_NAME = "context-search"
MCP_NAME = "context-foundation"
LEGACY_HOOK_FILE_NAME = "context-foundation.json"     # 0.1 で hooks フォルダに置いたもの


def find_copilot() -> str | None:
    # npm で入れたものは copilot.cmd。.exe があればそちらを使う(cmd を経ると、引数の記号が解釈されうる)
    return shutil.which("copilot.exe") or shutil.which("copilot")


def copilot_home() -> Path:
    return Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot")


def marketplace_name(root: Path = REPO_ROOT) -> str:
    """このフォルダの Marketplace の名前(.github/plugin/marketplace.json の name。Copilot はこれを登録名にする)。"""
    data = json.loads((root / ".github" / "plugin" / "marketplace.json").read_text(encoding="utf-8"))
    return data["name"]


def legacy_hook_file(home: Path | None = None) -> Path:
    return (home or copilot_home()) / "hooks" / LEGACY_HOOK_FILE_NAME


def _run(exe: str, *args: str) -> tuple[int, str]:
    p = subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _json_rows(out: str) -> list[dict]:
    try:
        rows = json.loads(out[out.index("["):])
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, dict)]


def installed_plugin(exe: str) -> dict | None:
    code, out = _run(exe, "plugin", "list", "--json")
    if code != 0:
        return None
    return next((r for r in _json_rows(out) if r.get("name") == PLUGIN_NAME), None)


def registered_marketplace(exe: str, name: str) -> dict | None:
    code, out = _run(exe, "plugin", "marketplace", "list", "--json")
    if code != 0:
        return None
    return next((r for r in _json_rows(out) if r.get("name") == name), None)


def legacy_skill_registered(exe: str) -> bool:
    """0.1 の `copilot skill add <skills フォルダ>` の登録が残っているか。"""
    code, out = _run(exe, "skill", "list", "--json")
    if code != 0:
        return False
    for s in _json_rows(out):
        if s.get("name") == SKILL_NAME and s.get("path") and \
                Path(s["path"]).resolve().is_relative_to(SKILLS_DIR.resolve()) and s.get("source") != "plugin":
            return True
    return False


def remove_legacy(exe: str) -> list[str]:
    """0.1 の登録の形が残っていれば外す。外したものの説明を返す。"""
    done = []
    if legacy_skill_registered(exe):
        _run(exe, "skill", "remove", str(SKILLS_DIR))
        done.append(f"0.1 の skill の登録を外しました({SKILLS_DIR})")
    hook = legacy_hook_file()
    if hook.is_file():
        hook.unlink()
        done.append(f"0.1 の hook のファイルを消しました({hook})")
    return done


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


def show_status(exe: str, mp: str) -> None:
    p = installed_plugin(exe)
    if p:
        state = "有効" if p.get("enabled") else "無効"
        print(f"プラグイン {PLUGIN_NAME}: 導入済み({state}、版 {p.get('version') or '?'}、"
              f"{p.get('marketplace') or p.get('source') or '?'})")
    else:
        print(f"プラグイン {PLUGIN_NAME}: 未導入")
    m = registered_marketplace(exe, mp)
    print(f"Marketplace {mp}: " + (m.get("source", "登録済み") if m else "未登録"))
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
    try:
        mp = marketplace_name()
    except (OSError, ValueError, KeyError):
        sys.stderr.write(f"Marketplace の定義(.github/plugin/marketplace.json)を読めません: {REPO_ROOT}\n")
        return 2
    if not (SKILLS_DIR / SKILL_NAME / "SKILL.md").is_file() or not LAUNCHER.is_file():
        sys.stderr.write(f"skill のフォルダか入口(ctxf.py)がありません: {REPO_ROOT}\n")
        return 2

    if args.action == "status":
        show_status(exe, mp)
        return 0

    if args.action == "register":
        print("Copilot CLI の利用者設定に、次のものを登録します(アプリも同じ設定を使います):")
        print(f"  Marketplace {mp}: {REPO_ROOT}(このフォルダ。写さずに、ここから直接読まれます)")
        print(f"  プラグイン {PLUGIN_NAME}@{mp}(skill {SKILL_NAME} と、セッションの始めの hook)")
        if args.mcp:
            print(f"  MCP サーバー {MCP_NAME}: {' '.join(mcp_command())}")
        if not _confirm("登録しますか", args.yes):
            print("登録しませんでした")
            return 0
        for line in remove_legacy(exe):
            print(line)
        if registered_marketplace(exe, mp) is None:
            code, out = _run(exe, "plugin", "marketplace", "add", str(REPO_ROOT))
            print(out.strip())
            if code != 0:
                sys.stderr.write("Marketplace の登録に失敗しました\n")
                return 1
        if installed_plugin(exe) is None:
            code, out = _run(exe, "plugin", "install", f"{PLUGIN_NAME}@{mp}")
            print(out.strip())
            if code != 0:
                sys.stderr.write("プラグインの導入に失敗しました\n")
                return 1
        else:
            _run(exe, "plugin", "enable", PLUGIN_NAME)
        if args.mcp:
            _run(exe, "mcp", "remove", MCP_NAME)
            code, out = _run(exe, "mcp", "add", MCP_NAME, "--", *mcp_command())
            print(out.strip())
            if code != 0:
                sys.stderr.write("MCP サーバーの登録に失敗しました\n")
                return 1
        print()
        show_status(exe, mp)
        print("\n使うには: 索引を作った作業フォルダで Copilot CLI(かアプリ)の新しいセッションを始めます。"
              "\n解除するには: unregister-copilot.bat(`ctxf copilot unregister`)")
        return 0

    # unregister
    if installed_plugin(exe) is not None:
        code, out = _run(exe, "plugin", "uninstall", PLUGIN_NAME)
        print(out.strip())
    m = registered_marketplace(exe, mp)
    if m is not None and str(m.get("source", "")).startswith("Local:"):
        code, out = _run(exe, "plugin", "marketplace", "remove", mp)
        print(out.strip())
    for line in remove_legacy(exe):
        print(line)
    ok, _ = mcp_registered(exe)
    if ok:
        code, out = _run(exe, "mcp", "remove", MCP_NAME)
        print(out.strip())
    print()
    show_status(exe, mp)
    return 0

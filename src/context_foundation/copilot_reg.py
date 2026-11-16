"""Copilot CLI・アプリへの登録と解除(このリポジトリを取得して使う人向け。register-copilot.bat から呼ぶ)。

登録は、このリポジトリのフォルダを Copilot の Marketplace として足し、そこからプラグイン
`context-foundation` を入れる(公式の仕組み。公式文書の場所は docs/copilot.md)。

  copilot plugin marketplace add <このフォルダ>
  copilot plugin install context-foundation@context-foundation

手元のフォルダの Marketplace から入れたプラグインは、写されずに、そのフォルダから直接読まれる
(Copilot CLI 1.0.112 で実測。「nothing was copied」と出る)。そのため、このフォルダの .venv を
そのまま使い、フォルダを更新すれば次のセッションから新しい中身で動く。プラグインには、読み取り専用の
MCP サーバー(mcp.json)、索引のある作業フォルダでだけ検索の使いどころを 1 文で知らせる hook、MCP の
道具が使えないときのための skill `context-search` が入る。

GitHub から入れる人(アプリの Customize など)は、この登録を使わない(docs/copilot.md)。同じ名前の
Marketplace やプラグインが、ほかの場所(GitHub・別のフォルダ)から入っているときは、触らずに知らせて止まる。

0.1 の登録の形(`copilot skill add` と hooks フォルダのファイル)が残っていれば、確認のうえで外す。
0.2 までの `--mcp` は、MCP サーバーを利用者の設定(mcp-config.json)に別に登録していた。0.3 からは
プラグインに入っているので、`--mcp` は何もしない(受け取るだけ)。前に登録したものは、解除のときに外す。
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
CMD_METACHARS = set('&|<>^%()!"')                     # copilot.cmd を経ると cmd に解釈される文字


class StateUnreadable(Exception):
    """Copilot の一覧を読めない(出力の形が違う・失敗した)。文面は利用者に見せる。"""


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


def same_path(a: str | os.PathLike, b: str | os.PathLike) -> bool:
    def norm(p) -> str:
        return os.path.normcase(os.path.normpath(os.path.abspath(str(p).replace("/", os.sep))))
    return norm(a) == norm(b)


def _run(exe: str, *args: str) -> tuple[int, str, str]:
    p = subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, p.stdout or "", p.stderr or ""


def parse_json_list(stdout: str) -> list[dict]:
    """`--json` の出力(stdout)から、最初の JSON の配列を読む。前後の文字は無視する。読めなければ StateUnreadable。"""
    dec = json.JSONDecoder()
    i = stdout.find("[")
    while i >= 0:
        try:
            value, _ = dec.raw_decode(stdout, i)
        except ValueError:
            i = stdout.find("[", i + 1)
            continue
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
        i = stdout.find("[", i + 1)
    raise StateUnreadable("Copilot CLI の一覧(JSON)を読めませんでした: " + stdout.strip()[:200])


def _list(exe: str, *args: str) -> list[dict]:
    code, out, err = _run(exe, *args)
    if code != 0:
        raise StateUnreadable(f"`copilot {' '.join(args)}` が失敗しました: {(err or out).strip()[:200]}")
    return parse_json_list(out)


def local_source_path(source: str) -> str | None:
    """Marketplace の一覧の source(例: 'Local: C:/x/y')から、手元のフォルダのパスを取り出す。"""
    if isinstance(source, str) and source.lower().startswith("local:"):
        return source.split(":", 1)[1].strip()
    return None


def find_marketplace(rows: list[dict], name: str) -> tuple[dict | None, bool]:
    """(同じ名前の Marketplace, それがこのフォルダか)。"""
    m = next((r for r in rows if r.get("name") == name), None)
    if m is None:
        return None, False
    path = local_source_path(m.get("source", ""))
    return m, bool(path) and same_path(path, REPO_ROOT)


def find_plugin(rows: list[dict]) -> dict | None:
    return next((r for r in rows if r.get("name") == PLUGIN_NAME), None)


def plugin_is_ours(p: dict, mp: str) -> bool:
    return p.get("marketplace") == mp and bool(p.get("installedFrom")) and same_path(p["installedFrom"], REPO_ROOT)


def legacy_registrations(exe: str) -> list[str]:
    """残っている 0.1 の登録の形(説明の文)。"""
    found = []
    try:
        for s in _list(exe, "skill", "list", "--json"):
            if s.get("name") == SKILL_NAME and s.get("source") != "plugin" and s.get("path") and \
                    Path(s["path"]).resolve().is_relative_to(SKILLS_DIR.resolve()):
                found.append(f"0.1 の skill の登録({SKILLS_DIR})")
                break
    except StateUnreadable:
        pass
    hook = legacy_hook_file()
    if hook.is_file() and "session-hint" in hook.read_text(encoding="utf-8", errors="replace"):
        found.append(f"0.1 の hook のファイル({hook})")
    return found


def remove_legacy(exe: str) -> list[str]:
    """0.1 の登録の形が残っていれば外す。結果の説明を返す。"""
    done = []
    for what in legacy_registrations(exe):
        if what.startswith("0.1 の skill"):
            code, out, err = _run(exe, "skill", "remove", str(SKILLS_DIR))
            done.append(("外しました: " if code == 0 else "外せませんでした: ") + what +
                        ("" if code == 0 else f" ({(err or out).strip()[:120]})"))
        else:
            legacy_hook_file().unlink(missing_ok=True)
            done.append("消しました: " + what)
    return done


def mcp_registered(exe: str) -> tuple[bool, str]:
    code, out, _ = _run(exe, "mcp", "get", MCP_NAME)
    return code == 0, out.strip()


def _confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return not input(f"{question} (Y/n) ").strip().lower().startswith("n")
    except EOFError:
        return False


def show_status(exe: str, mp: str) -> None:
    try:
        p = find_plugin(_list(exe, "plugin", "list", "--json"))
        m, ours = find_marketplace(_list(exe, "plugin", "marketplace", "list", "--json"), mp)
    except StateUnreadable as e:
        print(f"状態を読めません: {e}")
        return
    if p:
        state = "有効" if p.get("enabled") else "無効"
        where = p.get("installedFrom") or p.get("marketplace") or p.get("source") or "?"
        print(f"プラグイン {PLUGIN_NAME}: 導入済み({state}、版 {p.get('version') or '?'}、{where})")
    else:
        print(f"プラグイン {PLUGIN_NAME}: 未導入")
    print(f"Marketplace {mp}: " + ((m.get("source", "登録済み") + ("(このフォルダ)" if ours else "")) if m else "未登録"))
    ok, info = mcp_registered(exe)
    print(f"MCP サーバー {MCP_NAME}: " + ("登録済み" if ok else "未登録"))
    if ok:
        print("  " + info.replace("\n", "\n  "))


def _register(exe: str, mp: str, args) -> int:
    plugins = _list(exe, "plugin", "list", "--json")
    markets = _list(exe, "plugin", "marketplace", "list", "--json")
    m, m_ours = find_marketplace(markets, mp)
    p = find_plugin(plugins)
    if m is not None and not m_ours:
        sys.stderr.write(
            f"同じ名前の Marketplace {mp} が、別の場所から登録されています: {m.get('source')}\n"
            "このフォルダで登録し直すなら、先にそれを外してください(入っているプラグインも外れます):\n"
            f"  copilot plugin marketplace remove {mp} --force\n")
        return 1
    if p is not None and not plugin_is_ours(p, mp):
        sys.stderr.write(
            f"プラグイン {PLUGIN_NAME} が、別の場所から入っています"
            f"({p.get('marketplace') or '?'}、{p.get('installedFrom') or p.get('source') or '?'})。\n"
            f"このフォルダで入れ直すなら、先に `copilot plugin uninstall {PLUGIN_NAME}` で外してください\n")
        return 1
    legacy = legacy_registrations(exe)
    print("Copilot CLI の利用者設定に、次のものを登録します(アプリも同じ設定を使います):")
    print(f"  Marketplace {mp}: {REPO_ROOT}(このフォルダ。写さずに、ここから直接読まれます)")
    print(f"  プラグイン {PLUGIN_NAME}@{mp}(MCP サーバー {MCP_NAME}、セッションの始めの hook、skill {SKILL_NAME})")
    if args.mcp:
        print("  (--mcp は要らなくなりました。MCP サーバーは、プラグインに入っています)")
    for what in legacy:
        print(f"  外すもの: {what}")
    if not _confirm("登録しますか", args.yes):
        print("登録しませんでした")
        return 0
    for line in remove_legacy(exe):
        print(line)
    if m is None:
        code, out, err = _run(exe, "plugin", "marketplace", "add", str(REPO_ROOT))
        print((out + err).strip())
        if code != 0:
            sys.stderr.write("Marketplace の登録に失敗しました\n")
            return 1
    if p is None:
        code, out, err = _run(exe, "plugin", "install", f"{PLUGIN_NAME}@{mp}")
        print((out + err).strip())
        if code != 0:
            sys.stderr.write("プラグインの導入に失敗しました\n")
            return 1
    elif not p.get("enabled"):
        _run(exe, "plugin", "enable", PLUGIN_NAME)
    print()
    show_status(exe, mp)
    print("\n使うには: 索引を作った作業フォルダで Copilot CLI(かアプリ)の新しいセッションを始めます。"
          "\n解除するには: unregister-copilot.bat(`ctxf copilot unregister`)")
    return 0


def _unregister(exe: str, mp: str) -> int:
    plugins = _list(exe, "plugin", "list", "--json")
    m, m_ours = find_marketplace(_list(exe, "plugin", "marketplace", "list", "--json"), mp)
    p = find_plugin(plugins)
    if p is not None:
        if plugin_is_ours(p, mp):
            code, out, err = _run(exe, "plugin", "uninstall", PLUGIN_NAME)
            print((out + err).strip())
        else:
            print(f"プラグイン {PLUGIN_NAME} は、別の場所から入っているので外しません"
                  f"({p.get('marketplace') or '?'}、{p.get('installedFrom') or p.get('source') or '?'})")
    if m is not None:
        if m_ours:
            code, out, err = _run(exe, "plugin", "marketplace", "remove", mp)
            print((out + err).strip())
        else:
            print(f"Marketplace {mp} は、別の場所を指しているので外しません({m.get('source')})")
    for line in remove_legacy(exe):
        print(line)
    ok, _ = mcp_registered(exe)
    if ok:
        code, out, err = _run(exe, "mcp", "remove", MCP_NAME)
        print((out + err).strip())
    print()
    show_status(exe, mp)
    return 0


def main(args) -> int:
    exe = find_copilot()
    if exe is None:
        sys.stderr.write("Copilot CLI(copilot)が見つかりません。入れてから、もう一度実行してください\n"
                         "  https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli\n")
        return 2
    if exe.lower().endswith((".cmd", ".bat")) and CMD_METACHARS & set(str(REPO_ROOT)):
        sys.stderr.write(f"このフォルダのパスに、cmd が特別に扱う文字({''.join(sorted(CMD_METACHARS))})が入っています: "
                         f"{REPO_ROOT}\nCopilot CLI(copilot.cmd)に正しく渡せないので、記号の無いフォルダに置き直してください\n")
        return 2
    try:
        mp = marketplace_name()
    except (OSError, ValueError, KeyError):
        sys.stderr.write(f"Marketplace の定義(.github/plugin/marketplace.json)を読めません: {REPO_ROOT}\n")
        return 2
    if not (SKILLS_DIR / SKILL_NAME / "SKILL.md").is_file() or not LAUNCHER.is_file():
        sys.stderr.write(f"skill のフォルダか入口(ctxf.py)がありません: {REPO_ROOT}\n")
        return 2
    try:
        if args.action == "status":
            show_status(exe, mp)
            return 0
        if args.action == "register":
            return _register(exe, mp, args)
        return _unregister(exe, mp)
    except StateUnreadable as e:
        sys.stderr.write(f"Copilot の状態を読めないので、何も変えずに止まります: {e}\n")
        return 1

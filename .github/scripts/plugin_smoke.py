"""プラグインとして入れた人の道筋を、まっさらな環境で通しで確かめる(CI から呼ぶ。手元でも動く)。

  setup(利用者ごとの置き場に仮想環境。モデルは取得しない)→ model download → init → index → search → eval → MCP サーバー → 始めの 1 文
  → /context-index・/context-setup(Copilot CLI の拡張。node があるときだけ。SDK は偽物に差し替える)

テスト(pytest)は、埋め込みモデルの実物を使わない。ここでは、実物のモデルを取得して、テスト用の小さな
コーパス(tests/fixtures)で、検索が答えを上位に出すところまでを見る。標準ライブラリだけで動く。

usage: python plugin_smoke.py [--skip-setup]
  環境変数 CF_HOME(利用者ごとの置き場)を、空の一時フォルダに向けてから動かす。向けていないと、ふだんの
  置き場を書き換えないように、何もせずに止まる。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
# プラグインとして写される形に近づける: 隣に .venv の無い写しを作り、そこから入口を動かす
PLUGIN_FILES = ("ctxf.py", "pyproject.toml", "plugin.json", "mcp.json")
PLUGIN_DIRS = ("src", "skills", "com.github.copilot")
QUESTION = "GPU のバッファをまとめて確保しておき、解放されたものを使い回している処理はどこですか"
EXPECTED_PATH = "Render/BufferPool.cpp"
# tests/fixtures の質問で、実物のモデルの hybrid が上位 5 件に根拠を出す割合の下限。手元の実測は 0.80
# (keyword・semantic も同じ)。OS による計算のわずかな差で 1 問動いても落ちないよう、1 問分の余裕を見る
HIT5_FLOOR = 0.7
# /context-index を試すための、偽の Copilot の SDK(コマンドを globalThis に置き、知らせを標準出力に出す)と、
# コマンドを呼んで終わるまで待つ取り次ぎ。本物の SDK は Copilot CLI が渡すので、ここでは動かせない。
# SDK の名前とコマンドの置き場は版で違う(Copilot CLI 1.0.113 からは @microsoft/copilot-sdk の interaction.commands、
# 1.0.112 まで と GitHub Copilot app は @github/copilot-sdk の commands)。偽物も、その版の置き場だけを読む
SDK_KINDS = {"@github/copilot-sdk": "opts.commands", "@microsoft/copilot-sdk": "opts.interaction?.commands"}
# FAKE_DIALOGS=1 のときは、ダイアログを出せるホストのふりをして、どの質問にも初めの値(schema の default)で答える
FAKE_SDK = """export async function joinSession(opts) {
  globalThis.__commands = __COMMANDS__;
  const dialogs = process.env.FAKE_DIALOGS === "1";
  const defaults = (schema) => Object.fromEntries(Object.entries(schema?.properties ?? {})
    .filter(([, p]) => p.default !== undefined).map(([k, p]) => [k, p.default]));
  return {
    capabilities: dialogs ? { ui: { elicitation: true } } : {},
    ui: {
      elicitation: async ({ message, requestedSchema }) => {
        console.log(`[dialog] ${message}`);
        return { action: "accept", content: defaults(requestedSchema) };
      },
    },
    getEvents: async () => [{ type: "session.start", data: { context: { cwd: process.env.FAKE_CWD } } }],
    log: async (m, o) => { console.log(`[${o?.level ?? "info"}] ${m}`); },
  };
}
"""
DRIVER = """import { pathToFileURL } from "node:url";
await import(pathToFileURL(process.argv[2]).href);
globalThis.__commands.find((c) => c.name === process.argv[3]).handler({ args: process.argv[4] ?? "" });
"""


def run(cmd, **kw) -> subprocess.CompletedProcess:
    t = time.time()
    p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)
    print(f"$ {' '.join(Path(str(c)).name if os.sep in str(c) else str(c) for c in cmd[:6])} ... "
          f"-> {p.returncode} ({time.time() - t:.1f}s)", flush=True)
    return p


def check(ok: bool, what: str, detail: str = "") -> None:
    print(("  ok: " if ok else "  FAILED: ") + what, flush=True)
    if not ok:
        if detail:
            print(detail[-3000:])
        raise SystemExit(1)


def mcp_call(proc: subprocess.Popen, mid: int, method: str, params: dict | None = None) -> dict:
    proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}) + "\n")
                     .encode("utf-8"))
    proc.stdin.flush()
    return json.loads(proc.stdout.readline().decode("utf-8"))


def main() -> int:
    for stream in (sys.stdout, sys.stderr):          # Windows の既定の文字コードでも、日本語の知らせを出せるように
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if not os.environ.get("CF_HOME"):
        print("CF_HOME を、空の一時フォルダに向けてから動かしてください(ふだんの置き場を書き換えないため)")
        return 2
    env = dict(os.environ)
    env.pop("CF_PYTHON", None)
    env.pop("CF_CONFIG", None)
    work = Path(tempfile.mkdtemp(prefix="cf-smoke-"))
    plugin, ws, corpus = work / "plugin", work / "workspace", work / "corpus"
    plugin.mkdir()
    ws.mkdir()
    for name in PLUGIN_FILES:
        shutil.copy2(REPO / name, plugin / name)
    for name in PLUGIN_DIRS:
        shutil.copytree(REPO / name, plugin / name, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REPO / "tests" / "fixtures" / "corpus", corpus)
    ctxf = [sys.executable, "-I", plugin / "ctxf.py"]

    if "--skip-setup" not in sys.argv:
        p = run([*ctxf, "setup"], env=env)
        models = Path(env["CF_HOME"]) / "models"
        had_model = models.is_dir() and any(models.rglob("*.onnx"))      # CI のキャッシュから戻したもの
        check(p.returncode == 0 and "Python のパッケージ:" in p.stdout
              and ("埋め込みモデル: 入っています" if had_model else "埋め込みモデル: インストールされていません") in p.stdout,
              "setup が仮想環境を用意し、埋め込みモデルは取得せずに有無を知らせる", p.stdout + p.stderr)
        # モデルは、利用者が確かめたうえで自分で取得するもの。ここでは、その手順(model download)を流す
        p = run([*ctxf, "model", "download"], env=env)
        check(p.returncode == 0, "model download が、埋め込みモデルを取得する", p.stdout + p.stderr)
    p = run([*ctxf, "where"], env=env)
    check(p.returncode == 0 and "(runtime)" in p.stdout, "入口が、利用者ごとの置き場の仮想環境を使う", p.stdout)

    p = run([*ctxf, "init", corpus / "engine", corpus / "app", corpus / "docs", "--workspace", ws], env=env)
    check(p.returncode == 0 and (ws / ".cf" / "config.toml").is_file(), "init が、設定を作る", p.stdout + p.stderr)
    p = run([*ctxf, "index", "--config", ws, "--require-embedding"], env=env)
    check(p.returncode == 0 and (ws / ".cf" / "index.db").is_file(), "index が、埋め込みつきの索引を作る",
          p.stdout + p.stderr)

    p = run([*ctxf, "search", QUESTION, "--config", ws, "--format", "json"], env=env)
    check(p.returncode == 0, "search が動く", p.stdout + p.stderr)
    found = json.loads(p.stdout)
    top5 = [x["path"] for x in found["passages"][:5]]
    check(found["mode"] == "hybrid" and EXPECTED_PATH in top5,
          f"日本語の質問で、hybrid の上位 5 件に {EXPECTED_PATH} が入る", json.dumps(top5))

    out = work / "eval"
    p = run([*ctxf, "eval", "--config", ws, "--cases", REPO / "tests" / "fixtures" / "cases.jsonl", "--out", out],
            env=env)
    check(p.returncode == 0 and (out / "summary.json").is_file(), "eval が、要約を書く", p.stdout + p.stderr)
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    hits = {m["method"]: m["retrieval"]["hit5_passage"] for m in summary["methods"]}
    print("  上位 5 件に根拠が入った割合: " + ", ".join(f"{k} {v:.2f}" for k, v in sorted(hits.items())))
    check(hits.get("hybrid", 0) >= HIT5_FLOOR, f"テスト用のコーパスで、hybrid の上位 5 件が {HIT5_FLOOR} 以上")

    # MCP サーバー: Copilot と同じく、プラグインのフォルダを作業フォルダにして起動し、利用者の作業フォルダは
    # 環境変数で渡す
    proc = subprocess.Popen([str(c) for c in (*ctxf, "serve")], cwd=plugin, env=dict(env, COPILOT_SESSION_CWD=str(ws)),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        hello = mcp_call(proc, 1, "initialize", {"protocolVersion": "2025-06-18"})
        tools = mcp_call(proc, 2, "tools/list")
        names = [t["name"] for t in tools["result"]["tools"]]
        reply = mcp_call(proc, 3, "tools/call", {"name": "search", "arguments": {"query": QUESTION}})
        text = reply["result"]["content"][0]["text"]
        check(hello["result"]["serverInfo"]["name"] == "context-foundation" and names == ["search", "status"],
              "MCP サーバーが挨拶に答え、道具を 2 つ出す", json.dumps(names))
        check(reply["result"]["isError"] is False and EXPECTED_PATH in text,
              "MCP の search が、環境変数の作業フォルダの索引から答える", text)
    finally:
        proc.stdin.close()
        proc.wait(timeout=60)
        proc.stdout.close()
        proc.stderr.close()

    p = subprocess.run([str(c) for c in (*ctxf, "session-hint", "--mcp")], input=json.dumps({"cwd": str(ws)}),
                       capture_output=True, text=True, encoding="utf-8", env=env)
    hint = json.loads(p.stdout or "{}").get("additionalContext", "")
    check("context-foundation-search tool first" in hint, "始めの 1 文が、検索の道具を先に使うように促す", p.stdout)

    node = shutil.which("node")
    if node and shutil.which("python"):          # 拡張は、MCP サーバーと同じく PATH の python で入口を動かす
        def fake_sdk(name):  # 偽の SDK を 1 つだけ置く(ホストは、どちらか一方の名前しか向けない)
            shutil.rmtree(plugin / "node_modules", ignore_errors=True)
            sdk = plugin / "node_modules" / Path(*name.split("/"))
            sdk.mkdir(parents=True)
            (sdk / "package.json").write_text(json.dumps({"name": name, "type": "module",
                                                          "exports": {"./extension": "./extension.mjs"}}), encoding="utf-8")
            (sdk / "extension.mjs").write_text(FAKE_SDK.replace("__COMMANDS__", SDK_KINDS[name]), encoding="utf-8")

        (work / "drive.mjs").write_text(DRIVER, encoding="utf-8")
        ext = plugin / "com.github.copilot" / "extensions" / "context-foundation" / "extension.mjs"
        ws2 = work / "workspace2"
        ws2.mkdir()
        shutil.copytree(corpus / "engine", ws2 / "engine")
        ext_env = dict(env, FAKE_CWD=str(ws2), CF_DISPLAY="timeline")
        fake_sdk("@github/copilot-sdk")
        p = run([node, work / "drive.mjs", ext, "context-index", "engine"], env=ext_env, timeout=600)
        check(p.returncode == 0 and "索引ができました" in p.stdout and "keyword だけ" not in p.stdout
              and (ws2 / ".cf" / "index.db").is_file(), "/context-index が、設定と埋め込みつきの索引を作る(@github の SDK)",
              p.stdout + p.stderr)
        fake_sdk("@microsoft/copilot-sdk")
        p = run([node, work / "drive.mjs", ext, "context-index", ""], env=ext_env, timeout=600)
        check(p.returncode == 0 and "次の検索からこの索引を使います" in p.stdout,
              "/context-index が、索引を作り直す(@microsoft の SDK)", p.stdout + p.stderr)
        # ダイアログを出せるホスト: 引数は使わずに尋ねる。偽の SDK は、どの質問にも初めの値で答える
        dlg_env = dict(ext_env, FAKE_DIALOGS="1")
        p = run([node, work / "drive.mjs", ext, "context-index", "full"], env=dlg_env, timeout=600)
        check(p.returncode == 0 and "引数は使いません" in p.stdout and "何をしますか" in p.stdout
              and "次の検索からこの索引を使います" in p.stdout and "すべて埋め込み直す" not in p.stdout,
              "/context-index が、引数を使わずに尋ね、初めの値(更新する)で索引を更新する", p.stdout + p.stderr)
        ws3 = work / "workspace3"
        ws3.mkdir()
        shutil.copytree(corpus / "engine", ws3 / "engine")
        shutil.copytree(corpus / "docs", ws3 / "docs")
        p = run([node, work / "drive.mjs", ext, "context-index", ""], env=dict(dlg_env, FAKE_CWD=str(ws3)), timeout=600)
        made = (ws3 / ".cf" / "config.toml").read_text(encoding="utf-8") if (ws3 / ".cf" / "config.toml").is_file() else ""
        check(p.returncode == 0 and "(1/2)" in p.stdout and "(2/2)" in p.stdout and "索引ができました" in p.stdout
              and "埋め込みは" in p.stdout and "root = 'docs'" in made and "root = 'engine'" in made
              and (ws3 / ".cf" / "index.db").is_file(),
              "/context-index が、フォルダを選ばせ、確かめてから、設定と索引を作る(ダイアログ)", p.stdout + p.stderr)
        # 用意はすでに済んでいるので、確かめるだけで終わる(取得はしない)。ログは利用者ごとの置き場に書く
        p = run([node, work / "drive.mjs", ext, "context-setup", ""], env=ext_env, timeout=600)
        log = Path(env["CF_HOME"]) / "context-setup.log"
        check(p.returncode == 0 and "用意ができました" in p.stdout and "索引があります" in p.stdout and log.is_file()
              and "Python のパッケージ: 入っています" in log.read_text(encoding="utf-8"),
              "/context-setup が、済んでいる用意を確かめて終わる(@microsoft の SDK)", p.stdout + p.stderr)
    else:
        print("  skipped: /context-index・/context-setup(node か python が PATH に無い)")

    shutil.rmtree(work, ignore_errors=True)
    print("ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

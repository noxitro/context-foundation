// context-foundation の Copilot CLI 拡張。モデルは呼ばない(ctxf.py を裏で動かし、結果をセッションに出すだけ)。
//
// /context-index [full] [<フォルダ> ...]
//     作業中のフォルダの索引(.cf\index.db)を作る・作り直す。利用者が打つコマンドなので、打ったことを同意とみなす
//     (エージェントからは呼べない。MCP サーバーは読み取り専用のまま)。
//     - 作業フォルダか親に設定(.cf\config.toml)があれば、ctxf.py index でその索引を作り直す。変わっていない
//       passage の埋め込みは使い回す。full を付けると、使い回さずにすべて埋め込み直す(--full)
//     - 無ければ、並べたフォルダで作業フォルダに設定を作り(ctxf.py init)、続けて索引を作る。並べていなければ使い方を出す
//     - この PC の用意(setup)ができていなければ、何も作らずに、用意の仕方を出す(大きな取得は、利用者が自分で始める)
//     裏で動かし、走っている間は進み具合をときどき出し、終わったらそう出す。ログは .cf\context-index.log。
//
// 拡張は Copilot CLI の実験的機能で、機能フラグ EXTENSIONS が有効なときだけ読み込まれる。
import { joinSession } from "@github/copilot-sdk/extension";
import { spawn } from "node:child_process";
import { closeSync, existsSync, openSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { failureMessage, initSources, keywordOnly, lastCwd, makeNotifier, parseProgress, planIndex, pluginRoot, progressNote } from "./lib.mjs";

// MCP サーバーと hook と同じく、PATH の python で入口(ctxf.py)を動かす。-I: 作業フォルダのファイルを読み込まない
const PYTHON = "python";

// 出力をログのファイルに書く版
function run(args, log) {
  return new Promise((done) => {
    const fd = openSync(log, "w");  // 毎回書き直す(進み具合は、今回の行だけを読む)
    const p = spawn(PYTHON, ["-I", ...args], { stdio: ["ignore", fd, fd], windowsHide: true });
    p.on("error", (err) => { closeSync(fd); done({ code: null, error: err }); });
    p.on("exit", (code) => { closeSync(fd); done({ code }); });
  });
}

// 出力を受け取る版(init。ctxf.py はパイプにも UTF-8 で出す)
function runText(args) {
  return new Promise((done) => {
    const p = spawn(PYTHON, ["-I", ...args], { stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
    let out = "";
    p.stdout.setEncoding("utf8");
    p.stderr.setEncoding("utf8");
    p.stdout.on("data", (s) => { out += s; });
    p.stderr.on("data", (s) => { out += s; });
    p.on("error", (err) => done({ code: null, error: err, out }));
    p.on("close", (code) => done({ code, out }));
  });
}

const cannotStart = (r) => `python を起動できません(PATH に Python 3.11 以上が要ります): ${r.error.message}`;

function readText(path) {
  try {
    return readFileSync(path, "utf8");
  } catch {
    return "";
  }
}

let running = null; // 走っている索引づくり(同時に 1 つだけ)

// 利用者に見せる文は tell、進み具合は say で出す。GitHub Copilot app は session.log の info と warning を表示しない
// (error だけ出る)ので、アプリで動いているときは tell の文をダイアログにも出す(lib.mjs の makeNotifier。
// CF_DISPLAY で切り替えられる)。session は下の joinSession で決まり、コマンドが呼ばれるのはその後
const { tell, say } = makeNotifier(() => session, process.env);

// 索引づくりの間、ログを数秒おきに見て、進み具合を画面に出す。止める関数を返す
function watchProgress(log, t0) {
  let state = {};
  const timer = setInterval(() => {
    const now = Date.now();
    const r = progressNote(state, parseProgress(readText(log)), now, Math.round((now - t0) / 1000));
    state = r.state;
    if (r.note) say(r.note).catch(() => { });
  }, 3000);
  return () => clearInterval(timer);
}

async function indexCommand(ctx) {
  // await の前に印を付ける(続けて呼ばれても、2 つ目は始めない)
  if (running) {
    await tell(`context-foundation: 索引を作っている途中です(${running})。終わるまで待ってください`, "warning");
    return;
  }
  running = "準備中";
  try {
    const plugin = pluginRoot(dirname(fileURLToPath(import.meta.url)));
    const ctxf = join(plugin, "ctxf.py");
    const setup = `python -I "${ctxf}" setup`;
    const cwd = lastCwd(await session.getEvents()) || process.cwd();
    const plan = planIndex(ctx.args, cwd);
    if (plan.error) {
      await tell(`context-foundation: ${plan.error}`, "error");
      return;
    }
    const ws = plan.workspace;
    running = ws;
    const t0 = Date.now();
    let made = "";
    if (plan.action === "init") {
      // 用意ができていなければ、入口が init の前に 4 で終わる(設定は作らない)
      const r = await runText([ctxf, "init", "--workspace", ws, ...plan.folders]);
      if (r.code !== 0) {
        await tell(`context-foundation: ${r.error ? cannotStart(r)
          : r.code === 4 ? failureMessage(4, r.out, "", setup)
            : `設定を作れませんでした(終了コード ${r.code})\n${r.out.trim()}`}`, "error");
        return;
      }
      made = `設定を作りました(${join(ws, ".cf", "config.toml")}。source: ${initSources(r.out).join("、")})。` +
        "Perforce を使うなら、.cf を .p4ignore に足してください(索引には、ソースの本文の写しが入ります)。\n";
    }
    const dir = join(ws, ".cf");
    const log = join(dir, "context-index.log");
    const first = plan.action === "init" || !existsSync(join(dir, "index.db"));
    const what = first ? "初めての索引" : plan.full ? "作り直し。すべて埋め込み直す" : "作り直し。変わっていない passage の埋め込みは使い回す";
    await tell(`context-foundation: ${made}索引づくりを始めました(${what}、${ws})。` +
      (first ? "大きなコードでは数十分かかります(約 8,000 ファイルで 30 分ほど)。" : "") +
      `裏で動かすので、作業を続けてかまいません。ログ: ${log}`);
    const args = [ctxf, "index", "--config", ws, ...(plan.full ? ["--full"] : [])];
    const stop = watchProgress(log, t0);
    const r = await run(args, log).finally(stop);
    const sec = Math.round((Date.now() - t0) / 1000);
    if (r.error) {
      await tell(`context-foundation: ${cannotStart(r)}`, "error");
    } else if (r.code === 0) {
      const out = readText(log);
      await tell(`context-foundation: 索引ができました(${sec} 秒、${join(dir, "index.db")})。` +
        (keywordOnly(out) ? `埋め込みモデルが無いので、keyword だけの索引です(意味での検索はできません。わけはログ: ${log})。` : "") +
        (first
          ? "新しいセッションから、エージェントが grep より先にこの索引で探します(今のセッションでは、始めに「索引が無い」と伝わっているので、使うなら頼んでください)"
          : "検索は、次の検索からこの索引を使います"));
    } else {
      await tell(`context-foundation: ${failureMessage(r.code, readText(log), log, setup)}`, "error");
    }
  } finally {
    running = null;
  }
}

const session = await joinSession({
  commands: [
    {
      // 拡張のコマンドには引数のヒントを付けられないので、説明文の先頭に引数を書く
      name: "context-index",
      description: "[full] [<フォルダ> ...] — コードと設計文書の検索の索引(context-foundation)を作る・作り直す。初めては検索したいフォルダを並べる。full ですべて埋め込み直す。裏で動かす(モデルは呼ばない)",
      handler: (ctx) => {
        indexCommand(ctx).catch((err) => tell(`context-foundation: ${err?.message ?? err}`, "error"));
      },
    },
  ],
});

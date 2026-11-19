// context-foundation の Copilot CLI 拡張。モデルは呼ばない(ctxf.py を裏で動かし、結果をセッションに出すだけ)。
//
// /context-index [full] [<フォルダ> ...]
//     作業中のフォルダの索引(.cf\index.db)を作る・作り直す。利用者が打つコマンドなので、打ったことを同意とみなす
//     (エージェントからは呼べない。MCP サーバーは読み取り専用のまま)。
//     - 作業フォルダか親に設定(.cf\config.toml)があれば、ctxf.py index でその索引を作り直す。変わっていない
//       passage の埋め込みは使い回す。full を付けると、使い回さずにすべて埋め込み直す(--full)
//     - 無ければ、並べたフォルダで作業フォルダに設定を作り(ctxf.py init)、続けて索引を作る。並べていなければ、
//       作業フォルダの直下のフォルダから選ばせる(複数選べるダイアログ。出せないホストでは使い方を出す)
//     - この PC の用意(setup)ができていなければ、何も作らずに、用意の仕方を出す(大きな取得は、利用者が自分で始める)
//     裏で動かし、走っている間は進み具合をときどき出し、終わったらそう出す。ログは .cf\context-index.log。
//
// /context-setup [no-model] [gpu | cpu]
//     この PC の用意(ctxf.py setup。Python のパッケージ約 200 MB を、利用者ごとの置き場に
//     入れる)をする。利用者が打つコマンドなので、打ったことを同意とみなす(エージェントからは呼べない)。
//     済んでいれば、確かめるだけで終わる。リポジトリを取得して隣に .venv がある人は、setup.bat を案内して何もしない。
//     裏で動かし、進み具合をときどき出す。ログは利用者ごとの置き場の context-setup.log。
//     索引づくりと用意は、同時には動かさない(用意は、索引づくりが使う仮想環境を作り直すことがある)。
//
// 拡張は Copilot CLI の実験的機能で、実験的機能を有効にしたとき(--experimental か /settings experimental on。
// 1.0.114 で確かめた)か、機能フラグ EXTENSIONS を有効にしたとき(1.0.112・1.0.113 で確かめた)だけ読み込まれる。
// SDK は、ホストの版で名前が違う(lib.mjs の SDK_NAMES)ので、静的な import にせず、名前を順に試して読む。
import { spawn } from "node:child_process";
import { closeSync, existsSync, mkdirSync, openSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  chooseFallback, dataHome, failureMessage, findWorkspace, folderChoices, folderSchema, hasLocalVenv, initSources, joinOptions,
  keywordOnly, lastCwd, loadSdk, makeNotifier, modelMissing, parseProgress, parseSetupArgs, parseSetupProgress, pickedFolders, planIndex,
  pluginRoot, progressNote, SETUP_HELP, setupFailureMessage, setupProgressNote, setupRuntimeFolder,
} from "./lib.mjs";

const { joinSession } = await loadSdk((name) => import(name));

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

let running = null; // 走っている索引づくりか用意(同時に 1 つだけ)。何が走っているかの文

// 走っているものがあれば断る(true を返す)。await の前に印を付けるので、続けて呼ばれても 2 つ目は始めない
function busy() {
  if (!running) return false;
  tell(`context-foundation: ${running}の途中です。終わるまで待ってください`, "warning").catch(() => { });
  return true;
}

// 利用者に見せる文は tell、進み具合は say で出す。GitHub Copilot app は session.log の info と warning を表示しない
// (error だけ出る)ので、アプリで動いているときは tell の文をダイアログにも出す(lib.mjs の makeNotifier。
// CF_DISPLAY で切り替えられる)。session は下の joinSession で決まり、コマンドが呼ばれるのはその後
const { tell, say, exclusive } = makeNotifier(() => session, process.env);

// 初めての索引で、フォルダを並べずに打たれたとき: 作業フォルダの直下のフォルダから、索引に入れるものを選ばせる
// (複数選べるダイアログ。モデルは呼ばない)。選んだフォルダの一覧か、やめた・出せないときは null。
// ダイアログは知らせのダイアログと同じ列に並べる(出している間に来た知らせは、閉じてから出す)
async function chooseFolders(ws) {
  const { names, omitted } = folderChoices(ws);
  if (!session.capabilities?.ui?.elicitation) {
    await tell(`context-foundation: ${chooseFallback(ws, names)}`, "error");
    return null;
  }
  try {
    const picked = await exclusive(async () => {
      if (!names.length) {
        return (await session.ui.confirm(`context-foundation: 作業フォルダ全体(${ws})で索引を作りますか`)) ? ["."] : [];
      }
      const r = await session.ui.elicitation({
        message: `context-foundation: 索引に入れるフォルダを選んでください(${ws})`,
        requestedSchema: folderSchema(names, omitted),
      });
      return r.action === "accept" ? pickedFolders(r.content, names) : [];
    });
    if (picked.length) return picked;
  } catch (err) {
    // このホストが複数選ぶダイアログを描けないなど。並べて打つ使い方を出す
    await tell(`context-foundation: フォルダを選ぶダイアログを出せませんでした(${err?.message ?? err})。` +
      chooseFallback(ws, names), "error");
    return null;
  }
  await tell("context-foundation: 索引づくりをやめました(フォルダを選ばなかった)");
  return null;
}

// 索引づくりか用意の間、ログを数秒おきに見て、進み具合を画面に出す。止める関数を返す
function watchProgress(log, t0, parse = parseProgress, decide = progressNote) {
  let state = {};
  const timer = setInterval(() => {
    const now = Date.now();
    const r = decide(state, parse(readText(log)), now, Math.round((now - t0) / 1000));
    state = r.state;
    if (r.note) say(r.note).catch(() => { });
  }, 3000);
  return () => clearInterval(timer);
}

const plugin = () => pluginRoot(dirname(fileURLToPath(import.meta.url)));

async function indexCommand(ctx) {
  if (busy()) return;
  running = "索引づくり";
  try {
    const ctxf = join(plugin(), "ctxf.py");
    const setup = `python -I "${ctxf}" setup`;
    const cwd = lastCwd(await session.getEvents()) || process.cwd();
    let plan = planIndex(ctx.args, cwd);
    if (plan.error) {
      await tell(`context-foundation: ${plan.error}`, "error");
      return;
    }
    if (plan.action === "choose") {
      // 選ぶのを待っている間に打たれたコマンドには、そう断る(まだ何も走っていない)
      running = "索引に入れるフォルダの選択";
      const folders = await chooseFolders(plan.workspace);
      if (!folders) return;
      plan = { ...plan, action: "init", folders };
    }
    const ws = plan.workspace;
    running = `索引づくり(${ws})`;
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

async function setupCommand(ctx) {
  const opts = parseSetupArgs(ctx.args);
  if (opts.help || opts.error) {
    await tell(`context-foundation: ${opts.help ? SETUP_HELP : opts.error}`, opts.error ? "error" : undefined);
    return;
  }
  if (busy()) return;
  running = "用意(setup)";
  try {
    const root = plugin();
    if (hasLocalVenv(root)) {
      await tell(`context-foundation: このプラグインは、取得したリポジトリの .venv(${join(root, ".venv")})で動いています。` +
        `用意をやり直すときは、ターミナルで次を実行してください: "${join(root, "setup.bat")}"`, "error");
      return;
    }
    const home = dataHome(process.env);
    mkdirSync(home, { recursive: true });
    const log = join(home, "context-setup.log");
    const t0 = Date.now();
    await tell("context-foundation: 用意(setup)を始めました。" +
      `Python のパッケージ(PyPI から約 200 MB)を、${home} に入れます(埋め込みモデルは入れません)。` +
      "初めては数分かかります(済んでいれば、確かめるだけで終わります)。裏で動かすので、作業を続けてかまいません。" +
      `ログ: ${log}`);
    const stop = watchProgress(log, t0, parseSetupProgress, setupProgressNote);
    const r = await run([join(root, "ctxf.py"), "setup", ...opts.args], log).finally(stop);
    const sec = Math.round((Date.now() - t0) / 1000);
    if (r.error) {
      await tell(`context-foundation: ${cannotStart(r)}`, "error");
      return;
    }
    if (r.code !== 0) {
      await tell(`context-foundation: ${setupFailureMessage(r.code, readText(log), log)}`, "error");
      return;
    }
    const out = readText(log);
    const ws = findWorkspace(lastCwd(await session.getEvents()) || process.cwd());
    const next = ws && existsSync(join(ws, ".cf", "index.db"))
      ? `この作業フォルダには索引があります(${ws})。検索の道具は、新しいセッションから出ます(動いているセッションには出ません)`
      : ws
        ? "次に /context-index を打って、索引を作ってください。できたら、新しいセッションを始めます"
        : "次に /context-index を打ち、索引に入れるフォルダを選んでください(並べて打っても構いません。例: /context-index Engine Docs)。" +
          "できたら、新しいセッションを始めます";
    // 埋め込みモデル(PyPI の外の OSS)は、利用者の環境で確かめてから入れるものなので、入れずに、無いことを知らせる
    const missing = modelMissing(out);
    const modelNote = !missing ? ""
      : "埋め込みモデルはインストールされていません(Hugging Face の OSS なので、setup では入れません)。" +
        "意味での検索には要ります。入手先とライセンスを確かめ、この PC で使ってよければ、" +
        (missing.command ? `ターミナルで次を実行してください: ${missing.command}\n` : "設定の model_dir のフォルダにモデルを置いてください。\n") +
        "入れるまでは、検索は keyword だけです。\n";
    await tell(`context-foundation: 用意ができました(${sec} 秒、置き場: ${setupRuntimeFolder(out) || home})。` +
      modelNote + next);
  } finally {
    running = null;
  }
}

const commands = [
  {
    name: "context-setup",
    description: "[no-model] [gpu | cpu] — この PC の検索の用意(context-foundation)。Python のパッケージ約 200 MB を入れる(埋め込みモデルは入れず、有無を知らせる)。初めに 1 回。裏で動かす(モデルは呼ばない)",
    handler: (ctx) => {
      setupCommand(ctx).catch((err) => tell(`context-foundation: ${err?.message ?? err}`, "error"));
    },
  },
  {
    // 拡張のコマンドには引数のヒントを付けられないので、説明文の先頭に引数を書く
    name: "context-index",
    description: "[full] [<フォルダ> ...] — コードと設計文書の検索の索引(context-foundation)を作る・作り直す。初めは検索したいフォルダを並べるか、省いて一覧から選ぶ。full ですべて埋め込み直す。裏で動かす(モデルは呼ばない)",
    handler: (ctx) => {
      indexCommand(ctx).catch((err) => tell(`context-foundation: ${err?.message ?? err}`, "error"));
    },
  },
];
const session = await joinSession(joinOptions(commands));

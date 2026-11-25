// context-foundation の Copilot CLI 拡張。モデルは呼ばない(ctxf.py を動かし、結果をセッションに出すだけ)。
//
// /context-index(引数は取らない)
//     作業中のフォルダの索引(.cf\index.db)を作る・更新する。利用者が打つコマンドなので、打ったことを同意とみなす
//     (エージェントからは呼べない。MCP サーバーは読み取り専用のまま)。
//     始めに ctxf.py sources --json で今の状態(検索するフォルダ・索引・埋め込みの計算先とモデル)を読み、
//     ダイアログで尋ねる。
//     - 設定(.cf\config.toml)が無ければ: 索引に入れるフォルダを選ばせ(複数選べるダイアログ)、まとめて確かめてから、
//       ctxf.py init で設定を作り、続けて索引を作る
//     - あれば: 今の状態を見せて、することを選ばせる。更新する(変わっていない passage の埋め込みは使い回す)・
//       検索するフォルダを変える(ctxf.py sources --set。[[source]] だけを書き直す)・すべて作り直す(index --full)
//     - この PC の用意(setup)ができていなければ、何も尋ねずに、用意の仕方を出す(大きな取得は、利用者が自分で始める)
//     ダイアログを出せないホストでだけ、前の版の引数の形([full] [<フォルダ> ...])で動かす。
//     走っている間は進み具合をときどき出し、終わったらそう出す(埋め込みを GPU と CPU のどちらで計算したかも)。
//     ログは .cf\context-index.log(裏で動かすか、終わるまで待つかは、/context-setup と同じ)。
//
// /context-setup(引数は取らない)
//     この PC の用意(ctxf.py setup。Python のパッケージ約 200 MB を、利用者ごとの置き場に
//     入れる)をする。利用者が打つコマンドなので、打ったことを同意とみなす(エージェントからは呼べない)。
//     始めに、ダイアログで順に尋ねる: 埋め込みを GPU と CPU のどちらで計算するか(Windows だけ)、手元に置いた
//     埋め込みモデルのフォルダ、まとめた内容でよいか。尋ねる前に ctxf.py setup --status で今の状態を読み、
//     前の選び方と今のモデルのフォルダを初めの値にする。ダイアログを出せないホストでは、尋ねずに前の選び方で用意する。
//     済んでいれば、確かめるだけで終わる。リポジトリを取得して隣に .venv がある人は、setup.bat を案内して何もしない。
//     進み具合をときどき出す。ログは利用者ごとの置き場の context-setup.log。Copilot CLI では裏で動かし、
//     ダイアログを出すとき(GitHub Copilot app)は、終わって結果のダイアログを閉じるまで、コマンドを終えない。
//     索引づくりと用意は、同時には動かさない(用意は、索引づくりが使う仮想環境を作り直すことがある)。
//
// 拡張は Copilot CLI の実験的機能で、実験的機能を有効にしたとき(--experimental か /settings experimental on。
// 1.0.114 で確かめた)か、機能フラグ EXTENSIONS を有効にしたとき(1.0.112・1.0.113 で確かめた)だけ読み込まれる。
// SDK は、ホストの版で名前が違う(lib.mjs の SDK_NAMES)ので、静的な import にせず、名前を順に試して読む。
import { spawn } from "node:child_process";
import { closeSync, existsSync, mkdirSync, openSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  chooseFallback, commandHandler, dataHome, failureMessage, findConfig, folderChoices, folderSchema, hasLocalVenv, indexFile, initSources, joinOptions,
  keywordOnly, lastCwd, loadSdk, makeNotifier, modelFolderPrompt, modelFolderSchema, modelMissing, modelUseResult, parseProgress,
  accelPrompt, accelSchema, askAccel, choiceSchema, currentModelFolder, pickedYes, SETUP_NO, SETUP_YES, modelChoice, modelStepPrompt, parseSetupProgress, pickedFolders, pickedModelFolder,
  planIndex, pluginRoot, progressNote, setupArgs, setupConfirmText, setupFailureMessage, setupProgressNote, setupRuntimeFolder, setupStatus,
  modelNotUsedReason, REINDEX_AFTER_MODEL, setupStartText, setupSteps, splitArgs, workspaceOf,
  AFTER_SETUP_NO, AFTER_SETUP_YES, askOneOf, CHANGE_YES, changeConfirmText, changedText, computeName, embedNote, folderDefaults, folderPrompt,
  INDEX_BUILD, INDEX_CANCEL, INDEX_FULL, INDEX_NO, INDEX_UPDATE, INDEX_YES, indexConfirmText, indexMenuChoices, indexMenuText, indexState,
  logTail, MENU_RETRY, menuSchema, pickedAction, sourcesPlan,
} from "./lib.mjs";

const { joinSession } = await loadSdk((name) => import(name));

// MCP サーバーと hook と同じく、PATH の python で入口(ctxf.py)を動かす。-I: 作業フォルダのファイルを読み込まない
const PYTHON = "python";

// 出力をログのファイルに書く版。env を渡せば、子の環境変数をそれにする
function run(args, log, env) {
  return new Promise((done) => {
    const fd = openSync(log, "w");  // 毎回書き直す(進み具合は、今回の行だけを読む)
    // error のあとに exit が来ることもある(Node の文書)。ログを閉じて返すのは 1 回だけ
    let finished = false;
    const finish = (r) => {
      if (finished) return;
      finished = true;
      closeSync(fd);
      done(r);
    };
    let p;
    try {
      p = spawn(PYTHON, ["-I", ...args], { stdio: ["ignore", fd, fd], windowsHide: true, ...(env ? { env } : {}) });
    } catch (err) {
      finish({ code: null, error: err });
      return;
    }
    p.on("error", (err) => finish({ code: null, error: err }));
    p.on("exit", (code) => finish({ code }));
  });
}

// 出力を受け取る版(init・model use。ctxf.py はパイプにも UTF-8 で出す)。env を渡せば、子の環境変数をそれにする
function runText(args, env) {
  return new Promise((done) => {
    const p = spawn(PYTHON, ["-I", ...args], { stdio: ["ignore", "pipe", "pipe"], windowsHide: true, ...(env ? { env } : {}) });
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
const notifier = makeNotifier(() => session, process.env);
const { tell, say, exclusive } = notifier;
// 走っている間のことの断り。ダイアログを出すとき(GitHub Copilot app)は、コマンドは結果のダイアログを閉じるまで終わらない
const carryOn = notifier.waits
  ? "終わったら、結果をダイアログに出します(閉じると、このコマンドが終わります)。"
  : "裏で動かすので、作業を続けてかまいません。";
const handle = (fn) => commandHandler(fn, notifier, (err) => tell(`context-foundation: ${err?.message ?? err}`, "error"));

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

// はい・いいえを、日本語の選択肢のダイアログで尋ねる(session.ui.confirm は、ホストが英語の Yes・No を出す)。
// 知らせのダイアログと同じ列に並べる。yes を選んで送ったときだけ true
async function askYesNo(message, yes, no) {
  const r = await exclusive(() => session.ui.elicitation({ message, requestedSchema: choiceSchema(yes, no) }));
  return r.action === "accept" && pickedYes(r.content, yes);
}

// /context-setup の後で、手元に置いた埋め込みモデルのフォルダを ctxf.py model use で確かめて覚える
// (取得はしない。モデルは呼ばない)。first は始めの質問で入れられたフォルダ(1 回目は尋ねずにそれを使う)。
// 使えなければ、わけを添えて尋ね直す。ダイアログは知らせのダイアログと同じ列に並べる。
// { state: "saved", folder, notes } か { state: "skipped", why }。ダイアログを出せないホストでは null
const MODEL_TRIES = 3;
async function askModelFolder(ctxf, env, missing, first = "") {
  if (!first && !session.capabilities?.ui?.elicitation) return null;
  let retry = "";
  for (let i = 0; i < MODEL_TRIES; i++) {
    let folder;
    if (i === 0 && first) {
      folder = first;
    } else {
      try {
        const r = await exclusive(() => session.ui.elicitation({
          message: modelFolderPrompt(missing, retry), requestedSchema: modelFolderSchema(),
        }));
        folder = r.action === "accept" ? pickedModelFolder(r.content) : "";
      } catch (err) {
        return i ? { state: "skipped", why: `ダイアログを出せませんでした(${err?.message ?? err})` } : null;
      }
    }
    if (!folder) return { state: "skipped", why: retry };   // 尋ね直しを取り消したときは、前のだめだったわけを添える
    await say(`context-foundation: モデルを読み込んで確かめています(${folder})`);
    let r = await runText([ctxf, "model", "use", folder], env);
    if (r.error) return { state: "skipped", why: cannotStart(r) };
    let u = modelUseResult(r.code, r.out);
    if (u.state === "warnings") {
      // 大きさが控えの値と違う(別の版・別のモデル)など。覚える前に、使うかを尋ねる
      const ok = await askYesNo(
        `context-foundation: ${folder} を確かめると、気になる点がありました。\n${u.notes.map((n) => `- ${n}`).join("\n")}\n` +
        "このフォルダを使いますか", "このフォルダを使う", "使わない(別のフォルダを入れる)").catch(() => false);
      if (!ok) {
        retry = "前のフォルダは使いませんでした。";
        continue;
      }
      r = await runText([ctxf, "model", "use", "--yes", folder], env);
      if (r.error) return { state: "skipped", why: cannotStart(r) };
      u = modelUseResult(r.code, r.out);
    }
    if (u.state === "saved") return { state: "saved", folder, notes: u.notes };
    retry = `${folder} は使えませんでした: ${u.detail}`;
  }
  return { state: "skipped", why: `${MODEL_TRIES} 回とも使えませんでした(${retry})` };
}

// 設定を作る(ctxf.py init)。作れたら、始めの知らせに添える文。作れなければ、わけを知らせて null
async function makeConfig(ctxf, setup, ws, folders) {
  // 用意ができていなければ、入口が init の前に 4 で終わる(設定は作らない)
  const r = await runText([ctxf, "init", "--workspace", ws, ...folders]);
  if (r.code !== 0) {
    await tell(`context-foundation: ${r.error ? cannotStart(r)
      : r.code === 4 ? failureMessage(4, r.out, "", setup)
        : `設定を作れませんでした(終了コード ${r.code})\n${r.out.trim()}`}`, "error");
    return null;
  }
  return `設定を作りました(${join(ws, ".cf", "config.toml")}。source: ${initSources(r.out).join("、")})。` +
    "Perforce を使うなら、.cf を .p4ignore に足してください(索引には、ソースの本文の写しが入ります)。\n";
}

// 索引を作って(ctxf.py index)、結果を知らせる。config は設定のファイル(無ければ、作業フォルダの .cf\config.toml)、
// made は始めの知らせの前に添える文、init は設定を作ったばかりか、compute は埋め込みの計算先の呼び名(分からなければ "")
async function buildIndex(ctxf, setup, ws, { config, full = false, made = "", init = false, compute = "" } = {}) {
  running = `索引づくり(${ws})`;
  const t0 = Date.now();
  // 設定ファイルを渡す(作業フォルダを渡すと、ctxf.py は .cf\config.toml より先に、作業フォルダの直下の
  // config.toml を探す。そこにあるのは、ほかの道具の設定のことがある)
  const cfg = config ?? join(ws, ".cf", "config.toml");
  const log = join(dirname(cfg), "context-index.log");
  const index = indexFile(cfg);
  const first = init || !existsSync(index);
  const what = first ? "初めての索引" : full ? "作り直し。すべて埋め込み直す" : "更新。変わっていない passage の埋め込みは使い回す";
  await tell(`context-foundation: ${made}索引づくりを始めました(${what}、${ws}${compute ? `、埋め込みの計算: ${compute}` : ""})。` +
    (first ? "大きなコードでは数十分かかります(約 8,000 ファイルで 30 分ほど)。" : "") +
    `${carryOn}ログ: ${log}`);
  const args = [ctxf, "index", "--config", cfg, ...(full ? ["--full"] : [])];
  const stop = watchProgress(log, t0);
  const r = await run(args, log).finally(stop);
  const sec = Math.round((Date.now() - t0) / 1000);
  if (r.error) {
    await tell(`context-foundation: ${cannotStart(r)}`, "error");
  } else if (r.code === 0) {
    const out = readText(log);
    await tell(`context-foundation: 索引ができました(${sec} 秒、${index})。` + embedNote(out) +
      (keywordOnly(out) ? `埋め込みモデルが無いので、keyword だけの索引です(意味での検索はできません。わけはログ: ${log})。` : "") +
      (first
        ? "今のセッションでも、次の依頼から検索の道具が出ます(エージェントには始めに「索引が無い」と伝わっているので、" +
          "使うなら頼んでください。このセッションを始めた後に用意(setup)をした場合は、新しいセッションから出ます)。" +
          "新しいセッションからは、エージェントが grep より先にこの索引で探します"
        : "検索は、次の検索からこの索引を使います"));
  } else {
    await tell(`context-foundation: ${failureMessage(r.code, readText(log), log, setup, index)}`, "error");
  }
}

// ダイアログを出せないホストでの形: 引数([full] [<フォルダ> ...])で決める(lib.mjs の planIndex)
async function indexByArgs(args, ctxf, setup, cwd) {
  const plan = planIndex(args, cwd, process.env);
  if (plan.error) {
    await tell(`context-foundation: ${plan.error}`, "error");
    return;
  }
  if (plan.action === "choose") {
    await tell(`context-foundation: ${chooseFallback(plan.workspace, folderChoices(plan.workspace).names)}`, "error");
    return;
  }
  let made = "";
  if (plan.action === "init") {
    made = await makeConfig(ctxf, setup, plan.workspace, plan.folders);
    if (made === null) return;
  }
  await buildIndex(ctxf, setup, plan.workspace, { config: plan.config, full: plan.full, made, init: plan.action === "init" });
}

// 初めての索引: 索引に入れるフォルダを選ばせ、まとめて確かめる。{ action: "init", folders } か、{ error } か、
// やめたら null。始めのダイアログを出せなければ、投げる(呼ぶ側が、引数の形に替える)
async function askFirstIndex(ws, state, lead) {
  const { names, omitted } = folderChoices(ws);
  const total = names.length ? 2 : 1;
  let folders = ["."];       // 直下にフォルダが無ければ、作業フォルダ全体
  if (names.length) {
    const r = await exclusive(() => session.ui.elicitation({
      message: lead + folderPrompt(1, total, ws), requestedSchema: folderSchema(names, omitted),
    }));
    if (r.action !== "accept") return null;
    folders = pickedFolders(r.content, names);
    if (!folders.length) return null;
    // 書き込まれたパス(Other)が無ければ、設定を作る前に断る
    const missing = folders.map((f) => resolve(ws, f)).filter((f) => !existsSync(f));
    if (missing.length) return { error: `フォルダがありません: ${missing.join(", ")}。もう一度 /context-index を打って、選び直してください` };
  }
  const confirm = () => askYesNo((names.length ? "" : lead) + indexConfirmText(total, total, ws, folders, state), INDEX_YES, INDEX_NO);
  // 途中の段でダイアログを出せなくなったときは、答えの一部だけで作り始めない(やめたとみなす)
  const ok = names.length ? await confirm().catch(() => false) : await confirm();
  return ok ? { action: "init", folders } : null;
}

// 設定があるとき: 今の状態を見せて、することを選ばせる。{ action: "index", full } か { action: "change", folders } か、
// { done }(何もせずに終わるときの知らせ)か、{ error } か、やめたら null。始めのダイアログを出せなければ、投げる
async function askExistingIndex(ctxf, config, ws, state, lead) {
  const choices = indexMenuChoices(state);
  const action = await askOneOf((retry) => exclusive(() => session.ui.elicitation({
    message: lead + indexMenuText(state, retry), requestedSchema: menuSchema(choices),
  })), (content) => pickedAction(content, choices), MENU_RETRY);
  if (!action || action === INDEX_CANCEL) return null;
  if (action === INDEX_UPDATE || action === INDEX_BUILD) return { action: "index", full: false };
  if (action === INDEX_FULL) return { action: "index", full: true };
  // 検索するフォルダを変える: 今のフォルダを選んだ一覧を出し、何が変わるかを確かめてから書き直す
  try {
    const { names, omitted } = folderChoices(ws);
    const { extra, checked, missing } = folderDefaults(names, state.sources);
    const all = [...names, ...extra];
    const r = await exclusive(() => session.ui.elicitation({
      message: folderPrompt(1, 2, ws, true, missing), requestedSchema: folderSchema(all, omitted, checked, names.length),
    }));
    if (r.action !== "accept") return null;
    const folders = pickedFolders(r.content, all);
    if (!folders.length) return null;
    const dry = await runText([ctxf, "sources", "--config", config, "--set", ...folders, "--dry-run", "--json"]);
    if (dry.error) return { error: cannotStart(dry) };
    const plan = sourcesPlan(dry.out);
    if (!plan) return { error: `検索するフォルダを替えられません: ${logTail(dry.out, 2) || `終了コード ${dry.code}`}` };
    // 選び直しても同じなら、何もしない(確かめを出さずに、索引づくりを始めない)
    if (plan.result === "unchanged") {
      return { done: "検索するフォルダは、今と同じです。設定も索引も変えていません(索引を更新するなら、もう一度 /context-index を打って「更新する」を選びます)" };
    }
    if (!(await askYesNo(changeConfirmText(2, 2, plan), CHANGE_YES, INDEX_NO))) return null;
    return { action: "change", folders };
  } catch (err) {
    // 途中の段でダイアログを出せなくなった(複数選ぶダイアログを描けないホストなど): 答えの一部だけで書き直さず、
    // ほかの変え方を知らせる
    return {
      error: `フォルダを選ぶダイアログを出せませんでした(${err?.message ?? err})。設定は変えていません。検索するフォルダを変えるには、` +
        `ターミナルで次を実行するか、${config} の [[source]] を直してください: python -I "${ctxf}" sources --config "${config}" --set <フォルダ> ...`,
    };
  }
}

// /context-index の中身(/context-setup の終わりに、続けて索引を作るときも使う)。args はコマンドの引数
async function indexFlow(args) {
  const ctxf = join(plugin(), "ctxf.py");
  const setup = `python -I "${ctxf}" setup`;
  const cwd = lastCwd(await session.getEvents()) || process.cwd();
  if (!session.capabilities?.ui?.elicitation) return indexByArgs(args, ctxf, setup, cwd);
  let config;
  try {
    config = findConfig(cwd, process.env);
  } catch (err) {
    await tell(`context-foundation: ${err.message}(CF_CONFIG を外すか、指す先を直してください)`, "error");
    return;
  }
  const ws = config ? workspaceOf(config) : resolve(cwd);
  // 選ぶのを待っている間に打たれたコマンドには、そう断る(まだ何も走っていない)
  running = "索引づくりの質問への答え";
  // 今の状態を読んでから尋ねる。用意ができていなければ、入口が 4 で終わる(何も尋ねずに、用意の仕方を出す)
  const st = await runText([ctxf, "sources", "--json", ...(config ? ["--config", config] : ["--workspace", ws])]);
  if (st.error) {
    await tell(`context-foundation: ${cannotStart(st)}`, "error");
    return;
  }
  if (st.code === 4) {
    await tell(`context-foundation: ${failureMessage(4, st.out, "", setup)}`, "error");
    return;
  }
  const state = indexState(st.out);
  if (!state) {
    await tell(`context-foundation: 今の状態を読めませんでした(${logTail(st.out, 2) || `終了コード ${st.code}`})。` +
      (config ? `設定(${config})を直してから、もう一度 /context-index を打ってください` : ""), "error");
    return;
  }
  // 引数は取らない(前の版の full とフォルダの並びも、尋ねる形に替えた)。付いていれば、そう断る
  const lead = splitArgs(args ?? "").length ? "引数は使いません。次の質問に答えてください。\n" : "";
  let plan;
  try {
    plan = config ? await askExistingIndex(ctxf, config, ws, state, lead) : await askFirstIndex(ws, state, lead);
  } catch (err) {
    // このホストが選ぶダイアログを描けないなど。前の版の引数の形で動かす
    await say(`context-foundation: ダイアログを出せませんでした(${err?.message ?? err})。引数の形で動かします`);
    return indexByArgs(args, ctxf, setup, cwd);
  }
  if (!plan) {
    await tell("context-foundation: 索引づくりをやめました(何も変えていません)");
    return;
  }
  if (plan.error) {
    await tell(`context-foundation: ${plan.error}`, "error");
    return;
  }
  if (plan.done) {
    await tell(`context-foundation: ${plan.done}`);
    return;
  }
  let made = "";
  if (plan.action === "init") {
    made = await makeConfig(ctxf, setup, ws, plan.folders);
    if (made === null) return;
  } else if (plan.action === "change") {
    const r = await runText([ctxf, "sources", "--config", config, "--set", ...plan.folders, "--json"]);
    const done = r.error ? null : sourcesPlan(r.out);
    if (!done) {
      await tell(`context-foundation: ${r.error ? cannotStart(r)
        : `検索するフォルダを替えられませんでした(設定は変えていません): ${logTail(r.out, 2) || `終了コード ${r.code}`}`}`, "error");
      return;
    }
    made = changedText(done);
  }
  const e = state.embedding;
  const compute = e?.enabled && e.model?.state === "installed" ? computeName(e.provider) : "";
  await buildIndex(ctxf, setup, ws, { config: config || undefined, full: plan.full, made, init: plan.action === "init", compute });
}

async function indexCommand(ctx) {
  if (busy()) return;
  running = "索引づくり";
  try {
    await indexFlow(ctx.args);
  } finally {
    running = null;
  }
}

// 始めに、用意の中身を順に尋ねる(lib.mjs の setupSteps)。{ gpu, model }(gpu・model が null の段は尋ねていない)。
// lead は、始めの質問の前に添える文(引数を使わないことなど)。取り消されたら null。
// 始めのダイアログを出せなければ、投げる(呼ぶ側が、尋ねずに用意する)。途中の段で出せなくなったときは、
// 答えの一部だけで用意を始めないよう、取り消しとみなす(null)
async function askSetupPlan(status, home, lead = "") {
  const steps = setupSteps(status);
  const plan = { gpu: null, model: null };
  for (const [i, step] of steps.entries()) {
    const n = i + 1;
    const head = i === 0 ? lead : "";
    try {
      if (step === "accel") {
        // 選択肢に無い答え(書き込む欄)を読めなければ、尋ね直す(lib.mjs の askAccel。3 回読めなければ、やめる)
        const gpu = await askAccel((retry) => exclusive(() => session.ui.elicitation({
          message: head + accelPrompt(n, steps.length, status, retry), requestedSchema: accelSchema(status),
        })));
        if (gpu === null) return null;
        plan.gpu = gpu;
      } else if (step === "model") {
        const r = await exclusive(() => session.ui.elicitation({
          message: head + modelStepPrompt(n, steps.length, status), requestedSchema: modelFolderSchema(currentModelFolder(status)),
        }));
        if (r.action !== "accept") return null;
        plan.model = modelChoice(pickedModelFolder(r.content), status);
      } else if (!(await askYesNo(head + setupConfirmText(n, steps.length, plan, status, home), SETUP_YES, SETUP_NO))) {
        return null;
      }
    } catch (err) {
      if (i === 0) throw err;
      return null;
    }
  }
  return plan;
}
async function setupCommand(ctx) {
  if (busy()) return;
  running = "用意(setup)";
  try {
    const root = plugin();
    const ctxf = join(root, "ctxf.py");
    if (hasLocalVenv(root)) {
      await tell(`context-foundation: このプラグインは、取得したリポジトリの .venv(${join(root, ".venv")})で動いています。` +
        `用意をやり直すときは、ターミナルで次を実行してください: "${join(root, "setup.bat")}"`, "error");
      return;
    }
    const home = dataHome(process.env);
    mkdirSync(home, { recursive: true });
    const log = join(home, "context-setup.log");
    // setup がモデルの有無を確かめるときに、このセッションの作業フォルダの設定を読むように、設定のファイルを渡す
    // (Copilot は、拡張をプラグインのフォルダで動かすので、そのままでは子は作業フォルダの設定を見つけない。
    // 作業フォルダを今のフォルダとして渡すと、ctxf.py は直下の config.toml を先に探し、ほかの道具の設定を拾いうる)
    let config = "";
    const cwd = lastCwd(await session.getEvents()) || process.cwd();
    try {
      config = findConfig(cwd, process.env);
    } catch {
      // CF_CONFIG の指す先が無い(/context-index が、そのわけを出す)
    }
    const env = config && !process.env.CF_CONFIG ? { ...process.env, CF_CONFIG: config } : undefined;
    // 引数は取らない(前の版の gpu・cpu・no-model も、尋ねる形に替えた)。付いていれば、そう断る
    const hasArgs = splitArgs(ctx.args ?? "").length > 0;
    // 今の状態を読んでから尋ねる(前の選び方と今のモデルのフォルダを、初めの値にする)
    let plan = null;
    let asked = false;
    let status = null;
    let ignored = hasArgs ? "引数は使いません(ダイアログを出せないので、前の選び方で用意します)。" : "";
    if (session.capabilities?.ui?.elicitation) {
      const st = await runText([ctxf, "setup", "--status"], env);
      if (st.error) {
        await tell(`context-foundation: ${cannotStart(st)}`, "error");
        return;
      }
      status = setupStatus(st.out);
      const lead = (hasArgs ? "引数は使いません。次の質問に答えてください。\n" : "") +
        (status ? "" : `今の状態を読めませんでした(${st.out.trim().split(/\r?\n/).pop() || `終了コード ${st.code}`})。` +
          "前の選び方を初めの値にできません。\n");
      running = "用意(setup)の質問への答え";
      try {
        plan = await askSetupPlan(status, home, lead);
        asked = true;
        ignored = "";
      } catch (err) {
        // このホストが選ぶダイアログを描けないなど。尋ねずに、前の選び方で用意する
        await say(`context-foundation: ダイアログを出せませんでした(${err?.message ?? err})。尋ねずに、前の選び方で用意します`);
      }
      if (asked && !plan) {
        await tell("context-foundation: 用意をやめました(何も入れていません)");
        return;
      }
      running = "用意(setup)";
    }
    const t0 = Date.now();
    await tell(`context-foundation: ${ignored}${setupStartText(plan, status, home)}${carryOn}ログ: ${log}`);
    const stop = watchProgress(log, t0, parseSetupProgress, setupProgressNote);
    const r = await run([ctxf, "setup", ...setupArgs(plan)], log, env).finally(stop);
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
    const ws = config && workspaceOf(config);
    // 尋ねる形で用意して、この作業フォルダに索引がまだ無ければ、終わりに、続けて索引を作るかを尋ねる
    const offer = asked && !(config && existsSync(indexFile(config)));
    const later = "索引は、/context-index を打つと作れます(検索するフォルダを選ぶダイアログが出ます)。できたら、新しいセッションを始めます";
    const next = config && existsSync(indexFile(config))
      ? `この作業フォルダには索引があります(${ws})。検索の道具は、新しいセッションから出ます(動いているセッションには出ません)`
      : offer ? "" : later;
    // 埋め込みモデル(PyPI の外の OSS)は、利用者の環境で確かめてから入れるものなので、取得しない。
    // 始めの質問で入れられたフォルダを、model use で確かめて覚える(使えなければ、わけを添えて尋ね直す)。
    // 尋ねられなかったとき(ダイアログを出せないホスト)は、無いことと入れ方を知らせる
    const missing = modelMissing(out);
    const choice = plan?.model;
    let picked = null;
    // 入れたフォルダを使えない作業フォルダか(設定の model_dir が置き場を決めている・埋め込みを切っている)。
    // 尋ねる前に仮想環境が無く、設定を読めていなかったときは、用意の後に読み直して確かめる
    let notUsed = "";
    if (choice?.kind === "use") {
      const after = status?.model ? status : setupStatus((await runText([ctxf, "setup", "--status"], env)).out);
      notUsed = modelNotUsedReason(after?.model) || (missing && !missing.ask ? modelNotUsedReason({ own_folder: true, folder: "" }) : "");
    }
    if (choice?.kind === "use" && !notUsed) {
      running = "埋め込みモデルのフォルダの確かめ";
      picked = await askModelFolder(ctxf, env, missing, choice.folder);
    } else if (choice?.kind === "use") {
      picked = { state: "skipped", why: `入れたフォルダ ${choice.folder} は使いませんでした。${notUsed}` };
    } else if (!asked && missing?.ask) {
      running = "埋め込みモデルのフォルダの指定";
      picked = await askModelFolder(ctxf, env, missing);
    }
    const hasIndex = Boolean(config && existsSync(indexFile(config)));
    const saved = picked?.state === "saved"
      ? `埋め込みモデルのフォルダを覚えました(${picked.folder}。この PC のすべての作業フォルダで使います)。` +
        picked.notes.map((n) => `注意: ${n}\n`).join("") +
        (hasIndex ? REINDEX_AFTER_MODEL : "")
      : "";
    const modelNote = saved || (!missing ? (picked?.why ? `モデルのフォルダは替えていません(${picked.why})。` : "")
      : (picked?.why ? `モデルのフォルダは覚えていません(${picked.why})。` : "") +
        `埋め込みモデル(${missing.name})はインストールされていません(Hugging Face の OSS なので、setup では入れません)。` +
        "意味での検索には要ります。" +
        (!missing.ask ? "設定の model_dir のフォルダにモデルを置いてください。\n"
          : "この PC に置いてあれば、もう一度 /context-setup を打ってフォルダを入れるか、ターミナルで次を実行します: " +
            `python -I "${ctxf}" model use "<フォルダ>"\n` +
            (missing.command ? `取得するときは、入手先とライセンスを確かめ、この PC で使ってよければ、ターミナルで次を実行します: ${missing.command}\n` : "\n")) +
        "入れるまでは、検索は keyword だけです。\n");
    await tell(`context-foundation: 用意ができました(${sec} 秒、置き場: ${setupRuntimeFolder(out) || home})。` +
      modelNote + next);
    if (offer) {
      running = "索引を作るかの質問への答え";
      const go = await askYesNo(`context-foundation: 続けて、この作業フォルダ(${ws || cwd})の索引を作りますか。` +
        (config ? "" : "検索するフォルダは、次の質問で選びます。") + "後でするなら、/context-index を打つと作れます",
      AFTER_SETUP_YES, AFTER_SETUP_NO).catch(() => null);
      if (go) await indexFlow("");
      else await tell(`context-foundation: ${later}`);
    }
  } finally {
    running = null;
  }
}
const commands = [
  {
    name: "context-setup",
    description: "この PC の検索の用意(context-foundation)。GPU か CPU か、手元の埋め込みモデルのフォルダを順に尋ねてから、Python のパッケージ約 200 MB を入れる(埋め込みモデルは取得しない)。初めに 1 回(モデルは呼ばない)",
    handler: handle(setupCommand),
  },
  {
    name: "context-index",
    description: "コードと設計文書の検索の索引(context-foundation)を作る・更新する。引数は取らず、検索するフォルダや、更新・作り直し・フォルダの変更をダイアログで尋ねる(モデルは呼ばない)",
    handler: handle(indexCommand),
  },
];
const session = await joinSession(joinOptions(commands));

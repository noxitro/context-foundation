// /context-index の、Copilot の SDK に依らない部分(テストできるように分けた。lib.test.mjs を
// tests/test_plugin.py が node --test で流す)。出し方(makeNotifier など)は cxxref の拡張と同じ作り。
import { existsSync, realpathSync, statSync } from "node:fs";
import { dirname, join, resolve, win32 } from "node:path";

export const USAGE = "使い方: /context-index [full] [<フォルダ> ...]";

// プラグインのルート(plugin.json と ctxf.py のあるフォルダ)。start から親へさかのぼる
export function pluginRoot(start) {
  let d = start;
  for (let i = 0; i < 6; i++) {
    if (existsSync(join(d, "plugin.json")) && existsSync(join(d, "ctxf.py"))) return d;
    d = dirname(d);
  }
  throw new Error("context-foundation のプラグインのフォルダが見つかりません");
}

// セッションの作業フォルダ(最後の session.start / session.context_changed の cwd)
export function lastCwd(events) {
  let cwd = "";
  for (const e of events) {
    const c = e.data?.context?.cwd ?? (e.type === "session.context_changed" ? e.data?.cwd : undefined);
    if (typeof c === "string" && c) cwd = c;
  }
  return cwd;
}

// 作業フォルダから親へさかのぼって、設定(.cf\config.toml)のあるフォルダ。無ければ ""(ctxf.py の find_config と同じ探し方)
export function findWorkspace(start) {
  for (let d = resolve(start); ; d = dirname(d)) {
    if (existsSync(join(d, ".cf", "config.toml"))) return d;
    if (dirname(d) === d) return "";
  }
}

// 引数を空白で分ける。"…" で囲んだところは 1 つにする(空白を含むフォルダ)
export function splitArgs(arg) {
  const out = [];
  for (const m of (arg ?? "").matchAll(/"([^"]*)"|(\S+)/g)) out.push(m[1] ?? m[2]);
  return out.filter((s) => s !== "");
}

// /context-index の引数。先頭が full なら、前の索引の埋め込みを使い回さずに作り直す(残りはフォルダ)。
// full という名前のフォルダは ./full と書く
export function parseIndexArgs(arg) {
  const words = splitArgs(arg);
  const full = words.length > 0 && words[0].toLowerCase() === "full";
  return { full, folders: full ? words.slice(1) : words };
}

// 何をするか。{ action: "index", workspace, full } か { action: "init", workspace, folders, full } か { error }。
// - 作業フォルダか親に設定があれば、その索引を作り直す(フォルダを並べたら断る。設定を黙って書き換えない)
// - 無ければ、並べたフォルダで作業フォルダに設定を作ってから索引を作る。並べていなければ、使い方を出す
export function planIndex(arg, cwd) {
  const { full, folders } = parseIndexArgs(arg);
  const ws = findWorkspace(cwd);
  if (ws) {
    if (folders.length) {
      return {
        error: `設定がすでにあります(${join(ws, ".cf", "config.toml")})。フォルダを並べずに /context-index と打つと、` +
          "その設定で索引を作り直します。検索するフォルダを変えるなら、設定ファイルの [[source]] を直してください",
      };
    }
    return { action: "index", workspace: ws, full };
  }
  if (!folders.length) {
    return {
      error: `まだ索引がありません(${resolve(cwd)} とその親に .cf が無い)。検索したいフォルダ(エンジンのソース、` +
        "参考にするアプリ、設計文書など)を並べて打ってください。相対パスは作業フォルダから数えます。\n" +
        `${USAGE}(例: /context-index Engine Docs)`,
    };
  }
  const missing = folders.map((f) => resolve(cwd, f)).filter((p) => !isDir(p));
  if (missing.length) return { error: `フォルダがありません: ${missing.join(", ")}` };
  return { action: "init", workspace: resolve(cwd), folders, full };
}

function isDir(p) {
  try {
    return statSync(p).isDirectory();
  } catch {
    return false;
  }
}

// ctxf.py が「この PC の用意ができていない」ときに出す文(英語。エージェント向け)から、直すコマンドを取り出す。無ければ ""
export function repairCommand(out) {
  const m = (out ?? "").match(/then run: (.+)$/m) ?? (out ?? "").match(/ask the user to run (.+?); after that/);
  return m ? m[1].trim() : "";
}

// ログの最後の、空でない n 行(失敗したときに添える)
export function logTail(text, n = 4) {
  return (text ?? "").split(/\r?\n/).map((s) => s.trimEnd()).filter(Boolean).slice(-n).join("\n");
}

// ctxf.py init の出力から、作った source の一覧("id: ルート" の行)を返す
export function initSources(out) {
  return [...(out ?? "").matchAll(/^\s+source (\S+): (.+)$/gm)].map((m) => `${m[1]}: ${m[2].trim()}`);
}

// できた索引が keyword だけか(埋め込みモデルが無かった)。ctxf.py index のログから見る
export const keywordOnly = (out) => /keyword\(BM25\)だけの索引を作ります|今回は keyword だけの索引になります/.test(out ?? "");

// ctxf.py index の進み具合(標準エラーに出す行)から、今の段階を返す。読めなければ { phase: "start" }
//   "ingest"   ファイルを取り込んでいる({ files } 付き)
//   "ingested" 取り込みと分割が終わった({ files, passages } 付き)
//   "embed"    埋め込みを計算している({ done, total, eta } 付き。eta は残りの秒数、分からなければ null)
//   "embedded" 埋め込みが終わり、索引を書いている
//   "done"     索引ができた
export function parseProgress(text) {
  const num = (s) => Number(String(s).replace(/,/g, ""));
  let snap = { phase: "start" };
  for (const line of (text ?? "").split(/\r?\n/)) {
    let m;
    if ((m = line.match(/取り込み中: ([\d,]+) ファイル/))) snap = { phase: "ingest", files: num(m[1]) };
    else if ((m = line.match(/^取り込みと分割: ([\d,]+) ファイル \/ [\d,]+ 行 \/ ([\d,]+) passage/))) {
      snap = { phase: "ingested", files: num(m[1]), passages: num(m[2]) };
    } else if ((m = line.match(/埋め込み中: ([\d,]+) \/ ([\d,]+) passage(?:.*残り 約 (\d+) 秒)?/))) {
      snap = { phase: "embed", done: num(m[1]), total: num(m[2]), eta: m[3] === undefined ? null : Number(m[3]) };
    } else if (/^埋め込み: /.test(line)) snap = { phase: "embedded" };
    else if (/^索引ができました/.test(line)) snap = { phase: "done" };
  }
  return snap;
}

const minutes = (sec) => (sec < 60 ? `${sec} 秒` : `${Math.round(sec / 60)} 分`);

// 進み具合を画面に出すか決める。prev は前に出したときの { phase, tenth, at }(初めは {})。
// 出すのは、段階が変わったとき・埋め込みが 1 割進んだとき(前に出してから minGap ms 以上あける)・
// ほかの段階が 1 分以上続いているとき(1 分ごと)。{ note(出さなければ ""), state(次の prev) }
export function progressNote(prev, snap, now, elapsedSec, minGap = 10000) {
  const quiet = { note: "", state: prev };
  const changed = prev.phase !== snap.phase;
  if (snap.phase === "done") return quiet;  // できたことは、終わりの知らせで出す
  if (snap.phase === "embed" && snap.total > 0) {
    const tenth = Math.floor((snap.done * 10) / snap.total);
    if (!changed && (tenth <= prev.tenth || now - prev.at < minGap)) return quiet;
    if (snap.done >= snap.total) return quiet;  // 終わったことは "embedded" で出す
    const pct = Math.floor((snap.done * 100) / snap.total);
    const eta = snap.eta === null ? "" : `、残り 約 ${minutes(snap.eta)}`;
    const note = `埋め込みを計算しています ${snap.done.toLocaleString("en-US")}/${snap.total.toLocaleString("en-US")} passage(${pct}%${eta}、${elapsedSec} 秒)`;
    return { note: `context-foundation: ${note}`, state: { phase: snap.phase, tenth, at: now } };
  }
  if (!changed && now - prev.at < 60000) return quiet;
  const sec = `(${elapsedSec} 秒)`;
  const n = (v) => (v ?? 0).toLocaleString("en-US");
  const note = {
    start: "準備しています(埋め込みモデルの読み込みなど)" + sec,
    ingest: `ファイルを取り込んでいます(${n(snap.files)} ファイル、${elapsedSec} 秒)`,
    ingested: `取り込みと分割が終わりました(${n(snap.files)} ファイル、${n(snap.passages)} passage、${elapsedSec} 秒)`,
    embedded: "埋め込みが終わりました。索引を書いています" + sec,
  }[snap.phase];
  if (note === undefined) return quiet;
  if (snap.phase === "start" && changed) return { note: "", state: { phase: "start", at: now } };  // 始めてすぐは出さない
  return { note: `context-foundation: ${note}`, state: { phase: snap.phase, at: now } };
}

// index が 0 以外で終わったときの文(終了コードは readme-ja.md の「コマンド」・docs/copilot.md の「索引を更新する」)。
// out はログの中身、log はログのパス、setup は用意のやり直し方(既定のコマンド)
export function failureMessage(code, out, log, setup) {
  const tail = logTail(out);
  const more = tail ? `\n${tail}` : "";
  switch (code) {
    case 1:
      return "索引を置き換えられませんでした(この索引で検索しているプログラムが開いている)。できあがった索引は .cf\\index.db.new に" +
        `残したので、少し待ってからもう一度 /context-index を打つと、そこから使い回します。ログ: ${log}${more}`;
    case 2:
      return `索引を作れませんでした(設定の誤り・フォルダが無い・入るファイルが無い・モデルを読めないなど)。前の索引はそのまま使えます。ログ: ${log}${more}`;
    case 4: {
      const cmd = repairCommand(out) || setup;
      return "この PC の用意(setup)が済んでいないか、古くなっているので、索引を作りませんでした。" +
        "ターミナルで次を実行してから、もう一度 /context-index を打ってください" +
        `(PyPI から約 200 MB のパッケージ、Hugging Face から 490 MB のモデルを取得し、数分かかります):\n${cmd}`;
    }
    case 130:
      return `索引づくりが止められました。次の /context-index は、計算済みの埋め込みを使い回して続きから計算します。ログ: ${log}`;
    default:
      return `索引づくりに失敗しました(終了コード ${code})。ログ: ${log}${more}`;
  }
}

// ふつうの(level の無い)session.log は Markdown として表示され、\ の後ろが記号だと \ が消える
// (E:\src\Proj\.cf が E:\src\Proj.cf と出る)。その \ を二重にする。error の表示はそのまま出るので通さない
export const escapeMarkdown = (s) => s.replace(/\\(?=[!-/:-@[-`{-~])/g, "\\\\");

// パスを比べられる形にする(\\?\ を外す・/ を \ に・末尾の \ を外す・小文字に)。在る場所なら実体に揃える。空なら ""
export function pathKey(p) {
  return realOrSelf(p).replace(/\//g, "\\").replace(/\\+$/, "").toLowerCase();
}

function realOrSelf(p) {
  const plain = (s) => s.replace(/^\\\\\?\\UNC\\/i, "\\\\").replace(/^\\\\\?\\/, "");
  const s = plain(String(p ?? ""));
  if (!s) return "";
  try { return plain(realpathSync.native(s)); } catch { return s; }  // 無い場所は文字のまま
}

// CLI の配布物のフォルダか(npm で入れたもの・%LOCALAPPDATA%\copilot\pkg のもの、どちらにも npm-loader.js がある)
const looksLikeCliDist = (dir) => existsSync(join(dir, "npm-loader.js"));

// 拡張を動かしているのが Copilot CLI(画面に session.log を出す)か、GitHub Copilot app(info と warning を出さず、
// error だけを出す)か。違うのは拡張に渡す SDK の場所(COPILOT_SDK_PATH)で、CLI は自分の配布物の中のものを渡す。
// SDK の場所が分からなければアプリとみなす(アプリを CLI と取り違えると何も出ない。逆ならダイアログが余分に出るだけ)
export function runsUnderCli(env, isCliDist = looksLikeCliDist) {
  const sdk = pathKey(env.COPILOT_SDK_PATH);
  if (!sdk) return false;
  const dists = [env.COPILOT_CLI_RESOLVED_DIST_DIR, env.COPILOT_CLI_DIST_DIR].map(pathKey).filter(Boolean);
  return dists.some((d) => sdk === d || sdk.startsWith(`${d}\\`)) || isCliDist(win32.dirname(realOrSelf(env.COPILOT_SDK_PATH)));
}

// 結果をダイアログにも出すか。CF_DISPLAY が timeline なら出さない、dialog なら出す、
// それ以外(auto)はアプリで動いているときだけ出す
export function wantDialog(env, app = !runsUnderCli(env)) {
  const mode = env.CF_DISPLAY;
  if (mode === "timeline") return false;
  if (mode === "dialog") return true;
  return app;
}

// 利用者に見せる文の出し方。getSession は拡張のセッション(joinSession の戻り値)を返す関数、env は process.env。
// { tell(文, level), say(文) } を返す。
// - タイムライン(session.log): level の無い文は Markdown として表示されるので escapeMarkdown を通す。error はそのまま出る
// - ダイアログ: wantDialog のとき、tell の文のうち error でないものをダイアログにも出す(error はアプリでも表示される)。
//   say(進み具合)はダイアログにしない。ダイアログは 1 つずつ出す。待たないので、コマンドは先へ進む
// - アプリでダイアログを出せないときは、アプリでも表示される error で出し直す
export function makeNotifier(getSession, env) {
  const app = !runsUnderCli(env);
  const useDialog = wantDialog(env, app);
  let queue = Promise.resolve();
  const log = (message, level) =>
    getSession().log(level ? message : escapeMarkdown(message), { ephemeral: true, ...(level ? { level } : {}) });
  const show = async (message) => {
    const session = getSession();
    let why = "このホストはダイアログを出せません";
    if (session.capabilities?.ui?.elicitation) {
      try {
        await session.ui.elicitation({ message: app ? escapeMarkdown(message) : message, requestedSchema: { type: "object", properties: {} } });
        return;
      } catch (err) {
        why = `ダイアログを出せませんでした: ${err?.message ?? err}`;
      }
    }
    if (app) await log(`${message}\n(${why})`, "error");
  };
  return {
    say: (message) => log(message),
    tell: (message, level) => {
      if (useDialog && level !== "error") queue = queue.then(() => show(message)).catch(() => { });
      return log(message, level);
    },
  };
}

// /context-index と /context-setup の、Copilot の SDK に依らない部分(テストできるように分けた。lib.test.mjs を
// tests/test_plugin.py が node --test で流す)。出し方(makeNotifier など)は cxxref の拡張と同じ作り。
import { existsSync, readdirSync, readFileSync, realpathSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, join, resolve, win32 } from "node:path";

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

// 設定ファイル(config.toml)のパス。無ければ ""。ctxf.py の find_config と同じ探し方で、MCP サーバーや検索と
// 同じ設定を選ぶ: 環境変数 CF_CONFIG があればそれ(ファイルか、config.toml か .cf\config.toml を持つフォルダ。
// 指す先に無ければ例外)、無ければ作業フォルダから親へさかのぼって、最初の .cf\config.toml
export function findConfig(start, env = {}) {
  const explicit = env.CF_CONFIG;
  if (explicit) {
    const p = resolve(explicit.replace(/^~(?=$|[\\/])/, () => homedir()));
    if (isFile(p)) return p;
    if (isDir(p)) {
      for (const cand of [join(p, "config.toml"), join(p, ".cf", "config.toml")]) {
        if (isFile(cand)) return cand;
      }
    }
    throw new Error(`環境変数 CF_CONFIG の指す設定ファイルが見つかりません: ${explicit}`);
  }
  for (let d = resolve(start); ; d = dirname(d)) {
    const cand = join(d, ".cf", "config.toml");
    if (isFile(cand)) return cand;
    if (dirname(d) === d) return "";
  }
}

// 設定ファイルから見た作業フォルダ(.cf の親。.cf の中に無い設定なら、設定のあるフォルダ。config.py と同じ)
export function workspaceOf(config) {
  const dir = dirname(config);
  return basename(dir) === ".cf" ? dirname(dir) : dir;
}

// 設定の [index] path から、索引のファイルのパス(設定のあるフォルダからの相対。既定は index.db)。
// text は設定ファイルの中身(省けば読む)。TOML を全部は読まず、[index] の表の path = "…" か '…' の行だけを見る。
// 読めなければ既定の名前(誤りは ctxf.py が設定を読むときに出す)
export function indexFile(config, text) {
  if (text === undefined) {
    try {
      text = readFileSync(config, "utf8");
    } catch {
      text = "";
    }
  }
  let table = "";
  let name = "";
  for (const line of text.split(/\r?\n/)) {
    const head = /^\s*\[\s*([^\[\]]+?)\s*\]\s*(#.*)?$/.exec(line);
    if (head) {
      table = head[1];
      continue;
    }
    if (table !== "index") continue;
    const m = /^\s*path\s*=\s*(?:"((?:[^"\\]|\\.)*)"|'([^']*)')\s*(#.*)?$/.exec(line);
    if (m) name = m[1] !== undefined ? m[1].replace(/\\(["\\])/g, "$1") : m[2];
  }
  return join(dirname(config), name || "index.db");
}

function isFile(p) {
  try {
    return statSync(p).isFile();
  } catch {
    return false;
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

// 何をするか。{ action: "index", workspace, config, full } か { action: "init", workspace, folders, full } か
// { action: "choose", workspace, full } か { error }。config は設定ファイルのパス(findConfig。env は環境変数)。
// - 設定があれば(CF_CONFIG か、作業フォルダか親)、その索引を作り直す(フォルダを並べたら断る。設定を黙って書き換えない)
// - 無ければ、並べたフォルダで作業フォルダに設定を作ってから索引を作る。並べていなければ、フォルダを選ばせる(choose)
export function planIndex(arg, cwd, env = {}) {
  const { full, folders } = parseIndexArgs(arg);
  let config;
  try {
    config = findConfig(cwd, env);
  } catch (err) {
    return { error: `${err.message}(CF_CONFIG を外すか、指す先を直してください)` };
  }
  if (config) {
    if (folders.length) {
      return {
        error: `設定がすでにあります(${config})。フォルダを並べずに /context-index と打つと、` +
          "その設定で索引を作り直します。検索するフォルダを変えるなら、設定ファイルの [[source]] を直してください",
      };
    }
    return { action: "index", workspace: workspaceOf(config), config, full };
  }
  if (!folders.length) return { action: "choose", workspace: resolve(cwd), full };
  const missing = folders.map((f) => resolve(cwd, f)).filter((p) => !isDir(p));
  if (missing.length) return { error: `フォルダがありません: ${missing.join(", ")}` };
  return { action: "init", workspace: resolve(cwd), folders, full };
}

// ---- 初めての /context-index で、索引に入れるフォルダを選ばせる

// 選ぶ候補から外すフォルダ。ingest.py の DEFAULT_EXCLUDED_DIRS と同じ名前(どこにあっても索引に入らない。
// そろっていることは tests/test_plugin.py で確かめる)。名前が . で始まるものも外す
export const SKIP_DIRS = new Set([
  ".git", ".svn", ".hg", ".p4", ".vs", ".vscode", ".idea", ".cf", ".index", ".cache", ".venv", "venv",
  "__pycache__", "node_modules", "build", "out", "bin", "obj", "dist", "target", "intermediate", "binaries",
  "deriveddatacache", "saved", ".gradle", "packages",
]);
export const MAX_FOLDER_CHOICES = 40;
// 選択肢の「作業フォルダ全体」(直下のファイルも入る)。選ぶと、ほかの選択は使わない
export const WHOLE_WORKSPACE = ". (作業フォルダ全体。直下のファイルも入る)";

// 作業フォルダの直下の、選ぶ候補のフォルダ。{ names, omitted }。names は名前の順で、多すぎれば先頭の
// MAX_FOLDER_CHOICES 個(omitted は、出せなかった数)。フォルダへのリンク・ジャンクションも候補にする
// (ingest.py は、リンクそのものを source の root にできる)。読めなければ { names: [], omitted: 0 }
export function folderChoices(ws) {
  let entries;
  try {
    entries = readdirSync(ws, { withFileTypes: true });
  } catch {
    return { names: [], omitted: 0 };
  }
  const all = entries
    .filter((e) => !e.name.startsWith(".") && !SKIP_DIRS.has(e.name.toLowerCase()))
    .filter((e) => e.isDirectory() || (e.isSymbolicLink() && isDir(join(ws, e.name))))
    .map((e) => e.name)
    .sort((a, b) => a.localeCompare(b));
  return { names: all.slice(0, MAX_FOLDER_CHOICES), omitted: Math.max(0, all.length - MAX_FOLDER_CHOICES) };
}

// フォルダを選ばせるダイアログの形(複数選べる)。初めは、候補のフォルダをすべて選んである。
// 出せなかったフォルダがあれば(omitted)、そう書く(Other に書き込めば足せる)
export function folderSchema(names, omitted = 0) {
  const more = omitted
    ? `。名前の順で ${names.length} 個まで出しています(ほかに ${omitted} 個。入れるなら Other に名前を書いてください)`
    : "";
  return {
    type: "object",
    properties: {
      folders: {
        type: "array",
        title: "検索するフォルダ",
        description: `エンジンのソース・参考にするアプリ・設計文書など。後から .cf\\config.toml の [[source]] で直せます${more}`,
        minItems: 1,
        items: { type: "string", enum: [...names, WHOLE_WORKSPACE] },
        default: [...names],
      },
    },
    required: ["folders"],
  };
}

// ダイアログの答え(content)から、init に渡すフォルダ。作業フォルダ全体を選んでいれば ["."]。
// Copilot CLI は選択肢に「Other」(書き込む欄)を足すので、候補に無い答えは、書き込まれたパス(作業フォルダの外も可)
// として後ろに足す(前後の空白・囲みの引用符・末尾の区切りを外す。無いフォルダなら init が断る)。
// 書き込まれたものが候補の名前と同じなら(大文字小文字は見ない。Windows では同じフォルダ)、候補を選んだのと同じ
// に扱い、同じフォルダを 2 度渡さない
export function pickedFolders(content, names) {
  const v = content?.folders;
  const got = (Array.isArray(v) ? v : typeof v === "string" ? [v] : []).filter((s) => typeof s === "string");
  if (got.includes(WHOLE_WORKSPACE)) return ["."];
  const clean = got.map((s) => s.trim().replace(/^"(.*)"$/, "$1").trim().replace(/(?<=[^:\\/])[\\/]+$/, "")).filter(Boolean);
  const key = (s) => s.toLowerCase();
  const chosen = new Set(clean.map(key));
  const picked = names.filter((n) => chosen.has(key(n)));
  const seen = new Set(picked.map(key));
  for (const s of clean) {
    if (!seen.has(key(s))) {
      seen.add(key(s));
      picked.push(s);
    }
  }
  return picked;
}

// ダイアログを出せないときの文(並べて打つ例に、候補のフォルダを使う)
export function chooseFallback(ws, names) {
  const example = names.length ? names.slice(0, 3).map((n) => (/\s/.test(n) ? `"${n}"` : n)).join(" ") : ".";
  return `まだ索引がありません(${ws} とその親に .cf が無い)。検索したいフォルダ(エンジンのソース、` +
    "参考にするアプリ、設計文書など)を並べて打ってください。相対パスは作業フォルダから数えます。\n" +
    `${USAGE}(例: /context-index ${example})`;
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
// ctxf.py setup のログ(model_report の行)から、埋め込みモデルがインストールされていないか。されていなければ
// { command, name, about, ask }。command は利用者が自分で取得するコマンド(フォルダが決まっているときは "")、
// name はモデルの名前、about は括弧の中(名前・ライセンス・大きさ・置き場)、ask は手元のフォルダを尋ねて
// `model use` で覚えてよいか(作業フォルダの設定の [embedding] model_dir が置き場を決めているときは、覚えても
// そちらが先に使われるので尋ねない)。されている・使わない・分からないときは null
export function modelMissing(out) {
  const text = out ?? "";
  const head = text.match(/^ {2}埋め込みモデル: インストールされていません\((.*)\)$/m);
  if (!head) return null;
  const m = text.match(/^ {4}使うには、モデルの入手先とライセンスを確かめてから、次を実行します: (.+)$/m);
  return {
    command: m ? m[1].trim() : "",
    name: head[1].split("、")[0].trim(),
    about: head[1],
    ask: !/\[embedding\] model_dir のフォルダ:/.test(head[1]),
  };
}

// ---- /context-setup の、手元に置いた埋め込みモデルのフォルダを尋ねるダイアログ(取得はしない)

// フォルダを書き込む欄の形。now は初めの値(今のモデルのフォルダ。無ければ空)
export function modelFolderSchema(now = "") {
  return {
    type: "object",
    properties: {
      folder: {
        type: "string",
        title: "モデルのフォルダ",
        description: "onnx\\model.onnx(か model.onnx)と tokenizer.json の入ったフォルダ。Hugging Face のキャッシュのフォルダ" +
          "(models--<org>--<name>)や、使う .onnx のファイルのパスでも構いません。" +
          (now ? "そのまま送っても、空にして送っても、今のモデルを使います" : "空のままで飛ばします"),
        ...(now ? { default: now } : {}),
      },
    },
  };
}

// ダイアログの文。missing は modelMissing の戻り値(モデルが入っていて、別のフォルダに替えるときは null)、
// retry は前の答えがだめだったわけ(初めは "")
export function modelFolderPrompt(missing, retry = "") {
  return "context-foundation: " + (retry ? `${retry}\n` : "") +
    (missing ? `埋め込みモデル(${missing.about})がありません。` : "使う埋め込みモデルのフォルダを、もう一度入れてください。") +
    "意味での検索(日本語や言葉での質問で、英語のコードを探すなど)に要ります。" +
    "この PC に置いてあれば、そのフォルダを入れてください。1 回読み込んで確かめてから、この PC のすべての作業フォルダで使うように" +
    "覚えます(写しも取得もしません)。" +
    (missing ? "無ければ空のまま送るか取り消すと、keyword だけの検索で続けます" : "空のまま送るか取り消すと、今のモデルのまま続けます");
}

// ダイアログの答えから、フォルダのパス(前後の空白と囲みの引用符を外す)。無ければ ""
export function pickedModelFolder(content) {
  const v = content?.folder;
  return typeof v === "string" ? v.trim().replace(/^"(.*)"$/, "$1").trim() : "";
}

// ctxf.py model use の結果(終了コードと出力)を読む。英語の result: の行と終了コードで決める。
// { state: "saved" | "warnings" | "error", notes(「注意:」の行), detail(だめだったわけ。ログの最後の行) }
export function modelUseResult(code, out) {
  const text = out ?? "";
  const notes = [...text.matchAll(/^\s*注意: (.+)$/gm)].map((m) => m[1].trim());
  const result = text.match(/^result: (.+)$/m)?.[1] ?? "";
  if (code === 0 && result.startsWith("saved")) return { state: "saved", notes, detail: "" };
  if (code === 4 && result.startsWith("not saved (warnings")) return { state: "warnings", notes, detail: "" };
  const detail = text.split(/\r?\n/).map((s) => s.trim())
    .filter((s) => s && !s.startsWith("result: ") && !s.startsWith("モデルを読み込んで確かめます")).slice(-3).join("\n");
  return { state: "error", notes, detail: detail || `終了コード ${code}` };
}

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
// out はログの中身、log はログのパス、setup は用意のやり直し方(既定のコマンド)、index は索引のファイル(indexFile)
export function failureMessage(code, out, log, setup, index = ".cf\\index.db") {
  const tail = logTail(out);
  const more = tail ? `\n${tail}` : "";
  switch (code) {
    case 1:
      return `索引を置き換えられませんでした(この索引で検索しているプログラムが開いている)。できあがった索引は ${index}.new に` +
        `残したので、少し待ってからもう一度 /context-index を打つと、そこから使い回します。ログ: ${log}${more}`;
    case 2:
      return `索引を作れませんでした(設定の誤り・フォルダが無い・入るファイルが無い・モデルを読めないなど)。前の索引はそのまま使えます。ログ: ${log}${more}`;
    case 4: {
      const cmd = repairCommand(out) || setup;
      // 隣の .venv(リポジトリを取得した人)を直すのは setup.bat などで、/context-setup では直らない
      const how = usesLocalVenv(cmd)
        ? "ターミナルで次を実行してから、もう一度 /context-index を打ってください"
        : "/context-setup を打つか、ターミナルで次を実行して用意をしてから、もう一度 /context-index を打ってください";
      return "この PC の用意(setup)が済んでいないか、古くなっているので、索引を作りませんでした。" + how +
        `(PyPI から約 200 MB のパッケージを取得し、数分かかります):\n${cmd}`;
    }
    case 130:
      return `索引づくりが止められました。次の /context-index は、計算済みの埋め込みを使い回して続きから計算します。ログ: ${log}`;
    default:
      return `索引づくりに失敗しました(終了コード ${code})。ログ: ${log}${more}`;
  }
}

// 直すコマンドが、隣の .venv(リポジトリを取得した人)向けか(ctxf.py の repair_command の形)
const usesLocalVenv = (cmd) => /setup\.bat|-m venv --clear \.venv/.test(cmd ?? "");

// ---- /context-setup(この PC の用意。引数は取らず、ダイアログで順に尋ねてから ctxf.py setup を動かす)

// 埋め込みの計算の選択肢(ダイアログの答えは、この文がそのまま返る)
export const GPU_CHOICE = "GPU(DirectML。DirectX 12 に対応した GPU)";
export const CPU_CHOICE = "CPU";

// ctxf.py setup --status の出力(1 行の JSON)を読む。読めなければ null
export function setupStatus(out) {
  const line = (out ?? "").trim().split(/\r?\n/).pop() ?? "";
  try {
    const v = JSON.parse(line);
    return v && typeof v === "object" && typeof v.ready === "boolean" ? v : null;
  } catch {
    return null;
  }
}

// モデルのフォルダを尋ねてよいか。作業フォルダの設定の [embedding] model_dir が置き場を決めているとき
// (覚えても、そちらが先に使われる)と、設定で埋め込みを切っているときは尋ねない
export const modelAskable = (m) => !m || (m.state !== "off" && !(m.own_folder && m.dir_source !== "user"));

// 入れたモデルのフォルダを使わなかったわけ(設定が置き場を決めている・埋め込みを切っている)。使えるなら ""
export function modelNotUsedReason(m) {
  if (modelAskable(m)) return "";
  return m.state === "off" ? "作業フォルダの設定で、埋め込みを切っているため"
    : `作業フォルダの設定の [embedding] model_dir${m.folder ? `(${m.folder})` : " "}が置き場を決めていて、そちらが先に使われるため`;
}

// 尋ねる段の並び: accel(GPU か CPU か。Windows だけ)、model(モデルのフォルダ)、confirm(まとめて確かめる)。
// 状態を読めなかったとき(null)は、GPU を選べるかを、この OS で決める
export function setupSteps(status, platform = process.platform) {
  const gpu = status ? status.gpu_supported : platform === "win32";
  return [...(gpu ? ["accel"] : []), ...(modelAskable(status?.model) ? ["model"] : []), "confirm"];
}

const stepHead = (n, total) => `context-foundation の用意(${n}/${total}): `;

// retry は、前の答えを読めなかったときに添える文(初めは "")
export function accelPrompt(n, total, status, retry = "") {
  return stepHead(n, total) + (retry ? `${retry}\n` : "") + "索引づくりで、埋め込み(意味での検索に使う数値)をどこで計算するかを選んでください。" +
    "GPU を選ぶと、onnxruntime の代わりに onnxruntime-directml を入れます。DirectML を使えない PC では、入れても CPU で" +
    "計算します。" + (status?.ready ? `今は ${status.gpu ? "GPU" : "CPU"} です(変えると、onnxruntime を入れ直します)。` : "") +
    "取り消すと、用意をやめます";
}

export function accelSchema(status) {
  return {
    type: "object",
    properties: {
      accel: {
        type: "string",
        title: "埋め込みの計算",
        enum: [GPU_CHOICE, CPU_CHOICE],
        default: status?.gpu ? GPU_CHOICE : CPU_CHOICE,
      },
    },
    required: ["accel"],
  };
}

// ダイアログの答えから、GPU を使うか。選択肢に無い答え(Other に書き込まれたもの)は、GPU・CPU の言葉で読む。
// 読めなければ null(呼ぶ側が、尋ね直す)
export function pickedAccel(content) {
  const v = typeof content?.accel === "string" ? content.accel.trim() : "";
  if (v === GPU_CHOICE) return true;
  if (v === CPU_CHOICE) return false;
  if (/gpu|directml/i.test(v)) return true;
  if (/cpu/i.test(v)) return false;
  return null;
}

// 今のモデルのフォルダ(入っているときだけ)。モデルのフォルダの欄の初めの値にする
export const currentModelFolder = (status) => (status?.model?.state === "installed" ? status.model.folder ?? "" : "");

export function modelStepPrompt(n, total, status) {
  const m = status?.model;
  if (m?.state === "installed") {
    return stepHead(n, total) + `埋め込みモデル(${m.name})は入っています(${m.folder})。このまま使うなら、そのまま送って` +
      "ください。別のフォルダのモデルを使うなら、そのフォルダを入れてください(用意のあとで 1 回読み込んで確かめてから" +
      "覚えます)。欄を空にして送っても、今のモデルのままです(モデルを外すときは、ターミナルで ctxf.py model forget を" +
      "実行します)。取り消すと、用意をやめます";
  }
  // 仮想環境が無いうちは、モデルの状態を読めない(model が無い)。そのときは、既定のモデルの名前を出す
  const about = m?.name ? `${m.name}、ライセンス ${m.license || "不明"}${m.size_mb ? `、約 ${m.size_mb} MB` : ""}`
    : status?.default_model ? `既定は ${status.default_model}。入手先 https://huggingface.co/${status.default_model}` : "";
  return stepHead(n, total) + `埋め込みモデル${about ? `(${about})` : ""}のフォルダを入れてください。意味での検索` +
    "(日本語や言葉での質問で、英語のコードを探すなど)に要ります。この PC に置いてあれば、そのフォルダを入れると、" +
    "用意のあとで 1 回読み込んで確かめてから、この PC のすべての作業フォルダで使うように覚えます(写しも取得も" +
    "しません)。無ければ空のまま送ると、keyword だけの検索で続けます。取り消すと、用意をやめます";
}

// モデルの段の答え: { kind: "keep" }(今のまま)、{ kind: "none" }(入れない)、{ kind: "use", folder }
export function modelChoice(folder, status) {
  const now = currentModelFolder(status);
  if (now && (!folder || pathKey(folder) === pathKey(now))) return { kind: "keep" };
  return folder ? { kind: "use", folder } : { kind: "none" };
}

// はい・いいえを、日本語の選択肢で尋ねる形(session.ui.confirm は、ホストが英語の Yes・No を出す)。
// 初めは yes を選んである
export function choiceSchema(yes, no) {
  return {
    type: "object",
    properties: { answer: { type: "string", title: "答え", enum: [yes, no], default: yes } },
    required: ["answer"],
  };
}

// 答えが yes か。選択肢に無い答え(Other に書き込まれたもの)は、yes とみなさない
export const pickedYes = (content, yes) => typeof content?.answer === "string" && content.answer.trim() === yes;

export const SETUP_YES = "はい、この内容で用意する";
export const SETUP_NO = "やめる";

// ctxf.py setup に渡す指定。gpu が null(尋ねていない)なら、前の setup の選び方を引き継ぐ
export const setupArgs = (plan) => (plan?.gpu === true ? ["--gpu"] : plan?.gpu === false ? ["--cpu"] : []);

// 用意で入れるもの。status は尋ねる前に読んだ状態(読めなかった・尋ねなかったときは null)、plan は答え(無ければ null)
export function setupWhat(plan, status) {
  const gpu = plan?.gpu ?? Boolean(status?.gpu);
  const changed = Boolean(status?.ready) && plan?.gpu != null && plan.gpu !== Boolean(status?.gpu);
  return !status ? "Python のパッケージ(初めは PyPI から約 200 MB。済んでいれば確かめるだけです)"
    : !status.ready ? "Python のパッケージ(PyPI から約 200 MB。初めは数分かかります)"
      : changed ? `onnxruntime を${gpu ? " GPU 用(onnxruntime-directml)" : " CPU 用"}に入れ直します(PyPI から取得します)`
        : "済んでいるので、確かめるだけです";
}

// 用意を始めたときの知らせ(確かめの段で出した「入れるもの」と同じことを言う)
export function setupStartText(plan, status, home) {
  return `用意(setup)を始めました。入れるもの: ${setupWhat(plan, status)}。置き場: ${home}(埋め込みモデルは取得しません)。`;
}

// 用意の後、モデルのフォルダを覚えたときに、今の索引について添える文。索引づくりは、索引の埋め込みが今のモデルと
// 違うとき(keyword だけのときも)だけ、すべて埋め込み直す(indexer の _previous_vectors)。full は要らない
export const REINDEX_AFTER_MODEL = "今の索引が keyword だけか、別のモデルの埋め込みなら、/context-index を打つと、このモデルで" +
  "埋め込み直します(同じモデルの索引なら、変わった分だけです)。\n";

// 最後に、まとめて確かめる文
export function setupConfirmText(n, total, plan, status, home) {
  const gpu = plan.gpu ?? Boolean(status?.gpu);
  const what = setupWhat(plan, status);
  const m = plan.model;
  const s = status?.model;
  const model = !m
    ? (s?.state === "off" ? "使いません(作業フォルダの設定で、埋め込みを切っています)"
      : s?.own_folder ? `作業フォルダの設定の model_dir のフォルダ(${s.folder})`
        : s?.state === "installed" ? `今のまま(${s.folder})` : "用意のあとで、有無を知らせます")
    : m.kind === "keep" ? `今のまま(${status?.model?.folder ?? ""})`
      : m.kind === "use" ? `${m.folder}(用意のあとで、読み込んで確かめてから覚えます)`
        : "使いません(keyword だけの検索。後から /context-setup で入れられます)";
  return stepHead(n, total) + "次の内容で用意します。よければ「" + SETUP_YES + "」を選んでください。\n" +
    `- 入れるもの: ${what}\n` +
    (home ? `- 置き場: ${home}\n` : "") +
    `- 埋め込みの計算: ${gpu ? "GPU(DirectML)" : "CPU"}\n` +
    `- 埋め込みモデル: ${model}\n` +
    "埋め込みモデルは取得しません。モデル(LLM)は呼びません";
}
// ctxf.py の data_home と同じ、利用者ごとの置き場(CF_HOME。無ければ %LOCALAPPDATA%\context-foundation など)
export function dataHome(env, platform = process.platform, home = homedir()) {
  if (env.CF_HOME) return env.CF_HOME;
  if (platform === "win32" && env.LOCALAPPDATA) return join(env.LOCALAPPDATA, "context-foundation");
  return join(env.XDG_DATA_HOME || join(home, ".local", "share"), "context-foundation");
}

// プラグインの隣に .venv があるか(リポジトリを取得して register-copilot.bat で登録した人)。入口はその .venv を
// 先に使うので、ctxf.py setup が利用者ごとの置き場に作っても使われない(直すのは setup.bat)
export const hasLocalVenv = (plugin) => existsSync(join(plugin, ".venv"));

// ctxf.py setup のログから、今の段階を返す。読めなければ { phase: "start" }
//   "venv"      仮想環境を作っている
//   "packages"  Python のパッケージを入れている
//   "packaged"  パッケージが入った(すでに入っていた)
//   "done"      用意ができた
export function parseSetupProgress(text) {
  let snap = { phase: "start" };
  for (const line of (text ?? "").split(/\r?\n/)) {
    if (/仮想環境を作(?:ります|り直します)/.test(line)) snap = { phase: "venv" };
    else if (/Python のパッケージを PyPI から入れます/.test(line)) snap = { phase: "packages" };
    else if (/^\s*Python のパッケージ: /.test(line)) snap = { phase: "packaged" };
    else if (/^用意ができました/.test(line)) snap = { phase: "done" };
  }
  return snap;
}

// ctxf.py setup のログの、利用者ごとの置き場("利用者ごとの置き場: …" の行)。無ければ ""
export const setupRuntimeFolder = (out) => ((out ?? "").match(/^\s*利用者ごとの置き場: (.+)$/m)?.[1] ?? "").trim();

// 用意の進み具合を画面に出すか決める(progressNote と同じ考え方)。出すのは、段階が変わったときと、
// 同じ段階が 1 分以上続いているとき(1 分ごと)。{ note(出さなければ ""), state(次の prev) }
export function setupProgressNote(prev, snap, now, elapsedSec) {
  const quiet = { note: "", state: prev };
  if (snap.phase === "done") return quiet;  // できたことは、終わりの知らせで出す
  const changed = prev.phase !== snap.phase;
  if (!changed && now - prev.at < 60000) return quiet;
  const state = { phase: snap.phase, at: now };
  if (snap.phase === "start" && changed) return { note: "", state };  // 始めてすぐは出さない
  // [何をしているか, 括弧に添えること]
  const what = {
    start: ["準備しています", ""],
    venv: ["仮想環境を作っています", ""],
    packages: ["Python のパッケージを PyPI から入れています", "初めては約 200 MB"],
    packaged: ["Python のパッケージが入りました", ""],
  }[snap.phase];
  if (what === undefined) return quiet;
  const [label, detail] = what;
  return { note: `context-foundation: ${label}(${detail ? `${detail}、` : ""}${elapsedSec} 秒)`, state };
}

// setup が 0 以外で終わったときの文(終了コードは ctxf.py の setup)。out はログの中身、log はログのパス
export function setupFailureMessage(code, out, log) {
  const tail = logTail(out, 6);
  const more = tail ? `\n${tail}` : "";
  switch (code) {
    case 2:
      return `用意を始められませんでした(この Python では用意できないか、指定が合わない)。ログ: ${log}${more}`;
    case 3:
      return `別の用意(setup)が動いています。終わってから、もう一度 /context-setup を打ってください。ログ: ${log}${more}`;
    default:
      return `用意に失敗しました(終了コード ${code})。わけはログの最後の行です。直してから、もう一度 /context-setup を打つと、` +
        `済んだところは飛ばして続けます。ログ: ${log}${more}`;
  }
}

// 拡張の SDK の名前。ホストが、この名前の import を自分の同梱の SDK に向ける。Copilot CLI 1.0.113 から
// @microsoft/copilot-sdk になった(1.0.112 まで と GitHub Copilot app のランタイム 1.0.106-8 は @github/copilot-sdk)。
// どちらか一方の名前しか向けないので、新しい名前から順に試す
export const SDK_NAMES = ["@microsoft/copilot-sdk/extension", "@github/copilot-sdk/extension"];

// SDK_NAMES の順に読み込み、最初に読めたものを返す。load は名前を受け取り import する関数(拡張の import(n))。
// その名前のパッケージが見つからないときだけ次を試す(SDK の中の誤りは、そのまま投げる)
export async function loadSdk(load, names = SDK_NAMES) {
  const missing = [];
  for (const name of names) {
    try {
      return await load(name);
    } catch (err) {
      const pkg = name.split("/").slice(0, 2).join("/");
      if (err?.code !== "ERR_MODULE_NOT_FOUND" || !String(err?.message ?? "").includes(pkg)) throw err;
      missing.push(name);
    }
  }
  throw new Error(`Copilot の拡張の SDK が見つかりません(${missing.join("、")})。Copilot CLI の新しい版で起動してください`);
}

// joinSession に渡す設定。スラッシュコマンドの置き場も SDK の版で違う: @microsoft/copilot-sdk は interaction.commands、
// @github/copilot-sdk は commands。どちらの SDK も、知らない方の置き場は読まないので、両方に同じものを置く
export const joinOptions = (commands) => ({ commands, interaction: { commands } });

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
//   say(進み具合)はダイアログにしない。ダイアログは 1 つずつ出す。待たないので、コマンドは先へ進む。
//   waits はダイアログを出すか、settled() は出したダイアログがすべて閉じるのを待つ(commandHandler が使う)
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
    // ダイアログを出す fn を、知らせのダイアログと同じ列に並べて動かし、その結果を返す(先に出したダイアログが
    // 閉じてから出し、出している間の tell のダイアログは、閉じてから出す。重ねないため)
    exclusive: (fn) => {
      const p = queue.then(fn);
      queue = p.catch(() => { });
      return p;
    },
    waits: useDialog,
    // 待っている間に足されたダイアログも待つ
    settled: async () => {
      for (let q = null; q !== queue;) {
        q = queue;
        await q;
      }
    },
  };
}

// スラッシュコマンドのハンドラー。fn(ctx) は仕事の全体(失敗は onError で知らせる)。
// GitHub Copilot app は、どのコマンドも待っていないダイアログが開くと、閉じた後も実行中の表示(スピナー)を続け、
// スラッシュコマンドを使えなくする(m365 の拡張で見つけたこと)。そのため、ダイアログを出すとき(notifier.waits)は、
// 仕事が終わり、出したダイアログがすべて閉じるまで、コマンドを終えない。
// ダイアログを出さないとき(Copilot CLI の既定)は、これまでどおり待たずに戻り、仕事は裏で続ける
export function commandHandler(fn, notifier, onError) {
  return async (ctx) => {
    const work = Promise.resolve().then(() => fn(ctx)).catch((err) => onError(err));
    if (!notifier.waits) return;
    await work.catch(() => { });
    await notifier.settled();
  };
}

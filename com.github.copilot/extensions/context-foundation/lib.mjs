// /context-index と /context-setup の、Copilot の SDK に依らない部分(テストできるように分けた。lib.test.mjs を
// tests/test_plugin.py が node --test で流す)。出し方(makeNotifier など)は cxxref の拡張と同じ作り。
import { existsSync, readdirSync, realpathSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve, win32 } from "node:path";

export const USAGE = "使い方: /context-index [full] [<フォルダ> ...]";
export const SETUP_USAGE = "使い方: /context-setup [no-model] [gpu | cpu]";

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

// 何をするか。{ action: "index", workspace, full } か { action: "init", workspace, folders, full } か
// { action: "choose", workspace, full } か { error }。
// - 作業フォルダか親に設定があれば、その索引を作り直す(フォルダを並べたら断る。設定を黙って書き換えない)
// - 無ければ、並べたフォルダで作業フォルダに設定を作ってから索引を作る。並べていなければ、フォルダを選ばせる(choose)
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
  if (!folders.length) return { action: "choose", workspace: resolve(cwd), full };
  const missing = folders.map((f) => resolve(cwd, f)).filter((p) => !isDir(p));
  if (missing.length) return { error: `フォルダがありません: ${missing.join(", ")}` };
  return { action: "init", workspace: resolve(cwd), folders, full };
}

// ---- 初めての /context-index で、索引に入れるフォルダを選ばせる

// 選ぶ候補から外すフォルダ。ingest.py の DEFAULT_EXCLUDED_DIRS と同じ名前(どこにあっても索引に入らない)。
// 名前が . で始まるものも外す
const SKIP_DIRS = new Set([
  ".git", ".svn", ".hg", ".p4", ".vs", ".vscode", ".idea", ".cf", ".index", ".cache", ".venv", "venv",
  "__pycache__", "node_modules", "build", "out", "bin", "obj", "dist", "target", "intermediate", "binaries",
  "deriveddatacache", "saved", ".gradle", "packages",
]);
export const MAX_FOLDER_CHOICES = 40;
// 選択肢の「作業フォルダ全体」(直下のファイルも入る)。選ぶと、ほかの選択は使わない
export const WHOLE_WORKSPACE = ". (作業フォルダ全体。直下のファイルも入る)";

// 作業フォルダの直下の、選ぶ候補のフォルダの名前(名前の順)。読めなければ []。多すぎれば先頭の MAX_FOLDER_CHOICES 個
export function folderChoices(ws) {
  let entries;
  try {
    entries = readdirSync(ws, { withFileTypes: true });
  } catch {
    return [];
  }
  return entries
    .filter((e) => e.isDirectory() && !e.name.startsWith(".") && !SKIP_DIRS.has(e.name.toLowerCase()))
    .map((e) => e.name)
    .sort((a, b) => a.localeCompare(b))
    .slice(0, MAX_FOLDER_CHOICES);
}

// フォルダを選ばせるダイアログの形(複数選べる)。初めは、候補のフォルダをすべて選んである
export function folderSchema(names) {
  return {
    type: "object",
    properties: {
      folders: {
        type: "array",
        title: "検索するフォルダ",
        description: "エンジンのソース・参考にするアプリ・設計文書など。後から .cf\\config.toml の [[source]] で直せます",
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
// として後ろに足す(囲みの引用符は外す。無いフォルダなら init が断る)
export function pickedFolders(content, names) {
  const v = content?.folders;
  const got = (Array.isArray(v) ? v : typeof v === "string" ? [v] : []).filter((s) => typeof s === "string");
  if (got.includes(WHOLE_WORKSPACE)) return ["."];
  const typed = got.filter((s) => !names.includes(s)).map((s) => s.trim().replace(/^"(.*)"$/, "$1")).filter(Boolean);
  return [...names.filter((n) => got.includes(n)), ...new Set(typed)];
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
      // 隣の .venv(リポジトリを取得した人)を直すのは setup.bat などで、/context-setup では直らない
      const how = usesLocalVenv(cmd)
        ? "ターミナルで次を実行してから、もう一度 /context-index を打ってください"
        : "/context-setup を打つか、ターミナルで次を実行して用意をしてから、もう一度 /context-index を打ってください";
      return "この PC の用意(setup)が済んでいないか、古くなっているので、索引を作りませんでした。" + how +
        `(PyPI から約 200 MB のパッケージ、Hugging Face から 490 MB のモデルを取得し、数分かかります):\n${cmd}`;
    }
    case 130:
      return `索引づくりが止められました。次の /context-index は、計算済みの埋め込みを使い回して続きから計算します。ログ: ${log}`;
    default:
      return `索引づくりに失敗しました(終了コード ${code})。ログ: ${log}${more}`;
  }
}

// 直すコマンドが、隣の .venv(リポジトリを取得した人)向けか(ctxf.py の repair_command の形)
const usesLocalVenv = (cmd) => /setup\.bat|-m venv --clear \.venv/.test(cmd ?? "");

// ---- /context-setup(この PC の用意。ctxf.py setup を裏で動かす)

export const SETUP_HELP = `${SETUP_USAGE}
検索に要る Python のパッケージ(PyPI から約 200 MB)と、埋め込みモデル(Hugging Face から約 490 MB)を、利用者ごとの置き場に入れます。` +
  `1 回だけで済みます(済んでいれば、確かめるだけで終わります)。裏で動かし、モデルは呼びません。
- no-model: モデルを取らない(検索は keyword だけになる)
- gpu: (Windows)索引づくりの埋め込みを GPU で計算する onnxruntime-directml を入れる。cpu で戻す。選び方は次の用意に引き継ぐ`;

const SETUP_WORDS = { "no-model": "--no-model", gpu: "--gpu", cpu: "--cpu" };

// /context-setup の引数。{ args: ctxf.py setup に渡す指定 } か { help: true } か { error }。
// no-model・gpu・cpu は、-- を付けても付けなくてもよい
export function parseSetupArgs(arg) {
  const words = splitArgs(arg).map((w) => w.toLowerCase().replace(/^--?/, ""));
  if (words.some((w) => w === "help" || w === "h")) return { help: true };
  const unknown = words.filter((w) => !Object.hasOwn(SETUP_WORDS, w));
  if (unknown.length) return { error: `知らない指定です: ${unknown.join(" ")}\n${SETUP_USAGE}` };
  const args = [...new Set(words.map((w) => SETUP_WORDS[w]))];
  if (args.includes("--gpu") && args.includes("--cpu")) return { error: `gpu と cpu は一緒に指定できません\n${SETUP_USAGE}` };
  return { args };
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
//   "model"     埋め込みモデルを確かめている
//   "download"  モデルのファイルを取得している({ file } 付き。ログがファイルなので、取得の % は書かれない)
//   "done"      用意ができた
export function parseSetupProgress(text) {
  let snap = { phase: "start" };
  for (const line of (text ?? "").split(/\r?\n/)) {
    let m;
    if (/creating the virtual environment/.test(line)) snap = { phase: "venv" };
    else if (/installing the Python packages/.test(line)) snap = { phase: "packages" };
    else if (/^\s*Python packages: /.test(line)) snap = { phase: "packaged" };
    else if (/embedding model: checking/.test(line)) snap = { phase: "model" };
    else if ((m = line.match(/^取得します: \S+ (\S+)/))) snap = { phase: "download", file: m[1] };
    else if (/^Setup is done/.test(line)) snap = { phase: "done" };
  }
  return snap;
}

// ctxf.py setup のログの、利用者ごとの置き場("runtime folder: …" の行)。無ければ ""
export const setupRuntimeFolder = (out) => ((out ?? "").match(/^\s*runtime folder: (.+)$/m)?.[1] ?? "").trim();

// 用意の進み具合を画面に出すか決める(progressNote と同じ考え方)。出すのは、段階か取得するファイルが変わったときと、
// 同じ段階が 1 分以上続いているとき(1 分ごと)。{ note(出さなければ ""), state(次の prev) }
export function setupProgressNote(prev, snap, now, elapsedSec) {
  const quiet = { note: "", state: prev };
  if (snap.phase === "done") return quiet;  // できたことは、終わりの知らせで出す
  const changed = prev.phase !== snap.phase || prev.file !== snap.file;
  if (!changed && now - prev.at < 60000) return quiet;
  const state = { phase: snap.phase, file: snap.file, at: now };
  if (snap.phase === "start" && changed) return { note: "", state };  // 始めてすぐは出さない
  // [何をしているか, 括弧に添えること]
  const what = {
    start: ["準備しています", ""],
    venv: ["仮想環境を作っています", ""],
    packages: ["Python のパッケージを PyPI から入れています", "初めては約 200 MB"],
    packaged: ["Python のパッケージが入りました", ""],
    model: ["埋め込みモデルを確かめています", ""],
    download: ["埋め込みモデルを Hugging Face から取得しています", snap.file ?? ""],
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
    // 出したダイアログがすべて閉じたら解ける(選ぶダイアログを、知らせのダイアログに重ねないため)
    settled: () => queue,
  };
}

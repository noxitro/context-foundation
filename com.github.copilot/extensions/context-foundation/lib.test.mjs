// lib.mjs の単体テスト(node --test。tests/test_plugin.py から流す)
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  chooseFallback, dataHome, escapeMarkdown, failureMessage, findConfig, folderChoices, folderSchema, hasLocalVenv, indexFile, MAX_FOLDER_CHOICES, pickedFolders, WHOLE_WORKSPACE, initSources, joinOptions, keywordOnly, lastCwd, loadSdk, logTail,
  makeNotifier, modelFolderPrompt, modelFolderSchema, modelMissing, modelUseResult, parseIndexArgs, pickedModelFolder, parseProgress, parseSetupArgs, parseSetupProgress, planIndex, pluginRoot, progressNote, repairCommand,
  SDK_NAMES, SETUP_HELP, setupFailureMessage, setupProgressNote, setupRuntimeFolder, splitArgs, wantDialog, workspaceOf,
} from "./lib.mjs";

const here = fileURLToPath(new URL(".", import.meta.url));

function tree() {
  const root = mkdtempSync(join(tmpdir(), "cf-ext-"));
  mkdirSync(join(root, "ws", "sub"), { recursive: true });
  mkdirSync(join(root, "ws", "Engine"));
  mkdirSync(join(root, "ws", "My Docs"));
  mkdirSync(join(root, "indexed", ".cf"), { recursive: true });
  mkdirSync(join(root, "indexed", "deep", "er"), { recursive: true });
  writeFileSync(join(root, "indexed", ".cf", "config.toml"), "");
  return root;
}

test("pluginRoot は plugin.json と ctxf.py のあるフォルダ(このリポジトリ)", () => {
  const root = pluginRoot(here);
  assert.equal(join(root, "com.github.copilot", "extensions", "context-foundation"), here.replace(/[\\/]$/, ""));
});

test("lastCwd は最後の作業フォルダ", () => {
  const events = [
    { type: "session.start", data: { context: { cwd: "C:\\a" } } },
    { type: "user.message", data: {} },
    { type: "session.context_changed", data: { cwd: "C:\\b" } },
  ];
  assert.equal(lastCwd(events), "C:\\b");
  assert.equal(lastCwd([]), "");
});

test("splitArgs と parseIndexArgs: 引用符・full・./full", () => {
  assert.deepEqual(splitArgs(' Engine  "My Docs" '), ["Engine", "My Docs"]);
  assert.deepEqual(parseIndexArgs(""), { full: false, folders: [] });
  assert.deepEqual(parseIndexArgs("FULL"), { full: true, folders: [] });
  assert.deepEqual(parseIndexArgs("full Engine"), { full: true, folders: ["Engine"] });
  assert.deepEqual(parseIndexArgs("./full"), { full: false, folders: ["./full"] });
});

test("findConfig は親へさかのぼって .cf\\config.toml を探す(作業フォルダの直下の config.toml は見ない)", () => {
  const root = tree();
  const cfg = join(root, "indexed", ".cf", "config.toml");
  assert.equal(findConfig(join(root, "indexed", "deep", "er")), cfg);
  assert.equal(findConfig(join(root, "ws", "sub")), "");
  writeFileSync(join(root, "indexed", "config.toml"), "[app]\n");    // ほかの道具の設定
  assert.equal(findConfig(join(root, "indexed")), cfg);
  assert.equal(workspaceOf(cfg), join(root, "indexed"));
});

test("findConfig は CF_CONFIG を先に見る(ctxf.py の find_config と同じ)", () => {
  const root = tree();
  const cfg = join(root, "indexed", ".cf", "config.toml");
  assert.equal(findConfig(join(root, "ws"), { CF_CONFIG: cfg }), cfg);
  assert.equal(findConfig(join(root, "ws"), { CF_CONFIG: join(root, "indexed") }), cfg);
  assert.equal(findConfig(join(root, "ws"), { CF_CONFIG: join(root, "indexed", ".cf") }), cfg);
  assert.throws(() => findConfig(join(root, "indexed"), { CF_CONFIG: join(root, "nowhere") }), /CF_CONFIG/);
  const flat = join(root, "shared");                                      // .cf の中に無い設定
  mkdirSync(flat);
  writeFileSync(join(flat, "config.toml"), "");
  assert.equal(findConfig(join(root, "ws"), { CF_CONFIG: flat }), join(flat, "config.toml"));
  assert.equal(workspaceOf(join(flat, "config.toml")), flat);
});

test("indexFile は [index] path を読む(無ければ index.db)", () => {
  const cfg = join("W", ".cf", "config.toml");
  const at = (name) => join("W", ".cf", name);
  assert.equal(indexFile(cfg, ""), at("index.db"));
  assert.equal(indexFile(cfg, '[[source]]\nid = "e"\nroot = "E"\npath = "x.db"\n'), at("index.db"));  // [index] の外
  assert.equal(indexFile(cfg, '[index]\nmax_line_chars = 9000\npath = "engine.db"  # 名前を変えた\n[search]\n'), at("engine.db"));
  assert.equal(indexFile(cfg, "[ index ]\r\npath='sub/a.db'\r\n"), at(join("sub", "a.db")));
  assert.equal(indexFile(cfg, '[index]\npath = "a\\\\b.db"\n'), at("a\\b.db"));
  const root = tree();
  assert.equal(indexFile(join(root, "nowhere", "config.toml")), join(root, "nowhere", "index.db"));   // 読めない
  writeFileSync(join(root, "indexed", ".cf", "config.toml"), '[index]\npath = "engine.db"\n');
  assert.equal(indexFile(join(root, "indexed", ".cf", "config.toml")), join(root, "indexed", ".cf", "engine.db"));
});

test("planIndex: 設定があれば作り直し、無ければフォルダを並べて init、並べていなければ選ばせる", () => {
  const root = tree();
  const config = join(root, "indexed", ".cf", "config.toml");
  assert.deepEqual(planIndex("", join(root, "indexed", "deep")), { action: "index", workspace: join(root, "indexed"), config, full: false });
  assert.deepEqual(planIndex("full", join(root, "indexed")), { action: "index", workspace: join(root, "indexed"), config, full: true });
  assert.match(planIndex("Engine", join(root, "indexed")).error, /設定がすでにあります/);  // 設定を黙って書き換えない
  const ws = join(root, "ws");
  assert.deepEqual(planIndex("", ws), { action: "choose", workspace: ws, full: false });
  assert.deepEqual(planIndex("full", ws), { action: "choose", workspace: ws, full: true });
  assert.deepEqual(planIndex('Engine "My Docs"', ws), { action: "init", workspace: ws, folders: ["Engine", "My Docs"], full: false });
  assert.match(planIndex("Engine Nowhere", ws).error, /フォルダがありません: .*Nowhere/);
  // CF_CONFIG があれば、作業フォルダに設定が無くても、その索引を作り直す(MCP サーバーと検索も、その設定を使う)
  assert.deepEqual(planIndex("", ws, { CF_CONFIG: config }), { action: "index", workspace: join(root, "indexed"), config, full: false });
  assert.match(planIndex("", ws, { CF_CONFIG: join(root, "nowhere") }).error, /CF_CONFIG/);
});

test("folderChoices は直下のフォルダから、隠しフォルダと索引に入らない名前を除く", () => {
  const root = tree();
  const ws = join(root, "ws");
  for (const d of [".git", "node_modules", "Build", "Docs"]) mkdirSync(join(ws, d));
  writeFileSync(join(ws, "README.md"), "");
  assert.deepEqual(folderChoices(ws), { names: ["Docs", "Engine", "My Docs", "sub"], omitted: 0 });
  assert.deepEqual(folderChoices(join(root, "nowhere")), { names: [], omitted: 0 });
  const many = join(root, "many");
  for (let i = 0; i < MAX_FOLDER_CHOICES + 5; i++) mkdirSync(join(many, `d${String(i).padStart(2, "0")}`), { recursive: true });
  const m = folderChoices(many);
  assert.equal(m.names.length, MAX_FOLDER_CHOICES);
  assert.equal(m.omitted, 5);
  assert.match(folderSchema(m.names, m.omitted).properties.folders.description, /ほかに 5 個/);
  assert.doesNotMatch(folderSchema(["Docs"]).properties.folders.description, /ほかに/);
});

test("folderChoices はフォルダへのリンク・ジャンクションも候補にする(切れたリンクは出さない)", () => {
  const root = tree();
  const ws = join(root, "ws");
  // Windows ではジャンクション(管理者の権限が要らない)、ほかではフォルダへのシンボリックリンク
  symlinkSync(join(root, "indexed"), join(ws, "Linked"), "junction");
  symlinkSync(join(root, "gone"), join(ws, "Broken"), "junction");
  assert.deepEqual(folderChoices(ws).names, ["Engine", "Linked", "My Docs", "sub"]);
});

test("folderSchema と pickedFolders: 複数選べる形、初めは全部選んである、作業フォルダ全体は . にする", () => {
  const names = ["Docs", "Engine"];
  const s = folderSchema(names);
  assert.equal(s.properties.folders.type, "array");
  assert.deepEqual(s.properties.folders.items.enum, ["Docs", "Engine", WHOLE_WORKSPACE]);
  assert.deepEqual(s.properties.folders.default, ["Docs", "Engine"]);
  assert.deepEqual(pickedFolders({ folders: ["Engine"] }, names), ["Engine"]);
  assert.deepEqual(pickedFolders({ folders: ["Engine", WHOLE_WORKSPACE] }, names), ["."]);
  // 候補に無いもの(Other に書き込まれたパス)は後ろに足す
  assert.deepEqual(pickedFolders({ folders: [" \"D:\\Ref App\" ", "Engine", ""] }, names), ["Engine", "D:\\Ref App"]);
  assert.deepEqual(pickedFolders({ folders: "Docs" }, names), ["Docs"]);
  // 書き込まれたものが候補と同じフォルダなら、2 度渡さない(空白・引用符・末尾の区切り・大文字小文字)
  assert.deepEqual(pickedFolders({ folders: ["Engine", " \"Engine\" ", "engine\\", "Docs/", "D:\\", "D:\\"] }, names),
    ["Docs", "Engine", "D:\\"]);
  assert.deepEqual(pickedFolders(undefined, names), []);
  assert.match(chooseFallback("C:\\w", ["Engine", "My Docs"]), /まだ索引がありません.*\n使い方: \/context-index.*例: \/context-index Engine "My Docs"/s);
  assert.match(chooseFallback("C:\\w", []), /例: \/context-index \./);
});

// indexer.py の build_index が出す行の形(_Progress と log)
const LINES = [
  "  取り込み中: 1,200 ファイル / 9,000 passage",
  "取り込みと分割: 8,123 ファイル / 2,000,000 行 / 61,000 passage(36.0 秒)",
  "  前の索引の埋め込みを使い回します: 0 passage、",
  "  埋め込み中: 6,100 / 61,000 passage(40 件/秒、残り 約 1500 秒)",
  "埋め込み: 61,000 passage を計算、0 passage を使い回し、中身の無い 3 passage は埋め込まず(1500.0 秒、モデル m、CPU、4 プロセス)",
  "索引ができました: C:\\w\\.cf\\index.db(120.0 MB、合計 1600.0 秒)",
];

test("parseProgress は最後の段階を返す", () => {
  const upTo = (n) => LINES.slice(0, n).join("\n");
  assert.deepEqual(parseProgress(""), { phase: "start" });
  assert.deepEqual(parseProgress(upTo(1)), { phase: "ingest", files: 1200 });
  assert.deepEqual(parseProgress(upTo(3)), { phase: "ingested", files: 8123, passages: 61000 });
  assert.deepEqual(parseProgress(upTo(4)), { phase: "embed", done: 6100, total: 61000, eta: 1500 });
  assert.deepEqual(parseProgress(upTo(5)), { phase: "embedded" });
  assert.deepEqual(parseProgress(upTo(6)), { phase: "done" });
});

test("progressNote: 段階が変わったとき・1 割ごと・1 分ごとに出す", () => {
  let r = progressNote({}, { phase: "start" }, 0, 0);
  assert.equal(r.note, "");                                  // 始めてすぐは出さない
  assert.equal(progressNote(r.state, { phase: "start" }, 30000, 30).note, "");
  assert.match(progressNote(r.state, { phase: "start" }, 61000, 61).note, /準備しています.*61 秒/);
  r = progressNote(r.state, { phase: "ingest", files: 100 }, 62000, 62);
  assert.match(r.note, /^context-foundation: ファイルを取り込んでいます\(100 ファイル/);
  r = progressNote(r.state, { phase: "embed", done: 6100, total: 61000, eta: 1500 }, 70000, 70);
  assert.match(r.note, /6,100\/61,000 passage\(10%、残り 約 25 分、70 秒\)/);
  // 同じ 1 割の中では出さない。次の 1 割でも、前から 10 秒たっていなければ出さない
  assert.equal(progressNote(r.state, { phase: "embed", done: 6200, total: 61000, eta: 1400 }, 90000, 90).note, "");
  assert.equal(progressNote(r.state, { phase: "embed", done: 12200, total: 61000, eta: 30 }, 75000, 75).note, "");
  assert.match(progressNote(r.state, { phase: "embed", done: 12200, total: 61000, eta: 30 }, 81000, 81).note, /20%、残り 約 30 秒/);
  assert.match(progressNote(r.state, { phase: "embedded" }, 82000, 82).note, /索引を書いています/);
  assert.equal(progressNote(r.state, { phase: "done" }, 83000, 83).note, "");
});

test("repairCommand は入口の英語の知らせから直すコマンドを取り出す", () => {
  const out = "context-foundation is not set up on this PC. Search with grep meanwhile. To set it up, ask the user first " +
    '(it downloads ...), then run: python -I "C:\\p\\ctxf.py" setup\nA Copilot session that is already running ...\n';
  assert.equal(repairCommand(out), 'python -I "C:\\p\\ctxf.py" setup');
  assert.equal(repairCommand("nothing"), "");
});

test("failureMessage は終了コードごとに、わけと次にすることを出す", () => {
  const log = "C:\\w\\.cf\\context-index.log";
  assert.match(failureMessage(4, "", log, "SETUP"), /setup.*\nSETUP$/s);
  assert.match(failureMessage(4, "", log, "SETUP"), /\/context-setup を打つか/);
  // 隣の .venv(リポジトリを取得した人)は setup.bat で直す。/context-setup は勧めない
  const local = 'context-foundation is broken. To repair it, ask the user to run "C:\\r\\setup.bat"; after that, the search tool ...';
  const m = failureMessage(4, local, log, "SETUP");
  assert.match(m, /\n"C:\\r\\setup\.bat"$/);
  assert.doesNotMatch(m, /context-setup/);
  assert.match(failureMessage(2, "a\nb\nsource のルートがありません:\n  engine: E:\\x\n", log, "S"), /前の索引はそのまま使えます.*engine: E:\\x$/s);
  assert.match(failureMessage(1, "", log, "S"), /index\.db\.new/);
  assert.match(failureMessage(1, "", log, "S", join("W", ".cf", "engine.db")), /engine\.db\.new に/);
  assert.match(failureMessage(130, "", log, "S"), /続きから/);
  assert.match(failureMessage(7, "", log, "S"), /終了コード 7/);
  assert.equal(logTail("1\n\n2\n3\n4\n5\n"), "2\n3\n4\n5");
});

test("parseSetupArgs: no-model・gpu・cpu(-- はあってもなくても)、help、知らない指定", () => {
  assert.deepEqual(parseSetupArgs(""), { args: [] });
  assert.deepEqual(parseSetupArgs("No-Model --gpu gpu"), { args: ["--no-model", "--gpu"] });
  assert.deepEqual(parseSetupArgs("cpu"), { args: ["--cpu"] });
  assert.deepEqual(parseSetupArgs("--help"), { help: true });
  assert.match(parseSetupArgs("gpu cpu").error, /一緒に指定できません/);
  assert.match(parseSetupArgs("full").error, /知らない指定です: full/);
  assert.match(parseSetupArgs("constructor").error, /知らない指定です/);  // Object の持ち物の名前を指定と取り違えない
  assert.match(SETUP_HELP, /no-model.*gpu/s);
});

test("dataHome は ctxf.py の data_home と同じ順で置き場を決める", () => {
  assert.equal(dataHome({ CF_HOME: "C:\\cf", LOCALAPPDATA: "C:\\L" }, "win32"), "C:\\cf");
  assert.equal(dataHome({ LOCALAPPDATA: "C:\\L" }, "win32"), join("C:\\L", "context-foundation"));
  assert.equal(dataHome({ XDG_DATA_HOME: "/x" }, "linux", "/h"), join("/x", "context-foundation"));
  assert.equal(dataHome({}, "linux", "/h"), join("/h", ".local", "share", "context-foundation"));
});

test("hasLocalVenv は、プラグインの隣の .venv を見る", () => {
  const root = mkdtempSync(join(tmpdir(), "cf-venv-"));
  assert.equal(hasLocalVenv(root), false);
  mkdirSync(join(root, ".venv"));
  assert.equal(hasLocalVenv(root), true);
});

// ctxf.py setup のログの形(_setup の _say と model_report の行)。setup は埋め込みモデルを取得しない
const SETUP_LINES = [
  "== context-foundation setup",
  "  runtime folder: C:\\cf\\runtime",
  "  creating the virtual environment ...",
  "  installing the Python packages from PyPI (about 200 MB the first time) ...",
  "  Python packages: installed",
  "  embedding model: not installed (intfloat/multilingual-e5-small, license MIT, about 487 MB, from https://huggingface.co/intfloat/multilingual-e5-small)",
  "    Setup does not install it: it is open-source software from outside PyPI, and whether it may be used depends on this PC's environment (licence, terms of use, network). Search is keyword-only (BM25) until it is installed.",
  '    To use it, check the model\'s source and licence, then run: python -I "C:\\p\\ctxf.py" model download',
];

test("parseSetupProgress は setup のログの最後の段階を返す", () => {
  const upto = (n) => parseSetupProgress(SETUP_LINES.slice(0, n).join("\r\n"));
  assert.deepEqual(upto(2), { phase: "start" });
  assert.deepEqual(upto(3), { phase: "venv" });
  assert.deepEqual(upto(4), { phase: "packages" });
  assert.deepEqual(upto(5), { phase: "packaged" });
  assert.deepEqual(upto(8), { phase: "packaged" });  // モデルの有無を出すだけで、取得の段階は無い
  assert.deepEqual(parseSetupProgress([...SETUP_LINES, "Setup is done. Next, ..."].join("\n")), { phase: "done" });
  assert.equal(setupRuntimeFolder(SETUP_LINES.join("\n")), "C:\\cf\\runtime");
  assert.equal(setupRuntimeFolder("nothing"), "");
});

test("modelMissing は、setup のログから埋め込みモデルが無いことと、利用者が自分で取得するコマンドを読む", () => {
  const e5 = "intfloat/multilingual-e5-small, license MIT, about 487 MB, from https://huggingface.co/intfloat/multilingual-e5-small";
  assert.deepEqual(modelMissing(SETUP_LINES.join("\r\n")),
    { command: 'python -I "C:\\p\\ctxf.py" model download', name: "intfloat/multilingual-e5-small", about: e5, ask: true });
  // 作業フォルダの設定の model_dir が置き場を決めている: model use で覚えても、そちらが先なので尋ねない
  const ownFolder = "  embedding model: not installed (m, license unknown, in the folder set by [embedding] model_dir: D:\\m)\n" +
    "    Setup does not install it: ...\n    To use it, put the model files in that folder.\n";
  assert.deepEqual(modelMissing(ownFolder),
    { command: "", name: "m", about: "m, license unknown, in the folder set by [embedding] model_dir: D:\\m", ask: false });
  // model use で覚えたフォルダからファイルが無くなった: 尋ね直す
  const moved = "  embedding model: not installed (m, license MIT, in the folder set with `model use`: D:\\old)\n" +
    "    The model files are no longer in that folder. Check it, then run: python -I \"C:\\p\\ctxf.py\" model use \"<folder>\"\n";
  assert.equal(modelMissing(moved).ask, true);
  assert.equal(modelMissing(moved).command, "");
  assert.equal(modelMissing("  embedding model: installed (m, C:\\m)\n"), null);
  assert.equal(modelMissing("  embedding model: not used (embedding is off in C:\\w\\.cf\\config.toml)\n"), null);
  assert.equal(modelMissing(""), null);
});

test("setupProgressNote は段階が変わったとき・同じ段階が 1 分続くたびに出す", () => {
  let r = setupProgressNote({}, { phase: "start" }, 0, 0);
  assert.equal(r.note, "");  // 始めてすぐは出さない
  assert.equal(setupProgressNote(r.state, { phase: "start" }, 61000, 61).note, "context-foundation: 準備しています(61 秒)");
  r = setupProgressNote(r.state, { phase: "packages" }, 5000, 5);
  assert.equal(r.note, "context-foundation: Python のパッケージを PyPI から入れています(初めては約 200 MB、5 秒)");
  assert.equal(setupProgressNote(r.state, { phase: "packages" }, 30000, 30).note, "");
  assert.match(setupProgressNote(r.state, { phase: "packages" }, 66000, 66).note, /約 200 MB、66 秒\)$/);
  r = setupProgressNote(r.state, { phase: "packaged" }, 70000, 70);
  assert.equal(r.note, "context-foundation: Python のパッケージが入りました(70 秒)");
  assert.equal(setupProgressNote(r.state, { phase: "done" }, 83000, 83).note, "");
});

test("setupFailureMessage は終了コードごとに、わけと次にすることを出す", () => {
  const log = "C:\\cf\\context-setup.log";
  assert.match(setupFailureMessage(2, "Python 3.15.0 is not supported ...\n", log), /この Python では.*3\.15\.0/s);
  assert.match(setupFailureMessage(3, "Another setup is running (x).\n", log), /別の用意.*Another setup/s);
  assert.match(setupFailureMessage(1, "  installing ...\n  pip failed (exit code 1). pip could not reach PyPI.\n", log),
    /終了コード 1.*pip could not reach PyPI\.$/s);
});

test("initSources と keywordOnly は ctxf.py の出力を読む", () => {
  const init = "設定を書きました: C:\\w\\.cf\\config.toml\n  source Engine: Engine\n  source docs: D:\\Docs\n次は ...\n";
  assert.deepEqual(initSources(init), ["Engine: Engine", "docs: D:\\Docs"]);
  assert.equal(keywordOnly("注意: 埋め込みモデルが手元にありません\n      → keyword(BM25)だけの索引を作ります\n"), true);
  assert.equal(keywordOnly(LINES.join("\n")), false);
});

test("escapeMarkdown は記号の前の \\ を二重にする", () => {
  assert.equal(escapeMarkdown("E:\\src\\Proj\\.cf"), "E:\\src\\Proj\\\\.cf");
});

test("wantDialog: CF_DISPLAY で切り替える。既定はアプリのときだけ", () => {
  assert.equal(wantDialog({ CF_DISPLAY: "timeline" }, true), false);
  assert.equal(wantDialog({ CF_DISPLAY: "dialog" }, false), true);
  assert.equal(wantDialog({}, true), true);
  assert.equal(wantDialog({}, false), false);
});

test("makeNotifier: SDK の場所が分からなければアプリとみなし、error でない文をダイアログにも出す", async () => {
  const logs = [], dialogs = [];
  const session = {
    capabilities: { ui: { elicitation: true } },
    log: async (m, o) => { logs.push([m, o.level ?? ""]); },
    ui: { elicitation: async (o) => { dialogs.push(o.message); } },
  };
  const n = makeNotifier(() => session, {});
  await n.tell("できました C:\\w\\.cf");
  await n.tell("だめでした", "error");
  await n.say("進み具合");
  await new Promise((r) => setTimeout(r, 10));
  assert.deepEqual(logs, [["できました C:\\w\\\\.cf", ""], ["だめでした", "error"], ["進み具合", ""]]);
  assert.deepEqual(dialogs, ["できました C:\\w\\\\.cf"]);
});

test("makeNotifier: exclusive のダイアログを出している間の知らせのダイアログは、閉じてから出す", async () => {
  const order = [];
  let close;
  const session = {
    capabilities: { ui: { elicitation: true } },
    log: async () => { },
    ui: { elicitation: async (o) => { order.push(`open ${o.message}`); } },
  };
  const n = makeNotifier(() => session, {});
  const picked = n.exclusive(async () => {
    order.push("open pick");
    await new Promise((r) => { close = r; });
    order.push("close pick");
    return ["Engine"];
  });
  await new Promise((r) => setTimeout(r, 10));
  await n.tell("途中です", "warning");
  await new Promise((r) => setTimeout(r, 10));
  assert.deepEqual(order, ["open pick"]);
  close();
  assert.deepEqual(await picked, ["Engine"]);
  await new Promise((r) => setTimeout(r, 10));
  assert.deepEqual(order, ["open pick", "close pick", "open 途中です"]);
  // fn が失敗しても、列は止まらない
  await assert.rejects(n.exclusive(async () => { throw new Error("描けない"); }), /描けない/);
  await n.tell("次");
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(order.at(-1), "open 次");
});

// Copilot CLI 1.0.113 は @microsoft/copilot-sdk だけを、1.0.112 まで と GitHub Copilot app は @github/copilot-sdk だけを
// 同梱の SDK に向ける。見つからないときの誤りは、Node.js の ERR_MODULE_NOT_FOUND と同じ形にする
const notFound = (name) => Object.assign(new Error(`Cannot find package '${name.split("/").slice(0, 2).join("/")}' imported from x`),
  { code: "ERR_MODULE_NOT_FOUND" });

test("loadSdk: 新しい名前(@microsoft)を先に試す", async () => {
  const tried = [];
  const m = await loadSdk(async (n) => { tried.push(n); return { which: n }; });
  assert.deepEqual(m, { which: "@microsoft/copilot-sdk/extension" });
  assert.deepEqual(tried, ["@microsoft/copilot-sdk/extension"]);
  assert.deepEqual(SDK_NAMES, ["@microsoft/copilot-sdk/extension", "@github/copilot-sdk/extension"]);
});

test("loadSdk: 新しい名前が無ければ、古い名前(@github)を読む", async () => {
  const m = await loadSdk(async (n) => {
    if (n.startsWith("@microsoft/")) throw notFound(n);
    return { which: n };
  });
  assert.deepEqual(m, { which: "@github/copilot-sdk/extension" });
});

test("loadSdk: どちらも無ければ、名前を挙げて止まる", async () => {
  await assert.rejects(loadSdk(async (n) => { throw notFound(n); }),
    /SDK が見つかりません.@microsoft\/copilot-sdk\/extension、@github\/copilot-sdk\/extension./);
});

test("loadSdk: SDK の中の誤り(ほかのパッケージが無いなど)は、次を試さずに投げる", async () => {
  const tried = [];
  const inner = Object.assign(new Error("Cannot find package 'vscode-jsonrpc' imported from sdk"), { code: "ERR_MODULE_NOT_FOUND" });
  await assert.rejects(loadSdk(async (n) => { tried.push(n); throw inner; }), (e) => e === inner);
  assert.deepEqual(tried, ["@microsoft/copilot-sdk/extension"]);
  const syntax = new SyntaxError("Unexpected token");
  await assert.rejects(loadSdk(async () => { throw syntax; }), (e) => e === syntax);
});

test("joinOptions: コマンドを、新しい SDK の interaction.commands と古い SDK の commands の両方に置く", () => {
  const commands = [{ name: "context-index", handler: () => { } }];
  const o = joinOptions(commands);
  assert.equal(o.commands, commands);
  assert.equal(o.interaction.commands, commands);
});

test("モデルのフォルダのダイアログ: 形・文・答えの読み方", () => {
  const schema = modelFolderSchema();
  assert.equal(schema.properties.folder.type, "string");
  assert.equal(schema.required, undefined);  // 空のまま送ると飛ばす
  const missing = modelMissing(SETUP_LINES.join("\n"));
  const first = modelFolderPrompt(missing);
  assert.match(first, /^context-foundation: 埋め込みモデル\(intfloat\/multilingual-e5-small, license MIT/);
  assert.match(first, /写しも取得もしません/);
  assert.match(modelFolderPrompt(missing, "D:\\x は使えませんでした: 無い"), /^context-foundation: D:\\x は使えませんでした: 無い\n埋め込みモデル/);
  assert.equal(pickedModelFolder({ folder: '  "D:\\models\\e5"  ' }), "D:\\models\\e5");
  assert.equal(pickedModelFolder({ folder: "   " }), "");
  assert.equal(pickedModelFolder({}), "");
  assert.equal(pickedModelFolder(undefined), "");
  assert.equal(pickedModelFolder({ folder: 3 }), "");
});

test("modelUseResult は model use の終了コードと result: の行で決める", () => {
  assert.deepEqual(modelUseResult(0, "モデルを読み込んで確かめます: D:\\m\nintfloat/multilingual-e5-small のフォルダを覚えました: D:\\m\n" +
    "  注意: C:\\w\\.cf\\config.toml の [embedding] model_dir(x)が先に使われます\nresult: saved\n"),
  { state: "saved", notes: ["C:\\w\\.cf\\config.toml の [embedding] model_dir(x)が先に使われます"], detail: "" });
  const warned = modelUseResult(4, "モデルを読み込んで確かめます: D:\\m\n  注意: model.onnx の大きさが違います\n確かめると…\n" +
    "result: not saved (warnings; run again with --yes to save)\n");
  assert.deepEqual(warned, { state: "warnings", notes: ["model.onnx の大きさが違います"], detail: "" });
  const err = modelUseResult(2, "フォルダがありません: D:\\nope\nresult: not saved (error)\n");
  assert.equal(err.state, "error");
  assert.equal(err.detail, "フォルダがありません: D:\\nope");
  // 終了コードと result: の行が合わないときは、覚えていないものとみなす
  assert.equal(modelUseResult(0, "").state, "error");
  assert.equal(modelUseResult(1, "result: saved\n").state, "error");
  assert.equal(modelUseResult(null, "").detail, "終了コード null");
});

// lib.mjs の単体テスト(node --test。tests/test_plugin.py から流す)
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  chooseFallback, commandHandler, dataHome, escapeMarkdown, failureMessage, findConfig, folderChoices, folderSchema, hasLocalVenv, indexFile, MAX_FOLDER_CHOICES, pickedFolders, WHOLE_WORKSPACE, initSources, joinOptions, keywordOnly, lastCwd, loadSdk, logTail,
  makeNotifier, modelFolderPrompt, modelFolderSchema, modelMissing, modelUseResult, parseIndexArgs, pickedModelFolder, parseProgress, parseSetupProgress, planIndex, pluginRoot, progressNote, repairCommand,
  ACCEL_RETRY, accelPrompt, accelSchema, askAccel, choiceSchema, modelNotUsedReason, REINDEX_AFTER_MODEL, setupStartText, setupWhat, CPU_CHOICE, pickedYes, SETUP_NO, SETUP_YES, currentModelFolder, GPU_CHOICE, modelChoice, modelStepPrompt, pickedAccel, setupArgs, setupConfirmText,
  setupStatus, setupSteps, SDK_NAMES, setupFailureMessage, setupProgressNote, setupRuntimeFolder, splitArgs, wantDialog, workspaceOf,
  askOneOf, CHANGE_YES, changeConfirmText, changedText, computeName, embedNote, embeddingLines, folderDefaults, folderPrompt, INDEX_BUILD, INDEX_CANCEL,
  INDEX_FOLDERS, INDEX_FULL, INDEX_UPDATE, INDEX_YES, indexConfirmText, indexMenuChoices, indexMenuText, indexState, MENU_RETRY, menuSchema, pickedAction,
  sourcesLine, sourcesPlan,
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
  assert.match(chooseFallback("C:\\w", ["Engine", "My Docs"]), /まだ索引がありません.*ダイアログを出せないので.*\n使い方: \/context-index.*例: \/context-index Engine "My Docs"/s);
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

const READY = {
  ready: true, gpu: true, gpu_supported: true, home: "H",
  model: { state: "installed", name: "intfloat/multilingual-e5-small", folder: "D:\\models\\e5", own_folder: true, dir_source: "user" },
};
const FRESH = { ready: false, gpu: false, gpu_supported: true, home: "H", model: null };

test("setupStatus は setup --status の最後の行の JSON を読み、読めなければ null", () => {
  assert.deepEqual(setupStatus(`warn\n${JSON.stringify(FRESH)}\n`), FRESH);
  assert.equal(setupStatus("python が見つかりません"), null);
  assert.equal(setupStatus('{"gpu": true}'), null);   // ready の無いものは使わない
  assert.equal(setupStatus(""), null);
});

test("setupSteps: GPU は Windows だけ、モデルは設定が置き場を決めていないときだけ尋ね、最後に確かめる", () => {
  assert.deepEqual(setupSteps(FRESH), ["accel", "model", "confirm"]);
  assert.deepEqual(setupSteps({ ...FRESH, gpu_supported: false }), ["model", "confirm"]);
  // 状態を読めなくても、尋ねて進める(GPU を選べるかは、この OS で決める)
  assert.deepEqual(setupSteps(null, "linux"), ["model", "confirm"]);
  assert.deepEqual(setupSteps(null, "win32"), ["accel", "model", "confirm"]);
  assert.deepEqual(setupSteps(READY), ["accel", "model", "confirm"]);   // model use で覚えたフォルダは替えられる
  const byConfig = { ...READY, model: { ...READY.model, dir_source: "config" } };
  assert.deepEqual(setupSteps(byConfig), ["accel", "confirm"]);
  assert.deepEqual(setupSteps({ ...READY, model: { state: "off" } }), ["accel", "confirm"]);
});

test("入れたモデルのフォルダを使わないわけ: 設定の model_dir と、埋め込みを切った設定", () => {
  assert.equal(modelNotUsedReason(null), "");
  assert.equal(modelNotUsedReason(READY.model), "");   // model use で覚えたものは替えられる
  assert.match(modelNotUsedReason({ ...READY.model, dir_source: "config" }), /model_dir\(D:\\models\\e5\)が置き場を決めていて/);
  assert.match(modelNotUsedReason({ own_folder: true, folder: "" }), /model_dir が置き場を決めていて/);
  assert.match(modelNotUsedReason({ state: "off" }), /埋め込みを切っているため/);
});

test("GPU の段: 前の選び方を初めの値にし、答えを GPU か CPU かに読む(読めなければ null で、前の選び方)", () => {
  assert.equal(accelSchema(READY).properties.accel.default, GPU_CHOICE);
  assert.equal(accelSchema(FRESH).properties.accel.default, CPU_CHOICE);
  assert.deepEqual(accelSchema(FRESH).properties.accel.enum, [GPU_CHOICE, CPU_CHOICE]);
  assert.match(accelPrompt(1, 3, READY), /^context-foundation の用意\(1\/3\): .*今は GPU です/);
  assert.doesNotMatch(accelPrompt(1, 3, FRESH), /今は/);
  // 読めない答えの後の尋ね直しは、段の頭の後ろにわけを添える
  assert.match(accelPrompt(1, 3, FRESH, "前の答えを読めませんでした。"), /^context-foundation の用意\(1\/3\): 前の答えを読めませんでした。\n索引づくりで/);
  assert.equal(pickedAccel({ accel: GPU_CHOICE }), true);
  assert.equal(pickedAccel({ accel: CPU_CHOICE }), false);
  assert.equal(pickedAccel({ accel: " directml " }), true);   // Other に書き込まれたもの
  assert.equal(pickedAccel({ accel: "cpu で" }), false);
  assert.equal(pickedAccel({ accel: "どちらでも" }), null);
  assert.equal(pickedAccel(undefined), null);
  assert.deepEqual(setupArgs({ gpu: true }), ["--gpu"]);
  assert.deepEqual(setupArgs({ gpu: false }), ["--cpu"]);
  assert.deepEqual(setupArgs({ gpu: null }), []);
  assert.deepEqual(setupArgs(null), []);   // 尋ねられなかった: 前の setup の選び方を引き継ぐ
});

test("モデルの段: 今のフォルダを初めの値にし、そのままなら今のまま、空なら入れない、別のフォルダなら確かめて覚える", () => {
  assert.equal(currentModelFolder(READY), "D:\\models\\e5");
  assert.equal(currentModelFolder(FRESH), "");
  assert.equal(modelFolderSchema(currentModelFolder(READY)).properties.folder.default, "D:\\models\\e5");
  assert.equal("default" in modelFolderSchema("").properties.folder, false);
  assert.match(modelStepPrompt(2, 3, READY), /^context-foundation の用意\(2\/3\): 埋め込みモデル\(intfloat\/multilingual-e5-small\)は入っています/);
  assert.match(modelStepPrompt(2, 3, FRESH), /空のまま送ると、keyword だけの検索で続けます/);
  // 入っているモデルは、欄を空にしても外れない(そう書く)
  assert.match(modelStepPrompt(2, 3, READY), /欄を空にして送っても、今のモデルのままです\(覚えたフォルダを外すときは.*model forget/);
  // model forget は、model use で覚えたフォルダにだけ効く。取得して既定の置き場にあるモデルには勧めない
  const downloaded = { ...READY, model: { ...READY.model, own_folder: false, dir_source: "" } };
  assert.match(modelStepPrompt(2, 3, downloaded), /今のモデルのままです。取り消すと/);
  assert.doesNotMatch(modelStepPrompt(2, 3, downloaded), /model forget/);
  assert.match(modelFolderSchema("D:\\m").properties.folder.description, /空にして送っても、今のモデルを使います/);
  // 仮想環境が無いうち(model が無い)は、既定のモデルの名前と入手先を出す
  assert.match(modelStepPrompt(2, 3, { ...FRESH, default_model: "org/name" }),
    /埋め込みモデル\(既定は org\/name。入手先 https:\/\/huggingface\.co\/org\/name\)のフォルダ/);
  assert.match(modelStepPrompt(2, 3, FRESH), /埋め込みモデルのフォルダを入れてください/);   // 名前を読めなければ、名前なしで尋ねる
  const missing = { ...FRESH, model: { state: "missing", name: "m", license: "MIT", size_mb: 487 } };
  assert.match(modelStepPrompt(2, 3, missing), /埋め込みモデル\(m、ライセンス MIT、約 487 MB\)のフォルダ/);
  assert.deepEqual(modelChoice("D:\\models\\e5\\", READY), { kind: "keep" });   // 末尾の区切りと大文字小文字は見ない
  assert.deepEqual(modelChoice("", READY), { kind: "keep" });
  assert.deepEqual(modelChoice("E:\\other", READY), { kind: "use", folder: "E:\\other" });
  assert.deepEqual(modelChoice("", FRESH), { kind: "none" });
  assert.deepEqual(modelChoice("E:\\m", FRESH), { kind: "use", folder: "E:\\m" });
  // 入っているモデルを替えるときの、尋ね直しの文(missing が無い)
  assert.match(modelFolderPrompt(null, "E:\\x は使えませんでした: だめ"), /^context-foundation: E:\\x は使えませんでした: だめ\n使う埋め込みモデルのフォルダを.*今のモデルのまま続けます$/s);
});

test("はい・いいえは日本語の選択肢で尋ね、選んだ yes だけを yes とみなす", () => {
  const s = choiceSchema(SETUP_YES, SETUP_NO).properties.answer;
  assert.deepEqual([s.enum, s.default], [[SETUP_YES, SETUP_NO], SETUP_YES]);
  assert.equal(pickedYes({ answer: SETUP_YES }, SETUP_YES), true);
  assert.equal(pickedYes({ answer: ` ${SETUP_YES} ` }, SETUP_YES), true);
  assert.equal(pickedYes({ answer: SETUP_NO }, SETUP_YES), false);
  assert.equal(pickedYes({ answer: "はい" }, SETUP_YES), false);   // Other に書き込まれたものは yes にしない
  assert.equal(pickedYes(undefined, SETUP_YES), false);
});

test("始めの知らせは、確かめの段と同じ「入れるもの」を言い、覚えた後の案内は full を勧めない", () => {
  assert.match(setupStartText({ gpu: true, model: { kind: "keep" } }, READY, "H"), /^用意\(setup\)を始めました。入れるもの: 済んでいるので、確かめるだけです。置き場: H/);
  assert.match(setupStartText({ gpu: false, model: null }, READY, "H"), /CPU 用に入れ直します/);
  assert.match(setupStartText({ gpu: true, model: null }, FRESH, "H"), /PyPI から約 200 MB。初めは数分/);
  assert.match(setupStartText(null, null, "H"), /済んでいれば確かめるだけです/);   // 尋ねられなかった(ダイアログを出せないホスト)
  assert.equal(setupWhat(null, READY), "済んでいるので、確かめるだけです");
  // 索引づくりは、モデルが違うときだけ自分ですべて埋め込み直す。full を勧めると、同じモデルでも埋め込み直してしまう
  assert.match(REINDEX_AFTER_MODEL, /\/context-index で「更新する」を選ぶと、このモデルで埋め込み直します/);
  // すべて作り直すのは、見分けられない入れ替え(名前も大きさも同じ別の重み)のときだけ。引数の形(full)は案内しない
  assert.match(REINDEX_AFTER_MODEL, /名前も大きさも同じ別の重みに替えたときだけ、「すべて作り直す」を選びます/);
  assert.doesNotMatch(REINDEX_AFTER_MODEL, /context-index full/);
});

// ---- /context-index(引数は取らず、ダイアログで尋ねる)

const STATE = {
  config: "C:\\w\\.cf\\config.toml", workspace: "C:\\w",
  sources: [
    { id: "Engine", root: "Engine", exists: true, inside: "" },
    { id: "docs", root: "D:\\shared\\docs", exists: true, inside: "" },
    { id: "Vendor", root: "Engine\\external\\Vendor", exists: true, inside: "Engine" },
  ],
  index: { path: "C:\\w\\.cf\\index.db", created_at: "2026-11-24T10:12:33+0900", files: 8084, passages: 72784, model: "org/model", provider: "DmlExecutionProvider" },
  embedding: { enabled: true, device: "auto", provider: "DmlExecutionProvider", model: { state: "installed", name: "org/model" } },
};

test("indexState と sourcesPlan は、最後の行の JSON を読み、読めなければ null", () => {
  assert.equal(indexState("注意: …\n" + JSON.stringify(STATE) + "\n").workspace, "C:\\w");
  assert.equal(indexState("context-foundation is not set up"), null);
  assert.equal(indexState('{"ready":true}'), null);
  const plan = { result: "would change", config: "c", keep: [], add: [{ id: "a", root: "a" }], remove: [], backup: "" };
  assert.deepEqual(sourcesPlan(JSON.stringify(plan)), plan);
  assert.equal(sourcesPlan('{"result":"changed"}'), null);
  assert.equal(sourcesPlan("フォルダがありません: C:\\w\\x"), null);
});

test("今の状態の文: フォルダ・索引・検索の種類・計算先を並べ、することを選ばせる", () => {
  const text = indexMenuText(STATE);
  assert.match(text, /^context-foundation の索引: この作業フォルダには索引があります\(C:\\w\)。/);
  // ほかの source の中にある source(ジャンクションなど)は、数だけを言う
  assert.match(text, /- 検索するフォルダ: Engine、D:\\shared\\docs\(ほかに、その中の 1 個\)/);
  assert.match(text, /- 索引: 2026-11-24 10:12 に作成、8,084 ファイル、72,784 passage、埋め込みモデル org\/model\n/);
  assert.match(text, /- 検索: 意味と keyword\(埋め込みモデル org\/model\)\n- 埋め込みの計算: GPU\(DirectML\)\n/);
  assert.match(text, /何をしますか。取り消すと、何もしません/);
  assert.deepEqual(indexMenuChoices(STATE), [INDEX_UPDATE, INDEX_FOLDERS, INDEX_FULL, INDEX_CANCEL]);
  assert.match(indexMenuText(STATE, MENU_RETRY), /^context-foundation の索引: 前の答えを読めませんでした。一覧から選んでください。\n/);
  // 索引がまだ無い・読めないときは、「索引を作る」
  const none = { ...STATE, index: null };
  assert.match(indexMenuText(none), /設定はありますが、索引がまだありません/);
  assert.deepEqual(indexMenuChoices(none), [INDEX_BUILD, INDEX_FOLDERS, INDEX_CANCEL]);
  const broken = { ...STATE, index: { path: "p", error: "IndexError_: 古い形" } };
  assert.match(indexMenuText(broken), /索引を読めません\(C:\\w。IndexError_: 古い形\)。作り直すと直ります/);
  assert.deepEqual(indexMenuChoices(broken), [INDEX_BUILD, INDEX_FOLDERS, INDEX_CANCEL]);
  // 索引の埋め込みが今のモデルと違う・無いときは、更新で埋め込み直すことを言う
  assert.match(indexMenuText({ ...STATE, index: { ...STATE.index, model: "old/model" } }), /今のモデルは org\/model なので、更新すると、すべて埋め込み直します/);
  assert.match(indexMenuText({ ...STATE, index: { ...STATE.index, model: "" } }), /keyword だけ。更新すると、意味での検索のための埋め込みを計算します/);
  assert.equal(sourcesLine([{ id: "w", root: ".", exists: true, inside: "" }, { id: "gone", root: "gone", exists: false, inside: "" }]),
    "作業フォルダ全体、gone(フォルダがありません)");
});

test("検索の種類と計算先の行: モデルが無ければ keyword だけと、入れ方を言う", () => {
  const emb = (over) => ({ embedding: { ...STATE.embedding, ...over } });
  assert.deepEqual(embeddingLines(null), []);
  assert.deepEqual(embeddingLines(emb({ provider: "" }), "win32"),
    ["- 検索: 意味と keyword(埋め込みモデル org/model)", "- 埋め込みの計算: CPU(GPU で計算するなら、/context-setup で GPU を選びます)"]);
  // 設定で CPU に決めているとき・Windows でないときは、GPU を勧めない
  assert.equal(embeddingLines(emb({ provider: "", device: "cpu" }), "win32")[1], "- 埋め込みの計算: CPU");
  assert.equal(embeddingLines(emb({ provider: "" }), "linux")[1], "- 埋め込みの計算: CPU");
  assert.match(embeddingLines(emb({ model: { state: "missing", name: "org/model" } }))[0], /keyword だけ\(埋め込みモデル org\/model がありません。.*\/context-setup/);
  assert.match(embeddingLines(emb({ enabled: false, model: { state: "off" } }))[0], /keyword だけ\(作業フォルダの設定で、埋め込みを切っています\)/);
  assert.match(embeddingLines(emb({ model: { state: "unknown", error: "読めない" } }))[0], /分かりません\(読めない\)/);
  assert.equal(computeName("CUDAExecutionProvider"), "GPU(CUDA)");
  assert.equal(computeName(""), "CPU");
});

test("することの選択肢: 選んだものだけを読み、書き込まれた文は読めないとして尋ね直す", async () => {
  const choices = indexMenuChoices(STATE);
  assert.deepEqual(menuSchema(choices).properties.action, { type: "string", title: "すること", enum: choices, default: INDEX_UPDATE });
  assert.equal(pickedAction({ action: ` ${INDEX_FULL} ` }, choices), INDEX_FULL);
  assert.equal(pickedAction({ action: "full" }, choices), null);
  assert.equal(pickedAction({ action: INDEX_BUILD }, choices), null);   // この状態の選択肢に無い
  assert.equal(pickedAction({}, choices), null);
  const seen = [];
  const answers = [{ action: "accept", content: { action: "ぜんぶ" } }, { action: "accept", content: { action: INDEX_FOLDERS } }];
  const got = await askOneOf(async (retry) => {
    seen.push(retry);
    return answers[seen.length - 1];
  }, (c) => pickedAction(c, choices), MENU_RETRY);
  assert.equal(got, INDEX_FOLDERS);
  assert.deepEqual(seen, ["", MENU_RETRY]);
  assert.equal(await askOneOf(async () => ({ action: "cancel" }), (c) => pickedAction(c, choices), MENU_RETRY), null);
  await assert.rejects(askOneOf(async () => { throw new Error("no dialog"); }, () => null, MENU_RETRY), /no dialog/);
});

test("フォルダを変えるとき: 今の source にチェックを付け、直下に無い source は候補に足す", () => {
  const names = ["Docs", "Engine", "Tools"];
  // 直下の名前は、大文字小文字と区切りの違いを見ない。ほかの source の中にあるものは出さない
  assert.deepEqual(folderDefaults(names, STATE.sources), { extra: ["D:\\shared\\docs"], checked: ["Engine", "D:\\shared\\docs"] });
  assert.deepEqual(folderDefaults(names, [{ id: "e", root: "engine/", inside: "" }]), { extra: [], checked: ["Engine"] });
  // 作業フォルダ全体が source なら、それだけを選んでおく
  assert.deepEqual(folderDefaults(names, [{ id: "w", root: ".", inside: "" }, { id: "Docs", root: "Docs", inside: "w" }]),
    { extra: [], checked: [WHOLE_WORKSPACE] });
  const { extra, checked } = folderDefaults(names, STATE.sources);
  const f = folderSchema([...names, ...extra], 5, checked, names.length).properties.folders;
  assert.deepEqual(f.items.enum, ["Docs", "Engine", "Tools", "D:\\shared\\docs", WHOLE_WORKSPACE]);
  assert.deepEqual(f.default, ["Engine", "D:\\shared\\docs"]);
  assert.match(f.description, /後から \/context-index で変えられます。名前の順で 3 個まで出しています\(ほかに 5 個/);
  assert.match(folderPrompt(1, 2, "C:\\w", true), /^context-foundation の索引\(1\/2\): 検索するフォルダを選び直してください\(C:\\w\)。今のフォルダに、チェックが付いています/);
  assert.match(folderPrompt(1, 2, "C:\\w"), /まだ索引がありません。索引に入れるフォルダを選んでください\(C:\\w\)。一覧に無いフォルダは、Other にパスを書くと足せます/);
});

test("確かめの文: 初めての索引は入れるフォルダ・置き場・検索の種類を、フォルダの変更は足す・外す・そのままを言う", () => {
  const first = indexConfirmText(2, 2, "C:\\w", [".", "D:\\x"], { ...STATE, index: null }, "win32");
  assert.match(first, /^context-foundation の索引\(2\/2\): 次の内容で索引を作ります。よければ「はい、索引を作る」を選んでください。\n/);
  assert.match(first, /- 検索するフォルダ: 作業フォルダ全体、D:\\x\n- 設定と索引の置き場: /);
  assert.match(first, /- 埋め込みの計算: GPU\(DirectML\)\n- かかる時間: /);
  assert.equal(INDEX_YES, "はい、索引を作る");
  // 直下にフォルダが無いときは、1 段だけ
  assert.match(indexConfirmText(1, 1, "C:\\w", ["."], null), /^context-foundation の索引: 次の内容で/);
  const plan = {
    result: "would change", config: "C:\\w\\.cf\\config.toml", backup: "",
    keep: [{ id: "Engine", root: "Engine" }], add: [{ id: "Tools", root: "Tools" }], remove: [{ id: "docs", root: "D:\\shared\\docs" }],
  };
  const change = changeConfirmText(2, 2, plan);
  assert.ok(change.includes(`よければ「${CHANGE_YES}」を選んでください。\n- 足す: Tools\n- 外す: D:\\shared\\docs\n- そのまま: Engine\n`));
  assert.match(change, /\[\[source\]\] だけを書き直し、前の設定は config\.toml\.bak に残します/);
  assert.doesNotMatch(changeConfirmText(2, 2, { ...plan, remove: [] }), /- 外す/);
  assert.equal(changedText({ ...plan, result: "changed", backup: "C:\\w\\.cf\\config.toml.bak" }),
    "検索するフォルダを替えました(足した: Tools、外した: D:\\shared\\docs。前の設定: C:\\w\\.cf\\config.toml.bak)。\n");
});

test("embedNote は、索引づくりのログから、埋め込みをどこで計算したかを読む", () => {
  const gpu = "埋め込み: 1,234 passage を計算、70,000 passage を使い回し、中身の無い 12 passage は埋め込まず(12.3 秒、モデル org/model、GPU(DmlExecutionProvider))\n";
  assert.equal(embedNote("取り込みと分割: …\n" + gpu + "索引ができました: …"), "埋め込みは GPU(DirectML) で計算しました(1,234 passage。70,000 passage は使い回し)。");
  const cpu = "埋め込み: 50 passage を計算、0 passage を使い回し、中身の無い 0 passage は埋め込まず(3.0 秒、モデル org/model、CPU、4 プロセス)\n";
  assert.equal(embedNote(cpu), "埋め込みは CPU で計算しました(50 passage。0 passage は使い回し)。");
  // GPU を選んでいても、始められなければ CPU に替えて続ける。そのことを言う
  assert.match(embedNote("注意: GPU(DmlExecutionProvider)で埋め込みを始められないので、CPU で計算します(RuntimeError: x)\n" + cpu),
    /CPU で計算しました.*GPU では始められなかったので、CPU に替えました\(わけはログ\)。$/);
  assert.equal(embedNote(gpu.replace("1,234 passage を計算", "0 passage を計算")), "埋め込みは、すべて前の索引から使い回しました。");
  assert.equal(embedNote("keyword(BM25)だけの索引を作ります\n"), "");
});

test("askAccel: 読めない答えは尋ね直し、3 回読めなければやめる。尋ね直しで出せなくなっても、答えの一部では進めない", async () => {
  const run = async (answers) => {
    const seen = [];
    const got = await askAccel(async (retry) => {
      seen.push(retry);
      const a = answers[seen.length - 1];
      if (a instanceof Error) throw a;
      return a;
    });
    return { got, seen };
  };
  const ok = (accel) => ({ action: "accept", content: { accel } });
  assert.deepEqual(await run([ok(GPU_CHOICE)]), { got: true, seen: [""] });
  assert.deepEqual(await run([ok("どちらでも"), ok(CPU_CHOICE)]), { got: false, seen: ["", ACCEL_RETRY] });
  assert.deepEqual(await run([{ action: "cancel" }]), { got: null, seen: [""] });
  assert.deepEqual(await run([ok("?"), { action: "decline" }]), { got: null, seen: ["", ACCEL_RETRY] });
  // 3 回とも読めない(accel を返さないホストも含む): 終わらない輪にせず、やめる
  assert.deepEqual(await run([ok("?"), ok("?"), { action: "accept" }]), { got: null, seen: ["", ACCEL_RETRY, ACCEL_RETRY] });
  // 始めのダイアログを出せないときは投げる(呼ぶ側が、尋ねずに用意する)。尋ね直しで出せなくなったら、やめる
  await assert.rejects(run([new Error("no dialog")]), /no dialog/);
  assert.deepEqual(await run([ok("?"), new Error("gone")]), { got: null, seen: ["", ACCEL_RETRY] });
});

test("確かめの段: 入れるもの・置き場・計算・モデルをまとめる", () => {
  const first = setupConfirmText(3, 3, { gpu: true, model: { kind: "use", folder: "E:\\m" } }, FRESH, "H");
  assert.match(first, /^context-foundation の用意\(3\/3\): 次の内容で用意します/);
  assert.match(first, /入れるもの: Python のパッケージ\(PyPI から約 200 MB/);
  assert.match(first, /置き場: H\n/);
  assert.match(first, /埋め込みの計算: GPU\(DirectML\)/);
  assert.match(first, /埋め込みモデル: E:\\m\(用意のあとで/);
  assert.match(setupConfirmText(3, 3, { gpu: true, model: { kind: "keep" } }, READY, "H"), /確かめるだけです[\s\S]*今のまま\(D:\\models\\e5\)/);
  assert.match(setupConfirmText(3, 3, { gpu: false, model: { kind: "keep" } }, READY, "H"), /CPU 用に入れ直します/);
  assert.match(setupConfirmText(2, 2, { gpu: null, model: null }, null, "H"), /済んでいれば確かめるだけです/);   // 状態を読めなかった
  assert.match(setupConfirmText(2, 2, { gpu: null, model: { kind: "none" } }, { ...FRESH, gpu_supported: false }, ""),
    /埋め込みの計算: CPU\n- 埋め込みモデル: 使いません/);
  const byConfig = { ...READY, model: { ...READY.model, dir_source: "config" } };
  assert.match(setupConfirmText(2, 2, { gpu: true, model: null }, byConfig, "H"), /設定の model_dir のフォルダ\(D:\\models\\e5\)/);
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
  "== context-foundation の用意(setup)",
  "  利用者ごとの置き場: C:\\cf\\runtime",
  "  仮想環境を作ります ...",
  "  Python のパッケージを PyPI から入れます(初めては約 200 MB)...",
  "  Python のパッケージ: 入れました",
  "  埋め込みモデル: インストールされていません(intfloat/multilingual-e5-small、ライセンス MIT、約 487 MB、入手先 https://huggingface.co/intfloat/multilingual-e5-small)",
  "    setup では入れません。PyPI の外の OSS で、使ってよいかは、この PC の環境(ライセンス・利用の条件・ネットワーク)によるためです。入るまで、検索は keyword(BM25)だけです。",
  '    使うには、モデルの入手先とライセンスを確かめてから、次を実行します: python -I "C:\\p\\ctxf.py" model download',
  '    この PC にモデルが置いてあれば、代わりに次を実行します: python -I "C:\\p\\ctxf.py" model use "<フォルダ>"',
];

test("parseSetupProgress は setup のログの最後の段階を返す", () => {
  const upto = (n) => parseSetupProgress(SETUP_LINES.slice(0, n).join("\r\n"));
  assert.deepEqual(upto(2), { phase: "start" });
  assert.deepEqual(upto(3), { phase: "venv" });
  assert.deepEqual(upto(4), { phase: "packages" });
  assert.deepEqual(upto(5), { phase: "packaged" });
  assert.deepEqual(upto(9), { phase: "packaged" });  // モデルの有無を出すだけで、取得の段階は無い
  assert.deepEqual(parseSetupProgress([...SETUP_LINES, "用意ができました。次は、..."].join("\n")), { phase: "done" });
  // 入れたパッケージを読めず、仮想環境を作り直すときも、仮想環境を作る段階
  assert.deepEqual(parseSetupProgress("  入れたパッケージを読み込めません(x)。仮想環境を作り直します ..."), { phase: "venv" });
  assert.equal(setupRuntimeFolder(SETUP_LINES.join("\n")), "C:\\cf\\runtime");
  assert.equal(setupRuntimeFolder("nothing"), "");
});

test("modelMissing は、setup のログから埋め込みモデルが無いことと、利用者が自分で取得するコマンドを読む", () => {
  const e5 = "intfloat/multilingual-e5-small、ライセンス MIT、約 487 MB、入手先 https://huggingface.co/intfloat/multilingual-e5-small";
  assert.deepEqual(modelMissing(SETUP_LINES.join("\r\n")),
    { command: 'python -I "C:\\p\\ctxf.py" model download', name: "intfloat/multilingual-e5-small", about: e5, ask: true });
  // 作業フォルダの設定の model_dir が置き場を決めている: model use で覚えても、そちらが先なので尋ねない
  const ownFolder = "  埋め込みモデル: インストールされていません(m、ライセンス 不明、[embedding] model_dir のフォルダ: D:\\m)\n" +
    "    setup では入れません。...\n    使うには、そのフォルダにモデルのファイルを置きます。\n";
  assert.deepEqual(modelMissing(ownFolder),
    { command: "", name: "m", about: "m、ライセンス 不明、[embedding] model_dir のフォルダ: D:\\m", ask: false });
  // model use で覚えたフォルダからファイルが無くなった: 尋ね直す
  const moved = "  埋め込みモデル: インストールされていません(m、ライセンス MIT、`model use` で覚えたフォルダ: D:\\old)\n" +
    "    そのフォルダに、モデルのファイルがもうありません。確かめてから、次を実行します: python -I \"C:\\p\\ctxf.py\" model use \"<フォルダ>\"\n";
  assert.equal(modelMissing(moved).ask, true);
  assert.equal(modelMissing(moved).command, "");
  assert.equal(modelMissing("  埋め込みモデル: 入っています(m、C:\\m)\n"), null);
  assert.equal(modelMissing("  埋め込みモデル: 使いません(C:\\w\\.cf\\config.toml で、埋め込みを切っています)\n"), null);
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
  assert.match(setupFailureMessage(2, "Python 3.15.0 は使えません ...\n", log), /この Python では.*3\.15\.0/s);
  assert.match(setupFailureMessage(3, "別の setup が動いています(x)。\n", log), /別の用意.*別の setup が動いています/s);
  assert.match(setupFailureMessage(1, "  入れます ...\n  pip が失敗しました(終了コード 1)。pip が PyPI に届きませんでした。\n", log),
    /終了コード 1.*pip が PyPI に届きませんでした。$/s);
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

// アプリは、どのコマンドも待っていないダイアログが開くと、スピナーを回し続ける。ダイアログを出すときは、
// 仕事が終わり、ダイアログがすべて閉じるまでコマンドを終えない
test("commandHandler: アプリでは、仕事と結果のダイアログを閉じるまで終えない", async () => {
  const closers = [];
  const session = {
    capabilities: { ui: { elicitation: true } },
    log: async () => { },
    ui: { elicitation: (o) => new Promise((r) => closers.push([o.message, r])) },
  };
  const n = makeNotifier(() => session, {});
  assert.equal(n.waits, true);
  let finishWork;
  const h = commandHandler(async () => {
    await n.tell("始めました");
    await new Promise((r) => { finishWork = r; });
    await n.tell("できました");
  }, n, () => { });
  let ended = false;
  const run = h({ args: "" }).then(() => { ended = true; });
  const tick = () => new Promise((r) => setTimeout(r, 10));
  await tick();
  assert.deepEqual(closers.map((c) => c[0]), ["始めました"]);
  closers[0][1]({ action: "accept" });  // 始めのダイアログを閉じても、仕事が続く間は終えない
  await tick();
  assert.equal(ended, false);
  finishWork();
  await tick();
  assert.deepEqual(closers.map((c) => c[0]), ["始めました", "できました"]);
  assert.equal(ended, false);  // 結果のダイアログが開いている
  closers[1][1]({ action: "accept" });
  await run;
  assert.equal(ended, true);
});

test("commandHandler: CLI(ダイアログを出さない)では待たずに戻り、失敗は onError に渡す", async () => {
  const session = { capabilities: { ui: { elicitation: true } }, log: async () => { }, ui: { elicitation: async () => { } } };
  const n = makeNotifier(() => session, { CF_DISPLAY: "timeline" });
  assert.equal(n.waits, false);
  let release, failed = null;
  const h = commandHandler(async () => {
    await new Promise((r) => { release = r; });
    throw new Error("こわれた");
  }, n, (err) => { failed = err.message; });
  await h({ args: "" });  // 仕事が終わる前に戻る
  assert.equal(failed, null);
  release();
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(failed, "こわれた");
});

test("commandHandler: アプリで仕事が失敗しても、知らせてから終える", async () => {
  const session = { capabilities: { ui: { elicitation: true } }, log: async () => { }, ui: { elicitation: async () => { } } };
  const n = makeNotifier(() => session, {});
  let failed = null;
  await commandHandler(async () => { throw new Error("だめ"); }, n, (err) => { failed = err.message; })({});
  assert.equal(failed, "だめ");
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
  assert.match(first, /^context-foundation: 埋め込みモデル\(intfloat\/multilingual-e5-small、ライセンス MIT/);
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

---
name: context-search-setup
description: Use when the user asks to set up context-foundation (the local code and design-doc search) on this PC, or to point it at an embedding model they already have on disk - "set up context search", "install the search tool", "use my local model", "モデルのパスを指定したい", "セットアップして". Walks the user through the whole setup, asking before each step - installs the Python packages, asks for the folder of the embedding model already on the PC and registers it, then offers to build the index of the workspace. Never downloads the model. Not for searching (use context-search).
---

# context-search-setup

Set up context-foundation interactively, from nothing to a working search.
Ask the user one question at a time with the host's question tool (for
example `ask_user` or `AskUserQuestion`; if there is none, ask in chat and
wait for the reply). Do not skip a question because you can guess the
answer, and do not run a step the user did not agree to.

Run every command in PowerShell from the workspace, as a command of its own
(nothing chained with `;`, `|` or `&&`):

```powershell
python -I "<base directory of this skill>\ctxf.py" <command>
```

`<base directory of this skill>` is the absolute path of the folder that
holds this file. `ctxf.py` is directly in that folder: append the file name
to that path as it is, and do not go up to a parent folder. Below,
`ctxf.py <command>` is short for this whole command line.

Some output is in Japanese. If it is garbled, run
`[Console]::OutputEncoding = [Text.Encoding]::UTF8` once, as a command of
its own, and run the command again. Where this file says how to tell
success from failure, it names the exit code and an English line, so you do
not have to read the Japanese text to decide.

## 1. See what is there

Run `ctxf.py where`. It installs nothing. On a PC that is not set up it
exits with code 4 and prints `python: none: …`: that is expected, not a
failure. Tell the user in two or three lines what it found (the `python:`,
`model:` and `index:` lines), then pick the next step:

| `python:` line | `model:` line | Next |
| --- | --- | --- |
| a path, no `none` | starts with `present:` | step 4 |
| a path, no `none` | anything else | step 3 (the packages are already there) |
| `none: …` and `setup:` ends with `ctxf.py" setup` | | step 2 |
| `none: …` and `setup:` is any other command | | see below |

If `python:` is `none` and the `setup:` line is another command (it names
`setup.bat`, or is a `cd … && python3 -m venv …` line), the user cloned the
repository and its `.venv` next to `ctxf.py` is missing pieces or broken.
That one is repaired only by that command, which you do not run: show the
user the `setup:` line, ask them to run it themselves (`setup.bat` asks for
the model folder too), and when they say it finished, start again at step 1.

## 2. Install the Python packages

Ask the user whether to install now. Say what it does: it downloads about
200 MB of Python packages from PyPI into the folder on the `runtime:` line,
and takes a few minutes. It does not touch the workspace and does not
download the embedding model.

On Windows, in the same question, ask which one to install:

- CPU (default): works on every PC
- GPU (`--gpu`): builds the index faster on a DirectX 12 GPU, using
  onnxruntime-directml

If they agree, run `ctxf.py setup --no-model` (add `--gpu` if they chose
the GPU). Give the command a timeout of at least 15 minutes, or run it in
the background and wait for it; do not start it a second time while it
runs. It ends with `Setup is done` and exit code 0.

- Exit code 3 and `Another setup is running`: an earlier run is still
  going. Wait a minute and run it again; do not delete the lock file unless
  the user confirms no setup is running
- Any other failure: show the last lines of its output (they say the cause
  and the fix) and stop here

If they decline, stop here: say that search works with grep meanwhile and
that they can ask again later.

## 3. Point it at the embedding model

The embedding model makes meaning-based search work (a Japanese or
plain-language question finding English code). It is open-source software
from Hugging Face, outside PyPI. **Never download it**: do not run
`model download` or `index --download`, even if the user agrees to setup.
The user has it on disk already; this step registers that folder.

Run `ctxf.py model info`. Tell the user which model it expects (the
`モデル:` line: name and licence; the `大きさ:` line: size) and which files
the folder must hold: `onnx/model.onnx` (or `model.onnx` directly in the
folder) and `tokenizer.json`.

Then ask the user for the folder of the model on their PC. Offer these
answers:

- the folder path (they type it). The path of the `.onnx` file itself, a
  Hugging Face cache folder (`models--<org>--<name>`), or a folder above
  the model (when only one model is below it) also work. A quantized or
  otherwise renamed ONNX works too: pass the path of that `.onnx` file
- they have the other built-in model instead,
  `ibm-granite/granite-embedding-97m-multilingual-r2`: ask for its folder
  too, and add `--model ibm-granite/granite-embedding-97m-multilingual-r2`
  before the folder in the command below
- skip for now: search is keyword-only (BM25) until a model is set. Go to
  step 4

With a folder, run (put any `--model` option before the folder):

```powershell
python -I "<base directory of this skill>\ctxf.py" model use "<folder>"
```

It finds the model files, loads the model once to check it (a few
seconds), and remembers the folder (and the model, if `--model` was given)
for every workspace on this PC. It copies and downloads nothing. Its last
line says what happened:

- `result: saved`, exit code 0: tell the user it is registered
- `result: not saved (warnings…)`, exit code 4: the check found something
  odd (the `注意:` lines: a file size or an embedding dimension that differs
  from the expected model, which means another version or another model).
  Nothing is saved. Tell the user, and ask whether to use this folder
  anyway. If yes, run the same command with `--yes` added; if not, ask for
  another folder
- `result: not saved (error)`, exit code 2: the message says what is
  missing, which ONNX files it saw, or that several model folders are below
  the given one (it lists them: ask which one). Tell the user, and ask for
  the folder again, or whether to skip

## 4. Check

Run `ctxf.py where` again, and tell the user the `model:` line. If a folder
was registered, it starts with `present:`, names that folder, and says it
was set with `model use`. If it starts with `missing:` instead, the
workspace's `.cf/config.toml` may set another `model` or `model_dir` under
`[embedding]`, which comes first in that workspace: show the user those
lines and ask whether to remove them.

## 5. Build the index (optional)

The search needs an index of the workspace, built once per workspace.

- `index:` names an index already: ask whether to update it. An index built
  before the model was set is keyword-only; updating it adds the meaning
  search. If yes, go to the last bullet
- No index: ask which folders to search (the engine source, reference apps,
  design docs; the workspace itself if they are all in it). Then run
  `ctxf.py init "<folder>" "<folder>"`. It only writes `.cf/config.toml`
  (exit code 0)
- Building: the first index of a large codebase takes tens of minutes
  (about 30 minutes for 8,000 files); later updates take a minute or two.
  Ask how to run it. In Copilot CLI with extensions on, the user can type
  `/context-index` in the session: it runs in the background without the
  model. Or they run `ctxf.py index` in their own terminal (show them the
  whole command line). Run `ctxf.py index` here only if they ask you to,
  with a timeout of at least an hour, or in the background

## 6. Finish

Tell the user in a few lines what is set up (packages, model folder,
index), and that the search tool (MCP) appears only in a new session: a
session that is already running, this one included, keeps searching
keyword-only or without the tool. In this session, use the context-search
skill.

## Changing it later

- Another folder: run `model use "<folder>"` again; it replaces the old one
- Stop using the folder: `ctxf.py model forget` (the files stay on disk)
- A workspace whose `.cf/config.toml` sets `model` or `model_dir` under
  `[embedding]` uses those instead of the registered ones

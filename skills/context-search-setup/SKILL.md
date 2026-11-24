---
name: context-search-setup
description: Use when the user asks to set up context-foundation (the local code and design-doc search) on this PC, or to point it at an embedding model they already have on disk - "set up context search", "install the search tool", "use my local model", "モデルのパスを指定したい", "セットアップして". Walks the user through it step by step, asking before each step - installs the Python packages, then asks for the folder of the embedding model already on the PC and registers it. Never downloads the model. Not for searching (use context-search) or for building an index.
---

# context-search-setup

Set up context-foundation interactively. Ask the user one question at a
time with the host's question tool (for example `ask_user` or
`AskUserQuestion`; if there is none, ask in chat and wait for the reply).
Do not skip a question because you can guess the answer, and do not run a
step the user did not agree to.

Run every command in PowerShell from the workspace, as a command of its own
(nothing chained with `;`, `|` or `&&`):

```powershell
python -I "<base directory of this skill>\ctxf.py" <command>
```

`<base directory of this skill>` is the absolute path of the folder that
holds this file. `ctxf.py` is directly in that folder: append the file name
to that path as it is, and do not go up to a parent folder. Below,
`ctxf.py <command>` is short for this whole command line.

## 1. See what is there

Run `ctxf.py where`. It installs nothing. Tell the user in two or three
lines what it found: the `python:` line (whether the packages are set up),
the `model:` line and the `index:` line.

- If `python:` names a Python with no error and `model:` starts with
  `present:`, setup is already done: say so, and go to step 4
- If `setup:` names `setup.bat` (the user cloned the repository and has a
  `.venv` next to it), do not run step 2: ask the user to double-click
  `setup.bat` themselves (it asks for the model folder too), then go to
  step 3 when they say it finished

## 2. Install the Python packages

Ask the user whether to install now. Say what it does: it downloads about
200 MB of Python packages from PyPI into the user's local app data folder
(`runtime:` in step 1), and takes a few minutes. It does not touch the
workspace and does not download the embedding model.

On Windows, in the same question, ask which one to install:

- CPU (default): works on every PC
- GPU (`--gpu`): builds the index faster on a DirectX 12 GPU, using
  onnxruntime-directml

If they agree, run `ctxf.py setup --no-model` (add `--gpu` if they chose
the GPU). It may take several minutes; let it finish. If it fails, show
the last lines of its output (they say the cause and the fix) and stop
here. If they decline, stop here: say that search works with grep
meanwhile and that they can ask again later.

## 3. Point it at the embedding model

The embedding model makes meaning-based search work (a Japanese or
plain-language question finding English code). It is open-source software
from Hugging Face, outside PyPI. **Never download it**: do not run
`model download` or `index --download`, even if the user agrees to setup.
The user has it on disk already; this step registers that folder.

Run `ctxf.py model info`. Tell the user which model it expects (the
`モデル:` line: name, licence and size) and which files the folder must
hold: `onnx/model.onnx` (or `model.onnx` directly in the folder) and
`tokenizer.json`.

Then ask the user for the folder of the model on their PC. Offer these
answers:

- the folder path (they type it). The path of `model.onnx` itself, a
  Hugging Face cache folder (`models--<org>--<name>`), or a folder above
  the model (when only one model is below it) also work
- they have the other built-in model instead
  (`ibm-granite/granite-embedding-97m-multilingual-r2`): then ask for its
  folder too, and add
  `--model ibm-granite/granite-embedding-97m-multilingual-r2` below. The
  workspace's `.cf/config.toml` must then say
  `model = "ibm-granite/granite-embedding-97m-multilingual-r2"` under
  `[embedding]`; tell the user, and change it only if they agree
- skip for now: search is keyword-only (BM25) until a model is set. Go to
  step 4

With a folder, run:

```powershell
python -I "<base directory of this skill>\ctxf.py" model use "<folder>"
```

It finds the model files in that folder, loads the model once to check it
(a few seconds), and remembers the folder for every workspace on this PC
(the `設定:` line names the file). It copies and downloads nothing.

- Success (`フォルダを覚えました`): tell the user. Pass on every `注意:`
  line: a size or dimension that differs from the expected model means the
  folder may hold another version or another model; ask whether to keep it,
  and if not, run `ctxf.py model forget` and ask for another folder
- Failure: the message says what is missing (and lists the ONNX files it
  saw), or that several model folders are below the given one (and lists
  them: ask which one). Tell the user, and ask for the folder again, or
  whether to skip.
  If the ONNX file has another name (a quantized one, for example), the
  workspace's `.cf/config.toml` needs `onnx_file = "<name>"` under
  `[embedding]`; offer that edit and make it only if they agree

## 4. Check and finish

Run `ctxf.py where` again, and tell the user the `model:` line. If a folder
was registered, it starts with `present:` and names that folder, marked as
set with `model use`.

Then say what comes next, without running it:

- No index yet (`index: none`): an index is built per workspace. In
  Copilot CLI with extensions on, the user can type `/context-index` in the
  session; otherwise ask which folders to search, then the commands are
  `ctxf.py init "<folder>" "<folder>"` and `ctxf.py index`, which take tens
  of minutes on a large codebase (suggest the user runs them in their own
  terminal)
- An index that was built before the model was set is keyword-only:
  `ctxf.py index` again adds the embeddings
- The search tool (MCP) appears only in a new session. In this session,
  use the context-search skill

## Changing it later

- Another folder: run `model use "<folder>"` again; it replaces the old one
- Stop using the folder: `ctxf.py model forget` (the files stay on disk)
- A workspace whose `.cf/config.toml` sets `model_dir` under `[embedding]`
  uses that folder instead of the registered one

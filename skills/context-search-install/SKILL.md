---
name: context-search-install
description: Use when the user types /context-search-install (optionally with gpu, cpu or help), or asks to install only the Python packages of context-foundation (the local code and design-doc search) and check the `python` on PATH, without the embedding model and without an index - "install the search packages", "パッケージだけ入れて", "モデルと索引以外をセットアップして". Installs the Python packages (setup --no-model) and checks that the `python` command the plugin starts with works. Never downloads or registers a model and never builds an index (use context-search-setup for those). Not for searching (use context-search).
---

# context-search-install

The command form of the setup, for everything except the embedding model and
the index: it installs the Python packages and checks the `python` on PATH
that the plugin's MCP server and session hook start with.

```text
/context-search-install        install or check the packages, check python
/context-search-install gpu    use onnxruntime-directml (Windows only)
/context-search-install cpu    use the CPU build of onnxruntime
/context-search-install help   explain this command, run nothing
```

Without `gpu` or `cpu`, setup keeps the build chosen at the last setup (the
CPU build on a first install).

Run every command in PowerShell from the workspace, as a command of its own
(nothing chained with `;`, `|` or `&&`):

```powershell
python -I "<base directory of this skill>\ctxf.py" <command>
```

`<base directory of this skill>` is the absolute path of the folder that
holds this file. `ctxf.py` is directly in that folder: append the file name
to that path as it is, and do not go up to a parent folder. Below,
`ctxf.py <command>` is short for this whole command line. On Linux or macOS,
use the shell of the host and `/` in the path instead.

Some output is in Japanese. If it is garbled, run
`[Console]::OutputEncoding = [Text.Encoding]::UTF8` once, as a command of
its own, and run the command again. This file decides on exit codes and
English lines only.

## What this command never does

Run only `ctxf.py where` and `ctxf.py setup --no-model` (with `--gpu` or
`--cpu`). Do not run `init`, `index`, `model use`, `model download`,
`model forget` or `index --download` yourself, even if the user asks for
them in the same message, and even though setup ends with a line saying
`Next, build an index`: that line is for the user. Step 4 tells the user how
to do those. Do not change PATH, the app execution aliases or any other
system setting either; the user does it.

## 0. Arguments

Read the words after `/context-search-install`:

- none: no option (setup keeps the last choice)
- `gpu` (or `--gpu`): add `--gpu`. Windows only; on another system, say that
  it exists for Windows only and stop
- `cpu` (or `--cpu`): add `--cpu`
- `help`: tell the user what this file's first paragraph and the list above
  say, and stop. Run nothing
- `gpu` and `cpu` together, or any other word: say which words it takes
  (the list above) and stop. Run nothing

## 1. See what is there

Run `ctxf.py where`. It installs nothing. On a PC that is not set up it
exits with code 4: that is expected, not a failure. Keep its output: step 3
reads its `PATH python:` line.

If the command itself does not start (`python` is not recognized, or it
prints that Python was not found and offers the Microsoft Store), there is
no output to read. The plugin cannot work until the `python` command starts
Python, and this skill cannot fix that: go to step 3, give the user the fix
under "did not start", and stop there.

Then look at the line that starts with `python:` (not `PATH python:`) and,
for some rows, the `model:` line:

| `python:` line | Next |
| --- | --- |
| a path and `(runtime)` | step 2 (setup checks it, or installs what changed) |
| `none: …` and `setup:` ends with `ctxf.py" setup` | step 2 |
| a path and `(local .venv)` | do not run setup; see below |
| `none: …` and `setup:` is any other command | do not run setup; see below |
| a path and `(CF_PYTHON)` | do not run setup; see below |

- `(local .venv)`: the user cloned the repository, and the `.venv` next to
  `ctxf.py` comes first. `ctxf.py setup` would install into another folder
  that is not used. If the `model:` line starts with `unknown (`, that
  `.venv` cannot load its packages: show the user the `setup:` line and ask
  them to run it themselves. Otherwise the packages load; to install them
  again, the user runs the `setup:` line themselves. Go to step 3
- `none: …` with another `setup:` command: that `.venv` is missing pieces or
  broken. It is repaired only by the `setup:` command, which you do not run:
  show the user that line and ask them to run it themselves. Go to step 3
- `(CF_PYTHON)`: the environment variable `CF_PYTHON` names the Python to
  use, so setup is not used and the packages were not checked by setup. If
  the `model:` line starts with `unknown (`, that Python does not start or
  cannot load the packages: show the user the `python:` and `model:` lines,
  and say that `CF_PYTHON` must name a Python with the packages, or be
  removed so that setup is used. Go to step 3

## 2. Install the Python packages

Before running setup, check two things.

- **An index build.** Setup can replace or remove packages that a running
  index build uses (with `gpu` or `cpu`, or after an update that changed the
  dependency list). If an index build may be running on this PC (you or the
  user started `/context-index`, `index.bat` or the index command in a
  terminal, and it has not reported that it ended), do not run setup: tell
  the user to run this command again after the index build ends, and go to
  step 3
- **Consent.** The user typing `/context-search-install` is their consent.
  If this skill was loaded because the user asked in their own words
  instead, and step 1 said `none: …` or `gpu`/`cpu` was given, ask once with
  the host's question tool (for example `ask_user` or `AskUserQuestion`; if
  there is none, ask in chat and wait): say that it downloads about 200 MB
  of Python packages from PyPI and takes a few minutes, and on Windows ask
  CPU or GPU in the same question (CPU: add `--cpu`; GPU: add `--gpu`). If
  they decline, go to step 3. When step 1 said `(runtime)` and no `gpu` or
  `cpu` was given, setup only checks, so do not ask

Run `ctxf.py setup --no-model`, with `--gpu` or `--cpu` if chosen. Give the
command a timeout of at least 15 minutes, or run it in the background and
wait for it; do not start it a second time while it runs. When the packages
are already there, it only checks them and ends in a few seconds.

- Exit code 0 and `Setup is done`: the packages are installed. Its
  `Python packages:` line says whether they were installed now or already
  there, and whether it is the GPU build (`onnxruntime-directml`)
- Exit code 3 and `Another setup is running`: an earlier run (or
  `/context-setup`) is still going. Wait a minute and run it again, at most
  three times. If it still says so, show the user the lock file named in
  that line, and ask them to delete it only if no setup is running; do not
  delete it yourself
- Any other exit code: show the user the last lines of the output (they say
  the cause and the fix: PyPI not reachable, no build for this Python, a
  path too long, `--gpu` off Windows) and go to step 3

`--no-model` keeps setup from reporting the embedding model: the model is
not part of this command.

## 3. Check the `python` on PATH

The plugin's MCP server and session hook start with the command `python`,
not with the Python of the packages. Read the `PATH python:` line from the
output of step 1:

- `PATH python: <path>  (Python 3.x.y)`: it works. Tell the user the path and
  the version
- `PATH python: <path>  (does not start)`: tell the user what the next line
  says. If the path is under `WindowsApps`, that is the Microsoft Store
  shortcut, not Python
- `PATH python: none.`: no `python` on PATH. Tell the user what the line says
- did not start (step 1 had no output): the command `python` is missing, or
  on Windows it is the Microsoft Store shortcut. Tell the user the fix for
  their system:
  - Windows: run the python.org installer again, choose Modify, and turn on
    "Add Python to environment variables". If the Store shortcut comes
    first, also turn off `python.exe` under Settings > Apps >
    Advanced app settings > App execution aliases
  - Linux or macOS: make the command `python` start Python 3.11 to 3.14
    (for example, Debian and Ubuntu have the package `python-is-python3`)

After a fix, the user opens a new terminal and checks that
`python --version` prints a version, then types this command again.

## 4. Check and finish

If step 2 ran setup and it ended with code 0, run `ctxf.py where` again: the
`python:` line now names a path, with no `none`. If it still starts with
`none`, show the user that line and the `setup:` line, and stop.

Tell the user in a few lines:

- the packages: installed now, already there (CPU or GPU build), left to
  the user (the `setup:` line), not checked because `CF_PYTHON` is set, not
  run because an index build was running, declined, or failed (with the
  reason)
- the `python` on PATH: works, or what to fix
- what this command does not do (the user does these):
  - the embedding model (meaning-based search). Without it, search is
    keyword-only (BM25). To register a model already on the PC, ask for the
    context-search-setup skill, or run `ctxf.py model use "<folder>"` in a
    terminal. To download it, check its source and licence first, then run
    `ctxf.py model download` in a terminal
  - the index: type `/context-index` in Copilot CLI with extensions on, or
    run `ctxf.py init "<folder>"` and then `ctxf.py index` in a terminal
- the search tool (MCP) appears only in a new session; a session that is
  already running, this one included, keeps working without it

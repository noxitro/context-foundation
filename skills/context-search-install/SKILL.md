---
name: context-search-install
description: Use when the user types /context-search-install (optionally with gpu, cpu or help), or asks to install only the Python packages of context-foundation (the local code and design-doc search) and check the `python` on PATH, without the embedding model and without an index - "install the search packages", "パッケージだけ入れて", "モデルと索引以外をセットアップして". Installs the Python packages (setup --no-model) and checks that the `python` command the plugin starts with works. Never downloads or registers a model and never builds an index (use context-search-setup for those). Not for searching (use context-search).
---

# context-search-install

The command form of the setup, for everything except the embedding model and
the index: it installs the Python packages and checks the `python` on PATH
that the plugin's MCP server and session hook start with. It does not
download, register or check the embedding model, and does not create a
config or an index.

```text
/context-search-install        install the packages (CPU), check python
/context-search-install gpu    the same with onnxruntime-directml (Windows)
/context-search-install cpu    switch back to the CPU build of onnxruntime
/context-search-install help   explain this command, run nothing
```

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
its own, and run the command again. This file decides on exit codes and
English lines only.

## 0. Arguments and consent

Read the words after `/context-search-install`:

- none: CPU, the default
- `gpu` (or `--gpu`): add `--gpu` to the setup command. Windows only; on
  another system, say that it exists for Windows only and stop
- `cpu` (or `--cpu`): add `--cpu`
- `help`: tell the user what this file's first paragraph and the list above
  say, and stop. Run nothing
- `gpu` and `cpu` together, or any other word: say which words it takes
  (the list above) and stop. Run nothing

The user typing `/context-search-install` is their consent to the download
in step 2 (about 200 MB of Python packages from PyPI). If this skill was
loaded because the user asked in their own words instead, ask once before
step 2, with the host's question tool (for example `ask_user` or
`AskUserQuestion`; if there is none, ask in chat and wait): say that it
downloads about 200 MB from PyPI and takes a few minutes, and on Windows
ask CPU or GPU in the same question. If they decline, do step 1 and step 3
only.

## 1. See what is there

Run `ctxf.py where`. It installs nothing. On a PC that is not set up it
exits with code 4: that is expected, not a failure. Keep its output: step 3
reads the `PATH python:` line from it.

If the command itself does not start (`python` is not recognized, or it
prints that Python was not found and offers the Microsoft Store), the
`python` on PATH is missing or is the Store shortcut. The plugin cannot
work until that is fixed, and this skill cannot fix it: go to step 3 and
give the user the fix for `none` or `does not start`, then stop.

Then look at the line that starts with `python:` (not `PATH python:`):

| `python:` line | Next |
| --- | --- |
| a path and `(runtime)` | step 2 (setup checks it, or installs what changed) |
| `none: …` and `setup:` ends with `ctxf.py" setup` | step 2 |
| a path and `(local .venv)` | do not run setup; see below |
| `none: …` and `setup:` is any other command | do not run setup; see below |
| a path and `(CF_PYTHON)` | do not run setup; see below |

- `(local .venv)`: the user cloned the repository, and the packages are in
  the `.venv` next to `ctxf.py`, which comes first. `ctxf.py setup` would
  install into another folder that is not used. Tell the user the packages
  are there; to install them again, they run `setup.bat` (the `setup:` line)
  themselves. Go to step 3
- `none: …` with another `setup:` command: that `.venv` is missing pieces or
  broken. It is repaired only by the `setup:` command, which you do not run:
  show the user that line and ask them to run it themselves. Go to step 3
- `(CF_PYTHON)`: the environment variable `CF_PYTHON` names the Python to
  use, so setup is not used. Tell the user, and go to step 3

## 2. Install the Python packages

Run `ctxf.py setup --no-model`, with `--gpu` or `--cpu` if step 0 chose one.
Give the command a timeout of at least 15 minutes, or run it in the
background and wait for it; do not start it a second time while it runs.
When the packages are already there, it only checks them and ends in a few
seconds.

- Exit code 0 and `Setup is done`: the packages are installed. Its
  `Python packages:` line says whether they were installed now or already
  there
- Exit code 3 and `Another setup is running`: an earlier run (or
  `/context-setup`) is still going. Wait a minute and run it again; do not
  delete the lock file unless the user confirms no setup is running
- Any other exit code: show the user the last lines of the output (they say
  the cause and the fix: PyPI not reachable, no build for this Python, a
  path too long) and go to step 3

`--no-model` keeps setup from reporting the embedding model: the model is
not part of this command.

## 3. Check the `python` on PATH

The plugin's MCP server and session hook start with the command `python`,
not with the Python of the packages. Read the `PATH python:` line from the
output of step 1:

- `PATH python: <path>  (Python 3.x.y)`: it works. Tell the user the path and
  the version
- `PATH python: <path>  (does not start)`, or the command did not start in
  step 1: tell the user what the next line says. If the path is under
  `WindowsApps`, it is the Microsoft Store shortcut, not Python: turn off
  `python.exe` under Settings > Apps > Advanced app settings >
  App execution aliases. Otherwise, add Python to PATH (run the python.org
  installer again, choose Modify, and turn on "Add Python to environment
  variables")
- `PATH python: none.`: no `python` on PATH. Tell the user to add Python to
  PATH, as above

Do not change PATH, the app execution aliases or any system setting
yourself; the user does it. After a fix, the user opens a new terminal and
checks that `python --version` prints a version.

## 4. Check and finish

If step 2 ran setup and it ended with code 0, run `ctxf.py where` again: the
`python:` line now names a path, with no `none`.

Tell the user in a few lines:

- the packages: installed now, already there, left to `setup.bat`, or
  failed (with the reason)
- the `python` on PATH: works, or what to fix
- what this command does not do:
  - the embedding model (meaning-based search). Without it, search is
    keyword-only (BM25). To register a model already on the PC, ask for the
    context-search-setup skill, or run `ctxf.py model use "<folder>"`. Do not
    run `model download` or `index --download` here, even if asked in the
    same message: say the user can run it in their own terminal after
    checking the model's source and licence
  - the index: type `/context-index` in Copilot CLI with extensions on, or
    run `ctxf.py init "<folder>"` and then `ctxf.py index`, in a terminal
- the search tool (MCP) appears only in a new session; a session that is
  already running, this one included, keeps working without it

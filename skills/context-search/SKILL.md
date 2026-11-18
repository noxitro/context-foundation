---
name: context-search
description: Use first when you must find WHERE something is implemented or documented but do not know its name or file - the question uses another engine's or library's term, a plain-language or Japanese description ("which part handles X", "X に相当する処理はどこ"), or asks for the design doc behind some code. Searches a local index of the code and design docs by meaning and keywords, and returns ranked passages with path and line range. Needs a .cf folder (index) in the workspace. Not when you already know the identifier (where is Foo::Bar defined or used) - use grep or a reference index for that.
---

# context-search

Run it in PowerShell from the workspace, as a command of its own (nothing
chained with `;`, `|` or `&&`):

```powershell
python -I "<base directory of this skill>\ctxf.py" search "<question>"
```

- `<base directory of this skill>`: the absolute path of the folder that
  holds this file (shown with this skill as its base directory). `ctxf.py`
  is directly in that folder: append the file name to that path as it is,
  and do not go up to a parent folder
- `<question>`: what you are looking for. When the code is written in
  English and the question is not, put the English terms into the query:
  translate the key words, add likely identifiers, and keep the user's
  wording after them. A query in Japanese alone finds English code poorly
- Options: `--top-k <n>` (default 8), `--mode hybrid|keyword|semantic`
  (default hybrid), `--source <id>` (search one source only)
- One call returns the passages themselves. Run it once per distinct
  question; do not grep for the same thing meanwhile
- If Japanese text in the result is garbled, run
  `[Console]::OutputEncoding = [Text.Encoding]::UTF8` once, as a command of
  its own, then search again (the output is UTF-8)

## Reading the result

- Each hit is `[rank] source:path:start-end (kind)`, then `scope:` (the
  enclosing namespace, class or heading), `sig:` (the declaration) and the
  passage text. Lower-ranked hits may list the location only
- The text is the indexed content of those lines. Use it as evidence and
  cite it as `source:path:start-end`. Open the file only if you need
  lines outside the passage
- `[changed since index]` means the file was edited after indexing; open
  it to check the current lines. `[file no longer exists]` means it was
  deleted; its text is not shown, do not cite it. Files added after
  indexing are not searched: grep for those
- `note:` lines say what the tool did with the query (for example, the
  glossary terms it added) or why it fell back to keyword search
- `source` is an alias, not a folder name. A `roots:` line names the
  folder of a source that is not the workspace itself; `ctxf.py status`
  lists every source with its folder
- Results are candidates ranked by similarity, not proof. If none fits,
  rephrase once (other terms, English), then fall back to grep

## When it is not ready

Each of these steps changes the user's PC or takes time. Ask the user
before running one, say what it does, and run it only after they agree.

- "is not set up on this PC", "not set up completely", "set up for an
  older version" or "set up with a Python that is no longer installed": the
  Python packages or the embedding model are missing or out of date.
  `python -I "<...>\ctxf.py" setup` downloads about 200 MB of packages from
  PyPI and a 490 MB model from Hugging Face into the user's local app data
  folder, and takes a few minutes. `setup --help` only prints what it does.
  After setup, the search tool appears only in a new session; in this
  session, keep using this skill
- "No context-foundation index": ask which folders to search (the engine
  source, reference apps, design docs). Then suggest the user type
  `/context-index "<folder>" "<folder>"` in the session: it builds the
  index in the background without calling the model (it needs the
  `EXTENSIONS` feature flag of Copilot CLI). Without it, the commands
  are `python -I "<...>\ctxf.py" init "<folder>" "<folder>"`, then
  `python -I "<...>\ctxf.py" index`, in the workspace. The first index of
  a large codebase takes tens of minutes (about 30 minutes for 8,000
  files); suggest the user run them in their own terminal. Later runs
  (`/context-index` with no folders) reuse the embeddings of unchanged
  passages; when few files changed, they take a minute or two
- Meanwhile, search with grep as usual

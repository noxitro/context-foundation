---
name: context-search
description: Use first when you must find WHERE something is implemented or documented but do not know its name or file - the question uses another engine's or library's term, a plain-language or Japanese description ("which part handles X", "X に相当する処理はどこ"), or asks for the design doc behind some code. Searches a local index of the code and design docs by meaning and keywords, and returns ranked passages with path and line range. Needs a .cf folder (index) in the workspace. Not when you already know the identifier (where is Foo::Bar defined or used) - use grep or a reference index for that.
---

# context-search

Run it in PowerShell from the workspace, as a command of its own (nothing
chained with `;`, `|` or `&&`):

```powershell
python -I "<folder of this SKILL.md>\..\..\ctxf.py" search "<question>"
```

- `<folder of this SKILL.md>`: the absolute path of the folder that holds
  this file
- `<question>`: what you are looking for. When the code is written in
  English and the question is not, put the English terms into the query:
  translate the key words, add likely identifiers, and keep the user's
  wording after them. A query in Japanese alone finds English code poorly
- Options: `--top-k <n>` (default 8), `--mode hybrid|keyword|semantic`
  (default hybrid), `--source <id>` (search one source only)
- One call returns the passages themselves. Run it once per distinct
  question; do not grep for the same thing meanwhile

## Reading the result

- Each hit is `[rank] source:path:start-end (kind)`, then `scope:` (the
  enclosing namespace, class or heading), `sig:` (the declaration) and the
  passage text. Lower-ranked hits may list the location only
- The text is the indexed content of those lines. Use it as evidence and
  cite it as `source:path:start-end`. Open the file only if you need
  lines outside the passage
- `[changed since index]` means the file was edited after indexing; open
  it to check the current lines
- `note:` lines say what the tool did with the query (for example, the
  glossary terms it added) or why it fell back to keyword search
- `source` is an alias, not a folder name. `ctxf.py status` lists the
  sources
- Results are candidates ranked by similarity, not proof. If none fits,
  rephrase once (other terms, English), then fall back to grep

## When it is not ready

Each of these steps changes the user's PC or takes time. Ask the user
before running one, say what it does, and run it only after they agree.

- "is not set up on this PC", "not set up completely" or "set up for an
  older version": the Python packages or the embedding model are missing or
  out of date. `python -I "<...>\ctxf.py" setup` downloads about 200 MB of
  packages from PyPI and a 490 MB model from Hugging Face into the user's
  local app data folder, and takes a few minutes. `setup --help` only
  prints what it does
- "No context-foundation index": ask which folders to search (the engine
  source, reference apps, design docs). In the workspace, run
  `python -I "<...>\ctxf.py" init "<folder>" "<folder>"`, then
  `python -I "<...>\ctxf.py" index`. The first index of a large codebase
  takes tens of minutes (about 30 minutes for 8,000 files); suggest the
  user run the same command in their own terminal. Later runs reuse the
  embeddings of unchanged passages; when few files changed, they take a
  minute or two
- Meanwhile, search with grep as usual

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
- `<question>`: what you are looking for, in the user's words. Japanese is
  fine. If you know likely English terms or identifiers, add them to the
  same query
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
- `source` is an alias, not a folder name. `ctxf.py status` lists the
  sources
- Results are candidates ranked by similarity, not proof. If none fits,
  rephrase once (other terms, English), then fall back to grep
- "No context-foundation index": search with grep as usual. Building an
  index takes minutes; ask the user first

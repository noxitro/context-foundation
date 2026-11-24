# context-foundation

An independent retrieval foundation that helps AI agents find relevant evidence
in large codebases and design documents.

`context-foundation` complements tools such as cxxref: cxxref can identify
precise C++ references, while this project aims to discover relevant passages
from natural-language questions. It is not intended to replace reference
analysis.

日本語の説明(目的・導入・使い方)は [readme-ja.md](readme-ja.md) にあります。

## How it was built

1. **Define the scope and evaluation.** Select a corpus and prepare 20–30
   representative questions. Annotate the relevant files and line ranges for
   each question, then record results from the existing search workflow,
   including task time and answer quality.
2. **Build retrieval only.** Ingest code and documents and return ranked
   passages with file paths, line ranges, and supporting text. Preserve
   syntactic units when chunking code and use document headings as chunk
   boundaries. Compare keyword and semantic retrieval before committing to an
   embedding model or vector database.
3. **Provide a read-only agent interface.** Expose retrieval to the agent
   without giving it anything else to do. Leave answer generation and code
   changes to the agent. Every build re-chunks changed files; embeddings of
   unchanged passages are reused from the previous index.
4. **Compare with the existing workflow.** Run the same questions against
   the existing workflow (grep and file reads) and with this tool added.
   Evaluate evidence found in the top results, answer correctness, task time,
   search latency, and index creation and update costs. The comparison is
   made on public C++ game engines (Godot and O3DE); this project does not
   evaluate on private codebases.

## Target surfaces

The first targets are **GitHub Copilot CLI** and the **GitHub Copilot app**
(the desktop app, which is built on the CLI and uses the skills and MCP
servers configured for it). Visual Studio Code comes later: the MCP server
is the piece meant for it, but it has not been tried there.

The repository is a Copilot plugin (`plugin.json`, Agent Plugins 1.0) and a
plugin marketplace of its own (`.github/plugin/marketplace.json`):

```text
copilot plugin marketplace add noxitro/context-foundation
copilot plugin install context-foundation@context-foundation
```

The plugin adds a read-only **MCP server** with two tools, `search` and
`status`, and a **session-start hook** that tells the agent to search before
grepping, only in workspaces that have an index. A **skill**
(`context-search`) that runs the same search through the command line stays
as a fallback for when the MCP tools are not available. A Copilot CLI
**extension** adds two slash commands that the user types and that run in
the background without calling the model: `/context-setup` installs the
Python dependencies, and `/context-index` builds or
updates the index (extensions are experimental in Copilot CLI and load after
`/settings experimental on`). Python dependencies
(about 200 MB) and the embedding model (about 490 MB) live outside the
plugin, in a per-user folder. `/context-setup` (or `python -I ctxf.py setup`)
installs the Python dependencies once. It does not install the embedding
model: the model is open-source software from outside PyPI (Hugging Face),
and whether it may be used depends on the user's environment (licence,
terms of use, network). Setup only reports that the model is not installed,
with its source and licence; after checking, the user runs
`python -I ctxf.py model download` themselves. If the model is already on
the PC, `python -I ctxf.py model use "<folder>"` checks that folder and
remembers it for every workspace instead. A second skill,
**`context-search-setup`**, walks the user through all of this in the
session: it asks before installing the packages, asks for the folder of the
model already on disk and registers it (it never downloads the model), and
offers to build the index.
Without the model, search is
keyword-only. With these, everything from installing
the plugin (`/plugin install`) to building the index can be done inside a
Copilot CLI session.
Until version 0.2 the skill was the default; why the MCP server replaced it
is in [docs/copilot.md](docs/copilot.md).

How to install either way, the first-time setup, and what was verified are in
[docs/copilot.md](docs/copilot.md).

## Evaluation requirements

The evaluation data format is described in
[`docs/evaluation-data-spec.md`](docs/evaluation-data-spec.md). Set measurable
success thresholds before running the comparison. The provisional thresholds
are: at least one relevant passage in the top five for 80% of questions; an
acceptable answer for at least 80% of questions; quality no lower than the
existing workflow with at least 20% lower median task time or cost; and p95
search latency of no more than three seconds. Measure index creation and
update cost, but set a hard limit after the corpus and workflow are
understood. These thresholds are an initial decision gate, not a statistical
claim.

The retrieval tool keeps source content local: indexing and embedding run on
the local machine and nothing is sent to an external service. An agent that
uses the tool is a separate matter. The passages it reads, like the files it
opens, go to the model provider the agent host is configured with. Decide
which provider may receive a private corpus before running agents on it.
When a source root lies outside the workspace folder, its absolute path
(which may contain your user name) appears in the `roots:` line of every
search result and in `status`, so it reaches the model too.

Create the `.cf` folder yourself. If a fetched repository comes with a
`.cf` folder, delete it and build your own: the configuration can point at
folders outside the workspace (source roots, a model folder, a glossary).

Keep the questions, annotations, paths, and search results of a private
corpus out of this public repository. The specification uses generic field
names and placeholders only. Two sets of cases are committed here: the one
under `tests/fixtures`, for a synthetic corpus written for the tests, and the
worked example under [`examples/o3de`](examples/o3de/README.md), for a public
open-source corpus. The example holds questions and the locations of their
answers, not source text.

## Scope boundaries

This tool does not include:

- Replacing precise C++ reference analysis such as cxxref
- A production service shared across multiple repositories
- Training a custom model
- An organization-wide service with access-control management
- A dependency on Git: a Perforce workspace or a plain folder is indexed the
  same way

## Repository layout

| Path | Contents |
| --- | --- |
| `src/context_foundation` | The tool: ingest, chunking, index, search, evaluation, MCP server |
| `skills/context-search` | The skill that tells an agent when and how to search |
| `skills/context-search-setup` | The skill that sets the tool up end to end, asking the user (packages, the folder of a model already on disk, the index) |
| `plugin.json`, `.github/plugin`, `com.github.copilot` | The Copilot plugin, its marketplace entry, the session-start hook, and the `/context-index` extension |
| `ctxf.py` | Launcher (standard library only): finds the Python environment and runs the tool; `setup` prepares it |
| `bench` | Runs the agent methods with Copilot CLI and records the results |
| `tests` | Tests and a small synthetic corpus (C++, C#, Markdown) |
| `examples/o3de` | Worked evaluation example on a public C++ game engine: questions, answer locations, measured results |
| `docs` | Evaluation specification, design notes, Copilot setup, comparison runbook |
| `CHANGELOG.md` | Changes per version and what to do after updating |
| `*.bat` | Double-click entry points for Windows |

## Status

The tool (version 0.4) is past the proof-of-concept stage: it installs as a
Copilot plugin, sets itself up and builds its index from inside a Copilot CLI
session, and has been compared with the existing workflow on two public C++
game engines. It provides:

- the evaluation data format, schemas and validation;
- ingest, syntax-aware chunking for C, C++ and C# and heading-based chunking
  for Markdown, keyword, semantic and hybrid search over a single SQLite
  file;
- incremental index updates that reuse the embeddings of unchanged passages
  (the passages match a full rebuild; on a public codebase the vectors and the
  top-10 rankings matched too, though bit-exact vectors are not guaranteed);
- a glossary that adds English terms to a Japanese-only question before
  searching (a built-in one for general terms and one per workspace);
- passages without content (an include block, namespace braces, forward
  declarations) are kept out of the semantic ranking;
- the Copilot plugin and marketplace (MCP server, hook and fallback skill),
  `/context-setup` and `/context-index` in Copilot CLI, and registration from
  a local clone;
- an evaluation runner for the retrieval-only methods and a runner for the
  agent methods.

### Validation on public C++ game engines

The agent with this tool (the current form: an MCP server and a session-start
hint, since version 0.3) was compared with the same agent using grep and file
reads only, on held-out questions, three runs per question, with Copilot CLI
and Claude Haiku 5.5 (answers graded by AI against answer points fixed
beforehand; 80% acceptable answers is the threshold):

| Engine | Acceptable answers (this tool / grep only) | Median time | Median cost | Verdict |
| --- | --- | --- | --- | --- |
| Godot (29 questions, version 0.3) | 93.1% / 88.5% (reviewed key) | 35% lower | 32% lower | Meets the thresholds |
| O3DE (27 questions, version 0.4.9) | 95.1% / 90.1% | 12% lower | 14% lower | Quality holds; time and cost about a tenth lower |

- On O3DE, version 0.4.7 cut time and cost by about a third, but it more
  often answered from the returned passages without opening the file, and
  missed a related point nearby (77.8% acceptable answers). Version 0.4.9
  lists the other functions of the same file next to the top passages and
  asks the agent to check related code before answering. Answer quality is
  no longer below grep, and most of the time and cost gain is given back:
  answer quality comes first (see [examples/o3de](examples/o3de/README.md)).
  The figures come from runs interleaved with grep in the same time window;
  a comparison with grep answers from another day gave 4% and 3% lower, so
  these figures move from day to day.
- Godot has not been measured again with version 0.4.9.
- On O3DE, a harder set of 40 questions was also written, where a full answer
  must name the lower layer, the callee or the other variant (questions and
  answer points written and checked by AI; not yet reviewed by a person).
  Version 0.4.9 leans above grep there too: 82.5% against 72.5% with Claude
  Sonnet 5.5 (time 10% and cost 11% lower), and 90.0% against 85.0% with
  Haiku when Claude Code is the agent instead of Copilot CLI. The 95%
  interval of the difference excludes zero for a count of the expected
  identifiers in the answer, not for the AI grade. A better search hit rate
  (the granite embedding model) and a language server (clangd) given as a
  tool did not improve the answers ([docs/benchmarks.md](docs/benchmarks.md),
  in Japanese).
- On Godot, the answer key as written beforehand puts both methods below 80%
  (75.9% and 74.7%); a review of the key made after the results, by reviewers
  who had not seen them, gives the figures above, and is the one taken for
  the verdict. An earlier form (the skill, up to version 0.2) showed no gain
  on either engine.
- Searching alone, with the question as the query, does not meet its
  threshold: a relevant passage is in the top five for 50% (Godot) and 54%
  (O3DE) of questions. Search latency is well within the three-second
  threshold, including on part of a larger engine codebase that is not
  public (245,457 passages).
- The evaluation is limited to public C++ engines, which the model partly
  remembers (an advantage for grep), mostly to one model (Claude Haiku 5.5;
  the harder set also with Sonnet 5.5), and to AI grading.
  Private codebases are not evaluated by this project.

What has been measured so far, and what has not, is listed in
[docs/benchmarks.md](docs/benchmarks.md) (in Japanese).

Version 0.4.0 hardens the tool for production use (safety of the workspace
configuration, ingest rules, resuming an interrupted build, an output that
fits its budget, clearer setup failures). The changes, and what to do
after updating, are in [CHANGELOG.md](CHANGELOG.md) (in Japanese). They were
run by hand on Windows with Python 3.13; other systems and Python versions
are covered by the CI tests (and the tests were also run on Ubuntu under WSL
with Python 3.12), and version 0.4.0 has not been tried in Copilot yet.
Version 0.4.1 adds `/context-index`. Version 0.4.2 fixes it for Copilot CLI
1.0.113, which did not load it. In Copilot CLI 1.0.113 and 1.0.112 on
Windows, building and rebuilding the index worked with no model calls.
Version 0.4.3 adds `/context-setup`; in Copilot CLI 1.0.114 on Windows, the
setup and the first index worked from inside one session with no model
calls. Version 0.4.4 lets the first `/context-index` without folders show a
checklist of the workspace's folders to pick from. Version 0.4.7 stops the
MCP server from offering its tools in workspaces without an index. Neither
command has been tried in the app yet. Version 0.4.9 adds the related-code
change described above.

How to run it in production (setup, configuration, operation, limits) is in
[docs/production.md](docs/production.md) (in Japanese).

## Supported environments

| | Supported | Verified |
| --- | --- | --- |
| OS | Windows; the Python part also runs on Linux (the `.bat` files are for Windows) | Windows 11 by hand; Windows and Ubuntu in CI |
| Python | 3.11 to 3.14 (onnxruntime has no build for 3.15 yet) | 3.13 by hand; 3.11, 3.12, 3.13 and 3.14 in CI |
| Agent host | GitHub Copilot CLI and the Copilot app | Copilot CLI 1.0.112 (up to version 0.3); the app has not been tried |

The plugin starts its MCP server and hook with a command named `python`,
which must be on `PATH`. Systems that only have `python3` need a `python`
command as well. The fallback skill is written for PowerShell. Setup,
troubleshooting, what is stored where and how to remove it are in
[docs/copilot.md](docs/copilot.md) (in Japanese).

Setup installs the Python packages with pip, so `HTTPS_PROXY` and
`PIP_INDEX_URL` (a PyPI mirror) apply. On a PC without internet access,
download the packages on a connected PC with the same OS and Python version
(`pip download -d <wheels> <the dependencies in pyproject.toml>`), copy the
folder, and run setup with `PIP_NO_INDEX=1` and `PIP_FIND_LINKS=<wheels>`.
The model folder can be copied from another PC; the runtime virtual
environment cannot, because it records the path of its base Python.

## Reporting problems

Open an issue at <https://github.com/noxitro/context-foundation/issues>, with
the output of `python -I <plugin folder>/ctxf.py where` and the tool version.
The `where` output contains absolute paths (your user name, the workspace
folder); mask them before posting. Never paste source code, search results,
indexes or evaluation questions from a private corpus into an issue: issues
are public. Use the synthetic corpus under `tests/fixtures` or public code to
reproduce a problem. For a problem that could leak secrets, do not describe it
in an issue; use the repository's Security tab to report it privately, or open
an issue that only says you have something to report privately.

On the worked example for a public C++ game engine
([`examples/o3de`](examples/o3de/README.md)), the retrieval-only methods do
not reach the provisional threshold: the default hybrid search has a relevant
passage in the top five for 54% of the held-out questions, and for 22% of
the questions that describe behaviour without naming an identifier. On this
example, the correctness of answers written by an agent that uses the search
has not been measured.

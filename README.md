# context-foundation

An independent retrieval foundation that helps AI agents find relevant evidence
in large codebases and design documents.

`context-foundation` complements tools such as cxxref: cxxref can identify
precise C++ references, while this project aims to discover relevant passages
from natural-language questions. It is not intended to replace reference
analysis.

日本語の説明(目的・導入・使い方)は [README.ja.md](README.ja.md) にあります。

## Initial plan

1. **Define the scope and evaluation.** Select one corpus and prepare 20–30
   representative questions. Annotate the relevant files and line ranges for
   each question, then record results from the existing search workflow,
   including task time and answer quality.
2. **Build a retrieval-only proof of concept.** Ingest code and documents and
   return ranked passages with file paths, line ranges, and supporting text.
   Preserve syntactic units when chunking code and use document headings as
   chunk boundaries. Compare keyword and semantic retrieval before committing
   to an embedding model or vector database.
3. **Provide a read-only agent interface.** Expose retrieval to the agent
   without giving it anything else to do. Leave answer generation and code
   changes to the agent. Every build re-chunks all files; embeddings of
   unchanged passages are reused from the previous index.
4. **Compare and decide whether to continue.** Run the same questions against
   the existing workflow and the proof of concept. Evaluate evidence found in
   the top results, answer correctness, task time, search latency, and index
   creation and update costs.

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

The plugin adds a **skill** (`context-search`) that runs the command-line
tool once per question, and a **session-start hook** that points the agent to
the skill only in workspaces that have an index. Python dependencies (about
200 MB) and the embedding model (about 490 MB) live outside the plugin, in a
per-user folder that `python -I ctxf.py setup` fills once. A read-only
**MCP server** with two tools, `search` and `status`, is available as an
extra. The skill is the default because an MCP server's tool definitions are
sent with every model request, whether or not the tools are used.

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

Keep the questions, annotations, paths, and search results of a private
corpus out of this public repository. The specification uses generic field
names and placeholders only. Two sets of cases are committed here: the one
under `tests/fixtures`, for a synthetic corpus written for the tests, and the
worked example under [`examples/o3de`](examples/o3de/README.md), for a public
open-source corpus. The example holds questions and the locations of their
answers, not source text.

## Initial scope boundaries

The first proof of concept does not include:

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
| `plugin.json`, `.github/plugin`, `com.github.copilot` | The Copilot plugin, its marketplace entry, and the session-start hook |
| `ctxf.py` | Launcher (standard library only): finds the Python environment and runs the tool; `setup` prepares it |
| `bench` | Runs the agent methods with Copilot CLI and records the results |
| `tests` | Tests and a small synthetic corpus (C++, C#, Markdown) |
| `examples/o3de` | Worked evaluation example on a public C++ game engine: questions, answer locations, measured results |
| `docs` | Evaluation specification, design notes, Copilot setup, comparison runbook |
| `*.bat` | Double-click entry points for Windows |

## Status

Steps 1 to 3 are implemented as a proof of concept, together with the tools
for step 4:

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
- the Copilot plugin and marketplace (skill and hook), the MCP server, and
  registration from a local clone;
- an evaluation runner for the retrieval-only methods and a runner for the
  agent methods.

The comparison (step 4) has been run on a public codebase (Godot) only.
Neither candidate met the thresholds set beforehand, and the agent with this
tool did not differ from the grep-only baseline in answer quality, time or
cost. It has not been run on a private corpus. Everything here has been
exercised on the synthetic corpus and on public codebases only.
What has been measured so far, and what has not, is listed in
[docs/benchmarks.md](docs/benchmarks.md) (in Japanese).

On the worked example for a public C++ game engine
([`examples/o3de`](examples/o3de/README.md)), the retrieval-only methods do
not reach the provisional threshold: the default hybrid search has a relevant
passage in the top five for 54% of the held-out questions, and for 22% of
the questions that describe behaviour without naming an identifier. On this
example, the correctness of answers written by an agent that uses the search
has not been measured.

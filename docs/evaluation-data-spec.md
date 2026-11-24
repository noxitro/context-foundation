# Evaluation data specification

This specification defines a repeatable, corpus-independent format for
comparing an existing search workflow with a retrieval tool.

Do not store the questions, annotations, file paths, source content, or run
results of a private corpus in this public repository. Keep those evaluation
records in an access-appropriate private location. The examples below are
placeholders, not evaluation data. The cases committed here are the ones
under `tests/fixtures`, which describe a synthetic corpus written for the
tests, and the worked example under
[`examples/o3de`](../examples/o3de/README.md), which describes a public
open-source corpus and contains no source text.

The formats are also defined as JSON Schema files, and a command checks data
against them. See [Schemas and validation](#schemas-and-validation).

## Methods compared

Every method answers the same questions. The agent methods use the same agent
host, model, prompt and tool set; only the search tool differs.

| Method | Kind | What searches |
| --- | --- | --- |
| `agent_grep` | Agent | The existing workflow: the agent with grep and file reads |
| `agent_grep_cxxref` | Agent | `agent_grep` plus a symbol reference index (cxxref) |
| `keyword` | Retrieval only | BM25 over the index |
| `semantic` | Retrieval only | Local embeddings over the index |
| `hybrid` | Retrieval only | `keyword` and `semantic` fused by rank |
| `agent_cf` | Agent | `agent_grep` plus this tool as a skill |

`agent_cf_mcp` is the same as `agent_cf` with the tool exposed through an MCP
server instead of a skill, plus a session-start hint that tells the agent to
search before grepping. Since version 0.3 this is the packaging the plugin
ships, so choose it as the agent candidate when the plugin as shipped is what
is being judged. `agent_cf` stays as the packaging of version 0.2 and
earlier, which the comparison on Godot used.

Rules:

- A method name that starts with `agent_` is an agent method. Every other
  method is retrieval only: it returns ranked passages and writes no answer.
- The agent host for this tool is GitHub Copilot CLI.
- `agent_grep` is the baseline. It is the candidate with the search tool
  removed and nothing else changed.
- Instruction files that the agent host loads from the workspace
  (`AGENTS.md`, `CLAUDE.md`, `.github/copilot-instructions.md` and similar)
  are part of the input of every model call. Decide before the run whether
  they are loaded, apply the same choice to every agent method, and record
  the choice and the files found in the configuration of each method.
- Choose the candidate methods before the run and record them in the run
  metadata as `candidate_methods`. The decision thresholds apply to the
  candidates only. Other methods are reported for comparison. Picking the
  best-scoring variant after seeing the results weakens the gate. The default
  candidates are `hybrid` and `agent_cf`.

## Evaluation cases

Store one JSON object per line in a `cases.jsonl` file. Prepare 20–30
representative questions for the decision before measuring any method.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `case_id` | string | Yes | Stable, unique case identifier |
| `question` | string | Yes | The natural-language question, given unchanged to every method |
| `question_language` | string | Yes | Language of the question: an ISO 639 code such as `ja` or `en`, or `mixed` |
| `category` | string | Yes | What kind of search the question needs (see below) |
| `expected_evidence` | array | Yes | Relevant source passages, annotated before running the comparison. Empty only for `unanswerable` |
| `expected_answer_points` | array of strings | Yes | Facts a correct answer should establish |
| `split` | string | No | `eval` (default) or `tune` (see below) |
| `notes` | string | No | Remarks for the annotators. Not given to any method |
| `tags` | array of strings | No | Labels for private analysis |

Each `expected_evidence` item has this shape:

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `source_id` | string | Yes | Non-identifying alias for the corpus source |
| `path` | string | Yes | Path relative to that source, with forward slashes |
| `start_line` | integer | Yes | First relevant line, 1-based and inclusive |
| `end_line` | integer | Yes | Last relevant line, 1-based and inclusive |
| `rationale` | string | Yes | Why the passage is relevant to the question |

Example shape:

```json
{
  "case_id": "Q001",
  "question": "PLACEHOLDER: a representative question",
  "question_language": "ja",
  "category": "natural_language_only",
  "expected_evidence": [
    {
      "source_id": "source-01",
      "path": "path/to/file",
      "start_line": 10,
      "end_line": 18,
      "rationale": "PLACEHOLDER: why this passage answers the question"
    }
  ],
  "expected_answer_points": [
    "PLACEHOLDER: a fact required in a correct answer"
  ]
}
```

Line ranges refer to the version of the corpus used for that evaluation. Record
the corpus revision alongside the run metadata so annotations and results can
be interpreted against the same source version.

### Completeness of the expected evidence

A result counts as a hit only when it overlaps a listed range, so an
incomplete list scores correct results as misses. List every passage a
reviewer would accept as answering the question, not only the most central
one:

- the declaration and the definition;
- each platform or back-end implementation of the same thing;
- the whole of a long function rather than one part of it;
- the class declaration when the answer is a class.

Leave out call sites, tests, and passages that are merely about a related
topic. For the held-out questions, add the alternatives before running any
method: adding them after looking at the results of one method favours that
method. The tuning questions may be revised after looking at results.

In the worked example under `examples/o3de`, the tuning questions were first
annotated with three ranges each and then with up to ten (9.9 on average).
For the same search results, the share of questions with a hit in the top
five rose from 0.05 to 0.55. How broad the accepted evidence is changes the
score, so compare methods on the same cases, not scores across case sets.

### Categories

| `category` | The question |
| --- | --- |
| `identifier_known` | Names the identifier it is about. A control: grep or a reference index should already answer it |
| `natural_language_only` | Describes behaviour in plain words or in another engine's terms, without the identifier |
| `doc_to_code` | Starts from a statement in a design document and asks for the code that implements it |
| `code_to_doc` | Starts from code and asks for the document that explains it |
| `similar_implementation` | Asks for another place that does the same kind of thing |
| `unanswerable` | Has no answer in the corpus. A correct answer says so |

Guidance on counts, for the held-out questions:

- Write at least three questions in every category, and at least two in
  `unanswerable`, so that the per-category breakdown means something.
- Give the largest share to the categories the tool is for:
  `natural_language_only`, `doc_to_code` and `code_to_doc`.
- Keep `identifier_known` small. Its job is to show that the new tool does
  not make the already-solved case worse.
- When the team asks in more than one language, write at least five
  questions in each, and say the same kind of thing in both.

### Tuning questions and held-out questions

Looking at results while adjusting exclusion rules, ranking weights or the
instructions given to the agent fits those choices to the questions looked
at. Keep two sets:

- `split: "tune"`: a small set that may be inspected freely while adjusting.
- `split: "eval"` (the default): the 20–30 questions for the decision. Do
  not adjust anything after looking at their results. If something must
  change, re-run every method afterwards.

Summaries and thresholds use the `eval` questions only.

## Comparison results

Keep results separate from the case annotations so the expected evidence is
not changed to match observed results. Record one result per case, method and
trial in a `results.jsonl` file.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `run_id` | string | Yes | Identifier shared by results from one comparison run |
| `case_id` | string | Yes | Identifier from `cases.jsonl` |
| `method` | string | Yes | Method name (see [Methods compared](#methods-compared)) |
| `trial` | integer | Yes | Repetition number, starting at 1 |
| `status` | string | Yes | `completed`, `timeout` or `gave_up` |
| `retrieved_evidence` | array | Yes | What the method found, in order (see below) |
| `search_time_ms` | number or null | Retrieval only | Latency of the search call for this case |
| `answer` | string or null | Agent | Answer produced by the agent |
| `answer_correctness` | number, string or null | Agent | Score under a rubric fixed before evaluation |
| `task_time_seconds` | number or null | Agent | Time spent completing the case |
| `input_tokens` | integer or null | If known | Input tokens over all model requests, cached tokens included |
| `output_tokens` | integer or null | If known | Output tokens over all model requests |
| `cache_read_tokens` | integer or null | If known | Part of `input_tokens` read from the prompt cache |
| `cache_write_tokens` | integer or null | If known | Part of `input_tokens` written to the prompt cache |
| `ai_units` | number or null | If known | Billing units reported by the agent host (Copilot: AI credits) |
| `cost_usd` | number or null | If known | Estimated cost when a provider bills tokens directly |
| `tool_calls` | integer or null | Agent | Number of tool calls |
| `model_calls` | integer or null | If known | Number of model requests |
| `returned_bytes` | integer or null | If known | Bytes of tool output handed to the model |
| `offloaded_results` | integer or null | If known | Tool results the host withheld from the model because of their size |
| `session_id` | string | No | Key for joining with an external session log |
| `tools_by_name` | object | No | Agent methods: number of calls per tool, by the name the host uses |
| `notes` | string | No | Free-form remarks |

Each `retrieved_evidence` item records `rank`, `source_id`, `path`,
`start_line` and `end_line`, with the same path and line conventions as the
expected evidence. Optional fields: `score`, `origin` and `stale` (true when
the file changed after the index was built).

### What counts as retrieved evidence

- **Retrieval-only methods** return a ranked list. Record the top ten
  passages in rank order, with `origin: "search_result"`.
- **Agent methods** have no ranked list. Record, in order of first
  appearance during the session:
  - each file range the agent opened (`origin: "opened"`). A read of a whole
    file is recorded as line 1 to the last line;
  - each passage a search tool returned to the agent
    (`origin: "search_result"`);
  - the body range of each symbol a code-intelligence tool (Serena, a
    language server) returned to the agent (`origin: "symbol_result"`);
  - each place the final answer names as evidence (`origin: "cited"`).

  Drop exact duplicates and number what remains from 1. Matches that the
  agent only saw inside grep output are not recorded.

A whole-file read overlaps every expected line in that file, so it counts as
a passage match however much the agent had to read. Read the passage-level
and file-level numbers together with the cost fields, and use `origin` to
recompute the metrics over cited evidence only when that matters.

### Time limit, status and trials

- Set one time limit per case for all agent methods and record it in the run
  metadata. `status` is `completed` when the method produced an answer or a
  result list, `timeout` when it hit the limit, and `gave_up` when it ended
  without an answer. An answer that correctly says the corpus has no answer
  is `completed`.
- Run agent methods three times per case (`trial` 1 to 3), because their
  results vary from run to run. Run retrieval-only methods once.
- `search_time_ms` is the latency of one search call with the index and the
  embedding model already loaded. It is null for agent methods. Record the
  one-time loading cost separately if it matters.

### Cost and returned size

Record `input_tokens`, `output_tokens`, `tool_calls` and, where the host
reports them, `ai_units`. When a provider bills tokens directly, record
`cost_usd` and put the price table in the run metadata.

Agent hosts may withhold a large tool result from the model and save it to a
file instead. Copilot CLI documents this behaviour and the environment
variable that sets the limit
(<https://docs.github.com/en/copilot/concepts/prompting-and-context/context-management>).
A withheld result costs the agent extra calls or leaves the answer
incomplete, for the baseline's grep output as much as for a search tool.
Record `returned_bytes` and `offloaded_results` so that this shows up in the
comparison.

## Matching and metrics

- **Passage match**: a retrieved item and an expected item have the same
  `source_id` and `path`, and their line ranges share at least one line.
- **File match**: the same `source_id` and `path`.
- Paths are compared after replacing `\` with `/`, removing a leading `./`,
  and ignoring case.

For each result, the rank of the first passage match and of the first file
match give:

- **hit@1, hit@5, hit@10**, at passage level and at file level: whether a
  match appears within the first 1, 5 or 10 items;
- **MRR**: the mean of 1 / rank of the first passage match, counting 0 when
  there is none;
- **evidence recall**: the share of `expected_evidence` items that have a
  passage match within the first ten items.

Aggregate per method by averaging the trials of each case first, then
averaging over cases. Cases in `unanswerable` have no expected evidence and
are left out of these metrics; report their answer correctness separately.
Report every metric overall, per `category` and per `question_language`.
Latency is the 50th and 95th percentile of `search_time_ms`. Task time and
cost are medians over all trials.

## Run metadata

Record one JSON object per `run_id` in a `runs.jsonl` file.

| Field | Meaning |
| --- | --- |
| `run_id`, `created_at` | Identifier and start time of the run |
| `corpus_revision` | Revision the line numbers refer to (commit, changelist number or dated label) |
| `candidate_methods` | Methods the thresholds apply to, chosen before the run |
| `methods` | Configuration of each method. For an agent method: the tool set, the permissions, whether instruction files were loaded (`custom_instructions`) and which were found (`instruction_files`, as place and file name, no absolute paths) |
| `corpus` | Size: files, lines and chunks, in total and per source alias |
| `index` | Size in bytes, build time, peak memory, update cost, share of code the parser could not split by syntax, and the revision indexed when it differs from `corpus_revision` |
| `embedding` | Model name, model revision, dimensions and stored precision |
| `chunking` | Chunking parameters |
| `agent` | Agent host name and version (Copilot CLI), model, provider, time limit, trials, and the price table when `cost_usd` is used |
| `host` | Operating system, CPU model and core count. No host name or user name |
| `thresholds` | The thresholds selected for this run |
| `data_handling` | The data-handling decision for this run |

Until incremental updates exist, the update cost is the time of a full
rebuild. Use source aliases only; never record absolute paths.

Index freshness is an optional extra experiment: build the index at one
revision, evaluate at a later one, re-annotate only the questions whose
evidence moved, and compare answer correctness as well as hit rates.

## Chunking policy

- Split code by syntactic unit: functions, methods, classes and other type
  definitions. Split documents by heading.
- Index every chunk together with its enclosing scope (namespace, class, or
  the heading path) and its signature (the declaration, or the heading), so
  that a fragment from the middle of a long function still says what it
  belongs to.
- Split a unit that is too long at statement boundaries and keep the same
  scope and signature on every part. Merge units that are too short with
  their neighbours in the same scope.
- When the parser cannot read a range, fall back to splitting it by lines,
  and report the share of lines handled that way.

## Why built-in semantic search is not the default

The tool targets GitHub Copilot CLI and the GitHub Copilot app
first, and corpora that are not hosted on GitHub and must stay on the local
machine. Checked on 2026-11-16:

- GitHub Docs describes the semantic code search index as usable by Copilot
  Chat in GitHub and Visual Studio Code and by Copilot cloud agent. Copilot
  CLI and the app are not in that list.
  <https://docs.github.com/en/copilot/concepts/prompting-and-context/repository-indexing>
- The Copilot CLI command reference lists the tools the CLI can give the
  model: shell, file reads and edits, `glob`, `grep`, skills and web fetch.
  No semantic search tool is listed. The built-in GitHub MCP server offers
  `search_code`, which searches repositories hosted on GitHub.
  <https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference>
- Not confirmed: whether the app or the CLI has a semantic search over a
  local folder that these pages do not describe.
- For reference, Visual Studio Code does have semantic search. For a
  workspace without a supported GitHub or Azure DevOps index it uses
  "external ingest", which uploads the indexable workspace files to GitHub,
  and which an organization policy must allow.
  <https://code.visualstudio.com/docs/agents/reference/workspace-context>

So the options found either are not documented for the first target
surfaces or send files to an external service. This tool
computes embeddings locally instead and measures whether that is worth it.

## Decision thresholds

Use these provisional continuation thresholds for the initial comparison,
applied to the candidate methods.

Absolute conditions:

- At least one relevant annotated passage appears in the top five results
  for at least 80% of questions (hit@5 at passage level).
- At least 80% of answers are acceptable under a rubric fixed before the
  comparison. The default rubric scores `answer_correctness` from 0 to 1 as
  the share of `expected_answer_points` the answer establishes, and accepts
  0.8 or more. The strings `acceptable` and `unacceptable` are also valid.
- p95 search latency is no more than three seconds.

Condition relative to the existing workflow (`agent_grep`):

- Quality is not lower: the share of acceptable answers is at least the
  baseline's. Before answers are scored, compare hit@5 instead.
- And the work gets cheaper: median task time or median cost is at least 20%
  lower than the baseline's. Cost is compared in the first unit recorded for
  both methods: `ai_units`, then `cost_usd`, then total tokens.

When the existing workflow already meets the absolute conditions, meeting
them too is not a reason to continue. Continue only if the relative
condition holds, or if the candidate brings a category above the threshold
that the baseline leaves below it, without lowering another category.

Also:

- Record index creation and update cost; set a hard limit after the corpus
  and workflow are understood.
- Treat these thresholds as an initial decision gate, not as a statistically
  conclusive result from a 20–30-question sample.
- Record the selected thresholds and the data-handling decision with the
  private evaluation run metadata before running the comparison.

### Data handling

The retrieval tool keeps source content local: the index and the embeddings
are built and stored on the local machine, and the tool sends nothing to any
service. Downloading the embedding model fetches files and sends no content.

An agent is different. Whatever an agent reads becomes part of its prompt:
the question, the passages a search tool returns and the files it opens all
go to the model provider the agent host is configured with. That is true of
the existing workflow as well. Before running agent methods on a private
corpus, decide which provider may receive its content, and record the
decision as `data_handling`. Use a synthetic or public corpus for trial
runs.

## Schemas and validation

| Data | Schema |
| --- | --- |
| `cases.jsonl` | [`cases.schema.json`](../src/context_foundation/schemas/cases.schema.json) |
| `results.jsonl` | [`results.schema.json`](../src/context_foundation/schemas/results.schema.json) |
| `runs.jsonl` | [`runs.schema.json`](../src/context_foundation/schemas/runs.schema.json) |

Check files with the `validate` command. It reports each problem with its
file name and line number. `ctxf` below stands for the tool's command line:
`python -I <plugin folder>/ctxf.py`, or `ctxf.py --run` started with the
Python of the cloned repository's `.venv` (see "コマンド" in
[readme-ja.md](../readme-ja.md)). A command named `ctxf` is usually not on
`PATH`.

```text
ctxf validate --cases cases.jsonl --check-corpus
ctxf validate --cases cases.jsonl --results results.jsonl --runs runs.jsonl
```

`--check-corpus` also checks that every expected passage names a source,
file and line range that exist in the configured corpus.

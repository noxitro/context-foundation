# context-foundation

An independent retrieval foundation that helps AI agents find relevant evidence
in large codebases and design documents.

`context-foundation` complements tools such as cxxref: cxxref can identify
precise C++ references, while this project aims to discover relevant passages
from natural-language questions. It is not intended to replace reference
analysis.

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
3. **Provide a read-only agent interface.** Expose retrieval through a minimal
   MCP interface. Leave answer generation and code changes to the agent.
   Initially, rebuild the index from scratch; consider incremental updates
   only when their value and cost are understood.
4. **Compare and decide whether to continue.** Run the same questions against
   the existing workflow and the proof of concept. Evaluate evidence found in
   the top results, answer correctness, task time, search latency, and index
   creation and update costs.

## Evaluation requirements

The evaluation data format is described in
[`docs/evaluation-data-spec.md`](docs/evaluation-data-spec.md). Set measurable
success thresholds before implementing the retrieval proof of concept. The
provisional thresholds are: at least one relevant passage in the top five for
80% of questions; an acceptable answer for at least 80% of questions; at least
20% lower median task time than the existing workflow without reducing answer
quality; and p95 search latency of no more than three seconds. Measure index
creation and update cost, but set a hard limit after the corpus and workflow
are understood. These thresholds are an initial decision gate, not a
statistical claim.

Source content must remain local; do not send it to external services.

Keep corpus-specific questions, annotations, paths, and search results out of
this public repository. The specification uses generic field names and
placeholders only.

## Initial scope boundaries

The first proof of concept does not include:

- Replacing precise C++ reference analysis such as cxxref
- A production service shared across multiple repositories
- Training a custom model
- An organization-wide service with access-control management

## Status

Planning and evaluation design. Retrieval and MCP implementation have not
started.

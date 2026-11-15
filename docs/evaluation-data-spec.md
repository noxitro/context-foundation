# Evaluation data specification

This specification defines a repeatable, corpus-independent format for
comparing an existing search workflow with a retrieval proof of concept.

Do not store actual corpus-specific questions, annotations, file paths, source
content, or run results in this public repository. Keep evaluation records in
an access-appropriate private location. The examples below are placeholders,
not evaluation data.

## Evaluation cases

Store one JSON object per line in a `cases.jsonl` file. Prepare 20–30
representative questions before measuring either search method.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `case_id` | string | Yes | Stable, unique case identifier |
| `question` | string | Yes | The natural-language question given to the search workflow |
| `expected_evidence` | array | Yes | One or more relevant source passages, annotated before running the comparison |
| `expected_answer_points` | array of strings | Yes | Facts a correct answer should establish |

Each `expected_evidence` item has this shape:

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `source_id` | string | Yes | Non-identifying alias for the corpus source |
| `path` | string | Yes | Path relative to that source |
| `start_line` | integer | Yes | First relevant line, 1-based and inclusive |
| `end_line` | integer | Yes | Last relevant line, 1-based and inclusive |
| `rationale` | string | Yes | Why the passage is relevant to the question |

Example shape:

```json
{
  "case_id": "Q001",
  "question": "PLACEHOLDER: a representative question",
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

## Comparison results

Keep results separate from the case annotations so the expected evidence is
not changed to match observed results. Record one result per case and method
in a `results.jsonl` file.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `run_id` | string | Yes | Identifier shared by results from one comparison run |
| `case_id` | string | Yes | Identifier from `cases.jsonl` |
| `method` | string | Yes | Search workflow or proof-of-concept variant |
| `retrieved_evidence` | array | Yes | Ranked passages returned by the method |
| `search_time_ms` | number | Yes | Search latency for this case |
| `answer` | string | Yes | Answer produced using the retrieved evidence |
| `answer_correctness` | number or string | Yes | Score using a rubric fixed before evaluation |
| `task_time_seconds` | number | Yes | Time spent completing the case |

Each `retrieved_evidence` item records `rank`, `source_id`, `path`,
`start_line`, and `end_line`. Use the same path and line conventions as the
expected evidence.

Record run-level metadata with the results, including the corpus revision,
method configuration, index build time, and any index update time and cost.
Use the same cases and answer-quality rubric for every method being compared.

## Decisions required before implementation

- Use these provisional continuation thresholds for the initial comparison:
  - At least one relevant annotated passage appears in the top five results
    for at least 80% of questions.
  - At least 80% of answers are acceptable under a rubric fixed before the
    comparison.
  - Median task time is at least 20% lower than the existing workflow, with
    no reduction in answer quality.
  - p95 search latency is no more than three seconds.
  - Record index creation and update cost; set a hard limit after the corpus
    and workflow are understood.
- Treat these thresholds as an initial decision gate, not as a statistically
  conclusive result from a 20–30-question sample.
- Keep source content local. Do not send it to external services.
- Record the selected thresholds and data-handling decision with the private
  evaluation run metadata before implementing the retrieval proof of concept.

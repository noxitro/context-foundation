"""`ctxf validate`: cases.jsonl / results.jsonl / runs.jsonl を検査して、見つけたことを行番号つきで出す。"""
from __future__ import annotations

import sys
from pathlib import Path

from ..config import find_config, load_config
from .data import Issue, check_cases, check_results, check_runs, read_jsonl


def check_corpus(case_records: list[tuple[int, dict]], file: str, cfg) -> list[Issue]:
    """expected_evidence の source_id・パス・行の範囲が、設定のコーパスにあるかを確かめる。"""
    issues: list[Issue] = []
    line_counts: dict[Path, int | None] = {}
    for no, c in case_records:
        for i, ev in enumerate(c["expected_evidence"]):
            src = cfg.source(ev["source_id"])
            if src is None:
                issues.append(Issue(file, no, "error",
                                    f"expected_evidence/{i}: source_id {ev['source_id']} が設定にありません"
                                    f"(あるのは {', '.join(s.id for s in cfg.sources)})"))
                continue
            full = src.root / ev["path"]
            if full not in line_counts:
                try:
                    data = full.read_bytes()
                    line_counts[full] = data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1)
                except OSError:
                    line_counts[full] = None
            n = line_counts[full]
            if n is None:
                issues.append(Issue(file, no, "error", f"expected_evidence/{i}: ファイルがありません: "
                                                         f"{ev['source_id']}:{ev['path']}"))
            elif ev["end_line"] > n:
                issues.append(Issue(file, no, "error",
                                    f"expected_evidence/{i}: end_line {ev['end_line']} がファイルの行数 {n} を"
                                    f"超えています({ev['source_id']}:{ev['path']})"))
    return issues


def main(args) -> int:
    if not (args.cases or args.results or args.runs):
        sys.stderr.write("検査するファイルを渡してください(--cases / --results / --runs)\n")
        return 2
    issues: list[Issue] = []
    case_ids: set[str] | None = None
    counts: list[str] = []
    if args.cases:
        records, found = read_jsonl(args.cases, "cases")
        issues += found
        issues += check_cases(records, Path(args.cases).name)
        case_ids = {c["case_id"] for _, c in records}
        counts.append(f"cases {len(records)} 件")
        if args.check_corpus:
            path = find_config(args.config)
            if path is None:
                issues.append(Issue(Path(args.cases).name, 0, "error",
                                    "--check-corpus には設定(.cf/config.toml)が要ります。--config で指定してください"))
            else:
                issues += check_corpus(records, Path(args.cases).name, load_config(path))
    for path in args.results:
        records, found = read_jsonl(path, "results")
        issues += found
        issues += check_results(records, Path(path).name, case_ids)
        counts.append(f"results {len(records)} 件({Path(path).name})")
    for path in args.runs:
        records, found = read_jsonl(path, "runs")
        issues += found
        issues += check_runs(records, Path(path).name)
        counts.append(f"runs {len(records)} 件({Path(path).name})")

    order = {"error": 0, "warning": 1, "info": 2}
    for i in sorted(issues, key=lambda x: (order[x.level], x.file, x.line)):
        print(i.format())
    errors = sum(1 for i in issues if i.level == "error")
    warnings = sum(1 for i in issues if i.level == "warning")
    print(f"読めた行: {'、'.join(counts)}")
    print(f"結果: 誤り {errors} 件、注意 {warnings} 件" + ("(誤りを直してください)" if errors else "(形式は合っています)"))
    return 1 if errors else 0

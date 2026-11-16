"""cases.jsonl / results.jsonl / runs.jsonl の読み込みと検査。

検査は 2 段。1 行ずつ JSON Schema に合うかを見てから、行をまたぐ決まり(id の重なり、行の範囲、
結果が指す質問があるか、など)を見る。見つけたことは、ファイル名と行番号を付けて返す。
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path

CATEGORIES = ("identifier_known", "natural_language_only", "doc_to_code", "code_to_doc", "similar_implementation",
              "unanswerable")
STATUSES = ("completed", "timeout", "gave_up")
MIN_PER_CATEGORY = 3          # 内訳を読むための目安(unanswerable は 2)
MIN_CASES, MAX_CASES = 20, 30


@dataclass
class Issue:
    file: str
    line: int          # 0 はファイル全体
    level: str         # error / warning / info
    message: str

    def format(self) -> str:
        where = f"{self.file}:{self.line}" if self.line else self.file
        label = {"error": "誤り", "warning": "注意", "info": "情報"}[self.level]
        return f"{where}: [{label}] {self.message}"


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict:
    """name は cases / results / runs。"""
    text = resources.files("context_foundation").joinpath("schemas", f"{name}.schema.json").read_text("utf-8")
    return json.loads(text)


@lru_cache(maxsize=None)
def _validator(name: str):
    from jsonschema import Draft202012Validator
    return Draft202012Validator(load_schema(name))


def _explain(err) -> str:
    """jsonschema の誤りを、日本語の 1 行にする。"""
    path = "/".join(str(p) for p in err.absolute_path) or "(行全体)"
    v, val = err.validator, err.validator_value
    if v == "required":
        missing = [k for k in val if k not in err.instance]
        return f"{path}: 必須の項目がありません: {', '.join(missing)}"
    if v == "additionalProperties":
        extra = sorted(set(err.instance) - set(err.schema.get("properties", {})))
        return f"{path}: 知らない項目があります: {', '.join(extra)}"
    if v == "enum":
        return f"{path}: {err.instance!r} は使えません。次のどれかにしてください: {', '.join(map(str, val))}"
    if v == "type":
        want = val if isinstance(val, str) else " / ".join(val)
        return f"{path}: 型が違います(期待: {want}、実際: {type(err.instance).__name__})"
    if v == "pattern":
        return f"{path}: {err.instance!r} は決まった形に合いません({val})"
    if v in ("minimum", "maximum"):
        return f"{path}: {err.instance} は範囲の外です({v} {val})"
    if v == "minItems":
        return f"{path}: 少なくとも {val} 件が要ります"
    if v == "maxItems":
        return f"{path}: {val} 件までです(unanswerable の質問には expected_evidence を書きません)"
    if v == "minLength":
        return f"{path}: 空にできません"
    return f"{path}: {err.message}"


def read_jsonl(path: str | Path, schema: str) -> tuple[list[tuple[int, dict]], list[Issue]]:
    """JSONL を読み、(行番号, 中身) の並びと、見つけた誤りを返す。空行は飛ばす。"""
    path = Path(path)
    records: list[tuple[int, dict]] = []
    issues: list[Issue] = []
    name = path.name
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        return [], [Issue(name, 0, "error", f"読めません: {e}")]
    except UnicodeDecodeError:
        return [], [Issue(name, 0, "error", "UTF-8 で保存してください")]
    validator = _validator(schema)
    for no, line in enumerate(text.split("\n"), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            issues.append(Issue(name, no, "error", f"JSON として読めません: {e.msg}({e.colno} 文字目)"))
            continue
        if not isinstance(obj, dict):
            issues.append(Issue(name, no, "error", "1 行に 1 つの JSON オブジェクトを書いてください"))
            continue
        errs = sorted(validator.iter_errors(obj), key=lambda e: list(map(str, e.absolute_path)))
        # if/then/else の中の誤りは、外側の allOf の誤りとしても出るので、葉だけを出す
        leaf = [x for e in errs for x in (e.context or [e])] if errs else []
        for e in leaf or errs:
            issues.append(Issue(name, no, "error", _explain(e)))
        if not errs:
            records.append((no, obj))
    return records, issues


def check_cases(records: list[tuple[int, dict]], file: str) -> list[Issue]:
    issues: list[Issue] = []
    seen: dict[str, int] = {}
    for no, c in records:
        cid = c["case_id"]
        if cid in seen:
            issues.append(Issue(file, no, "error", f"case_id {cid} が {seen[cid]} 行目と重なっています"))
        seen.setdefault(cid, no)
        for i, ev in enumerate(c["expected_evidence"]):
            if ev["end_line"] < ev["start_line"]:
                issues.append(Issue(file, no, "error",
                                    f"expected_evidence/{i}: end_line({ev['end_line']})が start_line"
                                    f"({ev['start_line']})より前です"))
            if "\\" in ev["path"] or ev["path"].startswith(("/", "./")) or ":" in ev["path"][:3]:
                issues.append(Issue(file, no, "warning",
                                    f"expected_evidence/{i}: path は source のルートからの相対パスを / 区切りで"
                                    f"書きます: {ev['path']!r}"))
    held_out = [c for _, c in records if c.get("split", "eval") == "eval"]
    cats = Counter(c["category"] for c in held_out)
    langs = Counter(c["question_language"] for c in held_out)
    n = len(held_out)
    if n:
        if not MIN_CASES <= n <= MAX_CASES:
            issues.append(Issue(file, 0, "info", f"質問は {n} 件です(目安は {MIN_CASES}〜{MAX_CASES} 件)"))
        for cat in CATEGORIES:
            need = 2 if cat == "unanswerable" else MIN_PER_CATEGORY
            if cats[cat] < need:
                issues.append(Issue(file, 0, "warning",
                                    f"category {cat} が {cats[cat]} 件です(内訳を読むには {need} 件以上が目安)"))
        issues.append(Issue(file, 0, "info", "category ごとの件数: " +
                            "、".join(f"{k} {cats[k]}" for k in CATEGORIES)))
        issues.append(Issue(file, 0, "info", "question_language ごとの件数: " +
                            "、".join(f"{k} {v}" for k, v in sorted(langs.items()))))
        tune = sum(1 for _, c in records if c.get("split") == "tune")
        if tune:
            issues.append(Issue(file, 0, "info", f"調整用(split が tune)の質問: {tune} 件。判定には使いません"))
    return issues


def is_agent_method(method: str) -> bool:
    return method.startswith("agent_")


def check_results(records: list[tuple[int, dict]], file: str, case_ids: set[str] | None) -> list[Issue]:
    issues: list[Issue] = []
    seen: dict[tuple, int] = {}
    for no, r in records:
        key = (r["run_id"], r["case_id"], r["method"], r["trial"])
        if key in seen:
            issues.append(Issue(file, no, "error",
                                f"run_id・case_id・method・trial の組が {seen[key]} 行目と重なっています"))
        seen.setdefault(key, no)
        if case_ids is not None and r["case_id"] not in case_ids:
            issues.append(Issue(file, no, "error", f"case_id {r['case_id']} が cases.jsonl にありません"))
        ranks = [e["rank"] for e in r["retrieved_evidence"]]
        if ranks != list(range(1, len(ranks) + 1)):
            issues.append(Issue(file, no, "error", "retrieved_evidence の rank は 1 から順に並べてください"))
        for i, e in enumerate(r["retrieved_evidence"]):
            if e["end_line"] < e["start_line"]:
                issues.append(Issue(file, no, "error", f"retrieved_evidence/{i}: end_line が start_line より前です"))
        if is_agent_method(r["method"]):
            if r["status"] == "completed":
                for k in ("answer", "task_time_seconds"):
                    if r.get(k) is None:
                        issues.append(Issue(file, no, "error", f"エージェントを使う手法の結果には {k} が要ります"))
            if r.get("tool_calls") is None:
                issues.append(Issue(file, no, "warning", "tool_calls がありません(費用の比較に使います)"))
        elif r.get("search_time_ms") is None:
            issues.append(Issue(file, no, "error", "検索だけの手法の結果には search_time_ms が要ります"))
        ac = r.get("answer_correctness")
        if isinstance(ac, (int, float)) and not isinstance(ac, bool) and not 0 <= ac <= 1:
            issues.append(Issue(file, no, "error", "answer_correctness の数は 0〜1 にしてください"))
    # 回数の決まり(エージェントは 3 回、検索だけは 1 回)
    trials: dict[tuple, set[int]] = {}
    for _, r in records:
        trials.setdefault((r["run_id"], r["method"], r["case_id"]), set()).add(r["trial"])
    for (run, method), group in _group2(trials).items():
        want = 3 if is_agent_method(method) else 1
        short = sum(1 for t in group if len(t) < want)
        if short:
            issues.append(Issue(file, 0, "warning",
                                f"run {run} の {method}: {short} 件の質問で回数が {want} 回に足りません"))
    return issues


def _group2(trials: dict[tuple, set[int]]) -> dict[tuple, list[set[int]]]:
    out: dict[tuple, list[set[int]]] = {}
    for (run, method, _case), t in trials.items():
        out.setdefault((run, method), []).append(t)
    return out


def check_runs(records: list[tuple[int, dict]], file: str) -> list[Issue]:
    issues: list[Issue] = []
    seen: dict[str, int] = {}
    for no, r in records:
        if r["run_id"] in seen:
            issues.append(Issue(file, no, "error", f"run_id {r['run_id']} が {seen[r['run_id']]} 行目と重なっています"))
        seen.setdefault(r["run_id"], no)
        text = json.dumps(r, ensure_ascii=False)
        if ":\\\\" in text or "/Users/" in text or "/home/" in text:
            issues.append(Issue(file, no, "warning", "絶対パスらしいものが入っています(source は別名で書きます)"))
    return issues


def load_cases(path: str | Path) -> tuple[dict[str, dict], list[Issue]]:
    records, issues = read_jsonl(path, "cases")
    issues += check_cases(records, Path(path).name)
    return {c["case_id"]: c for _, c in records}, issues

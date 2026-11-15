"""要約(Markdown と JSON)と、しきい値の合否。

しきい値は docs/evaluation-data-spec.md の「Decision thresholds」と同じ。絶対の条件(上位 5 件の根拠、
回答の合格率、検索時間の p95)と、既存の手順との相対の条件(品質を下げず、時間か費用の中央値が下がる)。
"""
from __future__ import annotations

from collections import Counter

DEFAULT_THRESHOLDS = {
    "hit5_passage_min": 0.80,          # 上位 5 件に根拠の passage がある質問の割合
    "acceptable_rate_min": 0.80,       # 回答が合格の割合
    "acceptable_min": 0.8,             # answer_correctness(0〜1)が、この値以上なら合格
    "p95_search_ms_max": 3000.0,       # 検索時間の p95
    "relative_reduction_min": 0.20,    # 既存の手順より、作業時間か費用の中央値が下がる割合
}

PASS, FAIL, NA = "pass", "fail", "n/a"


def _verdict(value, ok) -> str:
    return NA if value is None else (PASS if ok else FAIL)


def _cost_measure(entry: dict, base: dict) -> tuple[str, float, float] | None:
    """2 つの手法に共通して記録のある費用の物差しを、ai_units → cost_usd → トークンの順に選ぶ。"""
    for key, label in (("ai_units_median", "ai_units"), ("cost_usd_median", "cost_usd"),
                       ("total_tokens_median", "tokens")):
        a, b = entry["cost"].get(key), base["cost"].get(key)
        if a is not None and b:
            return label, a, b
    return None


def judge(entry: dict, baseline: dict | None, th: dict) -> dict:
    """1 つの手法の合否。checks は表示の順。overall は pass / fail / incomplete。"""
    checks: list[dict] = []
    hit5 = entry["retrieval"]["hit5_passage"]
    checks.append({"id": "hit5_passage", "value": hit5, "threshold": th["hit5_passage_min"],
                   "verdict": _verdict(hit5, hit5 is not None and hit5 >= th["hit5_passage_min"])})
    rate = entry["answers"]["acceptable_rate"]
    if entry["agent"]:
        checks.append({"id": "acceptable_answers", "value": rate, "threshold": th["acceptable_rate_min"],
                       "verdict": _verdict(rate, rate is not None and rate >= th["acceptable_rate_min"])})
    p95 = entry["search_time_ms"]["p95"]
    if not entry["agent"] or p95 is not None:
        checks.append({"id": "p95_search_ms", "value": p95, "threshold": th["p95_search_ms_max"],
                       "verdict": _verdict(p95, p95 is not None and p95 <= th["p95_search_ms_max"])})

    relative = None
    if baseline is not None and entry["agent"] and entry is not baseline:
        b_rate, b_hit5 = baseline["answers"]["acceptable_rate"], baseline["retrieval"]["hit5_passage"]
        if rate is not None and b_rate is not None:
            basis, quality_ok = "acceptable_answers", rate >= b_rate
            q_values = (rate, b_rate)
        elif hit5 is not None and b_hit5 is not None:
            basis, quality_ok = "hit5_passage", hit5 >= b_hit5
            q_values = (hit5, b_hit5)
        else:
            basis, quality_ok, q_values = None, None, (None, None)
        t, bt = entry["task_time_seconds_median"], baseline["task_time_seconds_median"]
        time_red = (1 - t / bt) if (t is not None and bt) else None
        cm = _cost_measure(entry, baseline)
        cost_red = (1 - cm[1] / cm[2]) if cm else None
        need = th["relative_reduction_min"]
        cheaper = None
        if time_red is not None or cost_red is not None:
            cheaper = (time_red is not None and time_red >= need) or (cost_red is not None and cost_red >= need)
        if quality_ok is None or cheaper is None:
            verdict = NA
        else:
            verdict = PASS if (quality_ok and cheaper) else FAIL
        relative = {"baseline": {"run_id": baseline["run_id"], "method": baseline["method"]},
                    "quality_basis": basis, "quality": q_values[0], "baseline_quality": q_values[1],
                    "quality_not_lower": quality_ok, "time_reduction": time_red, "cost_measure": cm[0] if cm else None,
                    "cost_reduction": cost_red, "threshold": need, "verdict": verdict}
        checks.append({"id": "relative_to_baseline", "value": None, "threshold": need, "verdict": verdict})

    verdicts = [c["verdict"] for c in checks]
    overall = "fail" if FAIL in verdicts else ("incomplete" if NA in verdicts else "pass")
    return {"checks": checks, "relative": relative, "overall": overall}


def build_summary(cases: dict[str, dict], entries: list[dict], runs: list[dict], baseline_method: str,
                  thresholds: dict | None = None, candidates: list[str] | None = None) -> dict:
    th = dict(DEFAULT_THRESHOLDS, **(thresholds or {}))
    candidates = list(candidates or [])
    for e in entries:
        e["candidate"] = e["method"] in candidates
    bases = [e for e in entries if e["method"] == baseline_method]
    for e in entries:
        same_run = [b for b in bases if b["run_id"] == e["run_id"]]
        base = (same_run or bases[-1:] or [None])[0]
        e["judgement"] = judge(e, base, th)
    note = None
    if bases:
        b = bases[-1]
        absolute = [c for c in b["judgement"]["checks"] if c["id"] != "relative_to_baseline"]
        if absolute and all(c["verdict"] == PASS for c in absolute):
            gaps = {}
            for e in entries:
                if e is b or not e["agent"]:
                    continue
                won = [cat for cat, v in e["by_category"].items()
                       if (v.get("hit5_passage") or 0) >= th["hit5_passage_min"]
                       and (b["by_category"].get(cat, {}).get("hit5_passage") or 0) < th["hit5_passage_min"]]
                if won:
                    gaps[f'{e["method"]} [{e["run_id"]}]'] = won
            note = {"baseline_meets_absolute": True, "categories_recovered": gaps}
    cat = Counter(c["category"] for c in cases.values())
    lang = Counter(c["question_language"] for c in cases.values())
    return {"cases": {"total": len(cases), "by_category": dict(cat), "by_language": dict(lang)},
            "thresholds": th, "baseline_method": baseline_method, "candidate_methods": candidates,
            "baseline_note": note, "methods": entries, "runs": runs}


# --- Markdown ---
def _pct(v) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def _num(v, unit: str = "", digits: int = 1) -> str:
    if v is None:
        return "—"
    return f"{v:,.{digits}f}{unit}"


_VERDICT_JA = {PASS: "合格", FAIL: "不合格", NA: "未測定"}
_OVERALL_JA = {"pass": "合格", "fail": "不合格", "incomplete": "未確定(測っていない項目がある)"}
_CHECK_JA = {"hit5_passage": "上位 5 件に根拠の passage", "acceptable_answers": "回答の合格率",
             "p95_search_ms": "検索時間 p95", "relative_to_baseline": "既存の手順との相対"}


def to_markdown(summary: dict) -> str:
    th, cs = summary["thresholds"], summary["cases"]
    out = ["# 評価の要約", "",
           f"- 質問: {cs['total']} 件(" + "、".join(f"{k} {v}" for k, v in sorted(cs["by_category"].items())) + ")",
           "- 質問の言語: " + "、".join(f"{k} {v}" for k, v in sorted(cs["by_language"].items())),
           f"- 既存の手順とみなす手法: `{summary['baseline_method']}`",
           "- 判定の対象(実行の前に決めた手法): "
           + ("、".join(f"`{m}`" for m in summary.get("candidate_methods") or []) or "指定なし"),
           "- 指標の定義: docs/evaluation-data-spec.md の「Matching and metrics」", ""]

    out += ["## 実行ごとの情報", ""]
    for r in summary["runs"]:
        corpus, index, emb, agent = r.get("corpus") or {}, r.get("index") or {}, r.get("embedding") or {}, \
            r.get("agent") or {}
        out.append(f"- `{r['run_id']}`({r.get('created_at', '?')}、コーパスの版: {r.get('corpus_revision') or '未記入'})")
        if corpus:
            out.append(f"  - コーパス: {corpus.get('files', 0):,} ファイル / {corpus.get('lines', 0):,} 行 / "
                       f"{corpus.get('chunks', 0):,} passage")
        if index:
            out.append(f"  - 索引: {index.get('size_bytes', 0) / 1e6:.1f} MB、作成 {index.get('build_seconds', 0):.1f} 秒"
                       f"(分割 {index.get('ingest_chunk_seconds', 0):.1f} 秒、埋め込み {index.get('embed_seconds', 0):.1f} 秒)、"
                       f"構文で分けられなかった割合 {index.get('syntax_fallback_ratio', 0) * 100:.1f}%")
        if emb:
            out.append(f"  - 埋め込みモデル: {emb.get('model')}({emb.get('dimensions')} 次元)")
        if agent:
            out.append(f"  - エージェント: {agent.get('name')} {agent.get('version', '')}、モデル {agent.get('model')}"
                       f"({agent.get('provider', '')})、上限 {agent.get('time_limit_seconds', '?')} 秒、"
                       f"{agent.get('trials', '?')} 回")
    out.append("")

    out += ["## 手法ごとの指標", "",
            "| 手法 | run | 質問 | 回 | hit@1 | hit@5 | hit@10 | file@1 | file@5 | file@10 | MRR | 根拠の再現率 | p50 ms | p95 ms |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for e in summary["methods"]:
        r, lt = e["retrieval"], e["search_time_ms"]
        out.append(f"| {e['method']} | {e['run_id']} | {r['cases']} | {e['trials']} | {_pct(r['hit1_passage'])} | "
                   f"{_pct(r['hit5_passage'])} | {_pct(r['hit10_passage'])} | {_pct(r['hit1_file'])} | "
                   f"{_pct(r['hit5_file'])} | {_pct(r['hit10_file'])} | {_num(r['rr_passage'], '', 3)} | "
                   f"{_pct(r['evidence_recall'])} | {_num(lt['p50'])} | {_num(lt['p95'])} |")
    out += ["", "hit@k は passage の一致、file@k はファイルの一致。答えの無い質問(unanswerable)は、この表に入りません。", ""]
    stale = [(e["method"], e["run_id"], e["stale_evidence"]) for e in summary["methods"] if e.get("stale_evidence")]
    if stale:
        out += ["索引の後に変わったファイルを指していた結果: "
                + "、".join(f"{m} [{r}] {n} 件" for m, r, n in stale), ""]

    agents = [e for e in summary["methods"] if e["agent"]]
    if agents:
        out += ["## エージェントを使う手法の回答・時間・費用", "",
                "| 手法 | run | 完了 / 時間切れ / 断念 | 回答の合格率 | 答えの無い質問の合格率 | 作業時間(中央値) | 入力トークン | 出力トークン | AI credits | 費用(USD) | 道具の呼び出し | 道具の出力(バイト) | 退かされた結果 |",
                "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for e in agents:
            s, c = e["status"], e["cost"]
            out.append(f"| {e['method']} | {e['run_id']} | {s.get('completed', 0)} / {s.get('timeout', 0)} / "
                       f"{s.get('gave_up', 0)} | {_pct(e['answers']['acceptable_rate'])} | "
                       f"{_pct(e['unanswerable_answers']['acceptable_rate'])} | "
                       f"{_num(e['task_time_seconds_median'], ' 秒')} | {_num(c['input_tokens_median'], '', 0)} | "
                       f"{_num(c['output_tokens_median'], '', 0)} | {_num(c['ai_units_median'], '', 3)} | "
                       f"{_num(c['cost_usd_median'], '', 4)} | {_num(c['tool_calls_median'], '', 0)} | "
                       f"{_num(e.get('returned_bytes_median'), '', 0)} | {e.get('offloaded_results', 0)} |")
        out += ["", "トークン・費用・呼び出し・道具の出力は、1 回(1 質問 × 1 回)あたりの中央値。「退かされた結果」は、"
                "大きすぎてモデルに渡らなかった道具の結果の数(合計)。", ""]

    out += ["## しきい値の合否", "",
            f"- 上位 5 件に根拠の passage がある質問: {th['hit5_passage_min'] * 100:.0f}% 以上",
            f"- 回答の合格率: {th['acceptable_rate_min'] * 100:.0f}% 以上"
            f"(answer_correctness が {th['acceptable_min']} 以上を合格とする)",
            f"- 検索時間の p95: {th['p95_search_ms_max']:.0f} ms 以下",
            f"- 既存の手順との相対: 品質を下げず、作業時間か費用の中央値が {th['relative_reduction_min'] * 100:.0f}% 以上下がる",
            "",
            "判定に使うのは、実行の前に決めた手法(対象の列が「○」)だけです。ほかの手法は、比べるために載せています。",
            "",
            "| 手法 | run | 対象 | 判定 | 内訳 |", "| --- | --- | --- | --- | --- |"]
    for e in summary["methods"]:
        j = e["judgement"]
        parts = []
        for c in j["checks"]:
            if c["id"] == "relative_to_baseline":
                rel = j["relative"]
                detail = []
                if rel["quality_basis"]:
                    detail.append(f"品質({_CHECK_JA[rel['quality_basis']]}){_pct(rel['quality'])} 対 "
                                  f"{_pct(rel['baseline_quality'])}")
                if rel["time_reduction"] is not None:
                    detail.append(f"時間 {rel['time_reduction'] * 100:+.0f}% 減")
                if rel["cost_reduction"] is not None:
                    detail.append(f"費用({rel['cost_measure']}){rel['cost_reduction'] * 100:+.0f}% 減")
                parts.append(f"{_CHECK_JA[c['id']]}: {_VERDICT_JA[c['verdict']]}({'、'.join(detail) or '記録なし'})")
            else:
                val = _num(c["value"], " ms") if c["id"] == "p95_search_ms" else _pct(c["value"])
                parts.append(f"{_CHECK_JA[c['id']]}: {_VERDICT_JA[c['verdict']]}({val})")
        out.append(f"| {e['method']} | {e['run_id']} | {'○' if e.get('candidate') else ''} | "
                   f"{_OVERALL_JA[j['overall']]} | {';'.join(parts)} |")
    out.append("")
    note = summary.get("baseline_note")
    if note and note.get("baseline_meets_absolute"):
        out += ["既存の手順が、すでに絶対の条件を満たしています。続けるかどうかは、相対の条件(品質を下げずに、"
                "時間か費用の中央値が下がる)と、種類ごとの内訳で決めます。", ""]
        if note["categories_recovered"]:
            out.append("既存の手順がしきい値に届かず、次の手法が届いた種類:")
            out += [f"- {m}: {', '.join(cats)}" for m, cats in note["categories_recovered"].items()]
            out.append("")

    for title, key in (("種類(category)ごとの内訳", "by_category"), ("質問の言語ごとの内訳", "by_language")):
        out += [f"## {title}", "",
                "| 手法 | run | 区分 | 質問 | hit@1 | hit@5 | hit@10 | file@5 | MRR | 回答の合格率 |",
                "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for e in summary["methods"]:
            for name, v in e[key].items():
                out.append(f"| {e['method']} | {e['run_id']} | {name} | {v['cases']} | {_pct(v['hit1_passage'])} | "
                           f"{_pct(v['hit5_passage'])} | {_pct(v['hit10_passage'])} | {_pct(v['hit5_file'])} | "
                           f"{_num(v['rr_passage'], '', 3)} | {_pct(v.get('acceptable_rate'))} |")
        out.append("")
    out += ["このしきい値は、続けるかを決めるための最初の目安です。20〜30 件の質問では、統計として言い切れる差には"
            "なりません。", ""]
    return "\n".join(out)

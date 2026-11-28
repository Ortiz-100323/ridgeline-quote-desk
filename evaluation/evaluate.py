"""Evaluate the pipeline's outputs against the answer key.

    python -m evaluation.evaluate

This is the ONLY code allowed to read answer_key/ (DATA_PACK.md). The pipeline never
does: app.paths.safe_path() refuses it. Predictions are the files the worker wrote,
outputs/<rfq_id>.json.

Writes evaluation/results.json (the fields check_submission.py requires) and
evaluation/README.md (the report).

Metrics
- Extraction (per expected line; predicted lines are aligned to expected ones by text
  similarity of as_written, one-to-one):
    line_recall        expected lines that were extracted
    line_precision     extracted lines that correspond to an expected line
    quantity_accuracy  aligned lines with the right quantity
    unit_accuracy      aligned lines with the right unit (normalized: ea/each/EA, box/bx...)
    extraction_accuracy  expected lines extracted with the right quantity AND unit
- Matching, at the review threshold (a line is auto-accepted when needs_review is false):
    match_precision    auto-accepted lines whose part number is right
    match_recall       expected lines with a part number that were auto-accepted with the right one
    top1_accuracy      right part number, whether or not it was sent to review
    abstain_accuracy   lines the key expects to have no part, left without one
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from app.paths import OUTPUT_DIR, ROOT
from app.worker.matcher import BLOCKING_FLAGS, normalize_unit

ANSWER_KEY_DIR = ROOT / "answer_key"   # evaluation only
EVAL_DIR = ROOT / "evaluation"
ALIGN_MIN_SCORE = 60                   # rapidfuzz token_set_ratio, 0-100
SWEEP = [0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95]


def _norm_pn(pn: Any) -> str | None:
    return re.sub(r"[\s.\-/]", "", str(pn)).upper() if pn not in (None, "") else None


def _norm_unit(u: Any) -> str | None:
    if u in (None, ""):
        return None
    return normalize_unit(str(u)) or str(u).strip().upper()


def _qty_equal(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


def expected_part(line: dict[str, Any]) -> str | None:
    return line.get("expected_part_number", line.get("ridgeline_part_number"))


def align(expected: list[dict], predicted: list[dict]) -> list[tuple[int, int | None, float]]:
    """One-to-one alignment by as_written similarity (greedy on best pairs, ties by order)."""
    pairs = []
    for i, e in enumerate(expected):
        for j, p in enumerate(predicted):
            score = fuzz.token_set_ratio(str(e.get("as_written", "")).lower(), str(p.get("as_written", "")).lower())
            pairs.append((score, -abs(i - j), i, j))
    pairs.sort(reverse=True)
    used_e, used_p, out = set(), set(), {}
    for score, _, i, j in pairs:
        if score < ALIGN_MIN_SCORE or i in used_e or j in used_p:
            continue
        used_e.add(i), used_p.add(j)
        out[i] = (j, score)
    return [(i, out[i][0] if i in out else None, out[i][1] if i in out else 0.0) for i in range(len(expected))]


def accepted_at(line: dict[str, Any], threshold: float) -> bool:
    conf = line.get("confidence")
    return (line.get("ridgeline_part_number") is not None and conf is not None and conf >= threshold
            and not any(f in BLOCKING_FLAGS for f in line.get("flags", [])))


def evaluate() -> dict[str, Any]:
    threshold = float(os.getenv("MATCH_REVIEW_THRESHOLD", "0.8") or 0.8)
    per_rfq, rows = {}, []
    totals = dict(expected=0, predicted=0, aligned=0, qty_ok=0, unit_ok=0, extracted_ok=0,
                  with_part=0, accepted=0, accepted_right=0, top1_right=0, no_part=0, abstained=0)
    customers_right = customers_total = 0

    for key_file in sorted(ANSWER_KEY_DIR.glob("*.json")):
        key = json.loads(key_file.read_text(encoding="utf-8"))
        rfq_id = key.get("rfq_id", key_file.stem)
        out_file = OUTPUT_DIR / f"{rfq_id}.json"
        if not out_file.exists():
            per_rfq[rfq_id] = {"error": "no output file"}
            continue
        pred = json.loads(out_file.read_text(encoding="utf-8"))
        exp_lines, pred_lines = key.get("lines", []), pred.get("lines", [])
        t = dict.fromkeys(totals, 0)
        t["expected"], t["predicted"] = len(exp_lines), len(pred_lines)

        for i, j, score in align(exp_lines, pred_lines):
            e = exp_lines[i]
            p = pred_lines[j] if j is not None else None
            exp_pn = _norm_pn(expected_part(e))
            row = {"rfq_id": rfq_id, "line": e.get("line", i + 1), "as_written": e.get("as_written"),
                   "predicted_line": p.get("line") if p else None, "align_score": round(score),
                   "expected_quantity": e.get("quantity"), "quantity": p.get("quantity") if p else None,
                   "expected_unit": e.get("unit"), "unit": p.get("unit") if p else None,
                   "expected_part_number": expected_part(e),
                   "part_number": p.get("ridgeline_part_number") if p else None,
                   "match_method": p.get("match_method") if p else None,
                   "confidence": p.get("confidence") if p else None,
                   "needs_review": p.get("needs_review") if p else None,
                   "flags": p.get("flags", []) if p else []}
            if p is not None:
                t["aligned"] += 1
                row["quantity_ok"] = _qty_equal(e.get("quantity"), p.get("quantity"))
                row["unit_ok"] = _norm_unit(e.get("unit")) == _norm_unit(p.get("unit"))
                t["qty_ok"] += row["quantity_ok"]
                t["unit_ok"] += row["unit_ok"]
                t["extracted_ok"] += row["quantity_ok"] and row["unit_ok"]
            pred_pn = _norm_pn(row["part_number"])
            row["part_ok"] = pred_pn == exp_pn
            if exp_pn is None:
                t["no_part"] += 1
                t["abstained"] += pred_pn is None
            else:
                t["with_part"] += 1
                t["top1_right"] += pred_pn == exp_pn
            if p is not None and not p.get("needs_review") and pred_pn is not None:
                t["accepted"] += 1
                t["accepted_right"] += pred_pn == exp_pn
            rows.append(row)

        if "customer_id" in key:
            customers_total += 1
            customers_right += key.get("customer_id") == pred.get("customer_id")
        per_rfq[rfq_id] = t
        for k in totals:
            totals[k] += t[k]

    def ratio(a: int, b: int) -> float:
        return round(a / b, 4) if b else 0.0

    sweep = []
    for th in SWEEP:
        acc = [r for r in rows if r["part_number"] is not None and r["confidence"] is not None
               and accepted_at({"ridgeline_part_number": r["part_number"], "confidence": float(r["confidence"]),
                                "flags": r["flags"]}, th)]
        right = sum(r["part_ok"] for r in acc)
        sweep.append({"threshold": th, "auto_accepted": len(acc), "precision": ratio(right, len(acc)),
                      "recall": ratio(right, totals["with_part"])})

    predictions_without_key = {}
    keyed = set(per_rfq)
    for out_file in sorted(OUTPUT_DIR.glob("*.json")):
        if out_file.stem in keyed:
            continue
        pred = json.loads(out_file.read_text(encoding="utf-8"))
        lines = pred.get("lines", [])
        predictions_without_key[out_file.stem] = {
            "lines": len(lines), "needs_review": sum(bool(l.get("needs_review")) for l in lines),
            "matched": sum(l.get("ridgeline_part_number") is not None for l in lines),
            "customer_id": pred.get("customer_id")}

    return {
        "threshold": threshold,
        "extraction_accuracy": ratio(totals["extracted_ok"], totals["expected"]),
        "match_precision": ratio(totals["accepted_right"], totals["accepted"]),
        "match_recall": ratio(totals["accepted_right"], totals["with_part"]),
        "additional_metrics": {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "rfqs_with_answer_key": len(per_rfq),
            "line_recall": ratio(totals["aligned"], totals["expected"]),
            "line_precision": ratio(totals["aligned"], totals["predicted"]),
            "quantity_accuracy": ratio(totals["qty_ok"], totals["aligned"]),
            "unit_accuracy": ratio(totals["unit_ok"], totals["aligned"]),
            "top1_part_accuracy": ratio(totals["top1_right"], totals["with_part"]),
            "abstain_accuracy": ratio(totals["abstained"], totals["no_part"]),
            "review_rate": ratio(totals["aligned"] - totals["accepted"], totals["aligned"]),
            "customer_accuracy": ratio(customers_right, customers_total),
            "counts": totals,
            "per_rfq": per_rfq,
            "threshold_sweep": sweep,
            "rfqs_without_answer_key": predictions_without_key,
        },
        "_lines": rows,
    }


# --------------------------------------------------------------------------- report

def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _cell(v: Any) -> str:
    if v is None:
        return "–"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).replace("|", "\\|")


def write_report(res: dict[str, Any]) -> str:
    m, c = res["additional_metrics"], res["additional_metrics"]["counts"]
    th = res["threshold"]
    out = [
        "# Evaluation",
        "",
        f"Generated {m['generated_at']} by `python -m evaluation.evaluate`, from `outputs/*.json` "
        f"compared with `answer_key/` ({m['rfqs_with_answer_key']} RFQs, {c['expected']} expected lines). "
        "Only this script reads the answer key; the pipeline never does.",
        "",
        "## Headline",
        "",
        "| Metric | Value | Meaning |",
        "|---|---|---|",
        f"| **Extraction accuracy** | **{_pct(res['extraction_accuracy'])}** | Expected lines extracted with the right quantity and unit ({c['extracted_ok']}/{c['expected']}) |",
        f"| Match precision @ {th} | {_pct(res['match_precision'])} | Auto-accepted lines (no review) with the right part ({c['accepted_right']}/{c['accepted']}) |",
        f"| Match recall @ {th} | {_pct(res['match_recall'])} | Lines with an expected part, auto-accepted with the right one ({c['accepted_right']}/{c['with_part']}) |",
        "",
        "## Extraction",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Line recall (expected lines found) | {_pct(m['line_recall'])} ({c['aligned']}/{c['expected']}) |",
        f"| Line precision (extracted lines that are real items) | {_pct(m['line_precision'])} ({c['aligned']}/{c['predicted']}) |",
        f"| Quantity accuracy (on found lines) | {_pct(m['quantity_accuracy'])} ({c['qty_ok']}/{c['aligned']}) |",
        f"| Unit accuracy (on found lines, normalized) | {_pct(m['unit_accuracy'])} ({c['unit_ok']}/{c['aligned']}) |",
        f"| Customer identified | {_pct(m['customer_accuracy'])} |",
        "",
        "## Matching",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Top-1 part accuracy (right part, reviewed or not) | {_pct(m['top1_part_accuracy'])} ({c['top1_right']}/{c['with_part']}) |",
        f"| Abstain accuracy (no part expected, none given) | {_pct(m['abstain_accuracy'])} ({c['abstained']}/{c['no_part']}) |",
        f"| Review rate (found lines sent to a person) | {_pct(m['review_rate'])} |",
        "",
        "### Threshold sweep",
        "",
        "A line is auto-accepted when its confidence is at least the threshold and it has no blocking flag.",
        "",
        "| Threshold | Auto-accepted | Precision | Recall |",
        "|---|---|---|---|",
    ]
    for s in m["threshold_sweep"]:
        mark = " ← current" if abs(s["threshold"] - th) < 1e-9 else ""
        out.append(f"| {s['threshold']}{mark} | {s['auto_accepted']} | {_pct(s['precision'])} | {_pct(s['recall'])} |")
    out += ["", "## Per RFQ", "",
            "| RFQ | Expected | Extracted | Found | Qty ✓ | Unit ✓ | Right part | Auto-accepted (right) |",
            "|---|---|---|---|---|---|---|---|"]
    for rfq, t in m["per_rfq"].items():
        if "error" in t:
            out.append(f"| {rfq} | {t['error']} | | | | | | |")
            continue
        out.append(f"| {rfq} | {t['expected']} | {t['predicted']} | {t['aligned']} | {t['qty_ok']} | {t['unit_ok']} | "
                   f"{t['top1_right']}/{t['with_part']} | {t['accepted']} ({t['accepted_right']}) |")
    out += ["", "## Line by line", "",
            "✓ right, ✗ wrong. Review = sent to a person.", "",
            "| RFQ | # | As written (key) | Qty exp / got | Unit exp / got | Part exp / got | Method | Conf | Review | Flags |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for r in res["_lines"]:
        q = "✓" if r.get("quantity_ok") else "✗"
        u = "✓" if r.get("unit_ok") else "✗"
        pt = "✓" if r["part_ok"] else "✗"
        found = r["predicted_line"] is not None
        out.append(
            f"| {r['rfq_id']} | {r['line']} | {_cell(r['as_written'])} | "
            + (f"{_cell(r['expected_quantity'])} / {_cell(r['quantity'])} {q} | "
               f"{_cell(r['expected_unit'])} / {_cell(r['unit'])} {u} | " if found else "not extracted | | ")
            + f"{_cell(r['expected_part_number'])} / {_cell(r['part_number'])} {pt} | {_cell(r['match_method'])} | "
            f"{_cell(r['confidence'])} | {'yes' if r['needs_review'] else 'no' if found else '–'} | "
            f"{', '.join(r['flags']) or '–'} |")
    if m["rfqs_without_answer_key"]:
        out += ["", "## RFQs without an answer key", "",
                "Not scored. Shown so the review load is visible.", "",
                "| RFQ | Lines | Matched | Need review | Customer |", "|---|---|---|---|---|"]
        for rfq, t in m["rfqs_without_answer_key"].items():
            out.append(f"| {rfq} | {t['lines']} | {t['matched']} | {t['needs_review']} | {_cell(t['customer_id'])} |")
    out += ["", "## How to read this", "",
            "- **Extraction accuracy** scores the LLM step: did each requested item come out, with the right "
            "quantity and unit? Units are normalized (`ea`, `each`, `EA` are the same).",
            "- **Match precision** is the number that matters most (\"flag, don't guess\"): of the lines the "
            "system would let through without a person, how many have the right part.",
            "- **Match recall** is how much work the system saves: the share of lines that are both right and "
            "auto-accepted. The rest go to review.",
            "- Predicted lines are paired with expected lines by text similarity of `as_written`, one to one, "
            f"so a renumbered line still counts (minimum similarity {ALIGN_MIN_SCORE}/100).",
            ""]
    return "\n".join(out)


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    res = evaluate()
    EVAL_DIR.mkdir(exist_ok=True)
    public = {k: v for k, v in res.items() if not k.startswith("_")}
    (EVAL_DIR / "results.json").write_text(json.dumps(public, indent=2) + "\n", encoding="utf-8", newline="\n")
    (EVAL_DIR / "README.md").write_text(write_report(res), encoding="utf-8", newline="\n")
    m = res["additional_metrics"]
    print(f"extraction_accuracy={res['extraction_accuracy']}  match_precision={res['match_precision']}  "
          f"match_recall={res['match_recall']}  (threshold {res['threshold']})")
    print(f"line_recall={m['line_recall']}  line_precision={m['line_precision']}  "
          f"quantity={m['quantity_accuracy']}  unit={m['unit_accuracy']}  top1={m['top1_part_accuracy']}")
    print("wrote evaluation/results.json and evaluation/README.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

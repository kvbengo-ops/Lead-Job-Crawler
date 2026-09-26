"""Measure Laya's answers against hand labels and write eval/report.md.

Run from the project root:  .venv\\Scripts\\python.exe eval\\run_eval.py
Options: --labels PATH  --profile PATH  --report PATH

labels.jsonl has one JSON object per line. "text" is required; the rest are optional:
  {"text": "...", "title": "...", "company": "...", "location": "...",
   "type": "job", "relevant": true, "work_mode": "remote"}
"type" and "work_mode" are compared with the Laya answers of the same name, and "relevant"
with the "relevance" score (relevant when the score is >= 0.5). Include "title" when the
posting has one: production requests include it, so labels without it measure a different input.
"""
import argparse
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import db, pipeline  # noqa: E402
from app.laya_client import evaluate  # noqa: E402

CHOICE_QUESTIONS = ("type", "work_mode")
RELEVANT_AT = 0.5


def load_labels(path: Path) -> list[dict]:
    rows = []
    for n, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            sys.exit(f"{path}:{n}: not valid JSON ({e.msg})")
        if not isinstance(row, dict) or not str(row.get("text", "")).strip():
            sys.exit(f'{path}:{n}: each line needs a non-empty "text"')
        row["_line"] = n
        rows.append(row)
    if not rows:
        sys.exit(f"{path}: no labels found")
    return rows


def check_labels(rows: list[dict], profile: dict) -> None:
    for q in CHOICE_QUESTIONS:
        allowed = set((profile.get("questions", {}).get(q) or {}).get("criteria") or {})
        for row in rows:
            if q in row and allowed and row[q] not in allowed:
                sys.exit(f'line {row["_line"]}: {q} "{row[q]}" is not one of {sorted(allowed)}')


def pct(n: int, d: int) -> str:
    return f"{n}/{d} = {n / d:.0%}" if d else "n/a"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--labels", type=Path, default=ROOT / "eval" / "labels.jsonl")
    ap.add_argument("--profile", type=Path, help="a profile JSON file (default: the saved profile)")
    ap.add_argument("--report", type=Path, default=ROOT / "eval" / "report.md")
    args = ap.parse_args()

    if args.profile:
        profile = json.loads(args.profile.read_text(encoding="utf-8-sig"))
    else:
        db.init()
        profile = pipeline.load_profile()
    rows = load_labels(args.labels)
    check_labels(rows, profile)
    threshold = profile.get("confidence_threshold", 0.6)

    # per question: list of (correct, confident, expected, got, line)
    results: dict[str, list] = {}
    times, errors, model = [], [], None
    for i, row in enumerate(rows, 1):
        op = {k: row.get(k) for k in ("title", "company", "location")}
        op["description"] = row["text"]
        t = time.perf_counter()
        ev = evaluate(op, profile)
        times.append((time.perf_counter() - t) * 1000)
        if sys.stdout.isatty():
            print(f"\r{i}/{len(rows)}", end="", flush=True)
        if ev["error"]:
            errors.append((row["_line"], ev["error"]))
            continue
        model = ev["model"]
        answers = ev["answers"]
        for q in CHOICE_QUESTIONS:
            if q in row and q in answers:
                a = answers[q]
                results.setdefault(q, []).append(
                    (a["value"] == row[q], a["answer_confidence"] >= threshold, row[q], a["value"], row["_line"]))
        if "relevant" in row and "relevance" in answers:
            a = answers["relevance"]
            got = a["value"] >= RELEVANT_AT
            results.setdefault("relevance", []).append(
                (got == bool(row["relevant"]), a["answer_confidence"] >= threshold,
                 bool(row["relevant"]), f"{a['value']:.2f}", row["_line"]))
    print()
    if errors and len(errors) == len(rows):
        sys.exit(f"Every request failed. First error: {errors[0][1]}")

    out = [
        "# Laya evaluation report", "",
        f"- Labels: `{args.labels.name}` ({len(rows)} postings)",
        f"- Model: `{model}`, question version `{profile.get('question_version')}`",
        f"- Confidence threshold: {threshold:.0%}",
        f"- Time per posting: median {statistics.median(times):.0f} ms",
        "",
        "## Accuracy", "",
        "| Question | All answers | Confident (>= threshold) | Not confident |",
        "|---|---|---|---|",
    ]
    for q, res in results.items():
        conf = [r for r in res if r[1]]
        unconf = [r for r in res if not r[1]]
        out.append(f"| {q} | {pct(sum(r[0] for r in res), len(res))} | "
                   f"{pct(sum(r[0] for r in conf), len(conf))} | {pct(sum(r[0] for r in unconf), len(unconf))} |")
    out += ["", f"`relevance` counts as relevant when Laya's score is >= {RELEVANT_AT}.", ""]
    if "relevance" in results:
        # Ranking is what the score uses relevance for, so also report how often a posting you marked
        # relevant gets a higher relevance score than one you marked not relevant.
        pos = [float(r[3]) for r in results["relevance"] if r[2]]
        neg = [float(r[3]) for r in results["relevance"] if not r[2]]
        good = sum(p > n for p in pos for n in neg) + 0.5 * sum(p == n for p in pos for n in neg)
        pairs = len(pos) * len(neg)
        ranking = f"{good:g}/{pairs} = {good / pairs:.0%}" if pairs else "n/a (needs both relevant and not relevant labels)"
        out += [f"**Relevance ranking:** a relevant posting outranks a not-relevant one in {ranking} of pairs "
                "(50% is random, 100% is perfect).", ""]

    if "type" in results:
        labels = sorted({r[2] for r in results["type"]} | {r[3] for r in results["type"]})
        counts = Counter((r[2], r[3]) for r in results["type"])
        out += ["## Type confusion matrix", "", "Rows are your labels, columns are Laya's answers.", "",
                "| label \\ Laya | " + " | ".join(labels) + " |", "|---" * (len(labels) + 1) + "|"]
        for exp in labels:
            out.append(f"| **{exp}** | " + " | ".join(str(counts[(exp, got)] or "") for got in labels) + " |")
        out.append("")

    misses = [(q, r) for q, res in results.items() for r in res if not r[0]]
    if misses:
        out += ["## Misses", "", "| Line | Question | Your label | Laya | Confident |", "|---|---|---|---|---|"]
        out += [f"| {r[4]} | {q} | {r[2]} | {r[3]} | {'yes' if r[1] else 'no'} |" for q, r in sorted(misses, key=lambda m: m[1][4])]
        out.append("")
    if errors:
        out += ["## Errors", ""] + [f"- line {n}: {e}" for n, e in errors] + [""]

    args.report.write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out))
    print(f"\nWritten to {args.report}")


if __name__ == "__main__":
    main()

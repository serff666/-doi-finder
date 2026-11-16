"""Regression harness: run the live engine over a sample bibliography list and score it
against the hand-verified DOI mapping. Used to validate the app."""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter

from doi_engine import lookup_all, parse_entries

BASE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = os.path.join(BASE, "samples", "sample_180.txt")
EXPECTED = os.path.join(BASE, "samples", "sample_expected_dois.txt")
REPORT = os.path.join(BASE, "validation_report.md")


def norm_doi(d: str) -> str:
    return (d or "").strip().lower().rstrip(".")


def main():
    ap = argparse.ArgumentParser(description="Validate the engine against the author bibliography.")
    ap.add_argument("--author", default="", help="author hint passed to the engine (e.g. author)")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--report", default=REPORT)
    args = ap.parse_args()

    text = open(SAMPLE, encoding="utf-8").read()
    expected = [ln.strip() for ln in open(EXPECTED, encoding="utf-8").read().splitlines()]
    entries = parse_entries(text, "auto")
    assert len(entries) == len(expected), f"{len(entries)} entries vs {len(expected)} expected"

    t0 = time.time()

    def progress(done, total):
        sys.stderr.write(f"\r  {done}/{total}")
        sys.stderr.flush()

    results = lookup_all(entries, author_hint=(args.author or None), workers=args.workers,
                         cache=None, progress=progress)
    elapsed = time.time() - t0
    sys.stderr.write("\n")

    exact = different = missed = nodoi = 0
    rows = []
    for r, exp in zip(results, expected):
        got = norm_doi(r.get("doi"))
        exp_n = norm_doi(exp)
        if exp_n in ("", "nodoi"):
            verdict = "n/a"
            nodoi += 1
        elif got == exp_n:
            verdict = "exact"
            exact += 1
        elif got:
            verdict = "different"
            different += 1
        else:
            verdict = "missed"
            missed += 1
        rows.append({"n": r["n"], "title": r["title"], "expected": exp_n, "got": got,
                     "status": r.get("status"), "score": r.get("score"), "verdict": verdict,
                     "match_title": r.get("match_title", "")})

    scored = len(rows) - nodoi
    acc = exact / scored * 100 if scored else 0.0
    filled = sum(1 for r in results if r.get("doi"))
    hint = args.author or "—"

    lines = [
        "# Validation report — sample bibliography",
        "",
        f"- Input: `samples/sample_180.txt` ({len(entries)} titles, numbered, mixed bare-title and full-citation lines)",
        f"- Author hint: `{hint}`",
        f"- Runtime: {elapsed:.1f}s for {len(entries)} titles ({elapsed / len(entries):.2f}s/title)",
        f"- Rows with a DOI returned: **{filled}/{len(rows)}**",
        f"- Exact agreement with the verified mapping: **{exact}/{scored}** ({acc:.1f}%)",
        f"- Different DOI returned: {different} · No DOI returned: {missed} · No DOI exists: {nodoi}",
        "",
        "Status counts: " + ", ".join(f"{k}={v}" for k, v in Counter(r.get("status") for r in results).items()),
        "",
        "| # | Verdict | Expected | Returned | Status | Score | Title |",
        "|---:|---|---|---|---|---:|---|",
    ]
    for r in rows:
        if r["verdict"] == "exact":
            continue
        lines.append(
            f"| {r['n']} | {r['verdict']} | `{r['expected'] or '—'}` | `{r['got'] or '—'}` | "
            f"{r['status']} | {r['score']} | {r['title'][:80]} |"
        )
    with open(args.report, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    print(f"exact {exact}/{scored} ({acc:.1f}%) | different {different} | missed {missed} "
          f"| nodoi {nodoi} | {elapsed:.1f}s | author={hint}")
    print("report ->", args.report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Command-line interface: batch title -> DOI, exporting XLSX / MD / HTML / CSV / TXT.

Examples
--------
  python cli.py refs.txt
  python cli.py refs.txt -f xlsx md --author author
  type refs.txt | python cli.py - --mode titles -f csv
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from doi_engine import Cache, lookup_all, parse_entries
from exporters import EXPORTERS, summary

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(BASE, ".cache", "lookup_cache.json")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Batch-resolve DOIs from titles or full references.")
    ap.add_argument("input", help="input file (UTF-8) or '-' for stdin")
    ap.add_argument("-o", "--outdir", default="exports", help="output directory (default: exports)")
    ap.add_argument("-f", "--formats", nargs="+", default=["xlsx", "md"],
                    choices=sorted(EXPORTERS), help="export formats")
    ap.add_argument("--mode", default="auto", choices=["auto", "titles", "citations"])
    ap.add_argument("--author", default="", help="author hint used to disambiguate")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--rows", type=int, default=5, help="candidates per query")
    ap.add_argument("--mailto", default="", help="your email, for the CrossRef polite pool")
    ap.add_argument("--no-openalex", action="store_true", help="disable the OpenAlex fallback")
    ap.add_argument("--no-cache", action="store_true", help="bypass the on-disk cache")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.input == "-":
        text = sys.stdin.read()
        stem = "stdin"
    else:
        with open(args.input, encoding="utf-8-sig") as fh:
            text = fh.read()
        stem = os.path.splitext(os.path.basename(args.input))[0]

    entries = parse_entries(text, args.mode)
    if not entries:
        print("No titles found in input.", file=sys.stderr)
        return 2
    if not args.quiet:
        print(f"Parsed {len(entries)} entries from {args.input}", file=sys.stderr)

    t0 = time.time()
    last = [0.0]

    def progress(done, total):
        if args.quiet:
            return
        now = time.time()
        if now - last[0] > 0.15 or done == total:
            last[0] = now
            pct = int(done / total * 100)
            sys.stderr.write(f"\r  resolving {done}/{total} ({pct}%)")
            sys.stderr.flush()

    cache = None if args.no_cache else Cache(CACHE_PATH)
    results = lookup_all(entries, author_hint=(args.author or None), rows=args.rows,
                         workers=args.workers, mailto=args.mailto, cache=cache,
                         use_openalex=not args.no_openalex, progress=progress)
    if not args.quiet:
        sys.stderr.write(f"\r  resolved {len(results)}/{len(results)} in {time.time() - t0:.1f}s\n")

    os.makedirs(args.outdir, exist_ok=True)
    written = []
    for fmt in args.formats:
        fn, _ctype, ext = EXPORTERS[fmt]
        data = fn(results)
        path = os.path.join(args.outdir, f"{stem}.{ext}")
        mode = "wb" if isinstance(data, bytes) else "w"
        with open(path, mode, **({} if mode == "wb" else {"encoding": "utf-8"})) as fh:
            fh.write(data)
        written.append(path)

    if not args.quiet:
        print(f"\n{summary(results)}")
        for r in results:
            if not r.get("doi"):
                print(f"  ! row {r['n']}: {r['title'][:70]}", file=sys.stderr)
        for p in written:
            print(f"  -> {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

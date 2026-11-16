"""Field-level validation for title→DOI candidates.

Adapted from Crossref's SBMV (search-based matching with validation), MIT licensed,
https://github.com/CrossRef/reference-matching-evaluation

The idea, and why it beats pure title similarity: a reference string is full of
*bibliographic assertions* — volume, year, issue, pages, the numbers inside the
title, the first author's surname. A correct DOI should explain most of them.
A wrong DOI usually explains the title and nothing else.

Crossref measured this on 2,000 real reference strings: F1 96.3% versus 92.5%
for parsing-based matching, with precision down only 0.9 points and recall up
8.9 points. The gain comes from the validation step, not from better search.

We keep Crossref's generalised Jaccard and its "at least three independent
pieces of evidence" gate, but score against our own candidate dicts and use
difflib instead of fuzzywuzzy so the frozen build stays dependency-free.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

_DASH = "\u002D\u00AD\u2010\u2011\u2012\u2013\u2014\u2015\u207B\u208B\u2212"
NUM_RE = re.compile(r"(?<!\d)\d+(?!\d)")
_DOI_IN_REF = re.compile(r"(?<!\d)10\.\d{4,9}/[-\._;\(\)/:a-zA-Z0-9]+")
_ARXIV_RE = re.compile(r"(?<![a-zA-Z0-9])arXiv:[\d\.]+", re.I)
_BRACKET_RE = re.compile(r"\[[^\[\]]*\]")
_PAGE_RANGE_RE = re.compile(r"\d+[" + _DASH + r"]\d+")

# a reference that carries no numbers at all cannot be validated this way
MIN_NUMBERS = 2


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return s.encode("ascii", "ignore").decode().lower().strip()


def partial_ratio(a: str, b: str) -> float:
    """Best alignment of the shorter string inside the longer one, 0..1.

    Same intent as fuzzywuzzy's partial_ratio: an author surname should match
    even when it sits in a long reference string.
    """
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    sm = SequenceMatcher(None, a, b)
    m = sm.find_longest_match(0, len(a), 0, len(b))
    if m.size == 0:
        return 0.0
    start = max(0, m.b - m.a)
    window = b[start:start + len(a)]
    return SequenceMatcher(None, a, window).ratio()


def _numbers(s: str) -> list[str]:
    return NUM_RE.findall(s or "")


def _complete_page_ranges(ref: str) -> str:
    """'1425-37' -> '1425-1437', so the candidate's full last page can match."""
    for pages in _PAGE_RANGE_RE.findall(ref):
        nums = NUM_RE.findall(pages)
        if len(nums) != 2:
            continue
        first, last = nums
        if len(first) > len(last) and int(first[-len(last):]) <= int(last):
            ref = ref.replace(pages, first + "-" + first[: len(first) - len(last)] + last, 1)
    return ref


def reference_numbers(ref_string: str) -> list[str]:
    """The numbers a reference asserts, with DOIs/arXiv ids/brackets removed.

    DOIs and arXiv ids are dropped on purpose: they are their own strong signal
    and would otherwise mask a weak match on volume/page.
    """
    ref = _DOI_IN_REF.sub(" ", ref_string or "")
    ref = _ARXIV_RE.sub(" ", ref)
    ref = _BRACKET_RE.sub(" ", ref)
    ref = _complete_page_ranges(ref)
    return _numbers(ref)


def score(candidate: dict, ref_string: str) -> float:
    """Generalised Jaccard over the assertions in `ref_string`, 0..1.

    0 means either "the reference asserts too little to judge" or "fewer than
    three independent pieces of evidence line up" — in both cases the candidate
    must not be trusted on its own.
    """
    if not ref_string or not ref_string.strip():
        return 0.0
    ref_numbers = reference_numbers(ref_string)
    if len(ref_numbers) < MIN_NUMBERS:
        return 0.0

    # Every assertion is a (candidate weight, reference weight) pair. min/max
    # across the pair is what makes this a Jaccard rather than a plain sum:
    # fields the candidate claims but the reference never mentions pull it down.
    cand: dict[str, float] = {}
    ref: dict[str, float] = {}

    def claim(name: str, value: str) -> None:
        """A candidate field with numbers must have each number in the reference."""
        for i, number in enumerate(_numbers(value)):
            key = f"{name}_{i}"
            cand[key] = 1.0
            ref[key] = 0.0
            if number in ref_numbers:
                ref[key] = 1.0
                ref_numbers.remove(number)

    year = candidate.get("year")
    # Crossref stores volume and year in the same slot surprisingly often
    # ("J. Chem. Phys. 1998, 108" vs volume 108); duplicate so both can match.
    vol = str(candidate.get("volume") or "")
    if vol and year is not None and vol == str(year) and ref_numbers.count(vol) == 1:
        ref_numbers.append(vol)

    if vol:
        claim("volume", vol)
    if year is not None:
        claim("year", str(year))
    if candidate.get("issue"):
        claim("issue", str(candidate["issue"]))
    if candidate.get("page"):
        claim("page", str(candidate["page"]))
    if candidate.get("title"):
        claim("title", str(candidate["title"]))
    if candidate.get("journal"):
        claim("ctitle", str(candidate["journal"]))

    authors = candidate.get("authors") or []
    surname = ""
    if authors:
        surname = norm(str(authors[0]).split()[-1]) if str(authors[0]).strip() else ""
    if surname:
        cand["author"] = 1.0
        # the head of a reference string is where the first author lives
        ref["author"] = partial_ratio(surname, norm(ref_string)[: 3 * max(1, len(surname))])

    # A year printed one off (online-first vs issue) is common and forgivable.
    if year is not None and ref.get("year_0", 0) == 0:
        for delta in (-1, 1):
            alt = str(int(year) + delta)
            if alt in ref_numbers:
                ref["year_0"] = 0.5
                ref_numbers.remove(alt)
                break

    # The gate: without three independent corroborations, a title-ish match is
    # exactly the kind of coincidence that produces wrong DOIs.
    support = 0
    if candidate.get("title") and partial_ratio(norm(str(candidate["title"])), norm(ref_string)) > 0.7:
        support += 1
    if ref.get("year_0", 0) > 0:
        support += 1
    if ref.get("volume_0", 0) == 1:
        support += 1
    if ref.get("author", 0) > 0.7:
        support += 1
    if ref.get("page_0", 0) == 1:
        support += 1
    if support < 3:
        return 0.0

    # Numbers the reference carries that this candidate cannot explain are
    # evidence against it (a page number from a different article, say).
    for i, _ in enumerate(ref_numbers):
        cand[f"rest_{i}"] = 0.0
        ref[f"rest_{i}"] = 1.0

    num = sum(min(cand[k], ref[k]) for k in cand)
    den = sum(max(cand[k], ref[k]) for k in cand)
    return 1.0 if den == 0 else num / den


def looks_scoreable(raw: str) -> bool:
    """Is this a full reference, or just a title that happens to contain digits?

    A bare title such as "B800-to-B850 relaxation of excitation energy" has two
    numbers and no year; treating it as a reference makes the validator reject a
    match it never had the information to judge. Requiring a publication year
    plus several asserted numbers separates the two cases cleanly.
    """
    s = raw or ""
    if len(s) < 40:
        return False
    if not re.search(r"(?<!\d)(?:19|20)\d{2}(?!\d)", s):
        return False
    return len(reference_numbers(s)) >= MIN_NUMBERS + 1

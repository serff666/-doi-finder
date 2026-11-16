"""One filing rule for every page.

    <workspace>/<collection>/<sub>/<file>.pdf

* workspace  - one root folder the user picks once (e.g. D:\\Literature)
* collection - the first level under it, named after what is being collected:
               an author, a journal, a research group, a topic ...
* sub        - how a collection is divided, chosen by a *structure*:
               flat | year | journal | journal_year | volume_issue

Pure functions only (no network, no UI), so the rules can be tested and documented in one place.
"""
from __future__ import annotations

import os
import re

STRUCTURES = ("flat", "year", "journal", "journal_year", "volume_issue")
INBOX = "未分类"
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Names Windows refuses to create as files or folders, whatever the extension.
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
UNKNOWN_YEAR = "年份未知"
UNKNOWN_JOURNAL = "期刊未知"


def sanitize(name: str, maxlen: int = 110) -> str:
    name = _BAD.sub(" ", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > maxlen:
        name = name[:maxlen].rstrip(" .")
    if name.split(".")[0].strip().upper() in _RESERVED:
        name = "_" + name
    return name or "untitled"


def collection_name(name: str) -> str:
    """A safe single folder name for a collection; empty means the inbox."""
    name = " ".join(str(name or "").split())
    return sanitize(name, 80) if name else INBOX


_YEAR_IN = re.compile(r"(?<!\d)(19[89]\d|20\d\d)(?!\d)")


def find_year(text, *, last: bool = False) -> str:
    """A publication year found inside free text or a file name, '' if none.

    The year must stand alone (not inside a longer digit run such as the DOI tail
    acs.joc.4c02061, which holds "2061") and must not lie in the future.
    """
    import datetime
    limit = datetime.date.today().year + 1
    text = str(text or "")
    alone, ranged = [], []
    for m in _YEAR_IN.finditer(text):
        if not 1980 <= int(m.group(1)) <= limit:
            continue
        # "2052–2061" in "2025, 90, 2052–2061" is a page range, not years
        in_range = (re.search(r"\d\s*[–—-]\s*$", text[:m.start()])
                    or re.match(r"\s*[–—-]\s*\d", text[m.end():]))
        (ranged if in_range else alone).append(m.group(1))
    years = alone or ranged
    if not years:
        return ""
    return years[-1] if last else years[0]


def year_of(value) -> str:
    m = re.match(r"\s*(\d{4})", str(value or ""))
    return m.group(1) if m else ""


def volume_issue(volume, issue) -> str:
    vol, iss = str(volume or "").strip(), str(issue or "").strip()
    if vol and iss:
        return f"Volume {vol}, Issue {iss}"
    if vol:
        return f"Volume {vol}"
    if iss:
        return f"Issue {iss}"
    return "No volume-issue info"


def sub_for(structure: str, meta: dict | None) -> str:
    """The folder(s) under the collection for one paper, '/'-separated ('' = directly in the collection).

    meta: year, journal, volume, issue (any may be missing)."""
    meta = meta or {}
    year = year_of(meta.get("year")) or UNKNOWN_YEAR
    jname = str(meta.get("journal") or "").strip()
    journal = sanitize(jname, 80) if jname else UNKNOWN_JOURNAL
    if structure == "year":
        return year
    if structure == "journal":
        return journal
    if structure == "journal_year":
        return f"{journal}/{year}"
    if structure == "volume_issue":
        return f"{year}/{sanitize(volume_issue(meta.get('volume'), meta.get('issue')), 60)}"
    return ""


def destination(workspace: str, collection: str) -> str:
    """<workspace>/<collection>; an empty collection means the workspace itself (old behaviour)."""
    workspace = (workspace or "").strip()
    if not collection or not str(collection).strip():
        return workspace
    return os.path.join(workspace, collection_name(collection))


def inside(path: str, folder: str) -> bool:
    try:
        p = os.path.normcase(os.path.realpath(path))
        f = os.path.normcase(os.path.realpath(folder))
        return os.path.commonpath([p, f]) == f
    except (ValueError, OSError):
        return False

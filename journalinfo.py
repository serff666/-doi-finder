"""Optional journal facts (review time, JCR quartile, CAS zone, impact factor).

Source: the public CrushSCI-Journal-Review-Time dataset (CC BY-NC 4.0, non-commercial use,
attribution required): https://github.com/wangshunqi2020-commits/CrushSCI-Journal-Review-Time
The data is NOT bundled with this project. The user downloads it onto their own machine with one
click ("update journal data"), and it is only used to display facts next to a journal.
Column names are matched by keyword (English or Chinese), so a header change does not break it.
"""
from __future__ import annotations

import csv
import io
import os
import re
import threading
import time
import urllib.request

REPO = "wangshunqi2020-commits/CrushSCI-Journal-Review-Time"
URLS = [f"https://raw.githubusercontent.com/{REPO}/{b}/data/journals.csv" for b in ("main", "master")]
ATTRIBUTION = ("数据：CrushSCI-Journal-Review-Time（CC BY-NC 4.0，仅限非商业使用）。"
               "审稿天数是收稿到录用的日历天数，仅为观察样本，不代表保证周期。")
HOMEPAGE = f"https://github.com/{REPO}"

_DIR = ""
_LOCK = threading.Lock()
_CACHE: dict = {"mtime": 0.0, "by_issn": {}, "by_name": {}, "count": 0}

# field -> (keywords of which any must appear in the header, keywords that exclude it)
_RULES = {
    "eissn": (("eissn", "e-issn", "e_issn"), ()),
    "issn": (("issn",), ("eissn", "e-issn", "e_issn")),
    "name": (("journal_name", "journal name", "期刊名", "刊名", "name", "title"),
             ("subject", "学科", "publisher", "出版", "url", "link")),
    "publisher": (("publisher", "出版"), ()),
    "avg_days": (("avg", "average", "平均"), ("sample", "样本", "count", "impact", "jif", "rank")),
    "median_days": (("median", "中位"), ()),
    "min_days": (("min", "最短", "最小"), ()),
    "max_days": (("max", "最长", "最大"), ()),
    "samples": (("sample", "样本"), ()),
    "quartile": (("quartile", "jcr_q", "q区", "jcr分区", "jcr 分区"), ()),
    "cas": (("cas", "中科院", "中国科学院"), ("emerging", "新锐")),
    "jif": (("impact", "jif", "影响因子"), ()),
    "index": (("index", "scie", "ssci", "收录"), ()),
    "oa": (("open", "oa_", "_oa", "开放"), ()),
    "articles": (("article", "发文", "published_count", "pub_count"), ("review", "date", "日期")),
    "url": (("url", "link", "链接"), ()),
    "updated": (("update", "更新"), ()),
}


def configure(data_dir: str) -> None:
    global _DIR
    _DIR = data_dir


def path() -> str:
    return os.path.join(_DIR or ".", "journal_info.csv")


def _norm_issn(s: str) -> str:
    return re.sub(r"[^0-9Xx]", "", s or "").upper()


def _num(v: str):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _map_columns(header: list[str]) -> dict[str, int]:
    low = [h.strip().lower() for h in header]
    cols: dict[str, int] = {}
    taken: set[int] = set()
    for field in ("eissn", "issn", "name", "publisher", "avg_days", "median_days", "min_days", "max_days",
                  "samples", "quartile", "cas", "jif", "index", "oa", "articles", "url", "updated"):
        inc, exc = _RULES[field]
        for i, h in enumerate(low):
            if i in taken or any(x in h for x in exc):
                continue
            if any(x in h for x in inc):
                cols[field] = i
                taken.add(i)
                break
    return cols


def _parse(text: str) -> tuple[dict, dict, int]:
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        raise ValueError("文件是空的")
    cols = _map_columns(rows[0])
    if "issn" not in cols and "eissn" not in cols:
        raise ValueError("认不出 ISSN 列，数据格式可能变了")
    by_issn, by_name = {}, {}
    for r in rows[1:]:
        def g(f):
            i = cols.get(f)
            return (r[i].strip() if i is not None and i < len(r) else "")
        rec = {"name": g("name"), "publisher": g("publisher"), "index": g("index"), "oa": g("oa"),
               "cas": g("cas"), "quartile": g("quartile"), "jif": _num(g("jif")),
               "avg_days": _num(g("avg_days")), "median_days": _num(g("median_days")),
               "min_days": _num(g("min_days")), "max_days": _num(g("max_days")),
               "samples": _num(g("samples")), "articles": _num(g("articles")),
               "url": g("url"), "updated": g("updated")}
        for f in ("issn", "eissn"):
            for part in re.split(r"[/;,\s]+", g(f)):
                k = _norm_issn(part)
                if len(k) == 8:
                    by_issn[k] = rec
        if rec["name"]:
            by_name[rec["name"].lower()] = rec
    return by_issn, by_name, len(by_issn)


def _load() -> dict:
    p = path()
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return {"by_issn": {}, "by_name": {}, "count": 0, "mtime": 0.0}
    with _LOCK:
        if _CACHE["mtime"] != mt:
            with open(p, encoding="utf-8", errors="replace") as fh:
                by_issn, by_name, n = _parse(fh.read())
            _CACHE.update(by_issn=by_issn, by_name=by_name, count=n, mtime=mt)
        return _CACHE


def status() -> dict:
    try:
        c = _load()
    except Exception as e:  # noqa: BLE001
        return {"loaded": False, "count": 0, "error": str(e), "attribution": ATTRIBUTION, "homepage": HOMEPAGE}
    when = time.strftime("%Y-%m-%d", time.localtime(c["mtime"])) if c["mtime"] else ""
    return {"loaded": bool(c["count"]), "count": c["count"], "downloaded": when,
            "attribution": ATTRIBUTION, "homepage": HOMEPAGE}


def lookup(issn: str = "", name: str = "") -> dict | None:
    c = _load()
    k = _norm_issn(issn)
    rec = c["by_issn"].get(k) if k else None
    if rec is None and name:
        rec = c["by_name"].get(name.strip().lower())
    return dict(rec) if rec else None


def update(opener=urllib.request.urlopen) -> dict:
    """Download the dataset next to the app's other data, validating it before replacing the old copy."""
    last = ""
    for url in URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "DOI-Finder"})
            with opener(req, timeout=60) as r:
                raw = r.read()
            text = raw.decode("utf-8-sig", "replace")
            _by_issn, _by_name, n = _parse(text)         # raises if the format is not recognised
            if n < 100:
                raise ValueError(f"只解析到 {n} 本期刊，数据不完整")
            tmp = path() + ".part"
            os.makedirs(os.path.dirname(path()) or ".", exist_ok=True)
            with open(tmp, "wb") as fh:
                fh.write(raw)
            os.replace(tmp, path())
            _CACHE["mtime"] = 0.0
            return status()
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
    raise RuntimeError(f"下载失败（{last}）。也可以手动下载 data/journals.csv 放到：{path()}")

"""权限记忆：记住哪些期刊或出版社你没有权限，下次直接跳过，不再白开浏览器。

这个设计借鉴 PaperSearch 的 entitlements。它解决的问题很实际：一批文献里往往连续几篇
来自同一家没有订阅的出版社，每篇都开一次浏览器、等一次挑战页，浪费十几分钟。
用户标过一次"这家没有权限"，后面就该立刻跳过。

三种标记：
  no_access（期刊级）      这本期刊没订阅
  no_access（出版社级）    这家出版社整体没权限，除非某本期刊被单独标为有权限
  has_access（期刊级）     有权限，覆盖出版社级的跳过

期刊用 ISSN 认，拿不到 ISSN 时退回出版社前缀（DOI 的 10.xxxx）。
"""
from __future__ import annotations

import platform_util
import json
import os
import threading
import time

_LOCK = threading.Lock()
_CACHE: dict | None = None


def _path() -> str:
    base = os.environ.get("DOIFINDER_DATA") or platform_util.data_dir()
    return os.path.join(base, "entitlements.json")


def _load() -> dict:
    global _CACHE
    with _LOCK:
        if _CACHE is not None:
            return _CACHE
        try:
            with open(_path(), encoding="utf-8") as f:
                _CACHE = json.load(f)
        except Exception:  # noqa: BLE001
            _CACHE = {"journals": {}, "publishers": {}}
        _CACHE.setdefault("journals", {})
        _CACHE.setdefault("publishers", {})
        return _CACHE


def _save() -> None:
    data = _load()
    p = _path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except Exception:  # noqa: BLE001
        pass


def publisher_of(doi: str) -> str:
    parts = (doi or "").split("/")
    return parts[0].lower() if len(parts) > 1 else ""


def mark(doi: str = "", issn: str = "", publisher: str = "", has_access: bool = False) -> None:
    """记录一次用户判断。issn 优先，其次 publisher。"""
    has_access = bool(has_access)
    data = _load()
    if issn:
        data["journals"][issn.replace("-", "").strip()] = {
            "access": has_access, "at": int(time.time())}
    elif publisher or doi:
        key = (publisher or publisher_of(doi)).lower()
        if key:
            data["publishers"][key] = {"access": has_access, "at": int(time.time())}
    _save()


def skip_reason(doi: str = "", issn: str = "") -> str:
    """返回跳过原因；空字符串表示照常尝试。"""
    data = _load()
    key = (issn or "").replace("-", "").strip()
    j = data["journals"].get(key) if key else None
    if j and j.get("access"):
        return ""            # 期刊级有权限，覆盖出版社级跳过
    if j and not j.get("access"):
        return "该期刊已标记为无权限"
    pub = publisher_of(doi)
    p = data["publishers"].get(pub) if pub else None
    if p and not p.get("access"):
        return f"出版社 {pub} 已标记为无权限"
    return ""


def summary() -> dict:
    d = _load()
    return {"journals": len(d["journals"]), "publishers": len(d["publishers"]),
            "path": _path()}

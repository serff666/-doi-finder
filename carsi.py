"""机构登录（CARSI / Shibboleth）：下载都失败之后的最后一条路。

做法是让用户手动登录一次，把会话留在浏览器配置目录里，之后就能复用。

为什么必须手动：CARSI 要选学校、走统一身份认证、可能要双因子，任何自动化都会卡在
验证码上。软件能做的只是把用户送到正确的那一页，然后把会话存下来。

和 PaperSearch 的差别：它的 CARSI 是给检索用的，我们是给"这篇文章下不到"用的，
所以入口挂在失败的文献行上，一次只处理一篇。
"""
from __future__ import annotations

import os
import re
import urllib.parse

import platform_util

PROFILE_DIR = os.path.join(platform_util.data_dir(), "carsi_profile")

# 出版社站点 -> CARSI 或登录入口
PUBLISHER_HOME = {
    "10.1016": "https://www.sciencedirect.com",
    "10.1002": "https://onlinelibrary.wiley.com",
    "10.1021": "https://pubs.acs.org",
    "10.1038": "https://www.nature.com",
    "10.1126": "https://www.science.org",
    "10.1007": "https://link.springer.com",
    "10.1080": "https://www.tandfonline.com",
    "10.1093": "https://academic.oup.com",
    "10.1109": "https://ieeexplore.ieee.org",
    "10.1145": "https://dl.acm.org",
    "10.1088": "https://iopscience.iop.org",
    "10.1039": "https://pubs.rsc.org",
    "10.1016/j": "https://www.sciencedirect.com",
}

CARSI_PORTAL = "https://www.carsi.edu.cn/"


def publisher_of(doi: str) -> str:
    for k, v in PUBLISHER_HOME.items():
        if (doi or "").startswith(k):
            return v
    return ""


def article_url(doi: str) -> str:
    """统一走 doi.org 跳转。出版社的 PII、document id 从 DOI 推不出来，硬拼会 404。"""
    if not (doi or "").strip():
        return ""
    return "https://doi.org/" + urllib.parse.quote(doi.strip())


def profile_ready() -> bool:
    """配置目录里有 cookie 文件，粗略判断之前登录过。"""
    if not os.path.isdir(PROFILE_DIR):
        return False
    for root, _dirs, files in os.walk(PROFILE_DIR):
        for f in files:
            if f.lower() in ("cookies", "cookies-journal", "network persistent state"):
                return True
    return False


def open_login(doi: str, timeout: int = 300) -> dict:
    """打开出版社页面，让用户登录。返回 {ok, url, note}。

    用持久化配置目录，登录一次之后 downloader 走 browser 路线时能直接利用这个会话。
    """
    url = article_url(doi)
    if not url:
        return {"ok": False, "url": "", "note": "识别不出这个 DOI 的出版社"}
    try:
        import browser  # 复用已有的浏览器封装
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "url": url, "note": f"浏览器组件不可用：{e}"}

    os.makedirs(PROFILE_DIR, exist_ok=True)
    try:
        ok, note = browser.open_login(PROFILE_DIR, url)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "url": url, "note": f"{type(e).__name__}: {e}"}
    return {"ok": bool(ok), "url": url,
            "note": note or ("浏览器已打开，登录完成后回到软件重新下载" if ok else "打开失败")}


def find_pdf_link(html: str) -> str:
    m = re.search(r'<meta[^>]+name="citation_pdf_url"[^>]+content="([^"]+)"', html or "", re.I)
    if m:
        return m.group(1)
    m = re.search(r'href="([^"]+/pdf[^"]*)"', html or "", re.I)
    return m.group(1) if m else ""


if __name__ == "__main__":
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else "10.1016/j.cplett.2019.136746"
    print("publisher:", publisher_of(d))
    print("article  :", article_url(d))
    print("profile  :", profile_ready(), PROFILE_DIR)

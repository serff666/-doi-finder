"""用 OpenAlex 的学科分类替代靠标题猜。

OpenAlex 给每条文献标注了 topics（domain / field / subfield）和 keywords，这是学术
分类体系，不是关键词表。一次请求可以批量取 50 个 DOI，正好一页。
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

UA = "DOIFinder/2.2 (local desktop app)"

# OpenAlex 的 subfield 名 → 中文。只映射化学相关的常用项，其余保留英文。
SUBFIELD_ZH = {
    "Physical and Theoretical Chemistry": "物理化学",
    "Organic Chemistry": "有机化学",
    "Inorganic and Nuclear Chemistry": "无机化学",
    "Analytical Chemistry": "分析化学",
    "Catalysis": "催化",
    "Materials Chemistry": "材料化学",
    "Computational Chemistry": "计算化学",
    "Biochemistry": "生物化学",
    "Medicinal Chemistry": "药物化学",
    "Molecular Biology": "分子生物学",
    "Spectroscopy": "光谱学",
    "Condensed Matter Physics": "凝聚态物理",
    "Atomic and Molecular Physics, and Optics": "原子分子与光学",
    "Polymers and Plastics": "高分子",
    "Nanotechnology": "纳米技术",
    "Materials Science": "材料科学",
    "Energy": "能源",
    "Electrochemistry": "电化学",
    "Biophysics": "生物物理",
    "Chemical Engineering": "化学工程",
    "Environmental Chemistry": "环境化学",
}

KEYWORD_ZH = {
    "catalysis": "催化", "photocatalysis": "光催化", "electrocatalysis": "电催化",
    "density functional theory": "密度泛函", "molecular dynamics": "分子动力学",
    "machine learning": "机器学习", "spectroscopy": "光谱", "exciton": "激子",
    "perovskite": "钙钛矿", "quantum dot": "量子点", "protein": "蛋白质",
    "enzyme": "酶", "membrane": "膜", "polymer": "高分子", "battery": "电池",
    "electrode": "电极", "luminescence": "发光", "spin": "自旋",
    "charge transfer": "电荷转移", "energy transfer": "能量转移",
    "nonadiabatic": "非绝热", "polariton": "极化激元", "cavity": "光学腔",
    "aromaticity": "芳香性", "reaction mechanism": "反应机理",
}


def _get(url: str, timeout: int = 25) -> dict:
    import netpolicy
    url = netpolicy.prepare(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def topics_for(dois: list[str], mailto: str = "") -> dict[str, dict]:
    """批量取 topics 和 keywords。返回 {doi小写: {field, domain, tags}}。"""
    clean = []
    for d in dois:
        d = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", (d or "").strip(), flags=re.I).lower()
        if d and d not in clean:
            clean.append(d)
    if not clean:
        return {}
    out: dict[str, dict] = {}
    for i in range(0, len(clean), 40):
        chunk = clean[i:i + 40]
        flt = "doi:" + "|".join(chunk)
        url = ("https://api.openalex.org/works?per-page=40&filter="
               + urllib.parse.quote(flt, safe=":|./-")
               + ("&mailto=" + urllib.parse.quote(mailto) if mailto else ""))
        try:
            data = _get(url)
        except Exception:  # noqa: BLE001 - 限流就跳过，界面上退回本地规则
            continue
        for w in data.get("results") or []:
            doi = (w.get("doi") or "").replace("https://doi.org/", "").lower()
            if not doi:
                continue
            sub = ((w.get("primary_topic") or {}).get("subfield") or {}).get("display_name") or ""
            field = ((w.get("primary_topic") or {}).get("field") or {}).get("display_name") or ""
            domain = ((w.get("primary_topic") or {}).get("domain") or {}).get("display_name") or ""
            kws = [k.get("display_name") for k in (w.get("keywords") or []) if k.get("display_name")]
            tags = []
            for k in kws[:5]:
                zh = KEYWORD_ZH.get(k.lower())
                tags.append(f"{zh}" if zh else k)
            out[doi] = {"field": SUBFIELD_ZH.get(sub, sub), "field_en": sub,
                        "domain": domain, "field_wide": field, "tags": tags[:4]}
    return out

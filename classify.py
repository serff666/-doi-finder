"""给文献打学科标签：有机 / 无机 / 物理化学 / 分析 / 计算 / 材料 / 催化 / 生物。

两个层次，先看期刊，再看标题：

1. 期刊口径清楚时直接定（Org. Lett. 不会发无机，JCTC 不会发合成）。这一步准确率高，
   因为期刊的收稿范围是固定的。
2. 综合刊（JACS、Angew、Nature、Science 这类什么都发）才看标题关键词。这一步只能是
   粗略判断，所以标签写成"可能"，用户不会把它当成确定结论。

不联网、不调模型：分类要快、要能解释、要离线可用。
"""
from __future__ import annotations

import re

# 期刊名或 ISSN → 学科。按前缀/关键词匹配，顺序即优先级。
JOURNAL_RULES: list[tuple[str, str]] = [
    # 有机
    ("organic letters", "有机"), ("journal of organic chemistry", "有机"),
    ("org. lett", "有机"), ("j. org. chem", "有机"), ("european journal of organic", "有机"),
    ("advanced synthesis", "有机"), ("organic & biomolecular", "有机"),
    # 无机与配位
    ("inorganic chemistry", "无机"), ("dalton", "无机"),
    ("journal of inorganic", "无机"), ("european journal of inorganic", "无机"),
    ("organometallics", "无机"), ("inorganica chimica", "无机"),
    # 物理化学
    ("journal of physical chemistry", "物理化学"), ("physical chemistry chemical physics", "物理化学"),
    ("chemical physics letters", "物理化学"), ("journal of chemical physics", "物理化学"),
    ("faraday discussions", "物理化学"), ("chemphyschem", "物理化学"),
    ("journal of chemical theory", "计算"), ("journal of computational chemistry", "计算"),
    ("journal of chemical information", "计算"), ("journal of cheminformatics", "计算"),
    ("computational", "计算"),
    # 分析
    ("analytical chemistry", "分析"), ("analytica chimica", "分析"),
    ("analyst", "分析"), ("journal of analytical", "分析"), ("talanta", "分析"),
    # 催化
    ("acs catalysis", "催化"), ("journal of catalysis", "催化"), ("catalysis", "催化"),
    # 材料与能源
    ("acs nano", "材料"), ("nano letters", "材料"), ("advanced materials", "材料"),
    ("chemistry of materials", "材料"), ("acs applied", "材料"),
    ("journal of materials", "材料"), ("nanoscale", "材料"),
    ("acs energy", "能源"), ("energy & environmental", "能源"),
    ("batteries", "能源"), ("electrochem", "电化学"),
    # 生物与医药
    ("biochemistry", "生物"), ("journal of the american chemical society", "综合"),
    ("biophysical", "生物"), ("journal of medicinal chemistry", "药物化学"),
    ("acs chemical biology", "化学生物学"), ("bioconjugate", "化学生物学"),
    # 综合刊
    ("angewandte", "综合"), ("nature", "综合"), ("science", "综合"),
    ("chemical science", "综合"), ("chemical reviews", "综述"),
    ("chemical society reviews", "综述"), ("accounts of chemical", "综述"),
    ("proceedings of the national academy", "综合"), ("communications chemistry", "综合"),
    ("journal of the american chemical", "综合"), ("acs central science", "综合"),
    ("acs omega", "综合"), ("chemistry – a european", "综合"), ("chem", "综合"),
]

# 综合刊才用标题判断。每条是 (正则, 标签)，命中第一条即止。
TITLE_RULES: list[tuple[str, str]] = [
    (r"\b(dft|density functional|ab initio|wave ?function|quantum chemistry|"
     r"machine learning|neural network|molecular dynamics|monte carlo|"
     r"first-principles|computational|simulation|in silico)\b", "计算"),
    (r"\b(crystal structure|coordination (polymer|compound|complex|environment|sphere)|"
     r"metal[–-]organic framework|"
     r"mof\b|luminescence|lanthanide|cluster complex|chelat)\b", "无机"),
    (r"\b(catalys|photocatalys|electrocatalys|enzyme|catalytic)\b", "催化"),
    (r"\b(synthes(is|ized|is of)|total synthesis|asymmetric|enantioselective|"
     r"cycloaddition|arylation|c–h activation|protecting group|natural product)\b", "有机"),
    (r"\b(spectroscop|photophysics|photochem|excited state|dynamics|relaxation|"
     r"thermodynamic|kinetics|vibrational|electronic structure|band structure)\b", "物理化学"),
    (r"\b(quantum dot|perovskite|nanoparticle|nanocrystal|thin film|2d material|"
     r"graphene|polymer|electrode|battery|supercapacitor|semiconductor)\b", "材料"),
    (r"\b(protein|dna|rna|antibod|cell|in vivo|biosensor|drug|ligand binding|"
     r"biomarker|antimicrobial|virus)\b", "生物"),
    (r"\b(detection|sensing|sensor|chromatograph|mass spectrom|electroanalys|"
     r"quantification|assay)\b", "分析"),
]


def _find(rules, text: str, skip_wide: bool = False):
    t = (text or "").lower()
    for key, field in rules:
        if key in t:
            if skip_wide and field in ("综合", "综述"):
                continue
            return field
    return ""


def classify(journal: str = "", title: str = "", issn: str = "") -> dict:
    """返回 {field, basis}。basis 说明判断依据，便于读者自行取舍。"""
    j = (journal or "").strip()
    by_journal = _find(JOURNAL_RULES, j + " " + (issn or ""))
    if by_journal and by_journal not in ("综合", "综述"):
        return {"field": by_journal, "basis": "期刊"}

    by_title = _find(TITLE_RULES, title)
    if by_title:
        return {"field": by_title, "basis": "标题（综合刊，仅供参考）"}
    if by_journal:
        return {"field": by_journal, "basis": "期刊"}
    return {"field": "", "basis": ""}


# 标签分三类：这条用了什么方法、研究什么对象、关心什么性质。
# 带前缀是为了让读者一眼分清"这是手段"还是"这是对象"，不是一堆散词。
CATEGORY_TAGS: list[tuple[str, str, str]] = [
    # (正则, 前缀, 中文)
    (r"\bdft\b|density functional", "方法", "密度泛函"),
    (r"ab initio|first-principles", "方法", "第一性原理"),
    (r"coupled cluster|ccsd", "方法", "耦合簇"),
    (r"molecular dynamics|\bmd\b", "方法", "分子动力学"),
    (r"monte carlo", "方法", "蒙特卡洛"),
    (r"machine learning|neural network|deep learning", "方法", "机器学习"),
    (r"quantum master equation|master equation", "方法", "量子主方程"),
    (r"path integral", "方法", "路径积分"),
    (r"hierarchical equations|lindblad", "方法", "开放体系方法"),
    (r"spectroscop|spectra\b|spectrum", "方法", "光谱"),
    (r"\bx-ray\b|diffraction", "方法", "衍射"),
    (r"electrochem", "方法", "电化学"),
    (r"high-throughput|screening", "方法", "高通量筛选"),

    (r"perovskite", "对象", "钙钛矿"),
    (r"quantum dot|nanocrystal", "对象", "量子点"),
    (r"graphene|carbon nanotube", "对象", "碳材料"),
    (r"metal[–-]organic framework|\bmof\b", "对象", "金属有机框架"),
    (r"protein|enzyme", "对象", "蛋白质/酶"),
    (r"\bdna\b|\brna\b", "对象", "核酸"),
    (r"antibod", "对象", "抗体"),
    (r"catalyst|electrocatalyst|photocatalyst", "对象", "催化剂"),
    (r"polymer", "对象", "高分子"),
    (r"battery|electrode|electrolyte", "对象", "电池材料"),
    (r"cavity|polariton", "对象", "光学腔"),
    (r"exciton", "对象", "激子"),
    (r"spin", "对象", "自旋"),
    (r"light-harvesting|photosystem|antenna", "对象", "光捕获体系"),
    (r"membrane|lipid", "对象", "膜体系"),

    (r"excited state|photophysic|photochem", "性质", "激发态"),
    (r"charge transfer|electron transfer", "性质", "电荷转移"),
    (r"energy transfer", "性质", "能量转移"),
    (r"proton transfer", "性质", "质子转移"),
    (r"relaxation|decoherence|dephasing", "性质", "弛豫/退相干"),
    (r"thermodynamic|entropy|free energy", "性质", "热力学"),
    (r"kinetics|rate constant", "性质", "动力学"),
    (r"magnetic|magnetism", "性质", "磁性"),
    (r"catalysis|catalytic", "性质", "催化性能"),
    (r"selectivity|enantioselective", "性质", "选择性"),
    (r"stability|degradation", "性质", "稳定性"),
    (r"transport|conductiv|diffusion", "性质", "输运"),
    (r"aromatic|conjugation", "性质", "芳香性"),
    (r"luminescence|fluorescence|phosphorescence", "性质", "发光"),
]


def tags(title: str, limit: int = 6) -> list[str]:
    """按 方法 / 对象 / 性质 三类给标签，每类最多两条，顺序固定。

    这样读者扫一眼就知道：用了什么手段、研究什么东西、关心什么性质。
    类别是固定的，缺哪类就不显示，不拿泛词凑数。
    """
    t = (title or "").lower()
    buckets: dict[str, list[str]] = {"方法": [], "对象": [], "性质": []}
    for pat, kind, label in CATEGORY_TAGS:
        if len(buckets[kind]) >= 2:
            continue
        if re.search(pat, t) and label not in buckets[kind]:
            buckets[kind].append(label)
    out: list[str] = []
    for kind in ("对象", "方法", "性质"):
        for label in buckets[kind]:
            out.append(f"{kind}·{label}")
    return out[:limit]


def enrich(items: list[dict], journal: str = "", issn: str = "") -> list[dict]:
    """给一批文献加 field 字段，期刊名缺省时用整刊名兜底。"""
    for it in items:
        j = it.get("journal") or journal
        got = classify(j, it.get("title") or "", it.get("issn") or issn)
        it["field"] = got["field"]
        it["field_basis"] = got["basis"]
        it["tags"] = tags(it.get("title") or "")
    return items



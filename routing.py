"""Per-family download routing.

The old design used one fixed order for every DOI. That is wrong in a way that
costs real time: a ChemRxiv preprint is open by definition and lives on its own
site, while a brand-new ACS article exists nowhere except behind the subscriber's
institutional session. Ordering all of them the same way means the preprint path
wastes time on Sci-Hub and the ACS path wastes time on Sci-Hub and the OA graph
before reaching the one channel that works.

So the plan is computed from three facts about the article:
  1. who registered the DOI (the prefix identifies the publisher family)
  2. when it was published (Sci-Hub's coverage thins out sharply after ~2021)
  3. what the user has configured (publisher API keys, an institutional session)

Step names map to callables in downloader.py.
"""
from __future__ import annotations

# Open by definition: the publisher's own site is the canonical and fastest source.
PREPRINT = {
    "10.48550": "arXiv",
    "10.26434": "ChemRxiv",
    "10.1101": "bioRxiv/medRxiv",
    "10.21203": "Research Square",
    "10.2139": "SSRN",
    "10.20944": "Preprints.org",
}

# Fully open-access publishers: a deterministic PDF URL exists.
OA_PUBLISHER = {
    "10.1371": "PLOS",
    "10.3389": "Frontiers",
    "10.3390": "MDPI",
    "10.7554": "eLife",
    "10.5194": "Copernicus",
    "10.1186": "BMC",
    "10.1038/s41598": "Scientific Reports",
}

# Publishers with a licensed API the user can hold a key for.
API_PUBLISHER = {
    "10.1016": "elsevier",
    "10.1002": "wiley",
}

# Subscription publishers with no public API. Access is only through a
# subscribed session (institutional browser) or, for older years, Sci-Hub.
SUBSCRIPTION = {
    "10.1021": "ACS",
    "10.1063": "AIP",
    "10.1039": "RSC",
    "10.1126": "Science/AAAS",
    "10.1038": "Springer Nature",
    "10.1007": "Springer",
    "10.1080": "Taylor & Francis",
    "10.1177": "SAGE",
    "10.1088": "IOP",
    "10.1093": "Oxford",
    "10.1149": "ECS",
    "10.1089": "Mary Ann Liebert",
    "10.1002/anie": "Wiley Angewandte",
}

# Sci-Hub's bulk coverage stops around 2021; later papers are hit or miss.
SCI_HUB_LAST_GOOD_YEAR = 2021


def family(doi: str) -> str:
    d = (doi or "").lower()
    for p in PREPRINT:
        if d.startswith(p + "/"):
            return "preprint"
    # journal-specific prefixes (e.g. 10.1038/s41598) must win over the bare prefix
    for p in OA_PUBLISHER:
        if "/" in p and d.startswith(p):
            return "oa_publisher"
    head = d.split("/")[0]
    if head in OA_PUBLISHER:
        return "oa_publisher"
    if head in API_PUBLISHER:
        return "api_publisher"
    if head in SUBSCRIPTION:
        return "subscription"
    return "unknown"


def is_recent(year) -> bool:
    try:
        return int(year) > SCI_HUB_LAST_GOOD_YEAR
    except (TypeError, ValueError):
        return True  # unknown year: assume recent, the safer assumption


def plan(doi: str, year=None, has_inst: bool = False, has_api: bool = False,
         scihub_on: bool = True) -> list[str]:
    """Ordered step names for this DOI.

    Steps: api | direct | browser | scihub | oa | pmc
    """
    fam = family(doi)
    recent = is_recent(year)
    steps: list[str] = []

    def add(*names):
        for n in names:
            if n not in steps:
                steps.append(n)

    if fam == "preprint":
        # Open by definition; Sci-Hub would be slower and less complete. Some
        # preprint hosts (ChemRxiv) sit behind a JS challenge that a real
        # browser clears without any human interaction, so it is the fallback
        # rather than Sci-Hub.
        add("direct", "oa")
        if has_inst:
            add("browser")
    elif fam == "oa_publisher":
        add("direct", "oa", "pmc")
    elif fam == "api_publisher":
        if has_api:
            add("api")
        add("direct")
        if recent and has_inst:
            add("browser")       # new papers: the licensed session is the real answer
        if scihub_on:
            add("scihub")
        add("oa", "pmc")
        if has_inst:
            add("browser")
    elif fam == "subscription":
        if recent:
            # Nothing else works for a 2025 paper from ACS/AIP/Science: no API,
            # no Sci-Hub, usually no OA. Go straight to the entitled session.
            if has_inst:
                add("browser")
            add("direct", "oa", "pmc")
            if scihub_on:
                add("scihub")
        else:
            if scihub_on:
                add("scihub")
            if has_inst:
                add("browser")
            add("direct", "oa", "pmc")
    else:
        add("direct", "oa", "pmc")
        if scihub_on:
            add("scihub")
        if has_inst:
            add("browser")

    # Sci-Hub is always the first source tried when it is enabled.
    if scihub_on:
        steps = ["scihub"] + [s for s in steps if s != "scihub"]
    return steps

"""Multi-field matching: year / first author / journal evidence on top of title similarity.

Every network call is faked; nothing here touches the internet.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import citeparse  # noqa: E402
import doi_engine as de  # noqa: E402


def cr(doi, title, year=None, authors=(), journal="", **extra):
    """One Crossref work record. authors: ("Given Family", ...) or ("ORG:name",)."""
    a = []
    for name in authors:
        if name.startswith("ORG:"):
            a.append({"name": name[4:]})
        else:
            given, _, family = name.rpartition(" ")
            a.append({"given": given, "family": family})
    item = {"DOI": doi, "title": [title], "author": a,
            "container-title": [journal] if journal else []}
    if year:
        item["issued"] = {"date-parts": [[year]]}
    item.update(extra)
    return item


class FakeHttp:
    """Answers every Crossref search with `items`; /works/<doi> with the matching item."""
    mailto = ""

    def __init__(self, items, openalex=(), title_items=None, reject_authorships=False):
        self.items = list(items)
        self.openalex = list(openalex)
        self.title_items = None if title_items is None else list(title_items)
        self.reject_authorships = reject_authorships
        self.urls = []

    def get_json(self, url, tries=3):
        self.urls.append(url)
        if "api.openalex.org" in url:
            if self.reject_authorships and "authorships" in url:
                raise RuntimeError("HTTP 400")
            return {"results": self.openalex}
        if "query.title=" in url and self.title_items is not None:
            return {"message": {"items": self.title_items}}
        if "/works/" in url:
            doi = url.split("/works/", 1)[1]
            import urllib.parse
            doi = urllib.parse.unquote(doi)
            for it in self.items:
                if it["DOI"].lower() == doi.lower():
                    return {"message": it}
            raise RuntimeError("HTTP 404")
        return {"message": {"items": self.items}}


T = "Quantum dynamics of excitons in light harvesting complexes"


def run(items, expect=None, title=T, openalex=(), title_items=None, use_openalex=False, **kw):
    return de.resolve(title, FakeHttp(items, openalex, title_items), expect=expect,
                      use_openalex=use_openalex, **kw)


def oa(doi, title, year, authors=()):
    """One OpenAlex work in the shape the API returns."""
    return {"doi": "https://doi.org/" + doi, "title": title, "publication_year": year,
            "primary_location": {"source": {"display_name": "Some Journal"}}, "type": "article",
            "authorships": [{"author": {"display_name": a}} for a in authors]}


# ------------------------------------------------------------------ no regression
def test_plain_title_is_unchanged_and_has_no_evidence():
    r = run([cr("10.1/a", T, 2019, ["Ann Smith"])])
    assert r["status"] == "matched" and r["doi"] == "10.1/a"
    assert "evidence" not in r


def test_empty_expect_is_plain():
    for exp in (None, {}, {"year": "", "author": "  ", "journal": None}):
        r = run([cr("10.1/a", T, 2019, ["Ann Smith"])], expect=exp)
        assert r["status"] == "matched" and "evidence" not in r, exp


# ------------------------------------------------------------------ year
def test_year_match_near_mismatch():
    item = cr("10.1/a", T, 2019, ["Ann Smith"])
    r = run([item], {"year": "2019"})
    assert r["evidence"]["year"] == "match" and r["status"] == "matched"
    r = run([item], {"year": "2018"})
    assert r["evidence"]["year"] == "near" and r["status"] == "matched"
    r = run([item], {"year": "2020"})
    assert r["evidence"]["year"] == "near" and r["status"] == "matched"
    r = run([item], {"year": "2015"})
    assert r["evidence"]["year"] == "mismatch" and r["status"] == "likely", r["status"]
    assert "2015" in r["note"] and "2019" in r["note"]


def test_year_parsing_is_strict():
    for raw, want in (("2019", 2019), ("2019/2020", 2019), ("c. 1998", 1998), ("{2005}", 2005),
                      ("in press", None), ("n.d.", None), ("", None), ("12345", None),
                      ("0999", None), ("20191", None), (None, None)):
        assert de.parse_year(raw) == want, (raw, de.parse_year(raw))


def test_unparsable_year_means_unknown_not_mismatch():
    r = run([cr("10.1/a", T, 2019, ["Ann Smith"])], {"year": "in press"})
    assert r["status"] == "matched"
    assert "evidence" not in r or r["evidence"]["year"] == "unknown"


def test_candidate_without_year_is_unknown():
    r = run([cr("10.1/a", T, None, ["Ann Smith"])], {"year": "2019"})
    assert r["status"] == "matched" and r["evidence"]["year"] == "unknown"


def test_year_picks_the_right_version():
    pre = cr("10.1101/pre", T, 2018, ["Ann Smith"], "bioRxiv")
    pub = cr("10.1/pub", T + " ", 2020, ["Ann Smith"], "J Chem Phys")
    # the preprint is listed first and has the identical title
    r = run([pre, pub], {"year": "2020"})
    assert r["doi"] == "10.1/pub", r["doi"]
    r = run([pub, pre], {"year": "2018"})
    assert r["doi"] == "10.1101/pre", r["doi"]
    # alternates keep the other version
    assert any(a["doi"] == "10.1101/pre" for a in run([pre, pub], {"year": "2020"})["alts"])


# ------------------------------------------------------------------ author
def test_author_match_partial_mismatch_unknown():
    item = cr("10.1/a", T, 2019, ["Ann Smith", "Bo Chen"])
    assert run([item], {"author": "Smith, Ann"})["evidence"]["author"] == "match"
    assert run([item], {"author": "Ann Smith"})["evidence"]["author"] == "match"
    assert run([item], {"author": "Smith A"})["evidence"]["author"] == "match"
    r = run([item], {"author": "Chen, Bo"})
    assert r["evidence"]["author"] == "partial" and r["status"] == "matched"
    r = run([item], {"author": "Kowalski, Jan"})
    assert r["evidence"]["author"] == "mismatch" and r["status"] == "likely"
    assert "Kowalski" in r["note"]
    # nothing readable on one side: unknown, never a penalty
    assert run([item], {"author": "张伟"})["status"] == "matched"
    assert run([cr("10.1/a", T, 2019, [])], {"author": "Smith"})["evidence"]["author"] == "unknown"


def test_author_transliteration_and_diacritics():
    item = cr("10.1/a", T, 2019, ["Hans Müller"])
    assert run([item], {"author": "Mueller, Hans"})["evidence"]["author"] == "match"
    assert run([item], {"author": "Muller"})["evidence"]["author"] == "match"
    item = cr("10.1/a", T, 2019, ["Jan van der Berg"])
    assert run([item], {"author": "{van der Berg}, Jan"})["evidence"]["author"] == "match"
    assert run([item], {"author": "Berg"})["evidence"]["author"] == "match"


def test_author_is_token_not_substring():
    item = cr("10.1/a", T, 2019, ["Ann Williams"])
    assert run([item], {"author": "Li"})["evidence"]["author"] == "mismatch"


def test_organisation_author():
    item = cr("10.1/a", T, 2019, ["ORG:World Health Organization"])
    assert run([item], {"author": "World Health Organization"})["evidence"]["author"] == "match"
    assert run([item], {"author": "WHO"})["status"] in ("matched", "likely")


def test_two_contradictions_demote_twice():
    item = cr("10.1/a", T, 2019, ["Ann Smith"])
    r = run([item], {"year": "2001", "author": "Kowalski"})
    assert r["status"] == "uncertain", r["status"]
    assert r["doi"] == "10.1/a"          # still offered for review, never silently dropped


def test_contradiction_never_turns_a_hit_into_not_found():
    item = cr("10.1/a", T, 2019, ["Ann Smith"])
    r = run([item], {"year": "1990", "author": "Kowalski", "journal": "Nature"})
    assert r["doi"] == "10.1/a" and r["status"] == "uncertain"


# ------------------------------------------------------------------ promotion
def _mid_title():
    """A query whose similarity to T lands in the 'uncertain' band, found by search."""
    base = T.split()
    for k in range(1, len(base)):
        q = " ".join(base[:-k] + ["spectra"] * 1)
        s = de.similarity(q, T)
        if 0.70 <= s < 0.78:
            return q, s
    raise AssertionError("no probe title in band")


def test_promotion_needs_year_and_author():
    q, s = _mid_title()
    item = cr("10.1/a", T, 2019, ["Ann Smith"])
    assert run([item], None, title=q)["status"] == "uncertain"
    assert run([item], {"year": "2019"}, title=q)["status"] == "uncertain"
    assert run([item], {"author": "Smith"}, title=q)["status"] == "uncertain"
    r = run([item], {"year": "2019", "author": "Smith"}, title=q)
    assert r["status"] == "likely", (r["status"], s)
    # a near year is not a match
    assert run([item], {"year": "2018", "author": "Smith"}, title=q)["status"] == "uncertain"
    # and promotion never goes past 'likely'
    assert r["status"] != "matched"


def test_no_promotion_below_floor():
    q = "Quantum dynamics of excitons"          # similarity 0.68: uncertain, but under the floor
    assert 0.60 <= de.similarity(q, T) < de.PROMOTE_FLOOR
    item = cr("10.1/a", T, 2019, ["Ann Smith"])
    assert run([item], None, title=q)["status"] == "uncertain"
    r = run([item], {"year": "2019", "author": "Smith"}, title=q)
    assert r["status"] == "uncertain", r["status"]
    # and well below 0.60 nothing is claimed at all, evidence or not
    r = run([item], {"year": "2019", "author": "Smith"}, title="Quantum dynamics")
    assert r["status"] == "not_found" and not r["doi"]


# ------------------------------------------------------------------ journal
def test_journal_is_tiebreak_only():
    a = cr("10.1/a", T, 2019, ["Ann Smith"], "Physical Review Letters")
    b = cr("10.1/b", T, 2019, ["Ann Smith"], "Journal of Chemical Physics")
    assert run([a, b], {"journal": "J. Chem. Phys."})["doi"] == "10.1/b"
    assert run([b, a], {"journal": "Phys. Rev. Lett."})["doi"] == "10.1/a"
    # an unrecognised journal name must not hurt anything
    r = run([a], {"journal": "JACS"})
    assert r["status"] == "matched" and r["doi"] == "10.1/a"


def test_journal_abbreviation_helper():
    f = de.journal_matches
    assert f("J. Chem. Phys.", "The Journal of Chemical Physics")
    assert f("Phys. Rev. Lett.", "Physical Review Letters")
    assert f("Nature", "Nature")
    assert f("Angew. Chem. Int. Ed.", "Angewandte Chemie International Edition")
    assert not f("Nature", "Nature Communications")      # same words are not enough, same count is
    assert not f("Phys. Rev. B", "Physical Review Letters")
    assert not f("JACS", "Journal of the American Chemical Society")   # unknown, never a mismatch
    assert not f("", "Nature")
    assert not f("Nature", "")


# ------------------------------------------------------------------ author_hint (global)
def test_blank_author_hint_does_not_crash():
    r = run([cr("10.1/a", T, 2019, ["Ann Smith"])], None, author_hint="   ")
    assert r["status"] == "matched", r


def test_author_hint_is_not_substring_boosted():
    cands = [{"doi": "10.1/a", "title": T, "year": 2019, "authors": ["Ann Williams"],
              "source": "crossref", "score": 0.0}]
    de._score_all(cands, T, "Li")
    base = de.similarity(T, T)
    assert cands[0]["score"] == base, cands[0]["score"]
    cands = [{"doi": "10.1/a", "title": T, "year": 2019, "authors": ["Ann Smith"],
              "source": "crossref", "score": 0.0}]
    de._score_all(cands, T, "Smith")
    assert cands[0]["score"] == min(1.0, round(base + 0.06, 4)) or cands[0]["score"] == 1.0


# ------------------------------------------------------------------ provided DOI
def test_verified_supplied_doi_is_kept():
    # the typed DOI's own title agrees (0.90) but a sibling matches the query exactly (1.0)
    mine = cr("10.1/mine", "Quantum dynamics of excitons in light harvesting", 2018, ["Ann Smith"])
    other = cr("10.1/other", T, 2020, ["Ann Smith"])
    assert 0.85 <= de.similarity(T, mine["title"][0]) < 1.0
    r = run([other, mine], None, provided_doi="10.1/mine")
    assert r["doi"] == "10.1/mine", r["doi"]
    assert any(a["doi"] == "10.1/other" for a in r["alts"])
    assert "title agrees" in r["note"]
    # a supplied DOI whose title is unrelated is not "kept": the better record wins
    wrong = cr("10.1/wrong", "Completely unrelated study of glaciers in Norway", 2018, ["Ann Smith"])
    r = run([other, wrong], None, provided_doi="10.1/wrong")
    assert r["doi"] == "10.1/other" and r["provided_doi"] == "10.1/wrong"
    assert "differs" in r["note"]


def test_unverifiable_supplied_doi_does_not_block_better_match():
    other = cr("10.1/other", T, 2020, ["Ann Smith"])
    r = run([other], None, provided_doi="10.9/ghost")
    assert r["doi"] == "10.1/other" and r["status"] == "matched"
    assert r["provided_doi"] == "10.9/ghost" and "could not verify" in r["note"]
    # nothing better on offer: the typed DOI is reported as is
    r = run([cr("10.1/zzz", "Unrelated glacier work in Norway", 2001, ["Z Z"])], None,
            provided_doi="10.9/ghost")
    assert r["doi"] == "10.9/ghost" and r["status"] == "provided"


# ------------------------------------------------------------------ parsing
def test_bibtex_author_year_journal():
    bib = """@article{k1,
      author = {Smith, Ann and Doe, Jane},
      title = {A title that is long enough},
      journal = {J. Chem. Phys.}, year = {2019}}
    @article{k2, author = {{van der Berg}, Jan and others}, title = {Another one}, year = 2001}
    @article{k3, author = {Ann Smith and Bo Chen}, title = {Third}}
    @book{k4, title = {No author}}"""
    got = citeparse.parse_bibtex(bib)
    assert [g["author"] for g in got] == ["Smith, Ann", "van der Berg, Jan", "Ann Smith", ""]
    assert got[0]["year"] == "2019" and got[0]["journal"] == "J. Chem. Phys."


def test_ris_author():
    ris = "TY  - JOUR\nAU  - Smith, Ann\nAU  - Doe, Jane\nTI  - Some title\nPY  - 2019///\nER  - \n"
    got = citeparse.parse_ris(ris)
    assert got[0]["author"] == "Smith, Ann" and got[0]["year"] == "2019"


def test_parse_entries_carries_expectations():
    bib = "@article{k1, author = {Smith, Ann}, title = {A title that is long}, year = {2019}}"
    e = de.parse_entries(bib)[0]
    assert e["author"] == "Smith, Ann" and e["year"] == "2019"


# ------------------------------------------------------------------ batch: cache + dedup
def _with_fake(items, fn):
    orig = de.Http
    de.Http = lambda mailto="", timeout=25: FakeHttp(items)
    try:
        return fn()
    finally:
        de.Http = orig


def test_dedup_does_not_merge_entries_that_differ_in_year():
    pre = cr("10.1101/pre", T, 2018, ["Ann Smith"])
    pub = cr("10.1/pub", T, 2020, ["Ann Smith"])
    entries = [{"n": 1, "input": T, "title": T, "year": "2018"},
               {"n": 2, "input": T, "title": T, "year": "2020"}]
    res = _with_fake([pre, pub], lambda: de.lookup_all(entries, workers=1))
    assert [r["doi"] for r in res] == ["10.1101/pre", "10.1/pub"], [r["doi"] for r in res]


def test_dedup_does_not_merge_entries_with_different_supplied_doi():
    a = cr("10.1/a", T, 2018, ["Ann Smith"])
    b = cr("10.1/b", T, 2018, ["Ann Smith"])
    entries = [{"n": 1, "input": T, "title": T, "doi": "10.1/a"},
               {"n": 2, "input": T, "title": T, "doi": "10.1/b"}]
    res = _with_fake([a, b], lambda: de.lookup_all(entries, workers=1))
    assert [r["doi"] for r in res] == ["10.1/a", "10.1/b"], [r["doi"] for r in res]


def test_identical_plain_titles_still_deduplicate():
    item = cr("10.1/a", T, 2019, ["Ann Smith"])
    entries = [{"n": 1, "input": T, "title": T}, {"n": 2, "input": "2. " + T, "title": T}]
    http = FakeHttp([item])
    orig = de.Http
    de.Http = lambda mailto="", timeout=25: http
    try:
        res = de.lookup_all(entries, workers=1)
    finally:
        de.Http = orig
    assert res[0]["doi"] == res[1]["doi"] == "10.1/a"
    assert res[1].get("cached") is True


def test_cache_key_is_backward_compatible_for_plain_titles():
    assert de.Cache.key("Some Title", None) == de.norm_text("Some Title") + "||"
    assert de.Cache.key("Some Title", None, "y=2019") != de.Cache.key("Some Title", None)


# ------------------------------------------------------------------ candidate construction
def test_to_candidate_keeps_org_and_openalex_authors():
    c = de.to_candidate(cr("10.1/a", T, 2019, ["ORG:WHO", "Ann Smith"]), "crossref")
    assert c["authors"][0] == "WHO"
    oa = {"DOI": "10.1/b", "title": [T], "issued": {"date-parts": [[2019]]}, "author": [
        {"given": "Ann", "family": "Smith"}]}
    assert de.to_candidate(oa, "openalex")["authors"] == ["Ann Smith"]


# ------------------------------------------------------------------ gaps found by mutation testing
def test_no_promotion_when_the_reference_numbers_do_not_add_up():
    q, _ = _mid_title()
    item = cr("10.1/a", T, 2019, ["Ann Smith"], "J Chem Phys", volume="99", page="1-2")
    raw = q + ", Smith A, J. Chem. Phys. 2019, 120, 1234-1240."
    from validation import looks_scoreable
    assert looks_scoreable(raw)
    r = run([item], {"year": "2019", "author": "Smith"}, title=q, raw=raw)
    assert "not corroborated" in r["note"]
    assert r["status"] == "uncertain", r["status"]       # year+author agree, but the numbers do not


def test_openalex_supplies_authors_for_the_evidence():
    rec = oa("10.1/o", T, 2019, ["Ann Smith", "Bo Chen"])
    got = de.openalex_items(FakeHttp([], [rec]), T)
    assert got[0]["author"] == [{"name": "Ann Smith"}, {"name": "Bo Chen"}]
    r = run([], {"author": "Smith, Ann", "year": "2019"}, openalex=[rec], use_openalex=True)
    assert r["doi"] == "10.1/o" and r["evidence"] == {"year": "match", "author": "match"}, r
    r = run([], {"author": "Kowalski"}, openalex=[rec], use_openalex=True)
    assert r["evidence"]["author"] == "mismatch" and r["status"] == "likely"


def test_openalex_select_rejection_falls_back_instead_of_going_silent():
    http = FakeHttp([], [oa("10.1/o", T, 2019, ["Ann Smith"])], reject_authorships=True)
    got = de.openalex_items(http, T)
    assert got and got[0]["DOI"] == "10.1/o"
    assert sum("api.openalex.org" in u for u in http.urls) == 2
    # any other failure still propagates (and is handled by the caller)
    class Boom(FakeHttp):
        def get_json(self, url, tries=3):
            raise RuntimeError("HTTP 503")
    try:
        de.openalex_items(Boom([]), T)
    except RuntimeError as e:
        assert str(e) == "HTTP 503"
    else:
        raise AssertionError("expected RuntimeError")


def test_contradiction_triggers_the_second_search():
    pre = cr("10.1101/pre", T, 2018, ["Ann Smith"])
    pub = cr("10.1/pub", T, 2020, ["Ann Smith"])
    # page one only has the preprint (title score 1.0, so the usual rule would stop here);
    # the title-field search also finds the journal version
    r = run([pre], {"year": "2020"}, title_items=[pub])
    assert r["doi"] == "10.1/pub", r["doi"]
    # without an expectation the behaviour is exactly as before: no second search
    http = FakeHttp([pre], (), [pub])
    de.resolve(T, http, use_openalex=False)
    assert not any("query.title=" in u for u in http.urls)


def test_malformed_expect_never_raises_and_never_penalises():
    item = cr("10.1/a", T, 2019, ["Ann Smith"])
    for weird in ("2019", 2019, None, [], ["x"], 12.5, True, {"year": [2019]}, {"year": {"a": 1}},
                  {"year": float("nan")}, {"author": 123}, {"author": "1234"}, {"author": "---"},
                  {"journal": b"x"}, {"journal": 7}, {"author": "\u0000"}):
        r = run([item], weird)
        assert r["status"] == "matched" and r["doi"] == "10.1/a", (weird, r["status"], r["note"])
    assert de.norm_expect("2019") == {} and de.norm_expect({"author": "张伟"}) == {"author": "张伟"}


def test_contradiction_note_comes_first():
    item = cr("10.1/a", T, 2019, ["Ann Smith"])
    r = run([item], {"year": "2001"}, provided_doi="10.1/a")
    assert r["note"].startswith("year differs"), r["note"]
    assert "title agrees" in r["note"]


def test_alternates_carry_first_author_and_evidence():
    pre = cr("10.1101/pre", T, 2018, ["Ann Smith"])
    pub = cr("10.1/pub", T, 2020, ["Bo Chen"])
    r = run([pre, pub], {"year": "2020", "author": "Chen"})
    assert r["doi"] == "10.1/pub"
    alt = r["alts"][0]
    assert alt["doi"] == "10.1101/pre" and alt["first_author"] == "Ann Smith"
    assert alt["evidence"] == {"year": "mismatch", "author": "mismatch"}
    r = run([pre, pub], None)
    assert "evidence" not in r["alts"][0] and r["alts"][0]["first_author"]

# DOI Finder

**From "I need these papers" to a tidy local library of PDFs.**
A literature search, tracking and bulk-download tool that runs on your own computer. Have titles or references? It resolves them to DOIs and fetches the full texts. No list yet? Search by topic, author, ORCID, journal or a lab's publications page. Then it picks the right route per publisher, downloads the PDFs and files them into your workspace by one consistent rule.

The UI runs in your browser at `http://127.0.0.1:8765/`. No account, no registration; your data and keys stay on your machine.
Windows desktop app that also runs from source on Linux (see [README.linux.md](README.linux.md)).

> 中文说明见 [README.md](README.md)。

> **Disclaimer**
> This project is for **academic research and learning only** and is **not used for commercial profit**. The author provides and sells no publications.
> The app can connect to third-party sources, including Sci-Hub mirrors. Whether that is lawful depends on your country and institution;
> **whether and how to use it is your own decision and responsibility** (the Sci-Hub fallback can be turned off in Settings).
> Respect publishers' terms of service and copyright law; use downloaded papers for personal research only and do not redistribute them.
> 本项目仅用于学术研究与学习，不用于商业盈利；使用第三方来源的风险与责任由使用者自行承担。

## The problem

Papers usually start from one of two places:

- **You already have a list**: dozens or hundreds of titles, or a reference list, and you want the full texts.
- **You do not have a list yet**: you want everything a topic, an author, a journal or a lab has published.

Either way you hit the same problems. Searching each DOI by hand is error-prone; each publisher page is different (open access, subscription, captcha); and afterwards file names are a mess, you lose track of what you already have, and the same paper ends up in several folders.

DOI Finder chains finding, downloading and filing into one pipeline and **tells you, at every step, how trustworthy the result is and why something failed.**

## Highlights

**Title → DOI: accurate, and it shows its evidence**
- Not "take the first search hit": title similarity, first author, year, journal and volume/pages corroborate each other, giving four levels (high confidence / likely / needs a look / not found) with the reason for each.
- Paste one title per line, whole references, numbered lists, BibTeX or RIS; a trailing `doi:` is used directly.
- Crossref first, OpenAlex as fallback; a preprint and its journal version are told apart by year; Chinese papers can be completed from a Baidu Scholar link.
- Export Excel / Markdown / HTML / CSV / DOI list, and **GB/T 7714, ACS, APA** references.

**DOI → PDF: many routes, chosen per publisher**
- Order depends on DOI prefix and year: Elsevier / Wiley via their official APIs, subscription journals (ACS, RSC…) via your institutional browser, open publishers directly, preprints their own way.
- The open-access step queries OpenAlex, Unpaywall, Europe PMC, Semantic Scholar, PMC, OSTI, Zenodo, HAL, Crossref, DOAJ, CORE and Internet Archive Scholar in parallel and takes the first valid PDF.
- Sci-Hub / Sci-Net fallback (can be turned off): mirrors are ordered by success rate, ALTCHA challenges are solved automatically.
- **The institutional browser is always tried**: it reuses your own Chrome/Edge session (campus network or CARSI); the app never sees your password, and never skips it on a guess that you do not subscribe (the paper may be open access).
- **Preprint fallback**: when the journal version cannot be had, ChemRxiv is searched for the same paper. Title and DOI often differ, so the match uses the journal record's own link, title, first author and year, and every match says how it was made. For papers from 2025 on (often not in open-access indexes yet), a preprint the journal record itself names is tried right before the institutional browser.
- SSRN / Research Square preprint DOIs are traded for the published version when Crossref knows it.

**Robust batches**
- Resumable and de-duplicated (`.doi-index.json` in the folder); files are checked for a complete PDF and re-fetched if truncated.
- Pause / resume / stop; retry only what did not finish; show only the unfetched; export the unfetched list (CSV).
- Every row states the **channel it actually came from** and, if it failed, the reason (no open access / subscription / captcha / timeout).
- Filename template `{first_author}_{year}_{title}` (plus `{journal}`, `{doi}`); long titles, Chinese and Windows reserved names are handled.

**Several ways in, all able to download**
- **Topic search** (keywords / author / ORCID / years, optional open-access only), **ChemASAP**, **Journal feed** (what is new since you last looked), **Journal library** (a whole journal from its first issue, filed as `Journal/Year/Volume V, Issue I/`), **Lab pages** (any publications page, scraped in a real browser), **ResearchGate**, a dedicated **Preprints** page, and **Archive** for PDFs you got elsewhere.

**Every page on its own**
Each page downloads in place with its own progress panel (bar, pause, stop, retry, open folder) and its own **workspace, collection folder, structure, Sci-Hub switch, institutional-browser switch and worker count**. Download a journal on one page and a group's papers on another at the same time.

**Local and transparent**
No account; keys (Elsevier, Wiley, CORE, OpenAlex) are stored only in `settings.json` on your machine; English / Chinese UI; each channel can be switched off; certificate verification is on by default.

## Where files go: the workspace rule

Every page files the same way: `workspace / collection / sub / file.pdf`, e.g. `D:\Literature\Zhang Xinglong\2025\Zhang_2025_Title.pdf`.

- **Workspace**: one root folder you pick once (top bar).
- **Collection**: the first level, named after what is being collected and suggested automatically (editable on each page): topic search by author → the author's name (by year); by keywords → the keywords; ResearchGate → the author; journal feed → the journal (by year); journal library → the journal (`Year / Volume V, Issue I`); lab pages → the site's host (by year); ChemASAP → `ChemASAP` (by journal); preprints → `预印本`; ① and ② → `未分类` (inbox).
- **Sub**: flat / by year / by journal / journal → year / year → volume-issue, from the paper's metadata (missing values go to "年份未知" / "期刊未知", nothing is lost).
- Each page shows the full "will be saved to" path before you download.
- **No duplicates**: `.doi-index.json` at the workspace root remembers every file; a paper already filed under another collection is copied, not downloaded again; the per-folder indexes of older versions are recognised.

## Quick start

1. Start the app and open `http://127.0.0.1:8765/`; choose the workspace at the top and (optionally) put a contact email in Settings to be rate-limited less.
2. On **① Title → DOI** paste titles, click start, and look at the levels.
3. Click "→ download these PDFs" and watch the panel on the same page; for papers that failed use "find preprint version" or "retry".
4. Export the unfetched list, or open the download folder.

## Tabs

| Tab | Input | Output |
|---|---|---|
| ① Title → DOI | a list of titles or full citations | one DOI per line with confidence and evidence; exportable |
| ② DOI → PDF | a list of DOIs | local PDFs, normalised filenames, resumable |
| ③ Topic search | keywords, author, ORCID, years | matching papers; download on the same page |
| ④ ChemASAP | none | latest ASAP articles across chemistry (not only organic; ChemRxiv preprints included) |
| ⑤ Journal feed | journal name or ISSN | recent articles, unread marked |
| ⑥ ResearchGate | an author profile URL | that author's publication list |
| ⑦ Archive | a local PDF | renamed and filed into your library |
| ⑧ Lab pages | a group's publications page | titles, DOIs and PDF links from that page |
| ⑨ Journal library | a journal name or ISSN (optional year range) | the whole journal's article list, downloaded into `Journal/Year/Volume V, Issue I/` folders |
| ⑩ Preprints | a title or the journal DOI | the matching ChemRxiv preprint (title and DOI may differ from the published paper); tick and download on the same page |

Pasted BibTeX / RIS is matched on more than the title: year, first author and journal are compared with each candidate. A year two or more apart, or a first author who is none of the listed authors, lowers the status by one level and says so in the note; a missing field never counts against a match. When year and first author both agree and the title similarity is at least 0.70, an "uncertain" match is raised to "likely".

**Preprints.** ⑩ finds ChemRxiv preprints through the journal record's own link (Crossref `has-preprint`), ChemRxiv search and Crossref, and grades each candidate exact / likely / possible with the reason; only the first two are used automatically. A failed query is reported as an error, never as "none". It is also the last automatic step when every other route failed. A preprint may differ from the published version; downloads are labelled as preprints. The institutional browser is always tried when the route calls for it.

## Install and run

Installed build: the `DOI Finder` shortcut, or `%LOCALAPPDATA%\Programs\DOI Finder\DOI Finder.exe`.
The app is unsigned, so Windows may show a SmartScreen warning.

From source:

```bat
cd doi-finder
python server.py            :: starts the server and opens the browser
```

Requires Python 3.11+ (`pip install -r requirements.txt`). Excel export needs `openpyxl`; extracting text from PDFs needs `pypdf`.
The browser channel needs Chrome or Edge installed.

## First run

Two fields at the top are worth filling immediately:

- **Download folder** — where PDFs go.
- **Contact email** — sent to Crossref / Unpaywall / OpenAlex as part of the request. This puts you in their polite pool, which means fewer 429s. It is not an account and nothing is mailed to you.

No account or registration is required.

## How the download order is decided

Sci-Hub (if enabled) is always tried first; after that the order depends on the publisher prefix:

| Paper type | Order |
|---|---|
| Any paper (Sci-Hub enabled) | **Sci-Hub first**, then the rows below |
| ACS, RSC, Science, AIP and other subscription titles | institutional browser → direct → open access → PMC |
| Wiley (10.1002) | Wiley API → direct → open access → PMC |
| Elsevier (10.1016) | Elsevier API → direct → institutional browser → open access → PMC |
| Fully open publishers (MDPI, PLOS, Frontiers) | direct → open access → PMC |
| Preprints (arXiv, SSRN, ChemRxiv) | direct → open access → institutional browser |

Sci-Hub, when enabled, is always tried first.

Open-access resolution queries OpenAlex, Unpaywall, Europe PMC, Semantic Scholar, PMC, OSTI, Zenodo, HAL, Crossref, DOAJ, CORE (needs a free API key) and Internet Archive Scholar in parallel within a 15-second budget, and caches results for 24 hours.

## Journal facts (optional)

On the whole-journal tab, a panel can show a journal's average / median review time, JCR quartile, CAS zone and impact factor. The data comes from the public [CrushSCI-Journal-Review-Time](https://github.com/wangshunqi2020-commits/CrushSCI-Journal-Review-Time) dataset (CC BY-NC 4.0: non-commercial use, attribution required). It is **not bundled**: click "Download journal data" and it is saved on your own machine. Review days are calendar days from receipt to acceptance and are observed samples, not guaranteed timelines.

## Keys and privacy

- All keys live only in `%LOCALAPPDATA%\DOIFinder\settings.json` on your machine. Nothing is uploaded and nothing is bundled into the installer.
- This repository contains no keys; every field in `settings.example.json` is empty.
- Elsevier API keys and Wiley TDM tokens are issued to individual accounts and must not be shared.
- The Sci-Hub fallback is **on by default and tried first** (turn it off in the download tab). Whether using it is lawful depends on where you are; you are responsible for your own use. Mirror certificates are verified by default; a Settings checkbox can skip verification for mirrors with expired certificates, which you should only do on a trusted network.
- The institutional channel reuses your own browser session; the app never stores your password.

## Troubleshooting

**Many lookups fail / DOIs not found** — usually rate limiting. Fill in the contact email, lower the worker count, and run in smaller batches.

**Paper not downloaded although your institution subscribes** — click "institutional login" on that row, sign in once in the window that opens, then retry.

**Blank PDF pages** — older builds saved truncated files. Downloads are now validated by checking the PDF end marker; incomplete files are re-fetched.

**Where are the logs** — `%LOCALAPPDATA%\DOIFinder\doifinder.log`. Each download run logs every paper it failed to fetch, prefixed with `miss`.

**A button does nothing** — the status bar in the top right shows the script error and line number. A hard refresh (Ctrl+F5) usually clears it.

## Credits

See [NOTICE](NOTICE) for third-party projects this code was derived from or inspired by.

## License

MIT, see [LICENSE](LICENSE).

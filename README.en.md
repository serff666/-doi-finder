# DOI Finder

A literature search, DOI-resolution and bulk PDF download tool that runs on your own computer. The interface is a local web page in your browser; there are no accounts.

It does three things, and for every result it states the evidence or the reason for failure:

1. **Find papers**: build a list from titles, references, a topic, an author, an ORCID, a journal or a lab's publications page;
2. **Resolve DOIs**: match titles or whole references to DOIs and say how trustworthy each match is;
3. **Download and file**: fetch PDFs by a route chosen from the publisher and the publication year, and file them into your workspace by one rule.

> 中文说明：[README.md](README.md)　·　Linux: [README.linux.md](README.linux.md)　·　Changelog: [CHANGES.md](CHANGES.md)

> **Disclaimer**
> This project is for academic research and learning only and is not used for commercial profit. The author provides and sells no publications.
> The app can connect to third-party sources, including Sci-Hub mirrors. Whether that is lawful depends on your country and institution; **whether and how to use it is your own decision and responsibility**. The Sci-Hub fallback is on by default and can be turned off in Settings.
> Respect publishers' terms of service and copyright law; use downloaded papers for personal research only and do not redistribute them.

---

## 1. What it does and does not do

**It can**

- turn titles / references / BibTeX / RIS into DOIs in bulk, with the evidence for each match;
- download PDFs by DOI in bulk, recording the channel each file really came from and the reason for each failure;
- get a paper list from a topic, an author, a journal, a lab page, a ResearchGate profile or ChemASAP, and download from the same page;
- avoid repeating work: downloaded papers are recorded in `.doi-index.json` in the workspace.

**It does not promise**

- **That every paper can be fetched.** It depends on whether the paper is open access, whether your institution subscribes, whether the publisher blocks automated access, and whether a third-party source is up. What fails is reported with a reason.
- **That every DOI match is right.** Matches are graded by several pieces of evidence; low grades need your review.
- **To get around paywalls.** Subscription papers can only come from your own institutional session (§6.4) or from third-party sources you choose to enable.

## 2. Install and run

**Windows installer** (`DOI-Finder-Setup-<version>.exe` on the release page): a single-file installer, installs to `%LOCALAPPDATA%\Programs\DOI Finder` by default, needs no administrator rights, uninstalls from the Control Panel. It is not code-signed, so SmartScreen may warn: choose "More info → Run anyway". It is a desktop app: its own window (the system's WebView2), its own taskbar entry, native file dialogs, no address bar. Starting it a second time brings the open window forward.

**From source** (Python 3.11 or newer):

```bat
:: Windows
pip install -r requirements.txt
python desktop.py                  :: the desktop window; run_server.cmd does the same
```

```bash
# Linux / macOS
./run.sh                           # the first run creates .venv in the project and installs dependencies
```

- **The desktop window** (`desktop.py`) uses [pywebview](https://pywebview.flowrl.com/): WebView2 on Windows (built into Windows 10/11), WebKit on macOS, GTK or Qt on Linux (see [README.linux.md](README.linux.md)). Without pywebview, or without a GUI backend for it, the same interface opens in a Chrome / Edge app window or the default browser instead (`python server.py --open` does that directly).
- Closing the window stops the app; if a job is still running it asks first (downloaded files and history are kept either way). Size and position are remembered.
- The server listens on `127.0.0.1` only, default port 8765; if it is taken the next ports are tried (up to 40). Set the environment variable `DOIFINDER_PORT` to choose a port and `DOIFINDER_NO_BROWSER=1` to run only the server, with no window. There is no `--port` option.
- Optional packages (`requirements.txt`): `openpyxl` (Excel export), `pypdf` (guessing title and year from a PDF's first page when archiving) and `pywebview` (the desktop window). Without them those features are unavailable; the rest is unaffected.
- File / folder pickers and "Save as" are the system's own dialogs in the desktop window. In the browser fallback the pickers use Python's tkinter, and exports go to the browser's downloads; if tkinter is unavailable, type the path into the field.
- The institutional channel and some download steps need Chrome or Edge installed.

**What has been verified**: automated tests (unit tests, an end-to-end journal flow, browser-side UI checks) run on GitHub Actions on Windows and Ubuntu, Python 3.11 and 3.13. Real publisher traffic, institutional login and the availability of third-party sources are not covered by automation and can only be verified by use. Linux has been tested at the source level only; macOS has not been tested.

## 3. First use

Set two things first; the rest can wait:

| Where | What | Notes |
|---|---|---|
| "Workspace" in the top bar | one root folder, e.g. `D:\Literature` | every page files under it. Default: `~/Downloads/DOI Finder` (on Windows `C:\Users\<you>\Downloads\DOI Finder`) |
| "Contact email" on the Settings page | your own address | sent with requests to Crossref, OpenAlex, Unpaywall and similar services, which rate-limit such requests less. Optional, but HTTP 429 becomes more likely without it |

A typical flow: paste titles on **① Title → DOI**, click start, check the grades; click "→ download these PDFs" and follow the panel on the same page; open the download folder when done.

## 4. The interface

The window has a **sidebar** with the pages grouped by job (find papers · get full text · follow · organize) and **Settings** at the bottom, a **top bar** with the page's name and the workspace folder, and a **status bar** with the connection state, the running job and its progress (the window title shows the percentage too, so it is visible on the taskbar).

- **Command palette** (`Ctrl+K`, or the search box in the top bar): type part of a page or action name — "pdf", "settings", "dark" — and press Enter.
- **Shortcuts**: `Ctrl+1` … `Ctrl+0` open pages ① … ⑩; `Ctrl+,` opens Settings; `Ctrl+Enter` starts the current page's main action; `Ctrl+S` saves the settings. The sidebar collapses to icons (button at the bottom).
- **Light / dark**: follows the system by default; choose on the Settings page or with the moon button.
- **Drag and drop**: drop a `.txt` / `.md` / `.csv` / `.bib` / `.ris` file onto ①, ② or ⑩ and its text is appended to the input box. In the desktop window, PDFs dropped anywhere are filed straight away by the ⑦ Archive rules.
- **Notifications**: when a job finishes while the window is in the background, a system notification is shown (switch on the Settings page).

| Page | Input | Output |
|---|---|---|
| ① Title → DOI | titles, whole references, BibTeX, RIS | a DOI per entry with grade and evidence; exportable |
| ② DOI → PDF | a list of DOIs | local PDFs with each entry's source or failure reason |
| ③ Topic search | keywords; optionally author, ORCID, years, "open access only" | a paper list; tick and download on the page |
| ④ ChemASAP | none | the latest "ahead of print" articles from the third-party ChemASAP service |
| ⑤ Journal feed | journal name or ISSN | recent articles, marking what is new since your last refresh |
| ⑥ ResearchGate | an author profile URL | the publication list on that page (log in and pass the human check in the window first) |
| ⑦ Archive | PDFs you already have | renamed by the template and filed into the workspace |
| ⑧ Lab pages | a group's publications page URL | titles, DOIs and PDF links found on the page |
| ⑨ Journal library | journal name or ISSN, optional years | the journal's article list (Crossref), downloaded into `Journal/Year/Volume V, Issue I/` |
| ⑩ Preprints | a title or the journal DOI | candidate ChemRxiv preprints; check, then download |

Each page downloads in place with its own progress panel (progress, pause, stop, retry the unfinished, open folder) and its own workspace, collection folder, structure, Sci-Hub switch, institutional-browser switch and worker count; they do not interfere.

## 5. Title → DOI: how "right" is judged

**Input**: one title per line; whole references; numbered lists (`1.`, `2)`, `(3)`, `[4]`, `#5` are stripped); BibTeX; RIS. A trailing `doi: 10.xxxx/…` is used directly with no query.

**Sources**: Crossref first; when Crossref has no usable result, OpenAlex can be asked (the switch "Use OpenAlex when Crossref falls short", on by default). "Look up identical titles once" merges duplicates.

**Scoring**: title similarity is the base. If the input has a year, first author, journal or volume/pages, they are compared with each candidate:

- only a clear contradiction counts against a candidate: a year two or more apart, or a first author who is none of the candidate's authors, lowers the grade by one level and says so in the note; fields missing from the input never count against it;
- an "uncertain" match whose year and first author both agree and whose title similarity is at least 0.70 is raised to "likely";
- when a preprint and its journal version share a title, the one whose year fits is chosen and the other stays as an alternative.

**Grades**

| Grade | Meaning | What to do |
|---|---|---|
| High confidence | titles agree closely and no other evidence contradicts | use directly |
| Likely | similar title but not enough evidence | glance at the DOI link |
| Needs a look | a candidate was found but fields disagree | click "Edit" to confirm or type the DOI |
| Not found | no usable candidate | enter by hand, or use ③ Topic search |
| Lookup failed | the query itself did not succeed (network, rate limit…) | retry; this is not the same as "not found" |
| Provided / Confirmed | the input carried a DOI / you confirmed it | — |

**Export**: Excel, Markdown, HTML, CSV, a plain DOI list; reference formats GB/T 7714, ACS and APA, generated from Crossref metadata, with missing fields left blank, never guessed.

**Chinese papers**: CNKI and Wanfang papers often have no DOI in Crossref. Paste a Baidu Scholar paper link into "Baidu Scholar link → DOI" and the app fetches title, DOI, journal, volume and pages.

## 6. DOI → PDF: how the route is chosen

### 6.1 What decides the route

The app first classifies the DOI prefix by publisher, then combines that with the **publication year** (after 2021, or unknown, counts as recent) and your settings to build an ordered list of steps. They are tried in order; the first step that returns a valid file ends that paper. The steps:

| Step | What it does |
|---|---|
| `Sci-Hub` | tries Sci-Hub mirrors (ordered by past success rate) and Sci-Net; ALTCHA challenges are solved automatically |
| `API` | the Elsevier / Wiley official interface; needs your own key / token (§8) |
| `Open access` | see §6.3 |
| `PMC` | PubMed Central full text |
| `Institutional browser` | fetches the PDF with a session you logged into in your local Chrome/Edge (§6.4) |

**When Sci-Hub is enabled it comes first for every DOI**; the table below applies after it. When it is off, start from the table.

| DOI class | Order (steps not listed do not run) |
|---|---|
| Preprints (arXiv, ChemRxiv, bioRxiv/medRxiv, Research Square, SSRN, Preprints.org) | open access → (if the institutional channel is on) institutional browser |
| Fully open publishers (PLOS, Frontiers, MDPI, eLife, Copernicus, BMC, Scientific Reports) | open access → PMC |
| Elsevier (10.1016), Wiley (10.1002) | API (if you have a key) → open access → (recent, institutional channel on) institutional browser → PMC → (institutional channel on) institutional browser |
| Subscription titles: ACS, RSC, AIP, Science, Springer / Springer Nature, Taylor & Francis, SAGE, IOP, Oxford, ECS, Liebert | institutional browser → open access → PMC (the year does not change the order for this class) |
| Everything else | open access → PMC → (if the institutional channel is on) institutional browser |

Two more rules:

- **Preprint first for new papers**: for papers published in 2025 or later, a preprint is tried before the institutional browser, but only the one the journal record explicitly names; the setting and the per-page switch "New papers: preprint first" turn this off.
- **Preprint last resort**: when every route has failed, a preprint is tried once more (§6.5).

Each paper has a total time budget of 240 seconds, after which no new source is started; the final preprint fallback is not subject to it and only the Stop button ends it.

### 6.2 Naming and duplicates

- The filename template defaults to `{first_author}_{year}_{title}`; placeholders: `{first_author}` `{year}` `{title}` `{journal}` `{doi}`. Titles and authors are truncated, illegal characters replaced, and a duplicate name gets a number (only "Re-download" overwrites).
- `.doi-index.json` at the workspace root records downloaded DOIs. A DOI seen again is skipped if already downloaded; if it sits in another folder it is copied there with no network traffic. The per-folder indexes of older versions are recognised.
- When what comes back is structured full text (XML/HTML) rather than a PDF, the extension is honestly `.xml` / `.txt`, never disguised as a PDF.

### 6.3 The open-access step

1. For sources whose links can be built directly (arXiv, bioRxiv, eLife) candidate links are known without a request;
2. OpenAlex, Unpaywall, Europe PMC, Semantic Scholar, PubMed Central, OSTI, Zenodo, HAL, Crossref, DOAJ, CORE (free key, optional), Internet Archive Scholar, the Elsevier open-access endpoint and IEEE are queried in parallel for candidate links under one shared 25-second deadline; sources that time out are dropped;
3. candidates are de-duplicated and ranked (known mirror hosts and direct links first), then the first 8 are tried **one by one**, taking the first that yields a valid PDF;
4. if plain HTTP fails and Chrome/Edge is installed, the paper's DOI page is opened in a real browser and tried once more (many open-access landing pages sit behind a script challenge).

Results are cached: 24 hours when there are candidates, 6 hours when there are none.

"A valid PDF" means the file begins with `%PDF`. **Exception**: PDF links downloaded from lab pages (⑧) are also checked for the end-of-file marker and truncated files are re-fetched; other channels only check the header, not completeness.

### 6.4 The institutional channel (optional)

- How it works: the app opens the publisher page in your local Chrome/Edge and reuses the session you logged into there (campus network, CARSI…). It does not read or store your password; the login state lives in `%LOCALAPPDATA%\DOIFinder\browser-profile*`.
- First time: click "Institutional login" on a row in the download tab and log in once in the window that opens; log in again when the session expires.
- The setting `inst_enable` is on by default, provided Chrome or Edge can be found. When off, no "institutional browser" step in the table runs.
- The app does not skip this step on a guess that you have no subscription: the journal may be open access. But for classes where the table lists no institutional browser (fully open publishers) it is not used.
- The channel is **serial**, with at least 3 seconds between papers. A lot of downloading in a short time can be treated by publishers as systematic downloading and get a whole institution blocked; run batches of several hundred in smaller pieces.

### 6.5 Preprints

When the journal version cannot be had, the author often has the same paper on ChemRxiv, but a preprint's title and DOI frequently differ from the published ones, so they cannot be matched by DOI. The app:

1. first looks at the preprint relation in the journal record (Crossref `has-preprint`);
2. otherwise scores ChemRxiv's official search and Crossref's ChemRxiv records (DOI prefix 10.26434) by title similarity, first author and year;
3. grades candidates exact / likely / possible with the evidence; automatic download uses only "exact" and "likely", and "possible" is left for you to tick on page ⑩;
4. labels a downloaded preprint as such: **its content may differ from the published version**.

Also: for SSRN or Research Square preprint DOIs the app looks up the published version in Crossref by title and, if found, downloads that instead.

"Query failed" means the question was not answered, not that the paper has no preprint; retry.

## 7. Where files go

Every page files by the same rule, so the path is always:

```
workspace / collection / sub / file.pdf
e.g. D:\Literature\Zhang Xinglong\2025\Zhang_2025_Title.pdf
```

- **Workspace**: the root folder set at the top. A page can name a different workspace for itself.
- **Collection**: the first level, suggested from what you are collecting and editable on the page. The suggested names follow the interface language (the Chinese names in the table below when the UI is Chinese, English ones such as `Unsorted`, `Preprints` when it is English).
- **Sub**: flat / by year / by journal / journal → year / year → volume-issue. Year and journal come from the paper's metadata; when missing the file goes to "年份未知" / "期刊未知" (these two folder names are written by the program and do not change with the interface language), so nothing is lost.
- Each page's download row shows "Will be saved to: …", so you can see the full path first.

| Tab | Suggested collection | Suggested sub |
|---|---|---|
| ③ Topic search (author filled in) | the author's name | by year |
| ③ Topic search (keywords only) | the keywords | flat |
| ⑥ ResearchGate | the author's name | by year |
| ⑤ Journal feed | the journal | by year |
| ⑨ Journal library | the journal | year → `Volume V, Issue I` |
| ⑧ Lab pages | the site's host name | by year |
| ④ ChemASAP | `ChemASAP` | by journal |
| ⑩ Preprints | "Preprints" | flat |
| ① Title → DOI, ② DOI → PDF | "Unsorted" | flat |

## 8. Settings and keys

Everything below is on the **Settings** page (sidebar, bottom; `Ctrl+,`) and applies to every page; the workspace is in the top bar and saves itself. Settings are stored in `%LOCALAPPDATA%\DOIFinder\settings.json` (Linux: `~/.local/share/DOIFinder`). `settings.example.json` in the repository shows the fields and contains no keys.

| Setting | Default | Notes |
|---|---|---|
| Workers | 3, range 1–5 | how many papers at once; too many invites rate limiting |
| Sci-Hub / Sci-Net fallback | on | see §6.1; can be turned off |
| Skip Sci-Hub mirror certificate checks | off | only when a mirror's certificate has expired and it cannot be reached, and only on a trusted network |
| Institutional channel | on (needs Chrome/Edge) | §6.4 |
| New papers: preprint first | on | §6.1 |
| Elsevier API key / insttoken | empty | free key at dev.elsevier.com; subscribed full text also needs an insttoken from your institution. Without a key, an Elsevier failure note tells you to enter one |
| Wiley TDM token | empty | from the TDM page of Wiley Online Library |
| OpenAlex key, CORE key | empty | optional, raise quotas |
| Filename template | `{first_author}_{year}_{title}` | §6.2 |

Elsevier keys and Wiley tokens are issued to individual accounts: **do not share them**.

## 9. Notes on the other tabs

- **③ Topic search**: keyword search uses OpenAlex (up to 40 results by default); the table shows title and DOI, journal and year, authors, citations and whether it is open access. With an ORCID, the ORCID public API is used first for the works that author registered, and OpenAlex only if that returns nothing.
- **④ ChemASAP**: data comes from the third-party ChemASAP service (`api.baranlab.org`); which journals it covers is that service's decision, not this program's, and there are no per-journal or per-field tick boxes. You can filter by title, author or journal name, or show open access only. The data is cached for 10 minutes.
- **⑤ Journal feed**: data comes from Crossref's journal catalogue, not from publisher pages. The "new" mark means new since your last refresh; "Mark read" saves the current position.
- **⑥ ResearchGate**: the site has a Cloudflare human check that must be passed first with "Open and log in" in the window that opens. Scraping goes page by page; a few hundred papers take a minute or two. Entries may lack a DOI, in which case they go through title resolution.
- **⑦ Archive**: reads the PDF's first page to guess title and year (needs `pypdf`); you can also type title, authors, year, journal and DOI.
- **⑧ Lab pages**: opens the page in a real browser and loads it in rounds (some pages must be scrolled to the end), which can take a minute or two. Links the page gives as direct PDFs are downloaded from the link; entries with only a DOI follow the routes of §6; entries with only a title need to go to ① first. An arXiv paper with no DOI gets `10.48550/arXiv.<id>` synthesised. File names carry the title and a link hash to avoid overwriting.
- **⑨ Journal library**: the bibliography comes from Crossref; contents pages, covers, corrections and the like are filtered by the "type" option (default: exclude corrections / editorials / tables of contents). Review time and quartile can optionally be shown, see below.

### Journal facts (optional)

Tab ⑨ can show a journal's average / median review time, JCR quartile, CAS zone and impact factor. The data comes from the public [CrushSCI-Journal-Review-Time](https://github.com/wangshunqi2020-commits/CrushSCI-Journal-Review-Time) dataset (CC BY-NC 4.0: non-commercial use, attribution required). It is **not bundled**: click "Download journal data" and it is saved on your own machine. Review days are calendar days from receipt to acceptance and are observed samples in that dataset, not a guarantee.

## 10. Privacy and where data goes

- The program and its data run on your computer; the server listens on `127.0.0.1` only and checks the Host and Origin of requests, so other websites cannot drive its API. There are no accounts and no telemetry.
- Keys are written only to `settings.json` on your machine; they are not uploaded and not packed into the installer.
- **But searching and downloading need the network**: the titles, DOIs, keywords and author names you enter, and the contact email you give, are sent to the third-party services used (Crossref, OpenAlex, Unpaywall, Europe PMC and others; when enabled also Sci-Hub mirrors, publisher sites, ChemRxiv, ChemASAP, Baidu Scholar, ResearchGate). Be aware of this before using it.
- The institutional channel uses your own browser session; the program does not store passwords.

## 11. Troubleshooting

**Many "lookup failed" / DOIs not found**: usually rate limiting. Make sure the contact email is set, lower the workers, and run smaller batches (a few dozen at a time is steadier than hundreds).

**Subscribed but not downloaded**: click "Institutional login" on that row, log in once, retry. If it still fails read that row's note: "network slow" and "no subscription" are different, and the former just needs a retry.

**Blank PDF pages**: apart from direct links on ⑧, channels check only the file header, so a truncated file can slip through. Delete the file and retry with "Re-download".

**Stuck on one paper**: a paper has a 240-second budget, and a single institutional-browser wait defaults to 75 seconds (`inst_timeout`, 5–300). Pause, deal with the others, then resume.

**The dialog does not appear or is not in front**: in the desktop window the dialogs belong to the window itself. In the browser fallback they use tkinter (see §2); if it reports an error, type the path.

**Logs**: `%LOCALAPPDATA%\DOIFinder\doifinder.log` (rotated beyond 1 MB). Each finished batch logs every paper that was not fetched, with the reason, on lines starting with `miss`.

**A button does nothing**: the status bar at the bottom shows the script error and line number; "Reload UI" there (or Ctrl+F5) usually fixes it.

## 12. Layout

```
desktop.py        the desktop app: native window, dialogs, close guard, single instance
server.py         local HTTP server, job scheduling, settings
doi_engine.py     title → DOI: Crossref / OpenAlex queries and scoring
validation.py     reference field checks
citeparse.py      reference / BibTeX / RIS parsing
downloader.py     DOI → PDF: stepwise routes, naming, de-duplication
routing.py        download steps from publisher class and year
sources.py        open-access source aggregation and cache
scihub.py         Sci-Hub / Sci-Net mirrors and ALTCHA solving
elsevier.py       Elsevier official API
wiley.py          Wiley TDM API
browser.py        browser channel (institutional session, DOI pages, ResearchGate)
carsi.py          institutional login entry
preprints.py      ChemRxiv preprint search and grading
filing.py         workspace / collection / sub rules, year detection
journals.py       journal feed
journalinfo.py    journal facts (optional download)
labpage.py        lab page scraping
chem.py           ChemASAP interface
exporters.py      table export
classify.py, topics.py   topic classification and batch topic lookup
netpolicy.py, platform_util.py   network and platform helpers, file pickers
web/              the interface (plain JS, no build step; English strings in i18n.en.js)
tests/            unit tests and browser-side checks (run in CI)
```

## 13. License

MIT, see [LICENSE](LICENSE). Third-party sources and credits: [NOTICE](NOTICE). Third-party dependencies keep their own licenses.

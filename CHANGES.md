# Changes

## Unreleased

**English interface and file pickers**
- The English UI no longer leaves Chinese behind: a dictionary plus sentence rules (`web/i18n.en.js`) cover static text, tables, status lines, placeholders, titles and the page title; runtime text is translated by a MutationObserver, and the language can be switched back and forth without reloading. Text composed in JS (confirm dialogs, path preview, default folder names) follows the UI language.
- Pause/resume no longer reads the button's label (it broke in English); it uses a state flag.
- Windows pickers: the dialog's owner window is brought to the foreground explicitly (Alt-key tap + `SetForegroundWindow`), so it no longer opens behind a minimized/other browser window. On Windows this is the only dialog path (no Tk fallback); Tk remains only for macOS/Linux.
- Tests: `tests/web_i18n_check.py` (real English DOM crawl of every page and running state, plus zh↔en round trip) and `tests/i18n_audit.py` (every Chinese string in the web code must have an English form); both run in CI. Server-side messages and exported file names/CSV headers stay Chinese.

**Downloads**
- The institutional browser is always tried when the route calls for it; a past failure or a "no subscription" guess never skips it (the paper may be open access).
- New ⑩ Preprints page and `preprints.py`: finds ChemRxiv preprints whose title and DOI differ from the published paper (Crossref `has-preprint`, ChemRxiv search, Crossref prefix 10.26434), graded exact / likely / possible with a stated reason. Only exact/likely are used automatically; a query that could not be asked is an error, never "none". It is also the last automatic step when every other route failed, and for papers from 2025 on a journal-registered preprint is tried right before the institutional browser (Settings and per-page switch, default on). Downloaded preprints are labelled as such.
- Every page downloads in place with its own progress panel (bar, counts, pause, stop, retry the unfinished, open folder) instead of jumping to ②, and has its own folder, channel switches and worker count. The detail list shows the download channel of each file, defaults to the unfetched ones, and exports them as CSV.
- Per-page folders can be opened (the server accepts folders used by download jobs of this session).

**One filing rule for every page**
- Files go to `workspace / collection / sub / file.pdf` everywhere (`filing.py`). The top bar is now the workspace; each page suggests a collection folder from what it collects (author name, journal, group site host, keywords, ChemASAP, preprints, inbox) and a structure (flat / year / journal / journal→year / year→volume-issue), both editable on the page, with a "will be saved to" preview. ① and ② file into the `未分类` inbox by default.
- The workspace-root `.doi-index.json` is the one index: a paper already filed in another collection is copied, not downloaded again; older per-folder indexes are folded in. The lab page's direct PDFs follow the same rule (they used to make bare year folders). Old requests with only `dir` still work. ⑦ Archive keeps its own template.

**Fixes**
- Wrong publication years: a year must now stand alone, so the DOI tail in a file name (`acs.joc.4c02061` gave 2061) and the ends of a page range (`2025, 90, 2052–2061`) no longer count (`filing.find_year`, used by ⑦ Archive, PDF first-page reading and the lab-page parser). The lab parser also no longer returns `20` as a year. A PDF's first page is read for the year from the citation line (`J. Org. Chem. 2025, 90, …`), then a Published/Received date, then the copyright line, so older papers named in the text ("… in 2002 …") no longer win.
- README: ChemASAP covers all of chemistry, not only organic.

**Matching**
- BibTeX/RIS year, first author and journal take part in matching (a clear contradiction lowers the level and is written in the note; missing fields never count; year + author agreement can lift "uncertain" to "likely"); same-title records are told apart by year.
- Fixed: blank author hint turned the row into an error; author hints matched substrings; rows with the same title but a different year/DOI shared one result; a typed DOI could be replaced by a sibling record; OpenAlex results carried no authors.

**Reliability**
- Retry buttons for lookup and download re-run only failed rows and put results back in place; dropped polls reconnect automatically.
- Journal library: progress panel with pause/resume/stop and retry; fixed a script error when the tab badge was missing.
- Logic audit: Elsevier/Wiley hints were silently dropped; the "no open-access record" status could never trigger; preprint DOIs were re-downloaded every run; long/Chinese filenames and Windows reserved names could fail to save; same-title files could overwrite each other; paused time counted toward the per-paper timeout; journal year folder could disagree with its volume/issue.
- Sci-Hub certificate verification is on by default (opt-out checkbox in Settings).

**Removed**
- The terminal clients, the command-line script (`cli.py`), the unused `entitlements.py`, `papersearch.py`, `validate.py` and `app_entry.py`, and the translation feature.

## 2.1.1 (takeover)
Restored the email input, fixed `/api/jobs`, the archive path guard and the harvest front-matter filter; cross-platform helpers (`platform_util.py`), LF line endings, tests and CI; the journal tab got a type filter (research / review / exclude corrections).

## Earlier: whole-journal retrieval
`journals.harvest()` walks a journal year by year (Crossref `offset` paging per year, a whitelist of real type values, per-page retry with backoff, front matter and errata dropped). The ⑨ journal library page files downloads under `Journal/Year/Volume V, Issue I/`; downloads pasted into ② land flat. Publication dates use the earliest date that is not in the future.

## Known limitations
- Not verified against live services: Crossref/ChemRxiv preprint lookups, real publisher downloads, the institutional-login browser path. Unit, regression, end-to-end (fake backends) and headless-Chrome tests run in CI on Linux and Windows.
- The Windows installer / PyInstaller build (`DOI Finder.spec`) was not rebuilt after the cleanup.
- English is partial: `web/i18n.en.js` covers the static markup, and sentences assembled at run time (including the newest strings: preprints, per-page settings) may stay Chinese.
- `server.py` and `web/app.js` are large and mix several concerns; they grew by patching.
- Linux: see [README.linux.md](README.linux.md); the browser channel needs Chrome/Chromium on `PATH`.

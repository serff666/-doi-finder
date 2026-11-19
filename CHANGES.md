# Changes

## Unreleased

**File names**
- The default file name is now `{year}_{first_author}_{title}` (year first, then author). A saved setting that is still the old default `{first_author}_{year}_{title}` is switched automatically; any other template is left alone. Files already on disk are not renamed.

**A desktop app instead of a web page**
- `desktop.py` (the Windows build starts here too) shows the interface in its own native window through pywebview — WebView2 on Windows, WebKit on macOS, GTK/Qt on Linux — with its own taskbar entry and no address bar. The server runs inside the app and stops when the window closes.
- Native dialogs on the window: file / folder pickers (`platform_util.set_picker`, replacing tkinter there) and "Save as" for every export (`window.pywebview.api.save_file`).
- Closing while a job runs asks first; size and position are remembered (`<data dir>/window.json`); a second launch brings the open window forward (`POST /api/app/focus`) instead of starting another server; "Quit" closes the window.
- PDFs dropped onto the window arrive with their real paths and are filed by the Archive rules.
- Without pywebview or a GUI backend it falls back to the old behaviour (Chrome/Edge app window or the default browser). `DOIFINDER_NO_BROWSER=1` runs only the server.

**A new interface**
- App layout: a sidebar with the pages grouped by job (find · get full text · follow · organize), a top bar with the page name and the workspace, a status bar with the connection state and the running job. Nothing was renamed: every id the code and tests use is unchanged.
- A **Settings** page holds what used to sit in the side column of ② (Sci-Hub, institutional access, API keys, naming template, workers, contact email), plus appearance and notifications. The workspace in the top bar saves itself.
- Light and dark themes (follow the system by default); the whole stylesheet now runs on colour tokens.
- Command palette (`Ctrl+K`) for every page and action; shortcuts `Ctrl+1…0` (pages ①…⑩), `Ctrl+,`, `Ctrl+Enter` (main action of the page), `Ctrl+S`; collapsible icon sidebar.
- Job monitor: the status bar and the window title show the running job and its progress; a finished, stopped or failed job is announced, with a system notification when the window is in the background.
- Drag and drop: text / BibTeX / RIS files dropped on ①, ② or ⑩ are appended to the input box.
- Less clutter on every page: saved tasks moved from a strip above each page into a "History" button in the top bar (a popover with the current page's list); each page's download settings collapse to one line ("Will be saved to … · Sci-Hub · workers 3", click to change); export buttons are grouped in an "Export" menu on ① and the secondary actions of ② in a "More" menu; the card titles that repeated the page name are gone; counters stay hidden until there is something to count; empty states use line icons.
- Page layouts tidied: topic search keeps its fields on one row; lab pages use the same field / action rows as the other pages; the journal library shows its three steps as cards and its fields no longer overflow.
- `i18n.js` no longer rewrites whole elements that contain an icon (it would have wiped the sidebar icons); their labels are translated on their own.
- Tests: `tests/test_desktop.py` (fake pywebview: dialogs, filters, window state, close guard, single instance, server hooks) and `tests/web_shell_check.py` (headless Chrome: navigation, shortcuts, palette, theme, job monitor, drag and drop, language round trip, no horizontal overflow); both run in CI.

**Saved tasks on every page**
- Every page (lookup, download, search, ChemASAP, journal follow, ResearchGate, lab pages, journal library, preprints) now keeps a "History" list. Results are saved on this computer while they arrive (`taskstore.py`, `<data dir>/history/<page>/<id>.json`, newest 60 per page), so closing the app loses nothing. A saved task can be reopened later with its selection and its download panel, so the unfinished papers can be retried or filed.
- The newest task of a page reopens by itself when the app starts on an empty page; "New task" starts a fresh record without clearing the page; records can be deleted.
- New endpoints: `/api/history`, `/api/history/get`, `/api/history/save`, `/api/history/delete`.
- Tests: `tests/test_taskstore.py`, `tests/web_history_check.py` (real server + headless Chrome).

**ResearchGate**
- The author list is read page by page from the server-rendered HTML (100 per page) instead of the half-rendered live page, so profiles with hundreds of papers are no longer cut off after a few dozen; the count the page states is compared with what was read and a shortfall is reported.

**English interface and file pickers**
- The English UI no longer leaves Chinese behind: a dictionary plus sentence rules (`web/i18n.en.js`) cover static text, tables, status lines, placeholders, titles and the page title; runtime text is translated by a MutationObserver, and the language can be switched back and forth without reloading. Text composed in JS (confirm dialogs, path preview, default folder names) follows the UI language.
- Pause/resume no longer reads the button's label (it broke in English); it uses a state flag.
- File / folder pickers: tkinter only, on every platform, no fallback (the PowerShell dialog returned an empty answer without showing anything on some machines). The owner window is a 1x1 transparent top-most window that is given the foreground (Alt tap + `SetForegroundWindow`) before the dialog opens: measured on a real machine the dialog was top-most but had no focus; folders must exist; one dialog at a time; a failure is reported, not swallowed.
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
- The PyInstaller build (`DOI Finder.spec`, entry `desktop.py`) was built and started on Linux (Qt backend); the Windows build and its installer have not been rebuilt or tried on Windows with WebView2.
- Server-side messages and exported file names / CSV headers stay Chinese in the English UI.
- `server.py` and `web/app.js` are large and mix several concerns; they grew by patching.
- Linux: see [README.linux.md](README.linux.md); the browser channel needs Chrome/Chromium on `PATH`.

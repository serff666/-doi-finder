# Changes and known issues

**Unreleased:** **Logic audit:** download hints for Elsevier/Wiley were silently dropped; the "no open-access record" status could never trigger; preprint DOIs were re-downloaded every run; long/Chinese filenames and Windows reserved names could fail to save; same-title files could overwrite each other; paused time counted toward the per-paper timeout; a stop after the last paper marked the job stopped; journal year folder could disagree with its volume/issue and volumes sorted as text. All fixed with tests.  Sci-Hub certificate verification is now on by default (opt-out checkbox in Settings); journal tab resolves titles to ISSN.

**Terminal client (new):** `python tui.py` (or `./run.sh tui`, `run_tui.cmd`) — a Claude-Code-style prompt: paste titles to find DOIs, paste DOIs to download, `/search`, `/journal`, `/download 1 3 5-8`, `/retry`, `/set`. Standard library only, same engine and settings as the web UI. Tested on Linux with a terminal emulator and a fake engine; **not yet run on Windows** (uses msvcrt + VT mode). The web page lost its hero title and card shadows.

**2.1.1 (takeover):** restored the email input, fixed `/api/jobs`, archive path guard, front-matter filter in harvest, cross-platform helpers, LF line endings, tests + CI. Also: translation code fully removed, lint (ruff F/E9) clean, dead harvest JS removed, journal tab ⑨ now has a type filter (research / review / exclude corrections) that sets the default selection and shows a type column. Still unverified: real Crossref harvest, real downloads and the live UI (no network in the build sandbox).

**Terminal client, failures:** a failed request no longer ends the session (a dropped connection while pasting a list used to exit the program and lose the list); every failure gets a plain-words message and a retry on `Ctrl+R` / `/retry` (download misses, lookup rows that were not found, search, journal, jobs ended by error or stopped with Esc); a lookup retries only the unresolved rows and splices them back so the numbering holds; `/fix N` picks an alternative or sets a DOI by hand.

This file lists what was changed in this build and, just as importantly, what does
not work yet. It is meant to be read before continuing development.

---

## 1. What changed

### 1.1 Journal harvest (whole-journal retrieval)

`journals.harvest()` was added. It walks a journal year by year and returns every
record as `{doi, title, authors, date, year, volume, issue, page, journal, type}`.

Several problems had to be fixed before it produced usable data:

| Problem | Cause | Fix |
|---|---|---|
| Only 191 / 248 / 415 records | Crossref `cursor=*` deep paging returns HTTP 400 on this endpoint, so paging used `offset`, which stops at 10,000 | Page per year with `offset`; each year is far below the limit |
| `HTTP 400` on every year | The UI sent the literal option label `全部类型` as the Crossref type filter, which encoded to `type:????` | Whitelist of real Crossref type values; anything else means "no type filter" |
| `HTTP 400` again | Even with the whitelist, an earlier build passed the Chinese label straight through | Sanitising now happens at the top of `harvest()`, before the URL is built |
| `state=done, items=0` but 840 records fetched | `/api/job/<id>` serialised a fixed field whitelist that did not include `items` | `items`, `errors` and `markdown` are now part of the job payload |
| ~6–13 minutes for one journal | A failed page aborted the whole run, and an empty `year_from` started at 1900 | Per-page retry with backoff; the first year is probed with one cheap request |
| `anon_2026_Issue Information (5).pdf` in the output | Front matter, cover images and errata were treated as papers | `_JUNK` filter drops those titles during harvest |

### 1.2 A dedicated journal page

A ninth tab, `⑨ 期刊文库`, was added. It owns the whole "harvest a journal and keep
it tidy" workflow: journal + year range, a result table with year / volume / issue,
select-all, and its own download progress table.

The duplicate harvest controls that used to sit inside `⑤ 期刊追新` were removed
from that page, so the two pages no longer overlap.

Downloads started from this page carry a `layout` payload, so files land in
`<Journal>/<Year>/Volume V, Issue I/`. Downloads started by pasting DOIs into
`② DOI → PDF` still land flat, by design.

### 1.3 Interface

- Contact email is optional and lives in Settings (it was briefly removed from the HTML while the
  JS still read it, which broke lookup/download/settings; restored in 2.1.1).
- Journal-name input in the new page now offers Crossref suggestions; picking one
  fills in the **ISSN**, which is the only unambiguous identifier.
- Per-row "translate" links and the title-translation feature were removed. The
  page was simply opening Google Translate, which is unreachable from mainland
  China, and the offline glossary approach was not good enough to keep.
- A reload button was added next to "Quit" (equivalent to Ctrl+F5).
- Partial English localisation via `web/i18n.en.js` + `web/i18n.js`. Static text is
  fully covered; runtime-generated strings are covered by patterns. See §2.4.
- Application icon redrawn at 1024 px and downsampled into eight sizes, so the
  taskbar no longer shows an upscaled bitmap.

### 1.4 Download robustness

- `fetch_arxiv_source()` retries, resumes with `Range:` and reads in chunks, so a
  dropped connection no longer discards a long download.
- Per-request DOI cap raised from 500 to 2000 for the plain paste path (20,000 when
  a journal layout is supplied).
- Journal filters an item's publication date to the earliest date that is not in
  the future. Crossref's plain `published` field is often the print-issue date,
  which produced entries dated `2026-12-31` while the feed was read in November.

---

## 2. What is broken or unfinished

This section is deliberately blunt. Do not assume the items above are production
quality.

### 2.1 The journal page still does not work end to end

The harvest itself works when driven through the API: 840 records for
*WIREs Computational Molecular Science* (ISSN 1759-0876), matching Crossref's own
count. What has **not** been verified is the page as a user experiences it:
harvest → list → select → download → files on disk in the right folders.

Known suspects:

- `⑨ 期刊文库` was only smoke-tested: the tab opens, the buttons exist, the console
  is clean. No real harvest was run from the UI after the last rebuild.
- The download path from this page posts `layout` with a journal name taken from
  the text box. If the user typed a name instead of picking a suggestion, the
  folder name will be whatever was typed.
- The `errors` list from a harvest is shown as a count, not as a per-year detail.

### 2.2 Archive button path error — fixed in 2.1.1

`_path_allowed` now compares `realpath` + `normcase` (case/slash/symlink safe), and a
folder typed on the page but not yet saved is adopted as the download folder.

### 2.3 `/api/jobs` returned 404 — fixed in 2.1.1

The route lives in `do_GET` now and has a regression test.

### 2.4 English is incomplete

`web/i18n.en.js` covers the static markup. Runtime strings are handled by exact
match plus a few patterns, which means sentences assembled from fragments still
come out in Chinese. Buttons such as `Find`, `Load common journals` and `Clear`
are currently a mix of both languages.

### 2.5 Code quality

The project grew by patching rather than by design. Concretely:

- `server.py` is ~1,700 lines and mixes routing, job scheduling, settings,
  archiving and PDF text extraction.
- `web/app.js` is ~1,900 lines with nine panels sharing global state.
- Several handlers remain in `app.js` for UI that no longer exists (the removed
  harvest row). They are guarded with existence checks, so they do nothing, but
  they are dead weight.
- `tests/` has unit and regression tests (run `python -m pytest tests`); there is no browser-level test yet.
- Error messages are often Chinese-only even when the UI is in English.

---

## 3. Running on Ubuntu 24.04

The application is a local HTTP server plus a browser UI, so it is portable in
principle. The following has to be dealt with.

### 3.1 What already works

```bash
sudo apt install python3 python3-pip
pip3 install --user openpyxl pypdf
cd doi-finder
python3 server.py            # http://127.0.0.1:8765/
```

`DOIFINDER_PORT=8790 python3 server.py` picks a port; `DOIFINDER_NO_BROWSER=1`
prevents it from trying to open one.

### 3.2 Windows-only code — guarded in 2.1.1

All platform differences now go through `platform_util.py`: data dir (`%LOCALAPPDATA%` /
`~/Library/Application Support` / `$XDG_DATA_HOME`), `open_path`, `creationflags`,
Chrome/Chromium/Edge discovery, stale-browser kill (`pkill` on Linux/macOS) and
`curl` vs `curl.exe`. Not yet run on a real Linux desktop.

### 3.3 Browser channel on Linux

The institutional-login path needs a real Chrome/Chromium with a persistent
profile and remote debugging. On Ubuntu:

```bash
sudo apt install chromium-browser    # or google-chrome-stable
```

`browser.py` now also finds `chromium`, `chromium-browser` and `google-chrome` on `PATH`.

### 3.4 Packaging

`DOI Finder.spec` produces a Windows one-dir build. For Linux there are two
options:

- **PyInstaller** with the same spec, built *on Ubuntu* (PyInstaller cannot
  cross-compile). The `.spec` uses `icon=['assets/icon.ico']`, which should become
  `icon='assets/icon.png'` on Linux.
- **Run from source** with a small `run.sh`. This is the lower-risk option and is
  recommended until the Windows-only guards are in place.

### 3.5 Not yet done

The server, tests and API start fine on Linux (Python 3.11–3.13); the browser/institutional-login path has not been exercised there. The statements above are a
reading of the code, not a test result. The first Linux session should start with
`python3 server.py` and fix the imports and path errors it reports, one at a time.

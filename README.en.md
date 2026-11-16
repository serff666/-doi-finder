# DOI Finder

Batch-resolve paper titles to DOIs, then batch-download the DOIs as local PDFs.
Also: journal feed, lab-page scraping, and reference export. Windows desktop app that also runs from source on Linux (see [README.linux.md](README.linux.md)); the UI runs in your browser at `http://127.0.0.1:8765/`.

> 中文说明见 [README.md](README.md)。

> **Disclaimer**
> This project is for **academic research and learning only** and is **not used for commercial profit**. The author provides and sells no publications.
> The app can connect to third-party sources, including Sci-Hub mirrors. Whether that is lawful depends on your country and institution;
> **whether and how to use it is your own decision and responsibility** (the Sci-Hub fallback can be turned off in Settings).
> Respect publishers' terms of service and copyright law; use downloaded papers for personal research only and do not redistribute them.
> 本项目仅用于学术研究与学习，不用于商业盈利；使用第三方来源的风险与责任由使用者自行承担。

## What it does

| Tab | Input | Output |
|---|---|---|
| ① Title → DOI | a list of titles or full citations | one DOI per line, exportable |
| ② DOI → PDF | a list of DOIs | local PDFs, normalised filenames, resumable |
| ③ Topic search | keywords, author, ORCID, years | matching papers; add to the download queue |
| ④ ChemASAP | none | latest ASAP articles in organic chemistry |
| ⑤ Journal feed | journal name or ISSN (+ optional year range) | recent articles, or the whole journal downloaded into `Journal/Year/Volume V, Issue I/` folders |
| ⑥ ResearchGate | an author profile URL | that author's publication list |
| ⑦ Archive | a local PDF | renamed and filed into your library |
| ⑧ Lab pages | a group's publications page | titles, DOIs and PDF links from that page |

## Terminal client

A Claude-Code-style prompt that shares the engine and settings of the web UI (standard library only):

```bash
python tui.py        # or ./run.sh tui on Linux/macOS, run_tui.cmd on Windows
```

Paste titles → find DOIs; paste DOIs → download. `/search WORDS --oa --year 2020-2024`, `/journal NAME-or-ISSN 2011-2024`,
`/download 1 3 5-8`, `/settings`, `/set workers 5`, `/dir ~/Papers`. `Esc` stops a job; `Ctrl+C` twice quits; `↑↓` history, `Tab` completes a command, `Ctrl+L` clears.

**When something fails you can redo it.** A dropped connection, an API error, a download that missed, rows that were not found, or a job you stopped with `Esc` all end with a plain-words message and a hint at the bottom: press `Ctrl+R` (or type `/retry`). A lookup retries only the ✗ rows, so rows that were found and the numbering stay as they are. `/retry list` shows what can be retried, `/retry download` picks one. A failed request never ends the session.

**Change a row's DOI by hand:** `/fix 3` shows row 3's alternatives, `/fix 3 2` picks the second, `/fix 3 10.1021/xxxx` (or a doi.org link) sets a DOI, `/fix 3 -` clears it.

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

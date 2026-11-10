# DOI Finder

A local Windows desktop tool for working with bibliographic identifiers.

## What it does

- Resolves article titles and reference strings to DOIs, using Crossref and
  OpenAlex metadata search plus field-level validation.
- Downloads open-access PDFs, and PDFs from publisher APIs the user is
  personally entitled to (e.g. the Elsevier Article Retrieval API).
- Batch imports, exports results to XLSX / CSV / Markdown.

## What it does not do

- It does not bypass CAPTCHA, bot protection, or paywalls. Sites that gate
  access with a human-verification challenge are reported as such, not evaded.
- It does not ship, bundle, or share API credentials. Each user supplies their
  own key, stored only on their machine.
- It does not redistribute publisher content.

## Requirements

Windows, Python 3.10+ (or the packaged build). No external services are
required; publisher API access is optional and user-provided.

## Status

Personal research tooling. Not a commercial product.

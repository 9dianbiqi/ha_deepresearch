# GitHub Evidence/Intelligence acceptance

Date: 2026-08-12 (Asia/Shanghai)

## Scope

Validate the v1.1 GitHub evidence foundation and the first file-content/line-citation
increment without exposing credentials or raw authorization headers.

## Results

| Acceptance case | Result | Evidence |
| --- | --- | --- |
| Authenticated public repository collection | PASS | `openai/openai-python`; fixed commit obtained; 6 bounded source files and 15 line-addressable evidence items |
| Evidence and claim bundle | PASS | coverage score `1.0`; overview, architecture, maintenance, community, and license claims present |
| Citation sanitization | PASS | unlinked repository URL removed; citations derived from the evidence ledger |
| Artifact rendering and persistence round-trip | PASS | 5 artifacts rendered; bundle restored from serialized output |
| Anonymous API rate-limit degradation | PASS | stable `github_rate_limited` notice code returned; no Authorization header emitted |
| SSE compatibility | PASS | additive `github_evidence`, `coverage_update`, and `artifact_ready` projections |

## Invariants

- Source links are pinned to a commit SHA when line evidence is present.
- Line evidence carries `file_path`, `line_start`, `line_end`, and a bounded excerpt.
- Source bodies are not copied into ordinary operation or SSE event payloads.
- Evidence bundle schema remains backward-readable when the new source list is absent.
- The frontend exposes fixed-source links without accepting non-GitHub evidence URLs.

## Automated verification

- GitHub/evidence targeted regression: `14 passed`
- Backend full suite before this increment: `421 passed, 2 skipped`
- Ruff: passed
- mypy: passed
- compileall: passed
- Frontend contract: `4 passed`
- Frontend build: passed

No secrets, access tokens, or sensitive runtime payloads are included in this record.

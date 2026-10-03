# Phase 5 technical report

Phase 5 changes are limited to integration testing, concrete reliability fixes,
cleanup, and documentation. No live applications were submitted during testing.

## Technical inventory

1. **Architecture:** local FastAPI dashboard, one asynchronous Playwright worker,
   SQLite persistence, optional LLM assistance, and pandas/openpyxl reports.
2. **Major modules:** `main`, `browser`, `scraper`, `job_extractor`, `models`,
   `database`, `form_filler`, `external_application`, `ats/`, `answer_resolver`,
   `candidate_profile`, `answer_memory`, `llm/`, `review`, `runtime`, and `web/`.
3. **Job sources:** LinkedIn only. Google, Indeed, and independent ATS discovery
   are not implemented.
4. **Application methods:** LinkedIn Easy Apply and external ATS links; direct
   links, popups, and same-tab redirects are handled. Final submission requires
   dashboard approval and positive confirmation.
5. **ATS platforms:** Greenhouse, Lever, Ashby, basic Workday, and generic HTML.
   Coverage establishes local detection/form behavior, not full live compatibility.
6. **LLM options:** disabled/deterministic, LM Studio, OpenAI Responses,
   OpenAI-compatible endpoints, and Gemini via a configured compatible endpoint.
   Model, endpoint, key environment variable, timeout, and confidence are configurable.
7. **Candidate profile:** `data/user_profile.json`; canonical `candidate_profile`
   plus synchronized legacy dashboard fields. Uploaded resumes: `data/resumes/`.
8. **Database:** `data/jobs.db`, storing processed/applied/flagged jobs, application
   review decisions, and approved answer memory.
9. **Excel:** `exports/`, separate reviewed/applied latest and timestamped files;
   all reports regenerate from persistent SQLite history.
10. **Run:** from the project directory, `.\.venv\Scripts\python.exe run.py`, or
    `Start Job Applier.bat`; dashboard at `http://127.0.0.1:8000`.
11. **Test:** `.\.venv\Scripts\python.exe run_tests.py`; see evidence below.
12. **Limitations:** live sites/providers untested; manual login/CAPTCHA;
    limited custom/iframe ATS forms; session browser state cannot resume after
    process restart; local single-process operation only.
13. **Future improvements:** opt-in live smoke tests and versioned DOM fixtures,
    stronger platform-specific confirmations, explicit reconciliation of ambiguous
    submissions, dependency locking/CI, and richer form-widget support.

## Reliability fixes

- Fixed a misplaced JavaScript brace that prevented dashboard initialization and
  Start Session handler registration. Added real UI initialization coverage.
- Centralized confirmed submission persistence so external applications appear in
  applied history and Excel. Existing timestamped APPLIED external rows are repaired
  on database initialization. REVIEWED and READY_FOR_REVIEW are never promoted.
- Removed generic Done/thank-you/URL-only submission confirmation shortcuts;
  enforced review decision transitions and protected APPLIED against late failures.
- Canonicalized URL identity, repaired teaching/retry and requeue behavior, and
  made interrupted-state recovery a server-startup operation.
- Made profile replacement atomic, validated candidate data, rolled back failed
  saves, and preserved corrupt source files with actionable startup errors.
- Serialized report generation, used consistent database snapshots and atomic
  workbook replacement, retained timestamped output when latest files are locked,
  and prevented scraped text from becoming spreadsheet formulas.
- Moved session Excel generation and resume file writing off the event loop;
  bounded WebSocket sends and fixed disconnect-list races. Tracked/cancelled the
  session task on shutdown and added browser/page ownership cleanup.
- Added external-launch failure cleanup, per-job diagnostic/cleanup isolation,
  and persistent rotating logs at `data/logs/application.log`.
- Replaced blind initial page delays with readiness waits, corrected external
  Apply metadata, retained JSON-LD descriptions, and checked ATS hostnames rather
  than arbitrary URL substrings.
- Hardened malformed/empty LLM envelopes and JSON schema/type failures; explicit
  environment disable takes priority. Provider errors remain fallbacks.
- Removed the unused JSON memory module containing invented candidate defaults;
  retained the migration reader and the user's existing legacy data. Removed
  hardcoded fallback job roles/locations and shared duplicated URL/path logic.

## Verification evidence

Final result on 2026-09-08: **93 tests passed in 53.726 seconds, no skips**;
the process exited with code 0. The output is recorded in `test-results.log`.
The suite includes the 65 original tests plus 28 Phase 5 regressions and system tests. The system tests
use real FastAPI/Uvicorn, dashboard JavaScript, Playwright and Chromium, with
intercepted LinkedIn/ATS HTML. Both Easy Apply and external generic HTML paths run
through discovery, extraction, matching, resume upload, multiple steps, human
approval, confirmation, persistent history and Excel. Rejection leaves applied
history empty. A second Start request cannot launch a concurrent worker.

Additional checks cover JobRecord fields, missing/structured metadata, candidate
migration, answer priority, approved memory, duplicate URL variants, concurrent
history writes/exports, workbook formatting and formulas, locked latest files,
profile corruption/write rollback, stale reviews, illegal review transitions,
approval timeout, malformed LLM responses, and browser cleanup failure.

Failure injection verifies a failed job and failed diagnostic capture do not
prevent processing the next job. A separate Python process reads saved history
and regenerates reports. Restart recovery preserves confirmed applications and
expires pending approvals. Excel regeneration also succeeds after report deletion.

`node --check web/app.js` passed. `python -m compileall -q src tests run.py run_tests.py`
also passed with exit code 0.
Tested environment: Python 3.12.10, FastAPI 0.141.1, Uvicorn 0.52.1,
Playwright 1.62.0, pandas 3.0.5, openpyxl 3.1.5, Pydantic 2.13.4.

## Remaining audit findings

- Some broad exception handlers remain at recovery boundaries and around optional
  DOM fallbacks. Critical session, storage and cleanup errors are logged; not every
  optional selector miss is promoted to a user-visible failure.
- Short asynchronous polling intervals remain for DOM transitions, manual security
  checks and paced discovery; the launcher also retains its short dashboard-opening
  delay. Live selector freshness is unverified.
- Provider tests use fixture transports. No live LLM request, CDP attachment to a
  user's Chrome, real LinkedIn login/search, or live ATS submission was verified.
- Confirmation text is conservative but still heuristic. A crash after a successful
  remote submission and before its local commit cannot be reconciled automatically;
  verify on site before retrying an interrupted application.
- History represents the latest job outcome, not an append-only event timeline.
  The dashboard lists up to 100 recent history rows; Excel exports the complete
  stored history. Timestamped exports and failure artifacts have no retention policy.
- Unknown required answers and security challenges intentionally pause a browser.
  Teaching an answer does not automatically resume that form; restart/close the
  current session and retry. Workday account workflows and arbitrary custom or
  embedded controls require manual completion.
- Settings remain broadly dictionary-based beyond candidate validation. This is a
  local desktop-style server with no authentication or multi-process coordination.

The test runner isolates all storage before imports. Existing user profile,
history, browser login state and exports were not used as test fixtures.

# Job Applier

A local FastAPI dashboard and serial Playwright worker for LinkedIn discovery,
job matching, and application preparation. Submission requires an explicit
**Approve & Submit** decision and positive application confirmation.

## Run

Tested on Windows with Python 3.12.10. From this directory:

```powershell
.\.venv\Scripts\python.exe run.py
```

Alternatively double-click `Start Job Applier.bat`. The dashboard is at
`http://127.0.0.1:8000`. Keep the launcher open; Ctrl+C shuts down the worker and
its browser resources. Closing just the dashboard tab does not stop the server.
Use a single server process; do not enable Uvicorn reload or multiple workers.

For a fresh installation, using an installed Python 3.12 interpreter:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe run.py
```

Save target roles and locations, candidate details, and a resume before starting.
LinkedIn login/security checks must be completed manually in the session browser.
If login blocks a run, complete it and start again. Both modes discover Easy Apply
and external jobs. **Search & Save** saves matched listings as Pending, including
available application links, without opening or filling applications. **Autopilot**
uses the existing Easy Apply or external ATS workflow and requires approval before
submission. Unsupported external applications remain in Failed / Flagged with
their job details and links. The worker first tries local Chrome CDP on
port 9222, then launches Chromium with its own persistent profile.

At READY FOR REVIEW, inspect the live application and choose Approve & Submit or
Reject in the dashboard. Unknown required answers enter the teaching queue.
A paused application must be finished/closed or the server restarted before
starting another session. Teaching saves an approved answer for a subsequent run;
it does not resume an already-paused form automatically.

## Persistent files

Paths are relative to the project, independent of the launcher's working directory.

| Data | Default location |
| --- | --- |
| Candidate profile and settings | `data/user_profile.json`, including `candidate_profile` |
| History, review decisions, approved answer memory | `data/jobs.db` (SQLite) |
| Uploaded resumes | `data/resumes/` |
| Persistent Chromium login profile | `data/browser_profile/` |
| Rotating application logs | `data/logs/application.log` |
| Failure screenshots/HTML and history tracebacks | `data/failures/` and `processed_jobs.notes` |
| Reviewed/applied workbooks | `exports/reviewed_jobs_latest.xlsx`, `exports/applied_jobs_latest.xlsx`, plus timestamped copies |

`JOB_APPLIER_DATA_DIR` and `JOB_APPLIER_EXPORTS_DIR` override storage directories
when set before launch. Local failure artifacts may contain application/profile
details. Back up `data/` while the application is stopped.

History and approved answers survive restart. Pending browser approvals expire
on server startup; interrupted form states become retryable FAILED records.
Confirmed APPLIED records remain applied. If the process stopped just after a
submission click, verify the result on the site before retrying.

Use the dashboard Excel buttons to regenerate reports from SQLite at any time,
including after deleting old reports. If Excel locks a latest workbook, the
timestamped report is still saved and a warning identifies the locked file.
For regeneration without starting a browser:

```powershell
.\.venv\Scripts\python.exe -c "from src.database import db_manager; print(db_manager.export_job_reports())"
```

## Git privacy

Keep candidate details in the dashboard-managed `data/user_profile.json` and
resumes in `data/resumes/`. The root `.gitignore` excludes local data, browser
login state, databases, exports, logs, failure captures, virtual environments,
and machine-specific shortcuts. These files remain available locally.

`.env.example` lists supported environment variable names with no credentials.
The application reads process environment variables; it does **not** automatically
load `.env`. Set only the variables you need in your launcher environment. If
you use an external environment loader, keep its real `.env` file local.
Storage overrides outside `data/` and `exports/` require their own ignore rules
if they point elsewhere inside this repository.

Before staging, review `git status --short --untracked-files=all` and
`git ls-files --others --exclude-standard`. Never force-add private files:
ignore rules do not protect files that have already been tracked or force-added.
Use synthetic fixtures for tests. `tests/fixtures/synthetic_search_match.json`
contains invented job and candidate data; the original local regression capture
and `.privacy-audit/` backups are excluded from Git.

## LLM configuration

AI assistance is optional and disabled by default. Profile fields under
`ai_settings` are `enabled`, `provider`, `base_url`, `model`, `api_key_env`,
`timeout_seconds`, and `minimum_confidence`.

| Provider value | Implementation |
| --- | --- |
| `none` | Deterministic profile/approved-memory answers and matching |
| `lmstudio` | OpenAI-compatible chat endpoint; default base `http://localhost:1234/v1` |
| `openai-compatible` / `openai_compatible` | User-configured chat endpoint and model |
| `openai` | Responses endpoint; default base `https://api.openai.com/v1`, API key required |
| `gemini` | User-configured OpenAI-compatible endpoint; no native Gemini SDK integration |

Environment overrides: `LLM_PROVIDER`, `LLM_ENABLED`, `LLM_BASE_URL`, `LLM_MODEL`,
`LLM_API_KEY`, `LLM_TIMEOUT_SECONDS`. The configured `api_key_env` defaults to
`OPENAI_API_KEY`. An explicit `LLM_ENABLED=false` disables assistance even when a
provider is set. No model name is hardcoded. Credentials are read from the
environment, not written into the candidate profile.

Timeouts, malformed envelopes/JSON/schema, and low-confidence answers fall back
to deterministic processing or manual input. AI cannot supply missing factual
candidate details or authorize submission. Provider tests use fixture transports;
live provider connectivity and model quality have not been verified in Phase 5.

## Test

```powershell
.\.venv\Scripts\python.exe run_tests.py
.\.venv\Scripts\python.exe run_tests.py test_hardening.py
.\.venv\Scripts\python.exe run_tests.py test_system_browser.py
node --check web/app.js
```

Use `run_tests.py`: it establishes temporary profile, database, browser-profile,
and export paths before importing application modules. Direct unittest discovery
can initialize the real application's singleton storage. Chromium is required
for complete coverage; some older browser tests skip if it is missing, while
the system tests fail so missing browser coverage is visible.

The system tests serve the real dashboard/API and launch real headless Chromium.
LinkedIn and ATS HTTP traffic is intercepted with local HTML fixtures. They test
search, extraction, preparation, upload, multistep navigation, user approval or
rejection, confirmation, database updates, and Excel export. They do not verify
current production site markup or submit real applications.

See [architecture_overview.md](architecture_overview.md) and
[PHASE5_REPORT.md](PHASE5_REPORT.md) for module boundaries, results, and limitations.

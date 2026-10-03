# LinkedIn Apply modal regression check

Apply is now clicked on the current job page. Visible LinkedIn application
dialogs take priority over popup/new-page and URL-change results, including
dialogs that appear after a short delay. The launcher and form analyzer share
the same detector. A plain Apply label no longer determines the handler.

The modal handoff records APPLICATION_STARTED immediately. The existing state
callback timestamps the JobRecord, increments Applications Started once, and
updates the dashboard with that record's title/company. Contact fields and
subsequent steps use the existing analyzer and human approval gate.

Title extraction removes verification icon/accessibility descriptions and the
exact trailing phrase “with verification”; legitimate title words such as
“Verification Analyst” remain intact. Existing Excel files are not rewritten
until reports are regenerated from newly processed records.

## Manual test on LinkedIn

1. Stop the running server with Ctrl+C in its launcher and restart using
   `Start Job Applier.bat`. Open `http://127.0.0.1:8000`.
2. Confirm the saved email, phone number, country, and resume are correct.
   Use a test listing appropriate for your search/preferences, and
   clear **Easy Apply only** if needed to include plain Apply listings.
   Previously FAILED jobs are eligible for retry; preserve history.
3. Click **Start Autopilot** and complete any LinkedIn login prompt manually.
   When the target job is reviewed, verify the displayed title/company match
   the open job. Its pre-click record should be REVIEWED.
4. When **Apply to Example Company** opens, verify the URL can remain unchanged,
   Applications Started increases by exactly one (0 to 1 in a fresh run where
   this is the first application), and the log reports a LinkedIn modal.
   No external-navigation error should appear.
5. Verify email, phone country code, and mobile phone are filled or confirmed.
   The worker should report FILLING and click Next once required values are
   valid. Observe subsequent application steps.
6. If an unknown required question occurs, verify NEEDS_USER_INPUT and that
   the answer remains blank. Teach a confirmed answer through the existing
   queue if needed for a subsequent run; do not invent one to force progress.
7. At the final Submit page, verify READY_FOR_REVIEW and inspect the live
   application. **Do not click Approve & Submit during this test.** The existing
   approval timeout still applies; inspect before it expires. No APPLIED
   record should exist without explicit approval and submission confirmation.
8. Regenerate/download the reviewed Excel report with the dashboard Excel
   button. Verify the job title, without verification badge text,
   and the correct company. Close the workbook before refreshing its latest copy.

## Offline verification

From the project directory:

```powershell
.\.venv\Scripts\python.exe run_tests.py
.\.venv\Scripts\python.exe run_tests.py test_linkedin_modal_browser.py
```

The runner uses temporary data and exports. Browser tests intercept pages and
use fixtures; no live LinkedIn application is submitted. Coverage includes the
plain Apply handoff, delayed dialogs, modal-container/progress fallbacks,
unrelated dialogs, no application signal, contact-to-review navigation,
unknown required questions, counter deduplication, dashboard identity, title
cleanup, and APPLIED only after confirmed submission. Existing tests cover
external popup/same-tab/multiple redirects and dashboard approval behavior.

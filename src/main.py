"""
src/main.py
=============================================================================
FastAPI Web Dashboard Server & API Entrypoint

PYTHON LESSON:
- `FastAPI` builds modern Web APIs quickly with automatically generated documentation.
- `WebSocket` endpoints allow 2-way real-time communication between Python and the 
  web frontend without constant browser reloading.
=============================================================================
"""

import asyncio
import sys
import logging
from contextlib import asynccontextmanager, suppress
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Literal
from urllib.parse import unquote

# On Windows, force ProactorEventLoop policy so Playwright can launch subprocesses
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.browser import browser_manager
from src.answer_memory import answer_memory
from src.answer_resolver import IntentClassifier
from src.config import config_manager
from src.database import db_manager
from src.review import review_coordinator
from src.resume import save_uploaded_resume
from src.runtime import runtime_status
from src.scraper import LinkedInScraper
from src.suggestions import find_suggestions
from src.paths import DATA_DIR

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"

logger = logging.getLogger(__name__)
autopilot_task = None
search_task = None
search_running = False
browser_paused = False


@asynccontextmanager
async def lifespan(app):
    global autopilot_task, autopilot_running, browser_paused, search_task, search_running
    log_dir = DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / "application.log", maxBytes=2_000_000,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger("src")
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    try:
        db_manager.recover_interrupted_applications()
        yield
    finally:
        if search_task and not search_task.done():
            search_task.cancel()
            with suppress(asyncio.CancelledError):
                await search_task
        search_task = None
        search_running = False
        if autopilot_task and not autopilot_task.done():
            autopilot_task.cancel()
            with suppress(asyncio.CancelledError):
                await autopilot_task
        await browser_manager.close()
        autopilot_task = None
        autopilot_running = browser_paused = False
        root.removeHandler(handler)
        handler.close()


app = FastAPI(
    title="LinkedIn Autopilot Job Applier",
    description="Automated job search, form auto-filling, self-learning memory & tracking system.",
    version="1.0.0",
    lifespan=lifespan,
)

# Enable CORS for local dashboard development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Active WebSocket connections pool
active_websockets: List[WebSocket] = []
autopilot_running = False


async def broadcast_log(message: str):
    """Sends log messages in real-time to all connected web dashboard clients."""
    logger.info(message)
    async def send(ws):
        try:
            await asyncio.wait_for(ws.send_json({"type": "log", "data": message}), timeout=1)
        except (OSError, RuntimeError, asyncio.TimeoutError, WebSocketDisconnect):
            if ws in active_websockets:
                active_websockets.remove(ws)
    await asyncio.gather(*(send(ws) for ws in list(active_websockets)))


# Pydantic Schemas for API requests
class ProfileUpdateRequest(BaseModel):
    personal_info: Dict[str, Any] = Field(default_factory=dict)
    work_preferences: Dict[str, Any] = Field(default_factory=dict)
    default_answers: Dict[str, Any] = Field(default_factory=dict)
    ai_settings: Dict[str, Any] = Field(default_factory=dict)
    candidate_profile: Dict[str, Any] = Field(default_factory=dict)


class TeachRuleRequest(BaseModel):
    flagged_id: int
    question: str
    answer: str


class ReviewDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]


class ManualJobStatusRequest(BaseModel):
    job_url: str
    status: Literal["Pending", "Applied", "Rejected"]
    rejection_reason: str | None = None
    rejection_feedback: str | None = None


class DeleteJobsRequest(BaseModel):
    job_urls: List[str]
    bucket: Literal["review", "rejected", "skipped", "failed"]
    source_mode: Literal["all", "autopilot", "search_save"] = "all"
    application_method: Literal["all", "easy_apply", "external"] = "all"


# API REST Endpoints
@app.get("/api/profile")
def get_profile():
    """Returns persistent user profile settings."""
    return config_manager.profile


@app.post("/api/profile")
def update_profile(req: ProfileUpdateRequest):
    """Updates and persists user profile settings."""
    if autopilot_running or search_running or browser_paused:
        raise HTTPException(status_code=409, detail="Finish the current session before changing its candidate profile.")
    try:
        updated = config_manager.update_profile(req.model_dump(exclude_unset=True))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid profile: {exc}") from exc
    return {"status": "success", "profile": updated}


@app.post("/api/profile/resume")
async def upload_resume(request: Request):
    """Stores a validated resume uploaded as the raw request body."""
    if autopilot_running or search_running or browser_paused:
        raise HTTPException(status_code=409, detail="Finish the current session before replacing its resume.")
    encoded_filename = request.headers.get("x-resume-filename", "")
    filename = unquote(encoded_filename).strip()
    if not filename:
        raise HTTPException(status_code=400, detail="Resume filename is required.")
    from src.resume import MAX_RESUME_BYTES
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_RESUME_BYTES:
            raise HTTPException(status_code=413, detail="Resume must be 10 MB or smaller.")
    try:
        saved_path = await asyncio.to_thread(save_uploaded_resume, filename, bytes(content))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    config_manager.update_profile(
        {"personal_info": {"resume_path": str(saved_path)}}
    )
    return {
        "status": "success",
        "filename": saved_path.name,
        "resume_path": str(saved_path),
    }


@app.get("/api/suggestions/{field}")
def get_search_suggestions(field: str, q: str = "", limit: int = 10):
    """Returns ranked local suggestions for guided search-criteria inputs."""
    try:
        values = find_suggestions(field, q, min(20, max(1, limit)))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown suggestion field.") from exc
    return {"field": field, "query": q, "suggestions": values}


@app.get("/api/jobs/applied")
def get_applied_jobs():
    """Returns history of applied jobs."""
    return db_manager.get_applied_jobs()


@app.get("/api/jobs/dashboard")
def get_dashboard_jobs():
    return db_manager.get_dashboard_jobs()


@app.get("/api/jobs/reviewed")
def get_reviewed_jobs():
    """Returns jobs inspected by the worker and their processing outcomes."""
    return db_manager.get_reviewed_jobs()


@app.get("/api/jobs/flagged")
def get_flagged_jobs():
    """Returns queue of flagged jobs waiting for manual review."""
    return db_manager.get_flagged_jobs()


@app.get("/api/jobs/saved")
def get_saved_jobs():
    return db_manager.get_saved_jobs()


@app.get("/api/search/status")
def get_search_status():
    return {"running": search_running}


@app.get("/api/jobs/search-issues")
def get_search_issues():
    return db_manager.get_search_issues()


@app.post("/api/jobs/status")
async def set_manual_job_status(req: ManualJobStatusRequest):
    try:
        job = db_manager.set_manual_job_status(
            req.job_url, req.status, req.rejection_reason, req.rejection_feedback)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if req.status != "Pending":
        for review in db_manager.get_application_reviews(status=None):
            if db_manager.canonical_url(review["job_url"]) == job["job_url"]:
                review_coordinator.decide(review["id"], "rejected")
    deletion_protected = next((item["deletion_protected"] for item in db_manager.get_dashboard_jobs()
                               if item["job_url"] == job["job_url"]), True)
    return {"status": job["tracking_status"], "job_status": req.status,
            "deletion_protected": deletion_protected,
            "job_url": job["job_url"], "status_changed_at": job["status_changed_at"],
            "rejection_reason": job["rejection_reason"], "rejection_feedback": job["rejection_feedback"]}


@app.post("/api/jobs/delete")
async def delete_jobs(req: DeleteJobsRequest):
    if autopilot_running or search_running:
        raise HTTPException(status_code=409, detail="Wait for the current discovery/application run to finish before deleting jobs.")
    try:
        deleted = db_manager.delete_dashboard_jobs(req.job_urls, req.bucket, req.source_mode, req.application_method)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"deleted_job_urls": deleted, "deleted_count": len(deleted)}


@app.get("/api/runtime/status")
def get_runtime_status():
    """Return the current worker stage without blocking the dashboard."""
    return runtime_status.as_dict()


@app.get("/api/applications/reviews")
def get_application_reviews():
    """Returns filled applications currently waiting for user approval."""
    return db_manager.get_application_reviews(status="awaiting_approval", include_manually_tracked=True)


@app.post("/api/applications/reviews/{review_id}/decision")
async def decide_application_review(review_id: int, req: ReviewDecisionRequest):
    """Approve or reject the live browser application waiting at Submit."""
    decision = "approved" if req.decision == "approve" else "rejected"
    if decision == "approved":
        review = next((row for row in db_manager.get_application_reviews(status=None) if row["id"] == review_id), None)
        if review and db_manager.is_job_blocked(review["job_url"]):
            raise HTTPException(status_code=409, detail="This job is marked Applied or Rejected and cannot be submitted.")
    if not review_coordinator.is_active(review_id):
        raise HTTPException(
            status_code=409,
            detail="This review is no longer attached to an active browser application.",
        )
    saved = db_manager.resolve_application_review(
        review_id,
        decision,
        "Approved in dashboard" if decision == "approved" else "Rejected in dashboard",
    )
    if not saved:
        raise HTTPException(status_code=409, detail="Review record is unavailable; application remains unsubmitted.")
    review_coordinator.decide(review_id, decision)
    await broadcast_log(
        f"Application review #{review_id} was {decision} in the dashboard."
    )
    return {"status": "success", "review_id": review_id, "decision": decision}


@app.post("/api/jobs/teach")
def teach_rule(req: TeachRuleRequest):
    """Persist a manually confirmed answer for semantically similar questions."""
    intent = IntentClassifier.classify(req.question)
    record = answer_memory.save(
        req.question,
        req.answer,
        canonical_intent=intent.name,
        answer_source="user-approved",
        confidence=1.0,
        user_approved=True,
    )
    db_manager.resolve_flagged_job(req.flagged_id)
    return {
        "status": "success",
        "message": f"Approved answer saved for '{req.question}'",
        "answer_memory": record.as_dict(),
    }


@app.get("/api/answers")
def get_answer_memory():
    """Return answer provenance and approval status for local inspection."""
    return answer_memory.list_records()


@app.get("/api/export/excel")
def download_excel():
    """Backward-compatible route for downloading confirmed applications."""
    excel_path = db_manager.export_job_reports()["applied"]
    if excel_path.exists():
        return FileResponse(
            path=excel_path,
            filename=excel_path.name,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    raise HTTPException(status_code=404, detail="Applied-jobs report not found")


@app.get("/api/export/reviewed")
def download_reviewed_jobs():
    """Generates both reports and downloads the jobs-reviewed workbook."""
    excel_path = db_manager.export_job_reports()["reviewed"]
    if excel_path.exists():
        return FileResponse(
            path=excel_path,
            filename=excel_path.name,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    raise HTTPException(status_code=404, detail="Reviewed-jobs report not found")


@app.get("/api/export/applied")
def download_applied_jobs():
    """Generates both reports and downloads the confirmed-applications workbook."""
    excel_path = db_manager.export_job_reports()["applied"]
    if excel_path.exists():
        return FileResponse(
            path=excel_path,
            filename=excel_path.name,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    raise HTTPException(status_code=404, detail="Applied-jobs report not found")


@app.post("/api/autopilot/start")
async def start_autopilot():
    """Triggers autopilot browser job application run asynchronously."""
    global autopilot_running, autopilot_task, browser_paused
    if search_running:
        raise HTTPException(status_code=409, detail="Search & Save Jobs is using the browser. Wait for it to finish.")
    if autopilot_running:
        return {"status": "running", "message": "Autopilot is already running!"}
    if browser_paused and browser_manager.page and not browser_manager.page.is_closed():
        raise HTTPException(status_code=409, detail="A live application is paused for manual review. Finish it and close the session browser, or restart the server before starting again.")
    browser_paused = False
    prefs = config_manager.profile.get("work_preferences", {})
    if not prefs.get("target_roles") or not prefs.get("target_locations"):
        raise HTTPException(status_code=422, detail="Save at least one target role and location in Profile first.")

    autopilot_running = True
    autopilot_task = asyncio.create_task(_run_autopilot_task())
    return {"status": "started", "message": "Autopilot started in background."}


@app.post("/api/search/start")
async def start_search():
    global search_running, search_task
    if search_running or autopilot_running:
        raise HTTPException(status_code=409, detail="A browser workflow is already running. Wait for it to finish.")
    if browser_paused and browser_manager.page and not browser_manager.page.is_closed():
        raise HTTPException(status_code=409, detail="A live application is paused for review. Finish it and close the session browser first.")
    prefs = config_manager.profile.get("work_preferences", {})
    if not prefs.get("target_roles") or not prefs.get("target_locations"):
        raise HTTPException(status_code=422, detail="Save at least one target role and location in Profile first.")
    search_running = True
    search_task = asyncio.create_task(_run_search_task())
    return {"status": "started", "message": "LinkedIn Search & Save Jobs started (search only)."}


async def _run_search_task():
    global search_running
    try:
        page = await browser_manager.get_active_page(log_callback=broadcast_log)
        await LinkedInScraper(page).run_search_only(log_callback=broadcast_log)
        runtime_status.update(current_action="Search & Save complete")
    except Exception as exc:
        logger.exception("Search & Save failed")
        details = f"{type(exc).__name__}: {exc}"
        runtime_status.update(current_action=f"Search failed: {details}")
        await broadcast_log(f"Search & Save failed: {details}")
    finally:
        search_running = False
        await broadcast_log("Search & Save task finished.")


async def _run_autopilot_task():
    global autopilot_running, browser_paused
    try:
        page = await browser_manager.get_active_page(log_callback=broadcast_log)
        scraper = LinkedInScraper(page)
        result = await scraper.run_autopilot(log_callback=broadcast_log)
        browser_paused = bool(result.get("browser_paused"))
        runtime_status.update(current_action="Paused for manual review" if browser_paused else "Session complete")
    except Exception as e:
        import traceback
        # Some low-level exceptions (including NotImplementedError) have an
        # empty string representation. Include the class name so both the
        # terminal and dashboard always show a useful error.
        details = str(e).strip()
        err_text = f"{type(e).__name__}: {details}" if details else type(e).__name__
        print(f"Error during autopilot run: {err_text}")
        traceback.print_exc()
        logger.exception("Autopilot session failed")
        runtime_status.update(current_action=f"Session failed: {err_text}")
        await broadcast_log(f"❌ Error during autopilot run: {err_text}")
    finally:
        autopilot_running = False
        await broadcast_log("🏁 Autopilot task finished.")


# WebSocket Endpoint for real-time console streaming
@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    await websocket.accept()
    active_websockets.append(websocket)
    await websocket.send_json({"type": "log", "data": "🟢 Connected to Autopilot WebSocket log stream."})
    try:
        while True:
            await websocket.receive_text()  # Keep connection open
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in active_websockets:
            active_websockets.remove(websocket)


# Mount static frontend dashboard files
WEB_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="static")

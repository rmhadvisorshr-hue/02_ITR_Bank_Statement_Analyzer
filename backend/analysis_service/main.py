"""FastAPI backend - the single HTTP-serving process for this app.

Historically this module was a Phase 2/3 "analysis sidecar" spawned by a
Node/Express server.mjs, which owned routing, uploads, CORS, SQLite, R2
storage, and the cleanup sweep. This migration folds all of that in here so
FastAPI is the only backend service; server.mjs is retired. The one thing
that stays Node is build_hdfc_workbook.mjs (the Excel-formatting engine,
invoked as a CLI subprocess by pipeline.py, same as before) - that's a
deliberate decision, not something this migration missed.

/api/generate merges what used to be two hops (Node's POST /api/generate,
which itself called this service's POST /analyze then GET /analyze/{id}/stream
and relayed the result) into one: upload, run the pipeline, and stream
progress back over the same HTTP response - the exact contract the frontend
already expects, so it needed zero changes.
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

import db
from storage import r2
from analysis_service import pipeline
from analysis_service.channel import REDIS_URL, get_channel

APP_DIR = Path(__file__).resolve().parent.parent
WORKSPACE = APP_DIR.parent
PUBLIC_DIR = WORKSPACE / "frontend" / "public"
JOBS_DIR = WORKSPACE / "work" / "upload_jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)

# Statements can legitimately be tens of MB (long multi-year, multi-account
# PDFs, or scanned/photographed pages) - 100 MB per file covers those while
# still bounding worst-case disk use per upload.
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

SEVEN_DAYS_SECONDS = 7 * 24 * 60 * 60

# Lets frontend/dev-server.mjs (a separate static-file process for local dev)
# call this API cross-origin. Unset in production - the unified portal and
# the default single-server setup both serve the frontend from this same
# origin, so no CORS headers are needed there.
FRONTEND_DEV_URL = os.environ.get("FRONTEND_DEV_URL", "")

USE_QUEUE = bool(REDIS_URL) and os.environ.get("ITR_USE_QUEUE", "true") != "false"
WORKER_REPLICAS = max(1, int(os.environ.get("ITR_WORKER_REPLICAS", "1")))

CLEANUP_ENABLED = os.environ.get("ITR_ENABLE_CLEANUP") == "true"
JOB_RETENTION_HOURS = int(os.environ.get("ITR_JOB_RETENTION_HOURS", "48"))
CLEANUP_INTERVAL_HOURS = float(os.environ.get("ITR_CLEANUP_INTERVAL_HOURS", "1"))

arq_pool = None
_worker_procs: list[subprocess.Popen] = []


async def _cleanup_sweep():
    import cleanup

    result = cleanup.sweep_stale_job_dirs(JOBS_DIR, retention_hours=JOB_RETENTION_HOURS, dry_run=not CLEANUP_ENABLED)
    verb = "deleted" if CLEANUP_ENABLED else "would delete"
    suffix = "" if CLEANUP_ENABLED else " (dry run - set ITR_ENABLE_CLEANUP=true to actually delete)"
    print(f"[cleanup] job dirs: checked {result['checked']}, {verb} {result['deleted']}{suffix}")

    # Unlike the job-dir sweep above, this always runs for real, not gated
    # behind ITR_ENABLE_CLEANUP: it's not a new capability, just a scheduled
    # version of the exact prune list_reports_for_user() already runs inline
    # on every /api/reports request - deleting only rows whose expiry has
    # already passed.
    try:
        pruned = db.prune_expired_reports()
        if pruned:
            print(f"[cleanup] pruned {pruned} expired report row(s) from the database")
    except Exception as e:
        print(f"[cleanup] expired report prune failed: {e}")


async def _cleanup_loop():
    await asyncio.sleep(30)
    while True:
        try:
            await _cleanup_sweep()
        except Exception as e:
            print(f"[cleanup] job dir sweep failed: {e}")
        await asyncio.sleep(CLEANUP_INTERVAL_HOURS * 60 * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global arq_pool

    if USE_QUEUE:
        from arq import create_pool
        from arq.connections import RedisSettings
        arq_pool = await create_pool(RedisSettings.from_dsn(REDIS_URL))

        # Node used to spawn ITR_WORKER_REPLICAS copies of the arq worker as
        # sidecars whenever REDIS_URL was set; same job, ported here so one
        # process is still everything needed to run the app locally.
        for i in range(WORKER_REPLICAS):
            proc = subprocess.Popen(
                [sys.executable, "-m", "arq", "analysis_worker.WorkerSettings"],
                cwd=str(APP_DIR),
            )
            _worker_procs.append(proc)
            print(f"[worker#{i + 1}] started (pid={proc.pid})")

    cleanup_task = asyncio.create_task(_cleanup_loop())

    yield

    cleanup_task.cancel()
    for proc in _worker_procs:
        proc.terminate()
    if arq_pool:
        await arq_pool.close()


app = FastAPI(lifespan=lifespan)

if FRONTEND_DEV_URL:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[FRONTEND_DEV_URL],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
    )


def _base_path(request: Request) -> str:
    # When mounted behind the staff portal's reverse proxy, the proxy strips
    # its mount prefix before forwarding here and tells us what that prefix
    # was via this header, so we can echo it back into served HTML/URLs.
    return request.headers.get("x-forwarded-prefix", "").rstrip("/")


def _render_template(path: Path, base_path: str) -> str:
    html = path.read_text(encoding="utf-8")
    return html.replace("{{BASE_PATH}}", base_path).replace("{{BACKEND_URL}}", "")


@app.get("/health")
async def health():
    return {"status": "ok"}


# frontend/ is a sibling of backend/ in local dev (and when embedded in the
# unified portal), but doesn't exist at all when this backend is deployed
# standalone (e.g. on a VPS) with the frontend served separately by Vercel -
# in that shape, this backend only needs to answer /api/*, /download/*, and
# /health, so these routes and the static mount below are skipped entirely
# rather than crashing the app at startup.
if PUBLIC_DIR.exists():
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return _render_template(PUBLIC_DIR / "index.html", _base_path(request))

    @app.get("/reports", response_class=HTMLResponse)
    async def reports_page(request: Request):
        return _render_template(PUBLIC_DIR / "reports.html", _base_path(request))


@app.get("/download/{job_id}")
async def download(job_id: str, userId: str | None = None):
    if not re.fullmatch(r"[a-f0-9-]+", job_id, re.IGNORECASE):
        return PlainTextResponse("Invalid job id", status_code=400)

    report = db.get_report(job_id)

    if report:
        if not userId:
            return PlainTextResponse("Missing userId", status_code=400)
        if report["userId"] != userId:
            return PlainTextResponse("You do not have access to this report", status_code=403)
        if report["expiresAt"] <= int(time.time()):
            return PlainTextResponse("Report expired", status_code=404)
        try:
            url = r2.get_download_url(report["r2Key"], report["generatedFilename"])
            return RedirectResponse(url, status_code=302)
        except Exception as e:
            print(f"[download] R2 presign failed: {e}")
            return PlainTextResponse("Report storage is temporarily unavailable", status_code=502)

    # No R2/DB record for this job - fall back to serving the file straight
    # off local disk. No userId/ownership check here: without a DB row
    # there's nothing to check it against - jobId is still an unguessable UUID.
    job_dir = JOBS_DIR / job_id
    output_path = job_dir / "bank_statement_analysis.xlsx"
    if not output_path.exists():
        return PlainTextResponse("Report not found", status_code=404)

    download_name = "bank_statement_analysis.xlsx"
    summary_path = job_dir / "summary.json"
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            if summary.get("dynamic_filename"):
                download_name = summary["dynamic_filename"]
        except (json.JSONDecodeError, OSError):
            pass

    return FileResponse(output_path, filename=download_name)


@app.get("/api/reports")
async def api_reports(userId: str | None = None):
    if not userId:
        return JSONResponse({"error": "Missing userId"}, status_code=400)
    return {"reports": db.list_reports_for_user(userId)}


async def _save_pdf(file: UploadFile, dest: Path) -> bool:
    """Streams file to dest, aborting (and removing the partial file) if it
    exceeds MAX_UPLOAD_BYTES. Returns False in that case."""
    total = 0
    with open(dest, "wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                out.close()
                dest.unlink(missing_ok=True)
                return False
            out.write(chunk)
    return True


@app.post("/api/generate")
async def api_generate(
    request: Request,
    pdfs: list[UploadFile] = File(default=[]),
    userId: str | None = Form(None),
    statementPassword: list[str] = Form([]),
    useOCR: str | None = Form(None),
):
    base_path = _base_path(request)
    # userId/pdfs are optional at the FastAPI-parameter level on purpose:
    # required Form(...)/File(...) params fail with FastAPI's own 422
    # validation error before this body ever runs, which doesn't match the
    # {"error": "..."}/400 contract the frontend expects - so the checks are
    # done here instead, same as the original Express version.
    if not pdfs or not pdfs[0].filename:
        return JSONResponse({"error": "Please upload at least one PDF file."}, status_code=400)
    if not userId:
        return JSONResponse({"error": "Missing userId."}, status_code=400)

    job_id = str(uuid.uuid4())
    job_dir = JOBS_DIR / job_id
    upload_dir = job_dir / "uploads"
    data_dir = job_dir / "data"
    upload_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    for file in pdfs:
        name = file.filename or ""
        if not name.lower().endswith(".pdf"):
            continue
        ok = await _save_pdf(file, upload_dir / name)
        if not ok:
            return JSONResponse(
                {"error": f"PDF is too large. Maximum allowed size is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB per file."},
                status_code=413,
            )
        saved.append(name)

    pwds = []
    for raw in statementPassword:
        pwds.extend(p.strip() for p in raw.split(",") if p.strip())
    use_ocr = useOCR == "true"

    async def stream():
        request_start = time.monotonic()
        timings: dict[str, float] = {}

        def format_event(data: dict) -> str:
            elapsed_ms = int((time.monotonic() - request_start) * 1000)
            phase = data.get("phase")
            if phase and isinstance(data.get("phaseDurationMs"), (int, float)):
                timings[phase] = data["phaseDurationMs"]
            return json.dumps({**data, "elapsedMs": elapsed_ms}) + "\n"

        if not saved:
            yield format_event({"type": "error", "error": "Please upload at least one valid PDF file."})
            return

        yield format_event({"type": "progress", "message": "Extracting data from PDFs..."})

        summary_path = job_dir / "summary.json"
        params = {
            "jobId": job_id,
            "jobDir": str(job_dir),
            "uploadDir": str(upload_dir),
            "dataDir": str(data_dir),
            "summaryPath": str(summary_path),
            "passwords": pwds,
            "useOCR": use_ocr,
            # Always on regardless of which button the client used - the
            # auto-detection this gates only ever falls back to OCR when
            # normal extraction already found no usable text, so it can only
            # rescue a request that would otherwise have failed outright.
            "autoOCR": True,
        }

        channel = get_channel(job_id)
        if USE_QUEUE:
            await arq_pool.enqueue_job("run_analysis_task", params, _job_id=job_id)
        else:
            asyncio.create_task(pipeline.run_pipeline(params, emit=channel.write))

        summary = None
        error_event = None
        async for event in channel.read_from_start():
            if event.get("type") == "progress":
                yield format_event(event)
            elif event.get("type") == "error":
                error_event = event
            elif event.get("type") == "done":
                summary = event.get("payload")

        if error_event is not None:
            yield format_event(error_event)
            return
        if summary is None:
            yield format_event({"type": "error", "error": "Analysis service ended without a result."})
            return

        yield format_event({"type": "progress", "message": "Uploading report to secure storage..."})

        generated_filename = summary.get("dynamic_filename") or "bank_statement_analysis.xlsx"
        r2_key = f"reports/{job_id}/{generated_filename}"
        created_at = int(time.time())
        expires_at = created_at + SEVEN_DAYS_SECONDS
        output_path = job_dir / "bank_statement_analysis.xlsx"

        persisted_to_r2 = False
        upload_start = time.monotonic()
        try:
            r2.upload_report(str(output_path), r2_key)
            persisted_to_r2 = True
        except Exception as e:
            # R2 not configured, or the upload itself failed - the workbook
            # is still valid and sitting at output_path, so don't report
            # this as a failure. Fall back to serving it off local disk.
            print(f"[api/generate] R2 upload failed, falling back to local-only download: {e}")

        yield format_event({
            "type": "progress",
            "message": "Storage upload complete." if persisted_to_r2 else "Storage upload skipped (not configured).",
            "phase": "upload",
            "phaseDurationMs": int((time.monotonic() - upload_start) * 1000),
        })

        if persisted_to_r2:
            db.insert_report(
                job_id=job_id,
                user_id=userId,
                r2_key=r2_key,
                original_filenames=saved,
                generated_filename=generated_filename,
                created_at=created_at,
                expires_at=expires_at,
            )
            import shutil
            shutil.rmtree(job_dir, ignore_errors=True)

            summary["downloadUrl"] = f"{base_path}/download/{job_id}?userId={quote(userId)}"
            summary["expiresAt"] = expires_at
            summary["persistent"] = True
        else:
            summary["downloadUrl"] = f"{base_path}/download/{job_id}"
            summary["persistent"] = False

        summary["jobId"] = job_id
        summary["uploadedFiles"] = saved
        summary["generatedAt"] = created_at
        summary["timings"] = {**timings, "totalMs": int((time.monotonic() - request_start) * 1000)}

        yield format_event({"type": "done", "payload": summary})

    return StreamingResponse(stream(), media_type="text/plain; charset=utf-8")


# Registered last: a request only reaches here if nothing above matched, so
# this can never shadow /api/*, /download/*, /health, /, or /reports.
if PUBLIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(PUBLIC_DIR)), name="static")

"""The extract -> build -> chart pipeline, as an emit()-driven function
instead of Phase 2's async generator. Runs identically whether called
directly from a FastAPI request (no-queue fallback) or from an arq worker
task (analysis_worker/tasks.py) - this module has no idea which one is
calling it, and no idea whether progress is ultimately going out over
Redis or an in-process channel (see channel.py).

extract_bank.py, build_hdfc_workbook_cli.mjs, and add_chart.py are invoked
exactly as the original Node/Express server.mjs invoked them, before it was
retired in favor of this FastAPI backend - unchanged commands, args, env,
and cwd.
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Awaitable, Callable

APP_DIR = Path(__file__).resolve().parent.parent
WORKSPACE = APP_DIR.parent
NODE_EXE = os.environ.get("HDFC_NODE_EXE", "node")

Emit = Callable[[dict], Awaitable[None]]


async def run_subprocess_streaming(cmd, cwd, env=None):
    """Runs cmd, returning (async generator of progress events, state dict).
    Callers must fully drain the generator before reading state["returncode"]
    / state["stderr"], since both are only populated once the process exits."""
    proc_state = {}

    async def generator():
        p = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stderr_chunks = []

        async def drain_stderr():
            async for line in p.stderr:
                stderr_chunks.append(line)

        stderr_task = asyncio.create_task(drain_stderr())

        async for raw_line in p.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and parsed.get("type") == "progress":
                yield parsed

        await stderr_task
        proc_state["returncode"] = await p.wait()
        proc_state["stderr"] = b"".join(stderr_chunks).decode("utf-8", errors="replace")

    return generator(), proc_state


async def run_pipeline(params: dict, emit: Emit) -> None:
    job_dir = Path(params["jobDir"])
    upload_dir = Path(params["uploadDir"])
    data_dir = Path(params["dataDir"])
    summary_path = Path(params["summaryPath"])
    passwords = params.get("passwords", [])
    use_ocr = params.get("useOCR", False)
    auto_ocr = params.get("autoOCR", False)

    # --- extract_bank.py ---------------------------------------------
    extract_args = [
        sys.executable,
        str(APP_DIR / "extract_bank.py"),
        "--input-dir", str(upload_dir),
        "--work-dir", str(data_dir),
        "--summary-json", str(summary_path),
    ]
    for pwd in passwords:
        extract_args.extend(["--password", pwd])
    if use_ocr:
        extract_args.append("--use-ocr")
    if auto_ocr:
        extract_args.append("--auto-ocr")

    extract_start = time.monotonic()
    gen, state = await run_subprocess_streaming(extract_args, cwd=WORKSPACE)
    async for event in gen:
        await emit(event)
    await emit({
        "type": "progress", "message": "PDF extraction finished.",
        "phase": "extraction", "phaseDurationMs": int((time.monotonic() - extract_start) * 1000),
    })

    summary = {}
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            summary = {}

    if state["returncode"] != 0 or not summary.get("transactions"):
        await emit({
            "type": "error",
            "error": "The PDFs were uploaded, but one or more statements could not be reconciled.",
            "details": summary.get("issues") or state["stderr"][-2000:],
        })
        return

    await emit({"type": "progress", "message": "Initializing workbook generation..."})

    # --- build_hdfc_workbook.mjs (via the thin CLI wrapper) -----------
    output_path = job_dir / "bank_statement_analysis.xlsx"
    build_env = {**os.environ, "HDFC_SKIP_PREVIEWS": "1"}
    build_args = [
        NODE_EXE,
        str(APP_DIR / "build_hdfc_workbook_cli.mjs"),
        "--work-dir", str(data_dir),
        "--output-dir", str(job_dir),
        "--output-path", str(output_path),
    ]

    workbook_start = time.monotonic()
    gen, state = await run_subprocess_streaming(build_args, cwd=WORKSPACE, env=build_env)
    async for event in gen:
        await emit(event)

    if state["returncode"] != 0:
        await emit({
            "type": "error",
            "error": "Excel generation failed.",
            "details": state["stderr"][-2000:],
        })
        return

    if not output_path.exists():
        await emit({
            "type": "error",
            "error": "Excel generation failed.",
            "details": "Output file was not created.",
        })
        return
    await emit({
        "type": "progress", "message": "Workbook generation finished.",
        "phase": "workbook", "phaseDurationMs": int((time.monotonic() - workbook_start) * 1000),
    })

    await emit({"type": "progress", "message": "Finalizing formatting and chart generation..."})

    # --- add_chart.py --------------------------------------------------
    # Matches the original behavior exactly: this step's exit code was
    # never checked in server.mjs either, so it isn't checked here.
    chart_start = time.monotonic()
    chart_script = APP_DIR / "add_chart.py"
    if chart_script.exists():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(chart_script), str(output_path),
            cwd=str(WORKSPACE),
        )
        await proc.wait()
    await emit({
        "type": "progress", "message": "Chart generation finished.",
        "phase": "chart", "phaseDurationMs": int((time.monotonic() - chart_start) * 1000),
    })

    await emit({"type": "done", "payload": summary})

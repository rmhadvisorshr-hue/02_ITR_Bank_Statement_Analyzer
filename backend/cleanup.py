"""Stale job-dir sweep. Python port of cleanup.mjs.

Local job directories (uploaded PDFs + intermediate data) are only ever
meant to be transient scratch space - successful jobs are already deleted
right after their R2 upload; this catches failed/errored ones that weren't.

dry_run defaults to True by design: this only *logs* what it would delete
until ITR_ENABLE_CLEANUP=true is explicitly set. Every directory it touches
(or would touch) gets exactly one log line, so a log review always shows the
full picture.
"""
import shutil
import time
from pathlib import Path


def sweep_stale_job_dirs(jobs_dir, retention_hours, dry_run):
    jobs_path = Path(jobs_dir)
    if not jobs_path.exists():
        return {"checked": 0, "deleted": 0}

    retention_seconds = retention_hours * 60 * 60
    now = time.time()

    checked = 0
    deleted = 0

    for entry in jobs_path.iterdir():
        if not entry.is_dir():
            continue
        checked += 1

        age_seconds = now - entry.stat().st_mtime
        if age_seconds < retention_seconds:
            continue

        age_hours = age_seconds / 3600
        if dry_run:
            print(f"[cleanup] would delete {entry.name} (age {age_hours:.1f}h)")
            continue

        shutil.rmtree(entry, ignore_errors=True)
        print(f"[cleanup] deleted {entry.name} (age {age_hours:.1f}h)")
        deleted += 1

    return {"checked": checked, "deleted": deleted}

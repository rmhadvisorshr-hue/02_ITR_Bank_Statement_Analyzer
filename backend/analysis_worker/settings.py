"""arq WorkerSettings. Run via: python -m arq analysis_worker.WorkerSettings
(analysis_service/main.py's lifespan spawns ITR_WORKER_REPLICAS copies of
exactly this, as sidecars, whenever REDIS_URL is set)."""
import os

from arq.connections import RedisSettings
from arq.worker import func

from analysis_worker.tasks import run_analysis_task

REDIS_URL = os.environ.get("REDIS_URL")

MAX_JOBS = int(os.environ.get("ITR_WORKER_MAX_JOBS", "3"))
MAX_TRIES = int(os.environ.get("ITR_WORKER_MAX_TRIES", "2"))

# retry note: max_tries only ever kicks in on an *unhandled exception*
# escaping run_analysis_task - e.g. the process getting OOM-killed mid-job,
# or a genuine bug. pipeline.run_pipeline() catches every expected failure
# (unparseable PDF, workbook build failure, etc.) and emits a clean
# {"type": "error", ...} event instead of raising, so those stay one-shot
# and are never retried - only real crashes are.
run_analysis_task_with_retries = func(run_analysis_task, max_tries=MAX_TRIES)


class WorkerSettings:
    functions = [run_analysis_task_with_retries]
    redis_settings = RedisSettings.from_dsn(REDIS_URL) if REDIS_URL else None
    # Fan-out safety math: extract_bank.py's own ProcessPoolExecutor caps at
    # min(4, cpu_count()) per job (unchanged - not tuned here). Worst case
    # concurrent heavy OS processes across the whole worker fleet is
    # ITR_WORKER_REPLICAS x MAX_JOBS x 4. Defaults (1 x 3 x 4 = 12) are a
    # deliberately modest step up from Phase 3's serial (1 x 1 x 4 = 4) -
    # raise ITR_WORKER_MAX_JOBS/ITR_WORKER_REPLICAS to fit available memory,
    # not this constant.
    max_jobs = MAX_JOBS

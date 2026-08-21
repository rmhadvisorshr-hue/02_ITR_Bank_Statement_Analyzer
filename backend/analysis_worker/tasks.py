"""The arq task itself: a thin wrapper around the same pipeline.run_pipeline
that analysis_service/main.py calls inline in its no-queue fallback. All the
actual work (extract_bank.py -> build_hdfc_workbook_cli.mjs -> add_chart.py)
lives in pipeline.py, unchanged regardless of which of these two callers
invoked it.
"""
from analysis_service import pipeline
from analysis_service.channel import get_channel


async def run_analysis_task(ctx, params: dict) -> None:
    channel = get_channel(params["jobId"])
    await pipeline.run_pipeline(params, emit=channel.write)

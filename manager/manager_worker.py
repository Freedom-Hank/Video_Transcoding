"""Manager-side encoding worker.

When the scheduler's manager_mode flips to 'worker', this module's loop will
pull tasks from the same queue workers use and encode them in-process. The
manager's regular HTTP responsibilities (uploads, splitting, dispatching,
health checks, merging) keep running in their own threads.
"""

import logging
import os
import threading
import time

from encoder import encode_segment
from metrics import get_system_metrics
from scheduler import MANAGER_NODE_NAME, MODE_WORKER

log = logging.getLogger(__name__)

MANAGER_WORKER_NAME = MANAGER_NODE_NAME
HEARTBEAT_INTERVAL = float(os.environ.get("MANAGER_HEARTBEAT_INTERVAL", "5"))
POLL_INTERVAL = float(os.environ.get("MANAGER_POLL_INTERVAL", "2"))

DATA_DIR = os.environ.get("DATA_DIR", "/data")
SEGMENTS_DIR = os.path.join(DATA_DIR, "segments")

_busy = threading.Event()


def manager_heartbeat_loop(scheduler):
    """Always-on loop: report manager's own CPU/Mem/GPU metrics so the UI
    can show them whether or not the manager is currently encoding."""
    while True:
        try:
            metrics = get_system_metrics()
            scheduler.record_heartbeat(MANAGER_WORKER_NAME, metrics)
        except Exception as exc:
            log.warning("Manager heartbeat error: %s", exc)
        time.sleep(HEARTBEAT_INTERVAL)


def manager_worker_loop(scheduler):
    """Mode-gated loop: only pulls + runs tasks while manager_mode == 'worker'."""
    while True:
        try:
            if scheduler.manager_mode == MODE_WORKER and not _busy.is_set():
                task = scheduler.fetch_task_for_worker(MANAGER_WORKER_NAME)
                if task:
                    _busy.set()
                    threading.Thread(
                        target=_run_task,
                        args=(scheduler, task),
                        daemon=True,
                    ).start()
        except Exception as exc:
            log.error("Manager worker loop error: %s", exc)
        time.sleep(POLL_INTERVAL)


def _run_task(scheduler, task: dict):
    task_id = task["task_id"]
    job_id = task["job_id"]
    segments = task.get("segments") or [
        {
            "segment_index": task["segment_index"],
            "segment_file": task["segment_file"],
        }
    ]
    resolution = task.get("resolution", "1280x720")
    output_format = task.get("format", "mp4")
    bitrate = task.get("bitrate", "2M")

    output_dir = os.path.join(SEGMENTS_DIR, job_id, "encoded")
    os.makedirs(output_dir, exist_ok=True)
    ext = output_format.lstrip(".")

    segment_progress = [0 for _ in segments]
    last_pct = [-1]

    def update_progress(pct):
        if pct == last_pct[0]:
            return
        last_pct[0] = pct
        try:
            scheduler.update_task_progress(task_id, pct)
        except Exception:
            pass

    def progress_callback(offset):
        def on_progress(pct):
            segment_progress[offset] = pct
            overall = round(sum(segment_progress) / len(segment_progress))
            update_progress(overall)
        return on_progress

    try:
        output_files = []
        for offset, segment in enumerate(segments):
            segment_index = segment["segment_index"]
            input_path = segment.get("source_file") or segment["segment_file"]
            start_time = segment.get("start_time")
            duration = segment.get("duration")
            output_path = os.path.join(output_dir, f"seg_{segment_index:03d}.{ext}")
            encode_segment(
                input_path,
                output_path,
                resolution,
                output_format,
                bitrate,
                start_time=start_time,
                duration=duration,
                on_progress=progress_callback(offset),
            )
            segment_progress[offset] = 100
            output_files.append(
                {
                    "segment_index": segment_index,
                    "output_segment_file": output_path,
                }
            )
            update_progress(round(sum(segment_progress) / len(segment_progress)))

        scheduler.complete_task(task_id, MANAGER_WORKER_NAME, output_files)
        log.info("Manager finished task %s", task_id)
    except Exception as exc:
        log.error("Manager task %s failed: %s", task_id, exc)
        try:
            scheduler.fail_task(task_id, MANAGER_WORKER_NAME, str(exc))
        except Exception as fail_exc:
            log.warning("Failed to report manager task failure: %s", fail_exc)
    finally:
        _busy.clear()

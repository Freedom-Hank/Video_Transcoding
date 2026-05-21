import logging
import os
import threading
import time

import requests
from flask import Flask, jsonify

from encoder import encode_segment
from metrics import get_system_metrics

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

WORKER_NAME = os.environ.get("WORKER_NAME", "worker-1")
MANAGER_URL = os.environ.get("MANAGER_URL", "http://manager:8080")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
SEGMENTS_DIR = os.path.join(DATA_DIR, "segments")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "2"))
HEARTBEAT_INTERVAL = float(os.environ.get("HEARTBEAT_INTERVAL", "5"))
PROGRESS_MIN_INTERVAL = 0.5

_busy = threading.Event()


def register_with_manager():
    while True:
        try:
            resp = requests.post(
                f"{MANAGER_URL}/nodes/register",
                json={"name": WORKER_NAME},
                timeout=10,
            )
            if resp.status_code == 200:
                log.info("Registered with manager as %s", WORKER_NAME)
                return
        except requests.RequestException as exc:
            log.warning("Registration failed: %s", exc)
        time.sleep(3)


def heartbeat_loop():
    while True:
        try:
            metrics = get_system_metrics()
            requests.post(
                f"{MANAGER_URL}/nodes/{WORKER_NAME}/heartbeat",
                json={"metrics": metrics},
                timeout=10,
            )
        except requests.RequestException as exc:
            log.warning("Heartbeat failed: %s", exc)
        time.sleep(HEARTBEAT_INTERVAL)


def poll_tasks_loop():
    while True:
        if _busy.is_set():
            time.sleep(POLL_INTERVAL)
            continue

        try:
            resp = requests.get(
                f"{MANAGER_URL}/workers/{WORKER_NAME}/task",
                timeout=30,
            )
            if resp.status_code == 204:
                time.sleep(POLL_INTERVAL)
                continue
            if resp.status_code != 200:
                time.sleep(POLL_INTERVAL)
                continue

            task = resp.json()
            _busy.set()
            threading.Thread(target=_run_task, args=(task,), daemon=True).start()
        except requests.RequestException as exc:
            log.warning("Task poll failed: %s", exc)
            time.sleep(POLL_INTERVAL)


def _run_task(task: dict):
    task_id = task["task_id"]
    job_id = task["job_id"]
    segment_index = task["segment_index"]
    input_path = task["segment_file"]
    resolution = task.get("resolution", "1280x720")
    output_format = task.get("format", "mp4")
    bitrate = task.get("bitrate", "2M")

    output_dir = os.path.join(SEGMENTS_DIR, job_id, "encoded")
    os.makedirs(output_dir, exist_ok=True)
    ext = output_format.lstrip(".")
    output_path = os.path.join(output_dir, f"seg_{segment_index:03d}.{ext}")

    last_sent = {"pct": -1, "at": 0.0}

    def on_progress(pct):
        now = time.time()
        if (
            pct < 100
            and pct == last_sent["pct"]
            and now - last_sent["at"] < PROGRESS_MIN_INTERVAL
        ):
            return
        last_sent["pct"] = pct
        last_sent["at"] = now
        try:
            requests.post(
                f"{MANAGER_URL}/workers/{WORKER_NAME}/task/{task_id}/progress",
                json={"progress": pct},
                timeout=5,
            )
        except requests.RequestException:
            pass

    try:
        encode_segment(
            input_path,
            output_path,
            resolution,
            output_format,
            bitrate,
            on_progress=on_progress,
        )
        requests.post(
            f"{MANAGER_URL}/workers/{WORKER_NAME}/task/{task_id}/complete",
            json={"output_segment_file": output_path},
            timeout=30,
        )
    except Exception as exc:
        log.error("Task %s failed: %s", task_id, exc)
        try:
            requests.post(
                f"{MANAGER_URL}/workers/{WORKER_NAME}/task/{task_id}/fail",
                json={"error": str(exc)},
                timeout=10,
            )
        except requests.RequestException as fail_exc:
            log.warning("Failed to report task failure: %s", fail_exc)
    finally:
        _busy.clear()


@app.route("/task", methods=["GET"])
def get_task():
    if _busy.is_set():
        return jsonify({"status": "busy"})
    return "", 204


@app.route("/metrics", methods=["GET"])
def metrics():
    return jsonify(get_system_metrics())


if __name__ == "__main__":
    threading.Thread(target=register_with_manager, daemon=True).start()
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=poll_tasks_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, threaded=True)

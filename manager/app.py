import os
import shutil
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory

from health_monitor import HealthMonitor
from merger import merge_segments
from scheduler import Scheduler
from splitter import split_video

app = Flask(__name__, static_folder="static", static_url_path="")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
SEGMENTS_DIR = os.path.join(DATA_DIR, "segments")
OUTPUTS_DIR = os.path.join(DATA_DIR, "outputs")
SEGMENT_DURATION = int(os.environ.get("SEGMENT_DURATION", "10"))
HEARTBEAT_TIMEOUT = int(os.environ.get("HEARTBEAT_TIMEOUT", "15"))
HEARTBEAT_FAILURE_THRESHOLD = int(
    os.environ.get("HEARTBEAT_FAILURE_THRESHOLD", "3")
)

for d in (UPLOADS_DIR, SEGMENTS_DIR, OUTPUTS_DIR):
    os.makedirs(d, exist_ok=True)

scheduler = Scheduler()
health_monitor = HealthMonitor(
    scheduler,
    timeout_seconds=HEARTBEAT_TIMEOUT,
    failure_threshold=HEARTBEAT_FAILURE_THRESHOLD,
)


def process_job_queue():
    while True:
        try:
            for job_id in scheduler.claim_jobs_for_split():
                threading.Thread(
                    target=_process_new_job,
                    args=(job_id,),
                    daemon=True,
                ).start()

            for job_id in scheduler.claim_jobs_for_merge():
                threading.Thread(
                    target=_merge_job,
                    args=(job_id,),
                    daemon=True,
                ).start()
        except Exception as exc:
            app.logger.error("Job queue error: %s", exc)

        time.sleep(2)


def _process_new_job(job_id: str):
    with scheduler.lock:
        job = scheduler.jobs.get(job_id)
        if not job or job["status"] != "splitting":
            return
        input_path = job["input_path"]

    try:
        job_segments_dir = os.path.join(SEGMENTS_DIR, job_id, "raw")
        segment_paths = split_video(
            input_path, job_segments_dir, SEGMENT_DURATION
        )
        scheduler.start_job_processing(job_id, segment_paths)
    except (RuntimeError, OSError) as exc:
        app.logger.error("Split failed for job %s: %s", job_id, exc)
        scheduler.fail_job(job_id, f"Split failed: {exc}")


def _merge_job(job_id: str):
    with scheduler.lock:
        job = scheduler.jobs.get(job_id)
        if not job or job["status"] != "merging":
            return
        tasks = sorted(
            [scheduler.tasks[tid] for tid in job["task_ids"]],
            key=lambda t: t["segment_index"],
        )
        encoded_paths = [t["output_segment_file"] for t in tasks]
        ext = job.get("format", "mp4")

    try:
        output_path = os.path.join(OUTPUTS_DIR, f"{job_id}_output.{ext}")
        merge_segments(encoded_paths, output_path)
        scheduler.complete_job_merge(job_id, output_path)
    except (RuntimeError, OSError) as exc:
        app.logger.error("Merge failed for job %s: %s", job_id, exc)
        scheduler.fail_job(job_id, f"Merge failed: {exc}")


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/jobs", methods=["POST"])
def create_job():
    if "video" not in request.files:
        return jsonify({"error": "No video file provided"}), 400

    video = request.files["video"]
    if not video.filename:
        return jsonify({"error": "Empty filename"}), 400

    resolution = request.form.get("resolution", "1280x720")
    output_format = request.form.get("format", "mp4")
    bitrate = request.form.get("bitrate", "2M")

    job = scheduler.create_job(
        video.filename, "", resolution, output_format, bitrate
    )
    job_id = job["job_id"]
    upload_path = os.path.join(UPLOADS_DIR, f"{job_id}_{video.filename}")
    video.save(upload_path)

    with scheduler.lock:
        scheduler.jobs[job_id]["input_path"] = upload_path

    return jsonify(scheduler.get_job(job["job_id"])), 201


@app.route("/jobs", methods=["GET"])
def list_jobs():
    return jsonify(scheduler.list_jobs())


@app.route("/jobs/<job_id>", methods=["GET"])
def get_job(job_id):
    job = scheduler.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/jobs/<job_id>", methods=["DELETE"])
def delete_job(job_id):
    ok, message = scheduler.delete_job(job_id)
    if not ok:
        return jsonify({"error": message}), 400

    job_dir = os.path.join(SEGMENTS_DIR, job_id)
    if os.path.isdir(job_dir):
        shutil.rmtree(job_dir, ignore_errors=True)

    for f in Path(UPLOADS_DIR).glob(f"{job_id}_*"):
        f.unlink(missing_ok=True)

    return jsonify({"message": message})


@app.route("/jobs/<job_id>/download", methods=["GET"])
def download_job(job_id):
    with scheduler.lock:
        job = scheduler.jobs.get(job_id)

    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["status"] != "completed" or not job.get("output_path") or job.get("error"):
        return jsonify({"error": "Output not ready"}), 400
    if not os.path.isfile(job["output_path"]):
        return jsonify({"error": "Output file missing"}), 404

    return send_file(
        job["output_path"],
        as_attachment=True,
        download_name=os.path.basename(job["output_path"]),
    )


@app.route("/nodes", methods=["GET"])
def list_nodes():
    return jsonify(scheduler.list_nodes())


@app.route("/nodes/register", methods=["POST"])
def register_node():
    data = request.get_json(silent=True) or {}
    name = data.get("name") or request.form.get("name")
    if not name:
        return jsonify({"error": "Worker name required"}), 400

    node = scheduler.register_worker(name)
    return jsonify(node)


@app.route("/nodes/<name>/heartbeat", methods=["POST"])
def heartbeat(name):
    data = request.get_json(silent=True) or {}
    metrics = data.get("metrics")
    scheduler.record_heartbeat(name, metrics)
    return jsonify({"status": "ok"})


@app.route("/workers/<name>/task", methods=["GET"])
def worker_fetch_task(name):
    task = scheduler.fetch_task_for_worker(name)
    if not task:
        return "", 204
    return jsonify(task)


@app.route("/workers/<name>/task/<task_id>/progress", methods=["POST"])
def worker_task_progress(name, task_id):
    data = request.get_json(silent=True) or {}
    progress = int(data.get("progress", 0))
    scheduler.update_task_progress(task_id, progress)
    return jsonify({"status": "ok"})


@app.route("/workers/<name>/task/<task_id>/complete", methods=["POST"])
def worker_task_complete(name, task_id):
    data = request.get_json(silent=True) or {}
    output_file = data.get("output_segment_file")
    if not output_file:
        return jsonify({"error": "output_segment_file required"}), 400

    job = scheduler.complete_task(task_id, name, output_file)
    return jsonify({"status": "ok", "job": job})


@app.route("/workers/<name>/task/<task_id>/fail", methods=["POST"])
def worker_task_fail(name, task_id):
    data = request.get_json(silent=True) or {}
    error = data.get("error", "Encode failed")
    scheduler.fail_task(task_id, name, error)
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    health_monitor.start()
    threading.Thread(target=process_job_queue, daemon=True).start()
    app.run(host="0.0.0.0", port=8080, threaded=True)

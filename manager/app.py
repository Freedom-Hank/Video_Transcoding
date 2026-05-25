import os
import re
import shutil
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from health_monitor import HealthMonitor
from merger import merge_segments
from scheduler import Scheduler
from splitter import plan_video_segments

app = Flask(__name__, static_folder="static", static_url_path="")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
SEGMENTS_DIR = os.path.join(DATA_DIR, "segments")
OUTPUTS_DIR = os.path.join(DATA_DIR, "outputs")
SEGMENT_DURATION = int(os.environ.get("SEGMENT_DURATION", "10"))
MAX_SEGMENTS = int(os.environ.get("MAX_SEGMENTS", "90"))
TASK_BATCH_SIZE = int(os.environ.get("TASK_BATCH_SIZE", "5"))
HEARTBEAT_TIMEOUT = int(os.environ.get("HEARTBEAT_TIMEOUT", "15"))
HEARTBEAT_FAILURE_THRESHOLD = int(
    os.environ.get("HEARTBEAT_FAILURE_THRESHOLD", "3")
)
TASK_TIMEOUT_SECONDS = int(os.environ.get("TASK_TIMEOUT_SECONDS", "3600"))
MAX_TASK_RETRIES = int(os.environ.get("MAX_TASK_RETRIES", "3"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "2048"))
STATE_PATH = os.environ.get("STATE_PATH", os.path.join(DATA_DIR, "manager_state.json"))

ALLOWED_RESOLUTIONS = {"1920x1080", "1280x720", "854x480", "640x360"}
ALLOWED_FORMATS = {"mp4", "webm", "mkv"}
ALLOWED_VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".mkv",
    ".webm",
    ".avi",
    ".m4v",
}
BITRATE_RE = re.compile(r"^[1-9]\d{0,2}[kKmM]$")

app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

for d in (UPLOADS_DIR, SEGMENTS_DIR, OUTPUTS_DIR):
    os.makedirs(d, exist_ok=True)

scheduler = Scheduler(
    state_path=STATE_PATH,
    max_task_retries=MAX_TASK_RETRIES,
    task_timeout_seconds=TASK_TIMEOUT_SECONDS,
    task_batch_size=TASK_BATCH_SIZE,
)
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

            scheduler.requeue_timed_out_tasks()
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
        segments, split_metadata = plan_video_segments(
            input_path,
            SEGMENT_DURATION,
            max_segments=MAX_SEGMENTS,
        )
        scheduler.start_job_processing(job_id, segments, split_metadata)
    except (RuntimeError, OSError) as exc:
        app.logger.error("Segment planning failed for job %s: %s", job_id, exc)
        scheduler.fail_job(job_id, f"Segment planning failed: {exc}")


def _merge_job(job_id: str):
    with scheduler.lock:
        job = scheduler.jobs.get(job_id)
        if not job or job["status"] != "merging":
            return
        encoded_segments = []
        for task_id in job["task_ids"]:
            task = scheduler.tasks[task_id]
            segments = task.get("segments") or [
                {
                    "segment_index": task["segment_index"],
                    "output_segment_file": task.get("output_segment_file"),
                }
            ]
            for segment in segments:
                encoded_segments.append(
                    (
                        segment["segment_index"],
                        segment["output_segment_file"],
                    )
                )
        encoded_paths = [
            output_file
            for _segment_index, output_file in sorted(encoded_segments)
        ]
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

    filename = secure_filename(video.filename)
    if not filename:
        return jsonify({"error": "Invalid filename"}), 400
    if Path(filename).suffix.lower() not in ALLOWED_VIDEO_EXTENSIONS:
        return jsonify({"error": "Unsupported video file extension"}), 400

    resolution = request.form.get("resolution", "1280x720")
    output_format = request.form.get("format", "mp4").lower().lstrip(".")
    bitrate = request.form.get("bitrate", "2M")
    validation_error = _validate_job_options(resolution, output_format, bitrate)
    if validation_error:
        return jsonify({"error": validation_error}), 400

    job = scheduler.create_job(
        filename, "", resolution, output_format, bitrate
    )
    job_id = job["job_id"]
    upload_path = os.path.join(UPLOADS_DIR, f"{job_id}_{filename}")
    try:
        video.save(upload_path)
    except OSError as exc:
        scheduler.fail_job(job_id, f"Upload failed: {exc}")
        return jsonify({"error": "Upload failed"}), 500

    scheduler.set_job_input_path(job_id, upload_path)

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
    output_files = data.get("output_segment_files")
    if output_files is None and data.get("output_segment_file"):
        output_files = [data["output_segment_file"]]
    if not output_files:
        return jsonify({"error": "output_segment_files required"}), 400

    job = scheduler.complete_task(task_id, name, output_files)
    if job is None:
        return jsonify({"error": "Task is not assigned to this worker"}), 409
    return jsonify({"status": "ok", "job": job})


@app.route("/workers/<name>/task/<task_id>/fail", methods=["POST"])
def worker_task_fail(name, task_id):
    data = request.get_json(silent=True) or {}
    error = data.get("error", "Encode failed")
    scheduler.fail_task(task_id, name, error)
    return jsonify({"status": "ok"})


def _validate_job_options(resolution: str, output_format: str, bitrate: str) -> str | None:
    if resolution not in ALLOWED_RESOLUTIONS:
        return "Unsupported resolution"
    if output_format not in ALLOWED_FORMATS:
        return "Unsupported output format"
    if not BITRATE_RE.match(bitrate):
        return "Unsupported bitrate"
    return None


@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(_exc):
    return jsonify({"error": f"Uploaded file is larger than {MAX_UPLOAD_MB} MB"}), 413


if __name__ == "__main__":
    health_monitor.start()
    threading.Thread(target=process_job_queue, daemon=True).start()
    app.run(host="0.0.0.0", port=8080, threaded=True)

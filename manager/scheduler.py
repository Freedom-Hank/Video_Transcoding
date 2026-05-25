import json
import os
import threading
import uuid
from collections import deque
from datetime import datetime, timezone

DEFAULT_RESOLUTION = "1280x720"
DEFAULT_FORMAT = "mp4"
DEFAULT_BITRATE = "2M"


class Scheduler:
    def __init__(
        self,
        state_path: str | None = None,
        max_task_retries: int = 3,
        task_timeout_seconds: int = 3600,
        task_batch_size: int = 5,
        max_concurrent_jobs: int = 1,
    ):
        self.lock = threading.Lock()
        self.jobs: dict[str, dict] = {}
        self.tasks: dict[str, dict] = {}
        self.nodes: dict[str, dict] = {}
        self.task_queue: deque[str] = deque()
        self.state_path = state_path
        self.max_task_retries = max_task_retries
        self.task_timeout_seconds = task_timeout_seconds
        self.task_batch_size = max(1, task_batch_size)
        self.max_concurrent_jobs = max(1, max_concurrent_jobs)
        self._load_state()

    def register_worker(self, name: str) -> dict:
        with self.lock:
            existing = self.nodes.get(name)
            if existing and existing["status"] == "offline":
                existing["status"] = "idle"
                existing["missed_heartbeats"] = 0
                existing["last_heartbeat_at"] = datetime.now(timezone.utc)
                return existing

            self.nodes[name] = {
                "name": name,
                "status": "idle",
                "last_heartbeat_at": datetime.now(timezone.utc),
                "missed_heartbeats": 0,
                "current_task_id": None,
                "cpu_percent": 0.0,
                "memory_percent": 0.0,
                "memory_used_mb": 0.0,
                "memory_total_mb": 0.0,
                "gpu_available": False,
                "gpu_percent": 0.0,
                "gpu_memory_used_mb": 0.0,
                "gpu_memory_total_mb": 0.0,
                "gpu_memory_percent": 0.0,
                "gpu_name": "",
            }
            self._persist_locked()
            return self.nodes[name]

    def record_heartbeat(self, worker_name: str, metrics: dict | None = None):
        with self.lock:
            node = self.nodes.get(worker_name)
            if not node:
                self._register_worker_locked(worker_name)
                node = self.nodes[worker_name]
            node["last_heartbeat_at"] = datetime.now(timezone.utc)
            node["missed_heartbeats"] = 0
            if node["status"] in ("suspected", "offline"):
                if node["status"] == "offline":
                    node["status"] = "idle"
                elif not node.get("current_task_id"):
                    node["status"] = "idle"
            if metrics:
                node["cpu_percent"] = metrics.get("cpu_percent", 0.0)
                node["memory_percent"] = metrics.get("memory_percent", 0.0)
                node["memory_used_mb"] = metrics.get("memory_used_mb", 0.0)
                node["memory_total_mb"] = metrics.get("memory_total_mb", 0.0)
                node["gpu_available"] = bool(metrics.get("gpu_available", False))
                node["gpu_percent"] = metrics.get("gpu_percent", 0.0)
                node["gpu_memory_used_mb"] = metrics.get("gpu_memory_used_mb", 0.0)
                node["gpu_memory_total_mb"] = metrics.get(
                    "gpu_memory_total_mb", 0.0
                )
                node["gpu_memory_percent"] = metrics.get("gpu_memory_percent", 0.0)
                node["gpu_name"] = metrics.get("gpu_name", "")
            self._persist_locked()

    def run_health_check(self, timeout_seconds: int, failure_threshold: int):
        with self.lock:
            now = datetime.now(timezone.utc)
            to_offline: list[str] = []
            for name, node in list(self.nodes.items()):
                if node["status"] == "offline":
                    continue
                last = node.get("last_heartbeat_at")
                if last is None:
                    continue
                elapsed = (now - last).total_seconds()
                if elapsed > timeout_seconds:
                    node["missed_heartbeats"] = node.get("missed_heartbeats", 0) + 1
                    if node["missed_heartbeats"] >= failure_threshold:
                        to_offline.append(name)
                    else:
                        node["status"] = "suspected"
                else:
                    if node["status"] == "suspected":
                        node["status"] = (
                            "busy" if node.get("current_task_id") else "idle"
                        )
                    node["missed_heartbeats"] = 0
            for name in to_offline:
                self._mark_worker_offline_locked(name)
            if to_offline:
                self._persist_locked()

    def mark_worker_offline(self, worker_name: str):
        with self.lock:
            self._mark_worker_offline_locked(worker_name)

    def _mark_worker_offline_locked(self, worker_name: str):
        node = self.nodes.get(worker_name)
        if not node:
            return
        node["status"] = "offline"
        node["current_task_id"] = None
        for task_id, task in self.tasks.items():
            if (
                task["assigned_worker"] == worker_name
                and task["status"] == "running"
            ):
                self._requeue_task_locked(task_id, increment_attempts=False)

    def _requeue_task_locked(self, task_id: str, increment_attempts: bool = False):
        task = self.tasks.get(task_id)
        if not task:
            return
        if increment_attempts:
            task["attempts"] = task.get("attempts", 0) + 1
            if task["attempts"] > self.max_task_retries:
                self._fail_job_locked(
                    task["job_id"],
                    (
                        f"Task {task_id} failed after "
                        f"{self.max_task_retries} retries"
                    ),
                )
                return
        task["status"] = "queued"
        task["assigned_worker"] = None
        task["started_at"] = None
        task["progress"] = 0
        task["output_segment_file"] = None
        task["output_segment_files"] = []
        for segment in _task_segments(task):
            segment["progress"] = 0
            segment["output_segment_file"] = None
        if task_id not in self.task_queue:
            self.task_queue.append(task_id)

    def is_worker_available(self, worker_name: str) -> bool:
        node = self.nodes.get(worker_name)
        if not node:
            return False
        return node["status"] in ("idle", "busy", "suspected")

    def create_job(
        self,
        filename: str,
        input_path: str,
        resolution: str,
        output_format: str,
        bitrate: str,
        owner_id: str | None = None,
        owner_name: str | None = None,
        total_size_bytes: int | None = None,
    ) -> dict:
        job_id = str(uuid.uuid4())[:8]
        with self.lock:
            job = {
                "job_id": job_id,
                "filename": filename,
                "input_path": input_path,
                "resolution": resolution,
                "format": output_format,
                "bitrate": bitrate,
                "status": "queued" if input_path else "uploading",
                "created_at": _now_iso(),
                "started_at": None,
                "finished_at": None,
                "output_path": None,
                "task_ids": [],
                "segment_count": 0,
                "task_batch_size": self.task_batch_size,
                "split_metadata": {},
                "error": None,
                "owner_id": owner_id,
                "owner_name": owner_name or "",
                "total_size_bytes": total_size_bytes or 0,
                "uploaded_bytes": 0,
                "next_chunk_index": 0,
            }
            self.jobs[job_id] = job
            self._persist_locked()
        return job

    def record_chunk(
        self, job_id: str, chunk_index: int, chunk_size: int, owner_id: str
    ) -> tuple[bool, str, int]:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return False, "Job not found", 404
            if job.get("owner_id") != owner_id:
                return False, "Forbidden", 403
            if job["status"] != "uploading":
                return False, "Job is not in uploading state", 409
            expected = job.get("next_chunk_index", 0)
            if chunk_index != expected:
                return False, f"Expected chunk index {expected}", 409
            job["uploaded_bytes"] = (job.get("uploaded_bytes") or 0) + chunk_size
            job["next_chunk_index"] = chunk_index + 1
            self._persist_locked()
            return True, "", 200

    def get_pending_upload_bytes(self) -> int:
        with self.lock:
            return sum(
                max(
                    0,
                    (j.get("total_size_bytes") or 0)
                    - (j.get("uploaded_bytes") or 0),
                )
                for j in self.jobs.values()
                if j.get("status") == "uploading"
            )

    def list_finished_jobs_with_inputs(self) -> list[tuple[str, str]]:
        with self.lock:
            return [
                (j["job_id"], j["input_path"])
                for j in self.jobs.values()
                if j.get("status") in ("completed", "failed")
                and j.get("input_path")
            ]

    def clear_input_path(self, job_id: str):
        with self.lock:
            job = self.jobs.get(job_id)
            if job:
                job["input_path"] = None
                self._persist_locked()

    def set_job_input_path(self, job_id: str, input_path: str):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            job["input_path"] = input_path
            if job["status"] == "uploading":
                job["status"] = "queued"
            self._persist_locked()

    def claim_jobs_for_split(self) -> list[str]:
        with self.lock:
            active = sum(
                1
                for j in self.jobs.values()
                if j["status"] in ("splitting", "running", "merging")
            )
            slots = max(0, self.max_concurrent_jobs - active)
            if slots <= 0:
                return []
            claimed = []
            # FIFO by created_at so the oldest queued job runs first
            queued = sorted(
                (j for j in self.jobs.values() if j["status"] == "queued" and j.get("input_path")),
                key=lambda j: j.get("created_at") or "",
            )
            for job in queued:
                if len(claimed) >= slots:
                    break
                job["status"] = "splitting"
                claimed.append(job["job_id"])
            if claimed:
                self._persist_locked()
            return claimed

    def start_job_processing(
        self,
        job_id: str,
        segment_specs: list[dict],
        split_metadata: dict | None = None,
    ):
        with self.lock:
            job = self.jobs[job_id]
            job["status"] = "running"
            job["started_at"] = _now_iso()
            job["segment_count"] = len(segment_specs)
            job["task_batch_size"] = self.task_batch_size
            job["split_metadata"] = split_metadata or {}

            for batch_index, start in enumerate(
                range(0, len(segment_specs), self.task_batch_size)
            ):
                batch_specs = segment_specs[start : start + self.task_batch_size]
                segments = [
                    {
                        "segment_index": spec.get("segment_index", start + offset),
                        "segment_file": spec.get("segment_file")
                        or spec.get("source_file"),
                        "source_file": spec.get("source_file")
                        or spec.get("segment_file"),
                        "start_time": spec.get("start_time", 0),
                        "duration": spec.get("duration"),
                        "output_segment_file": None,
                        "progress": 0,
                    }
                    for offset, spec in enumerate(batch_specs)
                ]
                task_id = f"{job_id}-t{batch_index:03d}"
                task = {
                    "task_id": task_id,
                    "job_id": job_id,
                    "batch_index": batch_index,
                    "segment_index": start,
                    "segments": segments,
                    "segment_file": segments[0]["segment_file"],
                    "output_segment_files": [],
                    "output_segment_file": None,
                    "assigned_worker": None,
                    "status": "queued",
                    "progress": 0,
                    "attempts": 0,
                    "error": None,
                    "started_at": None,
                    "finished_at": None,
                }
                self.tasks[task_id] = task
                self.task_queue.append(task_id)
                job["task_ids"].append(task_id)
            self._persist_locked()

    def fail_job(self, job_id: str, error: str):
        with self.lock:
            self._fail_job_locked(job_id, error)
            self._persist_locked()

    def _fail_job_locked(self, job_id: str, error: str):
        job = self.jobs.get(job_id)
        if not job:
            return
        job["status"] = "failed"
        job["error"] = error
        job["finished_at"] = _now_iso()
        for task_id in job.get("task_ids", []):
            task = self.tasks.get(task_id)
            if not task or task["status"] == "completed":
                continue
            task["status"] = "failed"
            task["error"] = error
            task["assigned_worker"] = None
            task["finished_at"] = _now_iso()
            try:
                self.task_queue.remove(task_id)
            except ValueError:
                pass

    def claim_jobs_for_merge(self) -> list[str]:
        with self.lock:
            claimed = []
            for job_id, job in self.jobs.items():
                if job["status"] != "running" or job.get("output_path"):
                    continue
                tasks = [
                    self.tasks[tid]
                    for tid in job["task_ids"]
                    if tid in self.tasks
                ]
                if not tasks or not all(t["status"] == "completed" for t in tasks):
                    continue
                if not all(_task_output_paths(t) for t in tasks):
                    continue
                job["status"] = "merging"
                claimed.append(job_id)
            if claimed:
                self._persist_locked()
            return claimed

    def complete_job_merge(self, job_id: str, output_path: str):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            job["output_path"] = output_path
            job["status"] = "completed"
            job["finished_at"] = _now_iso()
            self._persist_locked()

    def delete_job(
        self, job_id: str, owner_id: str | None = None
    ) -> tuple[bool, str]:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return False, "Job not found"
            if owner_id is not None and job.get("owner_id") != owner_id:
                return False, "Not authorized to delete this job"
            if job["status"] not in ("uploading", "queued"):
                return False, "Only queued jobs can be deleted"
            for task_id in job["task_ids"]:
                if task_id in self.tasks:
                    del self.tasks[task_id]
                try:
                    self.task_queue.remove(task_id)
                except ValueError:
                    pass
            del self.jobs[job_id]
            self._persist_locked()
        return True, "Deleted"

    def fetch_task_for_worker(self, worker_name: str) -> dict | None:
        with self.lock:
            if not self.is_worker_available(worker_name):
                return None

            node = self.nodes.get(worker_name)
            if not node or node["status"] == "offline":
                return None

            if node.get("current_task_id"):
                task = self.tasks.get(node["current_task_id"])
                if task and task["status"] == "running":
                    return self._task_payload(task)

            while self.task_queue:
                task_id = self.task_queue.popleft()
                task = self.tasks.get(task_id)
                if not task or task["status"] != "queued":
                    continue

                task["status"] = "running"
                task["assigned_worker"] = worker_name
                task["started_at"] = _now_iso()
                task["error"] = None
                node["status"] = "busy"
                node["current_task_id"] = task_id
                self._persist_locked()
                return self._task_payload(task)

        return None

    def update_task_progress(self, task_id: str, progress: int):
        with self.lock:
            task = self.tasks.get(task_id)
            if task:
                task["progress"] = min(100, max(0, progress))
                self._persist_locked()

    def complete_task(
        self,
        task_id: str,
        worker_name: str,
        output_segment_files: list[dict] | list[str],
    ) -> dict | None:
        with self.lock:
            task = self.tasks.get(task_id)
            if not task or task["assigned_worker"] != worker_name:
                return None

            output_paths = _normalize_output_paths(output_segment_files)
            segments = _task_segments(task)
            outputs_by_index = {
                item["segment_index"]: item["output_segment_file"]
                for item in output_paths
            }
            for segment in segments:
                segment_index = segment["segment_index"]
                output_file = outputs_by_index.get(segment_index)
                if not output_file and len(output_paths) == 1 and len(segments) == 1:
                    output_file = output_paths[0]["output_segment_file"]
                if not output_file:
                    return None
                segment["output_segment_file"] = output_file
                segment["progress"] = 100

            task["status"] = "completed"
            task["output_segment_files"] = [
                segment["output_segment_file"] for segment in segments
            ]
            task["output_segment_file"] = task["output_segment_files"][0]
            task["progress"] = 100
            task["finished_at"] = _now_iso()
            task["error"] = None

            node = self.nodes.get(worker_name)
            if node:
                node["current_task_id"] = None
                node["status"] = "idle"

            self._persist_locked()
            return self.jobs.get(task["job_id"])

    def fail_task(self, task_id: str, worker_name: str, error: str):
        with self.lock:
            task = self.tasks.get(task_id)
            if not task or task["assigned_worker"] != worker_name:
                return
            task["error"] = error
            self._requeue_task_locked(task_id, increment_attempts=True)
            node = self.nodes.get(worker_name)
            if node:
                node["current_task_id"] = None
                node["status"] = "idle"
            self._persist_locked()

    def requeue_timed_out_tasks(self):
        with self.lock:
            now = datetime.now(timezone.utc)
            changed = False
            for task_id, task in self.tasks.items():
                if task["status"] != "running" or not task.get("started_at"):
                    continue
                started_at = _parse_iso(task["started_at"])
                if not started_at:
                    continue
                elapsed = (now - started_at).total_seconds()
                if elapsed <= self.task_timeout_seconds:
                    continue
                worker_name = task.get("assigned_worker")
                task["error"] = "Task timed out"
                self._requeue_task_locked(task_id, increment_attempts=True)
                if worker_name and worker_name in self.nodes:
                    self.nodes[worker_name]["current_task_id"] = None
                    self.nodes[worker_name]["status"] = "idle"
                changed = True
            if changed:
                self._persist_locked()

    def get_job(self, job_id: str) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return None
            return self._job_detail(job)

    def list_jobs(self) -> list[dict]:
        with self.lock:
            return [self._job_detail(j) for j in self.jobs.values()]

    def list_nodes(self) -> list[dict]:
        with self.lock:
            return [
                {
                    "name": n["name"],
                    "status": n["status"],
                    "last_heartbeat_at": _iso(n.get("last_heartbeat_at")),
                    "missed_heartbeats": n.get("missed_heartbeats", 0),
                    "current_task_id": n.get("current_task_id"),
                    "cpu_percent": n.get("cpu_percent", 0.0),
                    "memory_percent": n.get("memory_percent", 0.0),
                    "memory_used_mb": n.get("memory_used_mb", 0.0),
                    "memory_total_mb": n.get("memory_total_mb", 0.0),
                    "gpu_available": n.get("gpu_available", False),
                    "gpu_percent": n.get("gpu_percent", 0.0),
                    "gpu_memory_used_mb": n.get("gpu_memory_used_mb", 0.0),
                    "gpu_memory_total_mb": n.get("gpu_memory_total_mb", 0.0),
                    "gpu_memory_percent": n.get("gpu_memory_percent", 0.0),
                    "gpu_name": n.get("gpu_name", ""),
                }
                for n in self.nodes.values()
            ]

    def _register_worker_locked(self, name: str):
        self.nodes[name] = {
            "name": name,
            "status": "idle",
            "last_heartbeat_at": datetime.now(timezone.utc),
            "missed_heartbeats": 0,
            "current_task_id": None,
            "cpu_percent": 0.0,
            "memory_percent": 0.0,
            "memory_used_mb": 0.0,
            "memory_total_mb": 0.0,
            "gpu_available": False,
            "gpu_percent": 0.0,
            "gpu_memory_used_mb": 0.0,
            "gpu_memory_total_mb": 0.0,
            "gpu_memory_percent": 0.0,
            "gpu_name": "",
        }

    def _task_payload(self, task: dict) -> dict:
        job = self.jobs.get(task["job_id"], {})
        return {
            "task_id": task["task_id"],
            "job_id": task["job_id"],
            "segment_file": task["segment_file"],
            "segments": task.get("segments")
            or [
                {
                    "segment_index": task["segment_index"],
                    "segment_file": task["segment_file"],
                    "source_file": task["segment_file"],
                    "start_time": 0,
                    "duration": None,
                }
            ],
            "resolution": job.get("resolution", DEFAULT_RESOLUTION),
            "format": job.get("format", DEFAULT_FORMAT),
            "bitrate": job.get("bitrate", DEFAULT_BITRATE),
            "batch_index": task.get("batch_index", task["segment_index"]),
            "segment_index": task["segment_index"],
        }

    def _job_summary(self, job: dict) -> dict:
        tasks = [self.tasks[tid] for tid in job["task_ids"] if tid in self.tasks]
        return {
            "job_id": job["job_id"],
            "filename": job["filename"],
            "status": job["status"],
            "resolution": job["resolution"],
            "format": job["format"],
            "bitrate": job["bitrate"],
            "created_at": job["created_at"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "task_count": len(tasks),
            "segment_count": job.get("segment_count", len(tasks)),
            "completed_segments": sum(
                1
                for t in tasks
                for segment in _task_segments(t)
                if segment.get("output_segment_file")
            ),
            "completed_tasks": sum(1 for t in tasks if t["status"] == "completed"),
            "task_batch_size": job.get("task_batch_size", 1),
            "split_metadata": job.get("split_metadata", {}),
            "error": job.get("error"),
            "owner_id": job.get("owner_id"),
            "owner_name": job.get("owner_name", ""),
            "total_size_bytes": job.get("total_size_bytes", 0),
            "uploaded_bytes": job.get("uploaded_bytes", 0),
        }

    def _job_detail(self, job: dict) -> dict:
        detail = self._job_summary(job)
        detail["output_path"] = job.get("output_path")
        detail["tasks"] = []
        for tid in job["task_ids"]:
            t = self.tasks.get(tid)
            if t:
                detail["tasks"].append(
                    {
                        "task_id": t["task_id"],
                        "batch_index": t.get("batch_index", t["segment_index"]),
                        "segment_index": t["segment_index"],
                        "segment_file": t["segment_file"],
                        "segments": _task_segments(t),
                        "segment_count": len(_task_segments(t)),
                        "assigned_worker": t["assigned_worker"],
                        "status": t["status"],
                        "progress": t["progress"],
                        "attempts": t.get("attempts", 0),
                        "error": t.get("error"),
                        "started_at": t["started_at"],
                        "finished_at": t["finished_at"],
                    }
                )
        return detail

    def _load_state(self):
        if not self.state_path or not os.path.isfile(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return

        self.jobs = data.get("jobs", {})
        self.tasks = data.get("tasks", {})
        self.task_queue = deque(data.get("task_queue", []))
        self._recover_loaded_state()

    def _recover_loaded_state(self):
        queued = set(self.task_queue)
        for task in self.tasks.values():
            _ensure_task_shape(task)
        for job in self.jobs.values():
            task_segment_count = sum(
                len(_task_segments(self.tasks[task_id]))
                for task_id in job.get("task_ids", [])
                if task_id in self.tasks
            )
            job.setdefault("segment_count", task_segment_count or len(job.get("task_ids", [])))
            job.setdefault("task_batch_size", 1)
            job.setdefault("split_metadata", {})
            if job["status"] == "uploading":
                self._fail_job_locked(
                    job["job_id"],
                    "Upload did not finish before manager restart",
                )
                continue
            if job["status"] in ("splitting", "merging"):
                job["status"] = "queued" if job["status"] == "splitting" else "running"
            if job["status"] not in ("queued", "running"):
                continue
            for task_id in job.get("task_ids", []):
                task = self.tasks.get(task_id)
                if not task:
                    continue
                if task["status"] == "running":
                    task["status"] = "queued"
                    task["assigned_worker"] = None
                    task["started_at"] = None
                    task["progress"] = 0
                    task["output_segment_file"] = None
                    task["output_segment_files"] = []
                    for segment in _task_segments(task):
                        segment["progress"] = 0
                        segment["output_segment_file"] = None
                if task["status"] == "queued" and task_id not in queued:
                    self.task_queue.append(task_id)
                    queued.add(task_id)

    def _persist_locked(self):
        if not self.state_path:
            return
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            tmp_path = f"{self.state_path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "jobs": self.jobs,
                        "tasks": self.tasks,
                        "task_queue": list(self.task_queue),
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            os.replace(tmp_path, self.state_path)
        except OSError:
            pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(dt) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


def _parse_iso(value: str):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _task_segments(task: dict) -> list[dict]:
    segments = task.get("segments")
    if segments:
        return segments
    return [
        {
            "segment_index": task["segment_index"],
            "segment_file": task["segment_file"],
            "source_file": task.get("source_file", task["segment_file"]),
            "start_time": task.get("start_time", 0),
            "duration": task.get("duration"),
            "output_segment_file": task.get("output_segment_file"),
            "progress": task.get("progress", 0),
        }
    ]


def _task_output_paths(task: dict) -> list[str]:
    return [
        segment["output_segment_file"]
        for segment in _task_segments(task)
        if segment.get("output_segment_file")
    ]


def _normalize_output_paths(output_segment_files: list[dict] | list[str]) -> list[dict]:
    normalized = []
    for item in output_segment_files:
        if isinstance(item, dict):
            if "segment_index" not in item or not item.get("output_segment_file"):
                continue
            normalized.append(
                {
                    "segment_index": int(item["segment_index"]),
                    "output_segment_file": item["output_segment_file"],
                }
            )
        elif isinstance(item, str):
            normalized.append(
                {
                    "segment_index": len(normalized),
                    "output_segment_file": item,
                }
            )
    return normalized


def _ensure_task_shape(task: dict):
    if task.get("segments"):
        task.setdefault("output_segment_files", _task_output_paths(task))
        task.setdefault("batch_index", task.get("segment_index", 0))
        return

    task["segments"] = [
        {
            "segment_index": task["segment_index"],
            "segment_file": task["segment_file"],
            "source_file": task.get("source_file", task["segment_file"]),
            "start_time": task.get("start_time", 0),
            "duration": task.get("duration"),
            "output_segment_file": task.get("output_segment_file"),
            "progress": task.get("progress", 0),
        }
    ]
    task["batch_index"] = task.get("batch_index", task["segment_index"])
    task["output_segment_files"] = _task_output_paths(task)

import threading
import uuid
from collections import deque
from datetime import datetime, timezone

DEFAULT_RESOLUTION = "1280x720"
DEFAULT_FORMAT = "mp4"
DEFAULT_BITRATE = "2M"


class Scheduler:
    def __init__(self):
        self.lock = threading.Lock()
        self.jobs: dict[str, dict] = {}
        self.tasks: dict[str, dict] = {}
        self.nodes: dict[str, dict] = {}
        self.task_queue: deque[str] = deque()

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
            }
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
                self._requeue_task_locked(task_id)

    def _requeue_task_locked(self, task_id: str):
        task = self.tasks.get(task_id)
        if not task:
            return
        task["status"] = "queued"
        task["assigned_worker"] = None
        task["started_at"] = None
        task["progress"] = 0
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
                "status": "queued",
                "created_at": _now_iso(),
                "started_at": None,
                "finished_at": None,
                "output_path": None,
                "task_ids": [],
                "error": None,
            }
            self.jobs[job_id] = job
        return job

    def claim_jobs_for_split(self) -> list[str]:
        with self.lock:
            claimed = []
            for job_id, job in self.jobs.items():
                if job["status"] == "queued":
                    job["status"] = "splitting"
                    claimed.append(job_id)
            return claimed

    def start_job_processing(self, job_id: str, segment_paths: list[str]):
        with self.lock:
            job = self.jobs[job_id]
            job["status"] = "running"
            job["started_at"] = _now_iso()

            for idx, seg_path in enumerate(segment_paths):
                task_id = f"{job_id}-t{idx:03d}"
                task = {
                    "task_id": task_id,
                    "job_id": job_id,
                    "segment_index": idx,
                    "segment_file": seg_path,
                    "output_segment_file": None,
                    "assigned_worker": None,
                    "status": "queued",
                    "progress": 0,
                    "started_at": None,
                    "finished_at": None,
                }
                self.tasks[task_id] = task
                self.task_queue.append(task_id)
                job["task_ids"].append(task_id)

    def fail_job(self, job_id: str, error: str):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            job["status"] = "completed"
            job["error"] = error
            job["finished_at"] = _now_iso()

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
                if not all(t.get("output_segment_file") for t in tasks):
                    continue
                job["status"] = "merging"
                claimed.append(job_id)
            return claimed

    def complete_job_merge(self, job_id: str, output_path: str):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            job["output_path"] = output_path
            job["status"] = "completed"
            job["finished_at"] = _now_iso()

    def delete_job(self, job_id: str) -> tuple[bool, str]:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return False, "Job not found"
            if job["status"] != "queued":
                return False, "Only queued jobs can be deleted"
            for task_id in job["task_ids"]:
                if task_id in self.tasks:
                    del self.tasks[task_id]
                try:
                    self.task_queue.remove(task_id)
                except ValueError:
                    pass
            del self.jobs[job_id]
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
                node["status"] = "busy"
                node["current_task_id"] = task_id
                return self._task_payload(task)

        return None

    def update_task_progress(self, task_id: str, progress: int):
        with self.lock:
            task = self.tasks.get(task_id)
            if task:
                task["progress"] = min(100, max(0, progress))

    def complete_task(
        self, task_id: str, worker_name: str, output_segment_file: str
    ) -> dict | None:
        with self.lock:
            task = self.tasks.get(task_id)
            if not task or task["assigned_worker"] != worker_name:
                return None

            task["status"] = "completed"
            task["output_segment_file"] = output_segment_file
            task["progress"] = 100
            task["finished_at"] = _now_iso()

            node = self.nodes.get(worker_name)
            if node:
                node["current_task_id"] = None
                node["status"] = "idle"

            return self.jobs.get(task["job_id"])

    def fail_task(self, task_id: str, worker_name: str, error: str):
        with self.lock:
            task = self.tasks.get(task_id)
            if not task or task["assigned_worker"] != worker_name:
                return
            task["error"] = error
            self._requeue_task_locked(task_id)
            node = self.nodes.get(worker_name)
            if node:
                node["current_task_id"] = None
                node["status"] = "idle"

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
        }

    def _task_payload(self, task: dict) -> dict:
        job = self.jobs.get(task["job_id"], {})
        return {
            "task_id": task["task_id"],
            "job_id": task["job_id"],
            "segment_file": task["segment_file"],
            "resolution": job.get("resolution", DEFAULT_RESOLUTION),
            "format": job.get("format", DEFAULT_FORMAT),
            "bitrate": job.get("bitrate", DEFAULT_BITRATE),
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
            "completed_tasks": sum(1 for t in tasks if t["status"] == "completed"),
            "error": job.get("error"),
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
                        "segment_index": t["segment_index"],
                        "segment_file": t["segment_file"],
                        "assigned_worker": t["assigned_worker"],
                        "status": t["status"],
                        "progress": t["progress"],
                        "started_at": t["started_at"],
                        "finished_at": t["finished_at"],
                    }
                )
        return detail


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(dt) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()

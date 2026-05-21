import threading
import time


class HealthMonitor:
    def __init__(
        self,
        scheduler,
        timeout_seconds: int = 15,
        failure_threshold: int = 3,
        check_interval: int = 5,
    ):
        self.scheduler = scheduler
        self.timeout_seconds = timeout_seconds
        self.failure_threshold = failure_threshold
        self.check_interval = check_interval
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self._stop.is_set():
            self.scheduler.run_health_check(
                self.timeout_seconds, self.failure_threshold
            )
            self._stop.wait(self.check_interval)

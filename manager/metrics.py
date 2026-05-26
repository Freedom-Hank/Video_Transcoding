import re
import subprocess


def get_system_metrics() -> dict:
    """Parse CPU and memory usage from `top -bn 1 -i -c`."""
    try:
        result = subprocess.run(
            ["top", "-bn", "1", "-i", "-c"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        output = result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return _empty_metrics()

    cpu_percent = 0.0
    mem_percent = 0.0
    mem_used_mb = 0.0
    mem_total_mb = 0.0

    for line in output.splitlines():
        if "%Cpu" in line or "CPU usage" in line or "Cpu(s)" in line:
            idle_match = re.search(r"([\d.]+)\s*%?\s*id", line, re.IGNORECASE)
            if idle_match:
                cpu_percent = round(100.0 - float(idle_match.group(1)), 1)
            else:
                user_match = re.search(r"([\d.]+)\s*us", line)
                sys_match = re.search(r"([\d.]+)\s*sy", line)
                if user_match and sys_match:
                    cpu_percent = round(
                        float(user_match.group(1)) + float(sys_match.group(1)), 1
                    )

        if "MiB Mem" in line or "KiB Mem" in line or "Mem:" in line:
            numbers = re.findall(r"([\d.]+)", line)
            if "MiB Mem" in line and len(numbers) >= 3:
                mem_total_mb = float(numbers[0])
                mem_used_mb = float(numbers[2])
                if mem_total_mb > 0:
                    mem_percent = round(100.0 * mem_used_mb / mem_total_mb, 1)
            elif "KiB Mem" in line and len(numbers) >= 3:
                mem_total_mb = float(numbers[0]) / 1024
                mem_used_mb = float(numbers[2]) / 1024
                if mem_total_mb > 0:
                    mem_percent = round(100.0 * mem_used_mb / mem_total_mb, 1)
            elif len(numbers) >= 4:
                mem_total_mb = float(numbers[0]) / 1024
                mem_used_mb = float(numbers[2]) / 1024
                if mem_total_mb > 0:
                    mem_percent = round(100.0 * mem_used_mb / mem_total_mb, 1)

    metrics = {
        "cpu_percent": cpu_percent,
        "memory_percent": mem_percent,
        "memory_used_mb": round(mem_used_mb, 1),
        "memory_total_mb": round(mem_total_mb, 1),
    }
    metrics.update(_get_gpu_metrics())
    return metrics


def _empty_metrics() -> dict:
    return {
        "cpu_percent": 0.0,
        "memory_percent": 0.0,
        "memory_used_mb": 0.0,
        "memory_total_mb": 0.0,
        **_empty_gpu_metrics(),
    }


def _empty_gpu_metrics() -> dict:
    return {
        "gpu_available": False,
        "gpu_percent": 0.0,
        "gpu_memory_used_mb": 0.0,
        "gpu_memory_total_mb": 0.0,
        "gpu_memory_percent": 0.0,
        "gpu_name": "",
    }


def _get_gpu_metrics() -> dict:
    """Query GPU utilization via nvidia-smi (returns empty stats if unavailable)."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.total,name",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return _empty_gpu_metrics()

    if result.returncode != 0:
        return _empty_gpu_metrics()

    line = (result.stdout or "").strip().splitlines()
    if not line:
        return _empty_gpu_metrics()

    parts = [p.strip() for p in line[0].split(",")]
    if len(parts) < 3:
        return _empty_gpu_metrics()

    try:
        gpu_util = float(parts[0])
        mem_used = float(parts[1])
        mem_total = float(parts[2])
    except ValueError:
        return _empty_gpu_metrics()

    name = parts[3] if len(parts) >= 4 else ""
    mem_pct = round(100.0 * mem_used / mem_total, 1) if mem_total > 0 else 0.0
    return {
        "gpu_available": True,
        "gpu_percent": round(gpu_util, 1),
        "gpu_memory_used_mb": round(mem_used, 1),
        "gpu_memory_total_mb": round(mem_total, 1),
        "gpu_memory_percent": mem_pct,
        "gpu_name": name,
    }

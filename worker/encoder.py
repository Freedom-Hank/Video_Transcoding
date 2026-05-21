import os
import re
import subprocess
import threading


def parse_resolution(resolution: str) -> tuple[int, int]:
    match = re.match(r"(\d+)x(\d+)", resolution.strip())
    if match:
        return int(match.group(1)), int(match.group(2))
    return 1280, 720


def encode_segment(
    input_path: str,
    output_path: str,
    resolution: str,
    output_format: str,
    bitrate: str,
    on_progress=None,
) -> str:
    width, height = parse_resolution(resolution)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    ext = output_format.lstrip(".")
    if ext in ("webm", "mkv"):
        vcodec_args = ["-c:v", "libvpx-vp9", "-b:v", bitrate]
        acodec_args = ["-c:a", "libopus", "-b:a", "128k"]
    else:
        vcodec_args = ["-c:v", "libx264", "-preset", "fast", "-b:v", bitrate]
        acodec_args = ["-c:a", "aac", "-b:a", "128k"]

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        input_path,
        "-vf",
        f"scale={width}:{height}",
        *vcodec_args,
        *acodec_args,
        "-movflags",
        "+faststart",
        output_path,
    ]

    proc = subprocess.Popen(
        cmd,
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        text=True,
    )

    def _read_stderr():
        duration = _probe_duration(input_path)
        for line in proc.stderr:
            if on_progress and duration:
                time_match = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", line)
                if time_match:
                    h, m, s = time_match.groups()
                    current = int(h) * 3600 + int(m) * 60 + float(s)
                    pct = min(99, int(100 * current / duration))
                    on_progress(pct)

    reader = threading.Thread(target=_read_stderr, daemon=True)
    reader.start()
    proc.wait()
    reader.join(timeout=1)

    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg encode failed for {input_path}")

    if on_progress:
        on_progress(100)

    return output_path


def _probe_duration(path: str) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return float(result.stdout.strip())
    except (ValueError, subprocess.TimeoutExpired):
        return 0.0

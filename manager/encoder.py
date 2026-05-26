import os
import re
import subprocess
import threading


VIDEO_ENCODER = os.environ.get("VIDEO_ENCODER", "auto").lower()
FFMPEG_THREADS = os.environ.get("FFMPEG_THREADS", "")


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
    start_time: float | None = None,
    duration: float | None = None,
    on_progress=None,
) -> str:
    width, height = parse_resolution(resolution)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    ext = output_format.lstrip(".")
    encoder = _select_video_encoder(ext)
    cmd = _build_encode_cmd(
        input_path,
        output_path,
        width,
        height,
        ext,
        bitrate,
        encoder,
        start_time=start_time,
        duration=duration,
    )
    returncode = _run_ffmpeg(cmd, input_path, duration, on_progress)
    if returncode != 0 and encoder == "h264_nvenc":
        fallback_cmd = _build_encode_cmd(
            input_path,
            output_path,
            width,
            height,
            ext,
            bitrate,
            "libx264",
            start_time=start_time,
            duration=duration,
        )
        returncode = _run_ffmpeg(fallback_cmd, input_path, duration, on_progress)

    if returncode != 0:
        raise RuntimeError(f"FFmpeg encode failed for {input_path}")

    if on_progress:
        on_progress(100)

    return output_path


def _run_ffmpeg(cmd: list[str], input_path: str, duration: float | None, on_progress):
    proc = subprocess.Popen(
        cmd,
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        text=True,
    )

    def _read_stderr():
        progress_duration = duration or _probe_duration(input_path)
        for line in proc.stderr:
            if on_progress and progress_duration:
                time_match = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", line)
                if time_match:
                    h, m, s = time_match.groups()
                    current = int(h) * 3600 + int(m) * 60 + float(s)
                    pct = min(99, int(100 * current / progress_duration))
                    on_progress(pct)

    reader = threading.Thread(target=_read_stderr, daemon=True)
    reader.start()
    proc.wait()
    reader.join(timeout=1)
    return proc.returncode


def _build_encode_cmd(
    input_path: str,
    output_path: str,
    width: int,
    height: int,
    ext: str,
    bitrate: str,
    encoder: str,
    start_time: float | None = None,
    duration: float | None = None,
) -> list[str]:
    input_args = []
    if start_time is not None:
        input_args.extend(["-ss", str(start_time)])
    if duration is not None:
        input_args.extend(["-t", str(duration)])

    if ext == "webm":
        vcodec_args = ["-c:v", "libvpx-vp9", "-b:v", bitrate]
        acodec_args = ["-c:a", "libopus", "-b:a", "128k"]
    elif encoder == "h264_nvenc":
        vcodec_args = ["-c:v", "h264_nvenc", "-preset", "p4", "-b:v", bitrate]
        acodec_args = ["-c:a", "aac", "-b:a", "128k"]
    else:
        vcodec_args = ["-c:v", "libx264", "-preset", "fast", "-b:v", bitrate]
        if FFMPEG_THREADS:
            vcodec_args.extend(["-threads", FFMPEG_THREADS])
        acodec_args = ["-c:a", "aac", "-b:a", "128k"]

    cmd = [
        "ffmpeg",
        "-y",
        *input_args,
        "-i",
        input_path,
        "-vf",
        f"scale={width}:{height}",
        *vcodec_args,
        *acodec_args,
    ]
    if ext in ("mp4", "m4v"):
        cmd.extend(["-movflags", "+faststart"])
    cmd.append(output_path)
    return cmd


def _select_video_encoder(ext: str) -> str:
    if ext == "webm":
        return "libvpx-vp9"
    if VIDEO_ENCODER in ("h264_nvenc", "nvenc"):
        return "h264_nvenc"
    if VIDEO_ENCODER in ("libx264", "x264", "cpu"):
        return "libx264"
    if _encoder_available("h264_nvenc"):
        return "h264_nvenc"
    return "libx264"


def _encoder_available(name: str) -> bool:
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return name in result.stdout


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

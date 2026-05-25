import math
import subprocess


def plan_video_segments(
    input_path: str,
    segment_duration: int = 10,
    max_segments: int = 90,
) -> tuple[list[dict], dict]:
    """Create time-range segment tasks without writing physical split files."""
    duration = _probe_duration(input_path)
    if not duration:
        raise RuntimeError("Unable to read video duration with ffprobe")

    effective_duration = _effective_segment_duration(
        duration,
        segment_duration,
        max_segments,
    )
    segment_count = max(1, math.ceil(duration / effective_duration))
    segments = []

    for index in range(segment_count):
        start_time = index * effective_duration
        remaining = duration - start_time
        current_duration = min(effective_duration, remaining)
        if current_duration <= 0:
            break
        segments.append(
            {
                "segment_index": index,
                "source_file": input_path,
                "start_time": round(start_time, 3),
                "duration": round(current_duration, 3),
            }
        )

    metadata = {
        "requested_segment_duration": segment_duration,
        "effective_segment_duration": effective_duration,
        "max_segments": max_segments,
        "video_duration": duration,
        "segment_count": len(segments),
        "physical_split": False,
    }
    return segments, metadata


def _effective_segment_duration(
    video_duration: float,
    preferred_duration: int,
    max_segments: int,
) -> int:
    preferred_duration = max(1, preferred_duration)
    if max_segments <= 0:
        return preferred_duration

    duration_for_limit = math.ceil(video_duration / max_segments)
    return max(preferred_duration, duration_for_limit)


def _probe_duration(path: str) -> float | None:
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
        timeout=60,
    )
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None

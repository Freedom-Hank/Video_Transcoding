import os
import subprocess


def merge_segments(segment_paths: list[str], output_path: str) -> str:
    """Merge encoded segments using FFmpeg concat demuxer."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    list_path = output_path + ".list.txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for path in segment_paths:
            f.write(f"file '{_escape_concat_path(path)}'\n")

    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        list_path,
        "-c",
        "copy",
        output_path,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        if os.path.exists(list_path):
            os.remove(list_path)

    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg merge failed: {result.stderr}")

    return output_path


def _escape_concat_path(path: str) -> str:
    return path.replace("'", "'\\''")

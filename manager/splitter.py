import glob
import os
import subprocess


def split_video(input_path: str, output_dir: str, segment_duration: int = 10) -> list[str]:
    """Split video into segments at GOP (keyframe) boundaries."""
    os.makedirs(output_dir, exist_ok=True)
    pattern = os.path.join(output_dir, "seg_%03d.mp4")

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        input_path,
        "-c",
        "copy",
        "-map",
        "0",
        "-f",
        "segment",
        "-segment_time",
        str(segment_duration),
        "-reset_timestamps",
        "1",
        "-break_non_keyframes",
        "0",
        pattern,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg split failed: {result.stderr}")

    segments = sorted(glob.glob(os.path.join(output_dir, "seg_*.mp4")))
    if not segments:
        raise RuntimeError("No segments produced from split")

    return segments

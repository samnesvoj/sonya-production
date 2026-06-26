"""
smart_crop_adapter.py
=====================
Smart vertical crop adapter for SONYA production.
Output: 1080x1920 (9:16)

Priority:
  1. SMART_CROP_ENABLED=true + GeminiComposer available → compose_clip_auto (AI-guided)
  2. SMART_CROP_ENABLED=true + SmartCropper available   → crop_video (face-following)
  3. fallback → ffmpeg center crop
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920


def _flag(name: str, default: str = "true") -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes")


def get_crop_hints(
    input_video_path: str,
    yolo_detections: Optional[List] = None,
    webcam_layout: Optional[Dict] = None,
    pose_detections: Optional[List] = None,
    layout_segments: Optional[List] = None,
) -> Dict[str, Any]:
    """Synthesize crop region hints from available analysis data."""
    hints: Dict[str, Any] = {"method": "center", "strategy": "default"}

    # Highest priority: layout-aware strategy
    if layout_segments:
        types = [s.get("layout", s.get("type", "")) for s in layout_segments]
        dominant = max(set(types), key=types.count) if types else ""
        hints["dominant_layout"] = dominant
        if dominant in ("screen_share", "screen"):
            hints["strategy"] = "letterbox"
        elif dominant in ("single_speaker", "person"):
            hints["strategy"] = "person_center"

    # Person tracking from webcam detector
    if webcam_layout and webcam_layout.get("boxes"):
        hints["method"] = "webcam"
        hints["webcam_boxes"] = webcam_layout["boxes"]

    # YOLO person detection
    if yolo_detections:
        hints["method"] = hints.get("method", "yolo") if hints["method"] == "center" else hints["method"]
        hints["yolo_detections"] = yolo_detections

    # Pose keypoints
    if pose_detections:
        hints["pose_detections"] = pose_detections

    return hints


def compose_vertical_clip(
    input_video_path: str,
    output_path: str,
    crop_hints: Optional[Dict[str, Any]] = None,
    start_time: Optional[float] = None,
    duration: Optional[float] = None,
) -> str:
    """
    Compose a vertical 1080x1920 clip.
    Respects start_time/duration by pre-cutting with ffmpeg before composing.
    Returns path to output file.
    """
    output_path = str(output_path)

    # Pre-cut segment if time range is specified
    if start_time is not None or duration is not None:
        tmp_cut = _cut_segment(input_video_path, start_time, duration)
        source = tmp_cut
        cleanup_tmp = True
    else:
        source = input_video_path
        cleanup_tmp = False

    try:
        if _flag("SMART_CROP_ENABLED"):
            # Try GeminiComposer first (AI-guided, highest quality)
            if _flag("GEMINI_COMPOSER_ENABLED"):
                try:
                    return _compose_gemini(source, output_path, crop_hints)
                except Exception as exc:
                    logger.warning("[smart_crop] GeminiComposer failed (%s) — trying SmartCropper", exc)

            # Try SmartCropper (face-following Haar cascade)
            try:
                return _compose_smart_cropper(source, output_path)
            except Exception as exc:
                logger.warning("[smart_crop] SmartCropper failed (%s) — ffmpeg fallback", exc)

        # Final fallback: ffmpeg center crop
        return _ffmpeg_center_crop(source, output_path)

    finally:
        if cleanup_tmp and os.path.exists(source):
            try:
                os.unlink(source)
            except OSError:
                pass


def _cut_segment(input_path: str, start_time: Optional[float], duration: Optional[float]) -> str:
    """Cut video segment to a temp file. Returns temp file path."""
    suffix = Path(input_path).suffix or ".mp4"
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    os.close(fd)

    cmd = ["ffmpeg", "-y"]
    if start_time is not None and start_time > 0:
        cmd += ["-ss", str(start_time)]
    cmd += ["-i", input_path]
    if duration is not None:
        cmd += ["-t", str(duration)]
    cmd += ["-c:v", "libx264", "-crf", "18", "-c:a", "aac", "-avoid_negative_ts", "1", tmp]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg cut failed: {result.stderr[-300:]}")
    return tmp


def _compose_gemini(source: str, output_path: str, hints: Optional[Dict]) -> str:
    """Use GeminiComposer.compose_clip_auto for AI-guided vertical composition."""
    from scripts.shared.gemini.gemini_composer import GeminiComposer

    output_dir = str(Path(output_path).parent)
    composer = GeminiComposer(output_dir=output_dir)

    # compose_clip_auto handles scene detection + YOLO layout → produces 9:16 output
    result = composer.compose_clip_auto(input_path=source, output_path=output_path)
    if result and os.path.exists(result):
        return result
    raise RuntimeError("GeminiComposer produced no output")


def _compose_smart_cropper(source: str, output_path: str) -> str:
    """Use SmartCropper (face-following Haar cascade) for 9:16 crop."""
    from scripts.shared.crop.cropper import SmartCropper

    cropper = SmartCropper()
    # crop_video returns the output path; output_path treated as destination file
    result = cropper.crop_video(input_video=source, output_path=str(Path(output_path).parent))
    if result and os.path.exists(result):
        # SmartCropper writes its own filename — rename to expected output_path
        if result != output_path:
            import shutil
            shutil.move(result, output_path)
        return output_path
    raise RuntimeError("SmartCropper produced no output")


def _ffmpeg_center_crop(source: str, output_path: str) -> str:
    """Fallback: scale to fill 1080x1920, then center crop."""
    vf = (
        f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}"
    )
    cmd = [
        "ffmpeg", "-y",
        "-i", source,
        "-vf", vf,
        "-c:v", "libx264", "-crf", "23",
        "-c:a", "aac",
        output_path,
    ]
    logger.info("[smart_crop] ffmpeg center crop → %s", output_path)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg center crop failed: {result.stderr[-500:]}")
    return output_path

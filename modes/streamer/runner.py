"""
modes/streamer/runner.py
========================
Streamer mode (beta).

yuvelirochka integration:
  - modes/streamer/legacy/analyzer.py  (yuvelirochka analyzer)
  - modes/streamer/legacy/clipper.py   (yuvelirochka clipper)
  - scripts/shared/crop/cropper.py     (SmartCropper face-following)
  - scripts/shared/vision/webcam_detector.py (person bounding boxes)
  - scripts/shared/speaker/lip_sync_detector.py (active speaker)

webcam_detector is required (optional=false in mode.yaml).
If model not found → error logged, pipeline runs in degraded mode.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ROOT))


def run(
    input_video_path: str,
    output_dir: str,
    params: Optional[Dict[str, Any]] = None,
    progress_callback=None,
) -> Dict[str, Any]:
    if params is None:
        params = {}

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    from scripts.shared.enhancers.sonya_enhancer import enrich_video_for_mode
    enrichment = enrich_video_for_mode(
        input_video_path=input_video_path,
        output_dir=str(output_dir),
        mode="streamer",
        params=params,
        progress_callback=progress_callback,
    )

    # ── yuvelirochka enrichment data ──────────────────────────────────────────
    webcam_layout          = enrichment.get("webcam_layout") or {}
    active_speaker_segs    = enrichment.get("active_speaker_segments") or []
    word_timestamps        = enrichment.get("word_timestamps") or []
    layout_segments        = enrichment.get("layout_segments") or []
    crop_hints             = enrichment.get("crop_hints") or {}
    warnings               = list(enrichment.get("warnings") or [])

    webcam_boxes = webcam_layout.get("boxes", [])

    logger.info(
        "[streamer] webcam_boxes=%d active_speaker_segs=%d words=%d",
        len(webcam_boxes), len(active_speaker_segs), len(word_timestamps),
    )

    # Build transcript dict for yuvelirochka analyzer
    transcript = {
        "words": word_timestamps,
        "text": " ".join(w.get("word", "") for w in word_timestamps),
        "segments": [],
    }

    # ── yuvelirochka legacy analyzer + clipper ────────────────────────────────
    try:
        from modes.streamer.legacy.analyzer import analyze
        from modes.streamer.legacy.clipper import clip_segments

        # analyzer.py from yuvelirochka — accepts enriched webcam/lip-sync data
        analysis = analyze(
            input_video_path,
            webcam_boxes=webcam_boxes,
            lip_sync=active_speaker_segs,
            transcript=transcript,
        )
        raw_segments = clip_segments(analysis, params=params)
        logger.info("[streamer] yuvelirochka analyzer: %d segments", len(raw_segments))
    except Exception as exc:
        logger.warning("[streamer] yuvelirochka analyzer/clipper unavailable (%s) — fallback", exc)
        warnings.append(f"streamer_analyzer_unavailable: {exc}")
        raw_segments = _fallback_segments(active_speaker_segs, webcam_boxes, word_timestamps)

    # ── Select and compose with SmartCropper ─────────────────────────────────
    max_clips = params.get("max_clips", 5)
    top = sorted(raw_segments, key=lambda s: float(s.get("score", 0)), reverse=True)[:max_clips]

    from scripts.shared.crop.smart_crop_adapter import compose_vertical_clip
    output_paths = []
    for i, seg in enumerate(top):
        out = str(output_dir / f"stream_clip_{i+1:02d}.mp4")
        try:
            compose_vertical_clip(
                input_video_path=input_video_path,
                output_path=out,
                crop_hints=crop_hints,
                start_time=float(seg.get("start", 0)),
                duration=float(seg.get("duration", 30.0)),
            )
            output_paths.append(out)
        except Exception as exc:
            logger.warning("[streamer] Clip %d failed: %s", i + 1, exc)
            warnings.append(f"clip_{i+1}_failed: {exc}")

    return {
        "clips": output_paths,
        "mode": "streamer",
        "beta": True,
        "webcam_boxes_found": len(webcam_boxes),
        "active_speaker_segs": len(active_speaker_segs),
        "warnings": warnings,
    }


def _fallback_segments(
    active_speaker_segs: List[Dict],
    webcam_boxes: List[Dict],
    word_timestamps: List[Dict],
) -> List[Dict]:
    """Fallback segment selection from available enrichment."""
    segments = []

    # Use active speaker segments as clips
    for sp in active_speaker_segs:
        sp_start = float(sp.get("start", 0))
        sp_end   = float(sp.get("end", sp_start + 30))
        if sp_end - sp_start >= 5:
            segments.append({
                "start":    sp_start,
                "duration": sp_end - sp_start,
                "score":    2.0 + len([
                    b for b in webcam_boxes
                    if abs(float(b.get("frame", 0)) / 30 - sp_start) < 5
                ]) * 0.1,
                "source": "speaker_fallback",
            })

    if not segments:
        segments.append({"start": 0, "duration": 30.0, "score": 1.0, "source": "default"})

    return segments

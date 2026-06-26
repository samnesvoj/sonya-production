"""
modes/virality/runner.py
========================
Virality mode: hook_mode_v1 + modes_scoring + yuvelirochka visual boosts.

Visual boosts applied to hook_mode segments:
  - gemini_viral_moments  → overlap boost +0.3 per overlapping moment
  - person in first 3s    → webcam_layout boxes boost +0.2
  - layout_segments       → dominant layout awareness
  - active_speaker_segs   → speaking boost +0.15
  - pose_detections       → human action boost +0.1
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
        mode="virality",
        params=params,
        progress_callback=progress_callback,
    )

    clips = _select_and_compose(
        input_video_path=input_video_path,
        enrichment=enrichment,
        params=params,
        output_dir=output_dir,
    )

    return {
        "clips": clips,
        "mode": "virality",
        "warnings": enrichment.get("warnings", []),
    }


def _select_and_compose(
    input_video_path: str,
    enrichment: Dict[str, Any],
    params: Dict[str, Any],
    output_dir: Path,
) -> List[str]:
    """Score segments with hook_mode_v1 + yuvelirochka visual boosts, then compose."""

    # ── Base segments from hook_mode_v1 ──────────────────────────────────────
    try:
        from scripts.legacy_gpu.hook_mode_v1 import HookMode
        hook_mode = HookMode(params=params)
        raw_segments = hook_mode.extract_segments(input_video_path)
        logger.info("[virality] hook_mode_v1: %d raw segments", len(raw_segments))
    except Exception as exc:
        logger.warning("[virality] hook_mode_v1 unavailable (%s) — fallback segments", exc)
        raw_segments = [{"start": 0.0, "duration": 30.0, "score": 0.5}]

    # ── yuvelirochka visual boosts ────────────────────────────────────────────
    gemini_viral_moments   = enrichment.get("gemini_viral_moments") or []
    layout_segments        = enrichment.get("layout_segments") or []
    pose_detections        = enrichment.get("pose_detections") or []
    webcam_layout          = enrichment.get("webcam_layout") or {}
    active_speaker_segs    = enrichment.get("active_speaker_segments") or []
    crop_hints             = enrichment.get("crop_hints") or {}

    webcam_boxes = webcam_layout.get("boxes", [])

    for seg in raw_segments:
        start = float(seg.get("start", 0))
        end   = start + float(seg.get("duration", 30))
        boost = 0.0

        # Person visible in first 3 seconds → strong hook signal
        if start <= 3.0 and webcam_boxes:
            boost += 0.2

        # Gemini viral moment overlap
        for m in gemini_viral_moments:
            m_start = float(m.get("start", m.get("start_time", 0)))
            m_end   = m_start + float(m.get("duration", m.get("clip_duration", 0)))
            if m_start < end and m_end > start:
                boost += 0.3

        # Layout signal: person-focused layout → virality boost
        for ls in layout_segments:
            ls_start = float(ls.get("start", 0))
            ls_end   = float(ls.get("end", ls_start + 1))
            if ls_start < end and ls_end > start:
                layout_type = ls.get("layout", ls.get("type", ""))
                if layout_type in ("single_speaker", "person", "talking_head"):
                    boost += 0.1

        # Active speaker in segment
        for as_seg in active_speaker_segs:
            as_start = float(as_seg.get("start", 0))
            as_end   = float(as_seg.get("end", as_start))
            if as_start < end and as_end > start:
                boost += 0.15

        # Human pose detected (action visible)
        if pose_detections:
            boost += 0.1

        seg["score"] = float(seg.get("score", 0)) + boost

    # ── Select top segments ───────────────────────────────────────────────────
    top = sorted(raw_segments, key=lambda s: float(s.get("score", 0)), reverse=True)
    max_clips = params.get("max_clips", 3)

    # ── Compose vertical clips ────────────────────────────────────────────────
    from scripts.shared.crop.smart_crop_adapter import compose_vertical_clip
    output_paths = []
    for i, seg in enumerate(top[:max_clips]):
        out = str(output_dir / f"viral_clip_{i+1:02d}.mp4")
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
            logger.warning("[virality] Clip %d failed: %s", i + 1, exc)

    return output_paths

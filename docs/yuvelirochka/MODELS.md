# MODELS.md — ML Models in Boosta Standalone

---

## yolo11n.pt — General Object Detector

| Property | Value |
|----------|-------|
| File | `yolo11n.pt` |
| Type | YOLO11 Nano — General Object Detection |
| Classes | 80 COCO classes (person, car, laptop, phone, ...) |
| Key class | 0: person |
| Size | ~5.6 MB |
| Source | Ultralytics official pretrained |

Used in `gemini_composer.py`, `video_composer.py`, `gemini_analyzer.py`.

Person area thresholds in pipeline:
- person > 30% of frame  ->  single_speaker layout
- person 5-25% in corner ->  screen_share layout
- no person detected     ->  full_frame layout

---

## yolo11n-pose.pt — Body Pose Estimator

| Property | Value |
|----------|-------|
| File | `yolo11n-pose.pt` |
| Type | YOLO11 Nano — Human Pose Estimation |
| Keypoints | 17 COCO keypoints (nose, eyes, shoulders, hips, knees, ...) |
| Key keypoint | 0: nose (used for face centering) |
| Size | ~6.3 MB |
| Source | Ultralytics official pretrained |

Used in `gemini_composer.py` for nose x-position.
Enables precise speaker column assignment (left / center / right).
Nose confidence threshold: >= 0.15.

---

## models/webcam_detector.pt — Custom Webcam Frame Detector

| Property | Value |
|----------|-------|
| File | `models/webcam_detector.pt` |
| Original | `best.pt` (from `train_webcam_detector/runs/detect/webcam_2epochs/weights/`) |
| Type | YOLO11 Nano — Custom Single-Class Detector |
| Classes | 1: webcam_frame |
| Size | ~5.4 MB |
| Training | Custom, 2 epochs on webcam-frame images |

Used in `gemini_composer.py` -> `_try_webcam_model()`.

Detects the bounding box of an on-screen webcam overlay rectangle
in screen-share / gameplay recordings.

Confidence thresholds used in pipeline:
- < 0.333  : rejected
- 0.333 +  : accepted (with validation checks)
- 0.5 +    : high confidence, always accepted

Corner classification:
- cx < 40% frame width  -> left
- cx > 60% frame width  -> right
- cy < 40% frame height -> top
- cy > 60% frame height -> bottom
- combinations -> top_left / top_right / bottom_left / bottom_right / center

Test the model:
```powershell
python test_webcam_detector_real.py
python test_webcam_detector_real.py path\to\screenrecord.mp4 --conf 0.25
```

---

## face_landmarker.task — MediaPipe Face Landmarker (download separately)

| Property | Value |
|----------|-------|
| File | `face_landmarker.task` (not included, download manually) |
| Type | MediaPipe FaceLandmarker float16 |
| Used by | `lip_sync_detector.py` |

Download:
```powershell
Invoke-WebRequest `
  -Uri "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task" `
  -OutFile "face_landmarker.task"
```

If not present, `lip_sync_detector.py` returns an empty speaker timeline
(graceful fallback — pipeline continues).

---

## Summary Table

| Model | Classes | Size | Required |
|-------|---------|------|----------|
| yolo11n.pt | 80 COCO | 5.6 MB | Recommended |
| yolo11n-pose.pt | 17 keypoints | 6.3 MB | Recommended |
| models/webcam_detector.pt | 1 (webcam_frame) | 5.4 MB | For screen-share detection |
| face_landmarker.task | 468 landmarks | ~29 MB | Optional (lip sync) |

# GPU Worker Deployment Commands

## Requirements

- CUDA-capable GPU (RTX 3090 / A10 / etc.)
- Python 3.11+
- CUDA 12.x + cuDNN

## Setup on GPU node (vast.ai / RunPod / dedicated)

```bash
# Clone repo
git clone <REPO_URL> /workspace/sonya
cd /workspace/sonya

# Install worker deps
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-worker.txt

# Set env vars
cp .env.example .env
nano .env  # set DATABASE_URL, S3_*, MODELS_S3_BUCKET, WORKER_SECRET, API keys

# Pre-flight check
python scripts/prod_preflight_check.py worker

# Check deps
python scripts/check_dependencies.py worker

# Run single job (for testing)
python scripts/gpu_worker.py --once --job-id TEST_JOB_ID

# Run poll worker
python scripts/gpu_worker.py --poll
```

## Upload model weights to S3 (one-time)

```bash
python scripts/upload_models_to_s3.py --model trailer/best.pt --local /path/to/best.pt
python scripts/upload_models_to_s3.py --model common/yolo11n.pt --local /path/to/yolo11n.pt
python scripts/upload_models_to_s3.py --model common/yolo11n-pose.pt --local /path/to/yolo11n-pose.pt
python scripts/upload_models_to_s3.py --model common/webcam_detector.pt --local /path/to/webcam_detector.pt
# Optional — only if LIP_SYNC_DETECTION_ENABLED=true (disabled by default):
# python scripts/upload_models_to_s3.py --model common/face_landmarker.task --local /path/to/face_landmarker.task
```

## Notes

- Backend (VPS) does NOT install torch/ultralytics/faster-whisper
- Worker installs these from requirements-worker.txt
- Models download automatically from S3 before each job

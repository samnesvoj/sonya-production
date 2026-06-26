# Code Storage Strategy

## What is stored in this repo

- Python source code (`scripts/`, `modes/`)
- Configuration files (`configs/`, `modes/*/mode.yaml`)
- Requirements files
- Deploy documentation

## What is NOT stored in this repo

| Item | Where it lives |
|------|---------------|
| Model weights (*.pt, *.onnx) | S3: `models/` prefix |
| User uploads | S3: `inputs/` prefix |
| Generated outputs | S3: `outputs/` prefix |
| Job state | PostgreSQL: `generation_jobs` table |
| Secrets / API keys | `.env` file (not committed) |
| Dataset files | Not part of this repo |

## S3 layout

```
s3://BUCKET/
  models/
    trailer/best.pt
    common/yolo11n.pt
    common/yolo11n-pose.pt
    common/webcam_detector.pt
  inputs/
    {job_id}.mp4
  outputs/
    {job_id}/clip_01.mp4
    {job_id}/clip_02.mp4
```

## Gitignore rules

All weight formats are gitignored: `*.pt *.onnx *.safetensors *.bin *.task`
The `models/` directory is gitignored.

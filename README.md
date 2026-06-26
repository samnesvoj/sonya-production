# SONYA Production

Clean production repository for SONYA video generation pipeline.

## Architecture

```
scripts/           — core API, worker, shared utilities
  shared/          — reusable modules (gemini, crop, transcription, speaker, vision)
  legacy_gpu/      — original GPU scripts (reference only)
modes/             — production modes (each has runner.py + mode.yaml)
configs/           — environment-specific configs
deploy/            — VPS/GPU deployment docs
```

## Modes

| Mode | Status | Description |
|------|--------|-------------|
| trailer_film_breaker | production | Cinematic vertical trailer clips |
| virality | production | Highest-hook moment selection |
| stories | production | Narrative story segment extraction |
| educational | production | Layout-aware educational clips |
| streamer | beta | Webcam/streamer highlight extraction |
| sonya_gen | placeholder | Future generative mode |

## Setup

```bash
# Backend
pip install -r requirements-backend.txt

# GPU Worker
pip install -r requirements-worker.txt

# Copy and fill in env vars
cp .env.example .env
```

## Database

PostgreSQL only. Run migrations once before first deploy:

```bash
python scripts/run_migrations.py
```

## Pre-flight check

```bash
python scripts/prod_preflight_check.py backend
python scripts/prod_preflight_check.py worker
```

## Repo integrity

```bash
python scripts/validate_repo_integrity.py
```

## Worker

```bash
# Run one job
python scripts/gpu_worker.py --once --job-id JOB_ID

# Poll for jobs
python scripts/gpu_worker.py --poll
```

## Model weights

Weights are NOT stored in this repo. Download via:

```bash
python scripts/upload_models_to_s3.py --model trailer/best.pt --local /path/to/best.pt
```

Models are pulled from S3 automatically before each job runs.

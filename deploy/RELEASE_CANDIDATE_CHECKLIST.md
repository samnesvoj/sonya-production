# Release Candidate Checklist

Before tagging a release, verify all items:

## Repo integrity

- [ ] `python scripts/validate_repo_integrity.py` passes with no errors
- [ ] No `*.pt / *.onnx / *.safetensors / *.bin / *.task` files in repo
- [ ] No `.env` / `.env.local` / `.env.worker` files committed
- [ ] No forbidden top-level folders present (SONYA-DATASET, SONYA, sonya_clean_deploy, backend, raw_videos, test_videos, runs, outputs, models, weights) — verified by `validate_repo_integrity.py` section [2]
- [ ] `python scripts/validate_repo_integrity.py` — section [10] passes (no forbidden dataset/model reference words in docs)

## Code checks

- [ ] `trailer_film_breaker/runner.py` does NOT import `trailer_mode_v3`
- [ ] `downloader.py` has `_PUBLIC_API_DISABLED = True`
- [ ] All `mode.yaml` files exist for all 6 modes
- [ ] All `runner.py` files have a `run()` function

## Environment

- [ ] `.env.example` contains only `CHANGE_ME` placeholders — no real keys
- [ ] `python scripts/prod_preflight_check.py backend` passes on VPS
- [ ] `python scripts/prod_preflight_check.py worker` passes on GPU node

## Database

- [ ] `python scripts/run_migrations.py` runs without errors on fresh DB

## Models

- [ ] All required models uploaded to S3 (`models/trailer/best.pt` etc.)
- [ ] `model_downloader.py` successfully downloads and validates models

## API

- [ ] `POST /api/generation/jobs` returns `job_id`
- [ ] `POST /api/trailer/jobs` alias works
- [ ] `GET /api/generation/jobs/{id}` returns correct status

## Worker

- [ ] `python scripts/gpu_worker.py --once --job-id TEST_ID` completes without crash
- [ ] Output uploaded to S3 `outputs/` prefix

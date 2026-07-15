Fix SONYA worker Docker image crash.

Problem:
Persistent worker container starts correctly with WORKER_LOOP=true, passes preflight, then crashes:

ModuleNotFoundError: No module named 'fastapi'

Trace:
gpu_worker.py imports:
from scripts.security import new_trace_id

scripts/security.py imports fastapi at module import time:
from fastapi import Header, HTTPException, Request, status

The fast worker Docker image ghcr.io/samnesvoj/sonya-worker:fast does not include fastapi.

Task:
1. Update deploy/docker/Dockerfile.worker.fast so the worker image installs fastapi or the backend shared dependency set required by scripts/security.py.
2. Prefer minimal safe fix: add fastapi to the worker image Python dependencies.
3. Do not change runtime behavior.
4. Keep persistent worker mode unchanged:
   - WORKER_LOOP=true
   - gpu_worker.py --poll --worker-id
   - no JOB_ID required in loop mode.
5. Add/extend validate_repo_integrity.py check so Dockerfile.worker.fast includes fastapi or an equivalent dependency source that installs fastapi.
6. Run:
   - bash -n deploy/docker/worker_entrypoint.sh
   - python scripts/validate_repo_integrity.py
7. Commit and push.
8. Confirm GitHub Actions build-worker-fast-image.yml succeeds.
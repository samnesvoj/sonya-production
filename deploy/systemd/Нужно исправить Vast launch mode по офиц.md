Нужно исправить Vast launch mode по официальной документации Vast.

Документация Vast говорит:

1. Есть 3 launch modes:
- entrypoint
- ssh
- jupyter

2. Для automated GPU workers нужен entrypoint mode.

3. Entrypoint mode:
- запускает Docker container из image как есть
- вызывает Dockerfile ENTRYPOINT
- можно override entrypoint / args
- подходит для worker instances

4. SSH/Jupyter mode:
- Docker image ENTRYPOINT НЕ вызывается
- Vast override entrypoint
- onstart script вызывается только внутри ssh/jupyter mode

Наш текущий факт:
- ghcr.io/samnesvoj/sonya-worker:fast скачивается
- Vast пишет success, running
- но /entrypoint.sh не стартует
- backend не видит /api/worker calls
- job остаётся gpu_requested

Нужно сделать правильный production mode:

1. В scripts/gpu_orchestrator.py добавить/обновить:
VAST_LAUNCH_MODE=entrypoint|ssh_onstart|args
default = entrypoint

2. Если VAST_WORKER_IMAGE задан и VAST_LAUNCH_MODE=entrypoint:
- image = VAST_WORKER_IMAGE
- launch/runtype/mode должен быть именно entrypoint согласно Vast API
- НЕ использовать ssh
- НЕ использовать jupyter
- НЕ использовать onstart
- НЕ использовать docker pull/docker run внутри контейнера
- НЕ делать git clone
- Dockerfile ENTRYPOINT должен сам запустить /entrypoint.sh

3. Docker options/env:
Документация Vast говорит, что env vars передаются через Docker run options:
-e TZ=UTC -e CUDA_VISIBLE_DEVICES=0

Поэтому для entrypoint mode сформировать docker options / docker_args / docker_run_args / create_args — как это поле называется в текущем Vast API codebase:
- --shm-size=8gb
- -e JOB_ID=...
- -e MODE=...
- -e WORKER_BACKEND_MODE=api
- -e BACKEND_API_URL=https://sonya-e.com
- -e WORKER_SECRET=...
- -e S3_ENDPOINT_URL=...
- -e S3_ACCESS_KEY_ID=...
- -e S3_SECRET_ACCESS_KEY=...
- -e S3_BUCKET_NAME=...
- -e S3_BUCKET=...
- -e S3_REGION=...
- -e SHUTDOWN_AFTER_JOB=true

Важно:
- секреты не логировать
- в логах показывать только имена env vars
- raw docker options с секретами не печатать

4. Если Vast API поддерживает отдельный env dict в entrypoint mode — можно использовать env dict.
Но если entrypoint mode не получает env dict, использовать Docker options с -e.

5. Dockerfile.worker.fast уже должен иметь:
ENTRYPOINT ["/entrypoint.sh"]

Проверить, что /entrypoint.sh реально copied и chmod +x.

6. Оставить ssh_onstart fallback:
VAST_LAUNCH_MODE=ssh_onstart
- runtype=ssh
- onstart запускает /entrypoint.sh
Но это только fallback/debug, не основной production путь.

7. Убрать production args mode как основной.
args mode оставить experimental, если нужно.

8. validate_repo_integrity.py добавить checks:
- VAST_LAUNCH_MODE supports entrypoint
- default launch mode is entrypoint
- entrypoint mode does not use ssh
- entrypoint mode does not use onstart
- entrypoint mode does not docker pull/run
- entrypoint mode passes env vars via Vast-supported env dict or docker -e options
- docker options include --shm-size
- no secrets in logs
- ssh_onstart fallback still available
- Dockerfile.worker.fast has JSON ENTRYPOINT ["/entrypoint.sh"]

9. docs:
Обновить deploy/commands_production_queue_gpu.md:
- Vast production launch mode = entrypoint
- SSH mode overrides Docker ENTRYPOINT, поэтому не использовать для production
- onstart только fallback
- VAST_WORKER_IMAGE=ghcr.io/samnesvoj/sonya-worker:fast
- VAST_LAUNCH_MODE=entrypoint

Запустить:
python scripts/validate_repo_integrity.py

Отчёт:
entrypoint launch mode yes/no
ssh not used in production yes/no
env via docker options or env dict yes/no
validation passed yes/no
можно push yes/no
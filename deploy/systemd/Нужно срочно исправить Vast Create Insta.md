Нужно срочно исправить Vast Create Instance API payload по официальной документации.

Диагноз:
Мы использовали:
runtype=entrypoint
docker_options=...

Но по официальной Vast Create Instance API:
- runtype НЕ поддерживает "entrypoint"
- доступные runtype: ssh, jupyter, args, ssh_proxy, ssh_direct, jupyter_proxy, jupyter_direct
- если runtype не указан, default = ssh, если нет args/args_str
- env vars и docker run flags передаются через поле env как строка:
  "-e KEY=value -p 8000:8000 --shm-size=8gb"
- args/args_str передаются в image ENTRYPOINT

Нужно исправить scripts/gpu_orchestrator.py:

1. Убрать production использование runtype="entrypoint".
Оставить VAST_LAUNCH_MODE=entrypoint как наше человекочитаемое имя можно, но маппить его в Vast API так:
VAST_LAUNCH_MODE=entrypoint -> payload["runtype"]="args"

2. Production payload должен быть:
{
  "image": VAST_WORKER_IMAGE,
  "runtype": "args",
  "env": "<docker flags string>",
  "args": []
}

Или если Vast не принимает пустой args:
{
  "image": VAST_WORKER_IMAGE,
  "runtype": "args",
  "env": "<docker flags string>",
  "args_str": ""
}

3. Поле docker_options НЕ отправлять в Vast API.
Оно может оставаться только как внутреннее имя функции, но в итоговом payload должно быть именно:
env = docker flags string

4. Env string должен содержать:
--shm-size=8gb
-e JOB_ID=...
-e MODE=...
-e WORKER_BACKEND_MODE=api
-e BACKEND_API_URL=https://sonya-e.com
-e WORKER_SECRET=...
-e S3_ENDPOINT_URL=...
-e S3_ACCESS_KEY_ID=...
-e S3_SECRET_ACCESS_KEY=...
-e S3_BUCKET_NAME=...
-e S3_BUCKET=...
-e S3_REGION=...
-e SHUTDOWN_AFTER_JOB=true
-e VAST_DEBUG_SLEEP_ON_FAIL=...

5. Секреты не логировать.
В /tmp/sonya_vast_last_payload.json показывать:
api_runtype=args
launch_mode=entrypoint
env_keys=[...]
env_has_shm_size=true
env_raw_masked или не показывать raw env вообще

6. ssh_onstart оставить только fallback/debug:
VAST_LAUNCH_MODE=ssh_onstart -> runtype=ssh + onstart

7. args experimental больше не experimental — это официальный API production path.
Можно назвать:
VAST_LAUNCH_MODE=entrypoint
но внутри Vast API это runtype=args.

8. validate_repo_integrity.py:
- production Vast payload never uses runtype="entrypoint"
- production launch mode maps to api runtype="args"
- payload uses env string, not docker_options field
- env string includes --shm-size=8gb
- env string includes -e keys
- no raw secrets in logs or payload dump
- ssh_onstart fallback remains
- validation passes

9. docs:
Обновить:
- Vast UI говорит "Entrypoint", но Create Instance API использует runtype=args
- env vars передаются через env string, не docker_options
- manual GUI может открываться через SSH/Jupyter, но worker production не обязан открываться, ему нужно только стартовать ENTRYPOINT и постучаться в backend

Запустить:
python scripts/validate_repo_integrity.py

Отчёт:
api runtype args yes/no
no unsupported entrypoint runtype yes/no
env string yes/no
validation passed yes/no
можно push yes/no
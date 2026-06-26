# n8n GPU Orchestration

Integration pattern for triggering GPU jobs via n8n workflows.

## Flow

```
n8n Webhook → POST /api/generation/jobs → job created (pending)
n8n HTTP → GET /api/generation/jobs/{id} → poll status
GPU worker --poll → picks up pending job → runs → updates status → done
n8n → notify / deliver result URL
```

## n8n HTTP Request node — create job

- Method: POST
- URL: `https://YOUR_API/api/generation/jobs`
- Body: form-data
  - `mode`: `trailer_film_breaker`
  - `file`: binary video file
  - `params`: `{"max_clips": 3, "clip_duration": 15}`

## n8n poll loop

Use a Loop node + Wait node (10s) polling:
`GET /api/generation/jobs/{{$json.job_id}}`

Until `status` == `done` or `failed`.

## Triggering GPU worker from n8n (optional)

If GPU node is on-demand (vast.ai), n8n can SSH-exec:

```bash
python /workspace/sonya/scripts/gpu_worker.py --once --job-id {{$json.job_id}}
```

Or leave worker in `--poll` mode for always-on GPU instances.

## Auth

Add `Authorization: Bearer WORKER_SECRET` header when calling internal endpoints.

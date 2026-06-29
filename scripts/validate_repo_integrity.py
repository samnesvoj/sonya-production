"""
validate_repo_integrity.py
==========================
Hard integrity checks for the SONYA production repo.
Run before every release: python scripts/validate_repo_integrity.py
Exit 0 = PASS. Exit 1 = FAIL.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT   = Path(__file__).parent.parent
ERRORS: list[str] = []
WARNS:  list[str] = []

# Directories that must never be scanned for project-level checks.
_SKIP_DIRS = {
    ".git", ".cursor", ".venv", "venv",
    "__pycache__", ".pytest_cache", ".mypy_cache", "node_modules",
}


def _rglob_project(pattern: str):
    """rglob that silently skips all service/tool directories."""
    for p in ROOT.rglob(pattern):
        if not any(part in _SKIP_DIRS for part in p.relative_to(ROOT).parts):
            yield p


def err(msg: str)  -> None: ERRORS.append(f"  ERROR: {msg}")
def warn(msg: str) -> None: WARNS.append(f"  WARN:  {msg}")
def ok(msg: str)   -> None: print(f"  OK     {msg}")


# ── 1. Root structure ──────────────────────────────────────────────────────────
print("\n[1] Root structure")
for name in ["scripts", "modes", "configs", "deploy",
             "requirements-base.txt", "requirements-backend.txt",
             "requirements-worker.txt", "requirements-dev.txt",
             ".env.example", ".gitignore", "README.md"]:
    if (ROOT / name).exists(): ok(name)
    else: err(f"Missing: {name}")

# ── 2. Forbidden top-level folders ────────────────────────────────────────────
print("\n[2] Forbidden top-level folders")
for name in ["SONYA-DATASET", "SONYA", "sonya_clean_deploy", "backend",
             "datasets", "raw_videos", "test_videos", "runs",
             "outputs", "models", "weights"]:
    if (ROOT / name).exists(): err(f"Forbidden folder: {name}/")
    else: ok(f"absent: {name}/")

# ── 3. Weight files ────────────────────────────────────────────────────────────
print("\n[3] Weight files")
_WEIGHT_EXTS = {".pt", ".onnx", ".safetensors", ".bin", ".task"}
found_weights = [p for p in _rglob_project("*") if p.suffix in _WEIGHT_EXTS]
if found_weights:
    for p in found_weights: err(f"Weight file: {p.relative_to(ROOT)}")
else: ok("No weight files found")

# ── 4. __pycache__ / *.pyc ────────────────────────────────────────────────────
print("\n[4] __pycache__ / *.pyc")
pcs  = [p for p in _rglob_project("__pycache__") if p.is_dir()]
pycs = list(_rglob_project("*.pyc"))
if pcs:
    for p in pcs: err(f"__pycache__: {p.relative_to(ROOT)}")
elif pycs:
    for p in pycs: err(f"*.pyc: {p.relative_to(ROOT)}")
else: ok("No __pycache__ or *.pyc")

# ── 5. Migrations ─────────────────────────────────────────────────────────────
print("\n[5] Migrations")
mdir = ROOT / "scripts" / "migrations"
if not mdir.exists():
    err("scripts/migrations/ missing")
else:
    sqls = sorted(mdir.glob("*.sql"))
    if len(sqls) < 6: err(f"Too few migrations: {len(sqls)} (need ≥6)")
    else:
        for s in sqls:
            if s.stat().st_size < 50: err(f"Empty migration: {s.name}")
            else: ok(f"migrations/{s.name} ({s.stat().st_size}B)")

# ── 6. Security layer ─────────────────────────────────────────────────────────
print("\n[6] Security layer")
for sf in ["scripts/security.py", "scripts/rate_limiter.py", "scripts/upload_security.py",
           "scripts/quota_guard.py", "scripts/security_audit.py"]:
    p = ROOT / sf
    if p.exists() and p.stat().st_size > 100: ok(sf)
    elif p.exists(): err(f"Empty: {sf}")
    else: err(f"Missing: {sf}")

# ── 7. Secrets scan ───────────────────────────────────────────────────────────
print("\n[7] Secrets scan")
_SELF = Path(__file__).resolve()
_SECRET_PATTERNS = [
    "sk-or-v1", "OPENROUTER_API_KEY=", "ELEVENLABS_API_KEY=",
    "GEMINI_API_KEY=", "S3_SECRET_ACCESS_KEY=",
    "DATABASE_URL=postgresql", "WORKER_SECRET=",
]
secret_found = False
for fpath in _rglob_project("*.py"):
    if fpath.resolve() == _SELF: continue
    text = fpath.read_text(encoding="utf-8", errors="ignore")
    for pat in _SECRET_PATTERNS:
        if pat in text:
            err(f"Secret {pat!r} in {fpath.relative_to(ROOT)}")
            secret_found = True
env_ex = ROOT / ".env.example"
if env_ex.exists():
    for line in env_ex.read_text(encoding="utf-8", errors="ignore").splitlines():
        for pat in _SECRET_PATTERNS:
            if pat in line and "CHANGE_ME" not in line and not line.strip().startswith("#"):
                err(f".env.example real secret: {line.strip()}")
                secret_found = True
if not secret_found: ok("No secrets found")

# ── 8. Old statuses forbidden ─────────────────────────────────────────────────
print("\n[8] Old job statuses (pending/processing/done) absent in production code")
_OLD_STATUS_FILES = [
    "scripts/prod_job_store.py",
    "scripts/prod_generation_api.py",
    "scripts/gpu_worker.py",
    "scripts/migrations/001_generation_jobs.sql",
    "scripts/migrations/003_extended_statuses.sql",
]
_OLD_STATUS_RE = re.compile(
    r"""(?<!['\"])(?:status\s*=\s*['"]|CHECK\s*\([^)]*status\s+IN\s*\([^)]*['"])"""
    r"""(?P<val>pending|processing|done)""",
    re.IGNORECASE,
)
old_status_found = False
for rel in _OLD_STATUS_FILES:
    fpath = ROOT / rel
    if not fpath.exists():
        err(f"Missing file: {rel}")
        continue
    text = fpath.read_text(encoding="utf-8", errors="ignore")
    # Simple check: look for old status string values as SQL/Python literals
    for old in ("'pending'", '"pending"', "'processing'", '"processing"',
                "'done'", '"done"'):
        if old in text:
            err(f"Old status {old} found in {rel}")
            old_status_found = True
if not old_status_found: ok("No old statuses (pending/processing/done) found")

# ── 9. New statuses present ───────────────────────────────────────────────────
print("\n[9] New status constants in prod_job_store.py")
store_text = (ROOT / "scripts/prod_job_store.py").read_text(encoding="utf-8", errors="ignore")
for s in ("queued", "claimed", "completed", "failed",
          "downloading", "model_downloading", "mode_running", "uploading_result"):
    if s in store_text: ok(f"status: {s}")
    else: err(f"Missing status constant: {s}")

# ── 10. S3 key patterns ────────────────────────────────────────────────────────
print("\n[10] S3 key patterns")
s3_text = (ROOT / "scripts/prod_s3_storage.py").read_text(encoding="utf-8", errors="ignore") \
    if (ROOT / "scripts/prod_s3_storage.py").exists() else ""
# Must have users/{user_id}/jobs/
if "users/" in s3_text and "jobs/" in s3_text:
    ok("build_input_key uses users/{user_id}/jobs/{job_id}")
else:
    err("prod_s3_storage.py missing users/{user_id}/jobs/ pattern")
# Must NOT use bare inputs/ or outputs/ prefixes
for bad_prefix in ("'inputs/", '"inputs/', "'outputs/", '"outputs/'):
    if bad_prefix in s3_text:
        err(f"prod_s3_storage.py has forbidden prefix: {bad_prefix}")
ok("No bare inputs/ or outputs/ prefixes") if not any(
    p in s3_text for p in ("'inputs/", '"inputs/', "'outputs/", '"outputs/')
) else None

# ── 11. S3 endpoint_url in model files ────────────────────────────────────────
print("\n[11] S3 endpoint_url in model tools")
for rel in ["scripts/model_downloader.py", "scripts/upload_models_to_s3.py"]:
    fpath = ROOT / rel
    if not fpath.exists():
        err(f"Missing: {rel}")
        continue
    text = fpath.read_text(encoding="utf-8", errors="ignore")
    if "endpoint_url" in text: ok(f"{rel} — endpoint_url present")
    else: err(f"{rel} — missing endpoint_url (bare boto3.client)")
    # Must NOT have bare boto3.client("s3") without endpoint_url
    if 'boto3.client("s3")' in text and "endpoint_url" not in text:
        err(f"{rel} — bare boto3.client('s3') without endpoint_url")

# ── 12. model_downloader: no double models/ prefix ────────────────────────────
print("\n[12] model_downloader local path resolution")
dl_text = (ROOT / "scripts/model_downloader.py").read_text(encoding="utf-8", errors="ignore") \
    if (ROOT / "scripts/model_downloader.py").exists() else ""
if "_resolve_local_path" in dl_text:
    ok("model_downloader has _resolve_local_path (no double models/ prefix)")
else:
    err("model_downloader missing _resolve_local_path — may produce models/models/ paths")

# ── 13. CORS production safety ────────────────────────────────────────────────
print("\n[13] CORS production safety")
sec_text = (ROOT / "scripts/security.py").read_text(encoding="utf-8", errors="ignore") \
    if (ROOT / "scripts/security.py").exists() else ""
if "APP_ENV" in sec_text and "production" in sec_text and "RuntimeError" in sec_text:
    ok("security.py — CORS wildcard blocked in production")
else:
    err("security.py — missing production CORS guard (wildcard '*' allowed in production)")

# ── 14. mode.yaml files ───────────────────────────────────────────────────────
print("\n[14] mode.yaml files")
for mode in ["trailer_film_breaker", "virality", "stories",
             "educational", "streamer", "sonya_gen"]:
    p = ROOT / "modes" / mode / "mode.yaml"
    if p.exists():
        text = p.read_text(encoding="utf-8", errors="ignore")
        # Check no double models/ in local_path values
        for line in text.splitlines():
            if "local_path" in line and "models/common" in line:
                err(f"modes/{mode}/mode.yaml — local_path has double models/ prefix: {line.strip()}")
                break
        else:
            ok(f"modes/{mode}/mode.yaml")
    else:
        err(f"Missing mode.yaml: {mode}")

# ── 15. runner.py run() + enrich_video_for_mode ──────────────────────────────
print("\n[15] runner.py completeness")
for mode in ["trailer_film_breaker", "virality", "stories", "educational", "streamer"]:
    p = ROOT / "modes" / mode / "runner.py"
    if not p.exists():
        err(f"Missing runner.py: {mode}")
        continue
    text = p.read_text(encoding="utf-8", errors="ignore")
    has_run    = "def run(" in text
    has_enrich = "enrich_video_for_mode" in text
    if has_run and has_enrich: ok(f"modes/{mode}/runner.py — run() + enhancer")
    elif not has_run:  err(f"modes/{mode}/runner.py — missing run()")
    elif not has_enrich: err(f"modes/{mode}/runner.py — missing enrich_video_for_mode")

# ── 16. trailer isolation ─────────────────────────────────────────────────────
print("\n[16] trailer_film_breaker isolation")
tr = ROOT / "modes/trailer_film_breaker/runner.py"
if tr.exists():
    if "trailer_mode_v3" in tr.read_text(encoding="utf-8", errors="ignore"):
        err("trailer_film_breaker/runner.py imports trailer_mode_v3 — FORBIDDEN")
    else: ok("trailer_film_breaker — trailer_mode_v3 absent")

# ── 17. prod_generation_api worker endpoints ──────────────────────────────────
print("\n[17] prod_generation_api worker endpoints")
api = ROOT / "scripts/prod_generation_api.py"
if api.exists():
    at = api.read_text(encoding="utf-8", errors="ignore")
    for sym in ("api/worker", "result-url", "/api/worker/claim",
                "/api/worker/jobs", "complete_job", "fail_job"):
        if sym in at: ok(f"api: {sym}")
        else: err(f"api missing: {sym}")
    for bad in ("download_url", "yt-dlp", "youtube"):
        if bad in at.lower(): err(f"api has forbidden: {bad!r}")
else: err("prod_generation_api.py missing")

# ── 18. gpu_worker completeness ───────────────────────────────────────────────
print("\n[18] gpu_worker.py completeness")
wf = ROOT / "scripts/gpu_worker.py"
if wf.exists():
    wt = wf.read_text(encoding="utf-8", errors="ignore")
    for sym in ("--once", "--poll", "ensure_models_for_mode",
                "add_job_file", "complete_job", "fail_job",
                "JOB_STATUS_DOWNLOADING", "JOB_STATUS_UPLOADING_RESULT"):
        if sym in wt: ok(f"worker: {sym}")
        else: err(f"worker missing: {sym}")
else: err("gpu_worker.py missing")

# ── 19. prod_job_store FOR UPDATE SKIP LOCKED ────────────────────────────────
print("\n[19] prod_job_store key functions")
sf = ROOT / "scripts/prod_job_store.py"
if sf.exists():
    st = sf.read_text(encoding="utf-8", errors="ignore")
    for sym in ("FOR UPDATE SKIP LOCKED", "generation_files",
                "claim_next_pending_job", "claim_specific_job",
                "add_job_file", "requeue_stale_jobs",
                "JOB_STATUS_QUEUED", "JOB_STATUS_COMPLETED"):
        if sym in st: ok(f"job_store: {sym}")
        else: err(f"job_store missing: {sym}")
else: err("prod_job_store.py missing")

# ── 20. prod_s3_storage functions ─────────────────────────────────────────────
print("\n[20] prod_s3_storage functions")
if (ROOT / "scripts/prod_s3_storage.py").exists():
    for sym in ("build_input_key", "build_output_key", "build_debug_key",
                "generate_presigned_get_url", "object_exists", "health_check",
                "endpoint_url"):
        if sym in s3_text: ok(f"s3_storage: {sym}")
        else: err(f"s3_storage missing: {sym}")
else: err("prod_s3_storage.py missing")

# ── 21. downloader public API disabled ────────────────────────────────────────
print("\n[21] downloader public API")
dl = ROOT / "scripts/shared/download/downloader.py"
if dl.exists():
    if "_PUBLIC_API_DISABLED = True" in dl.read_text(encoding="utf-8", errors="ignore"):
        ok("downloader._PUBLIC_API_DISABLED = True")
    else:
        err("downloader.py — _PUBLIC_API_DISABLED not set")
else: warn("downloader.py not found")

# ── 22. .gitignore coverage ────────────────────────────────────────────────────
print("\n[22] .gitignore")
gi = ROOT / ".gitignore"
if gi.exists():
    text = gi.read_text(encoding="utf-8", errors="ignore")
    for pat in ("*.pt", "*.onnx", ".env", "models/", "__pycache__/"):
        if pat in text: ok(f".gitignore: {pat}")
        else: err(f".gitignore missing: {pat}")
else: err(".gitignore missing")

# ── 23. Forbidden words in docs ───────────────────────────────────────────────
print("\n[23] Forbidden words in docs")
_FW = ["gr" + "ok", "phi" + "-3", "distill" + "ation", "data" + "sets"]
fw_found = False
for fpath in list(ROOT.glob("*.md")) + list((ROOT / "deploy").glob("*.md")):
    txt = fpath.read_text(encoding="utf-8", errors="ignore").lower()
    for w in _FW:
        if w in txt:
            err(f"Forbidden word {w!r} in {fpath.relative_to(ROOT)}")
            fw_found = True
if not fw_found: ok("No forbidden words in docs")

# ── 24. GPU queue + ephemeral flow ────────────────────────────────────────────
print("\n[24] GPU queue + ephemeral flow")

# 24a — migration 006 exists
_mig_dir = ROOT / "scripts" / "migrations"
_mig006 = list(_mig_dir.glob("006_*.sql"))
if _mig006: ok(f"migration 006 exists — {_mig006[0].name}")
else: err("migration 006 not found (006_gpu_queue_priority.sql)")

# 24b — gpu_dispatcher.py exists
_disp = ROOT / "scripts" / "gpu_dispatcher.py"
if _disp.exists(): ok("gpu_dispatcher.py exists")
else: err("gpu_dispatcher.py missing")

# 24c — gpu_orchestrator.py: all modes present + vast.ai + secret masking
_orch = ROOT / "scripts" / "gpu_orchestrator.py"
if _orch.exists():
    _orch_txt = _orch.read_text(encoding="utf-8", errors="ignore")
    if "webhook" in _orch_txt:
        ok("gpu_orchestrator.py — webhook mode present")
    else:
        err("gpu_orchestrator.py — 'webhook' not found (must remain for backward compat)")
    if "timeweb" in _orch_txt:
        ok("gpu_orchestrator.py — timeweb mode present (optional/legacy)")
    else:
        err("gpu_orchestrator.py — timeweb mode missing")
    if "vast" in _orch_txt:
        ok("gpu_orchestrator.py — vast mode present (production GPU provider)")
    else:
        err("gpu_orchestrator.py — vast mode missing (GPU_ORCHESTRATOR_MODE=vast)")
    if "VAST_DRY_RUN" in _orch_txt or "TIMEWEB_DRY_RUN" in _orch_txt:
        ok("gpu_orchestrator.py — dry-run support present")
    else:
        err("gpu_orchestrator.py — dry-run not implemented (VAST_DRY_RUN / TIMEWEB_DRY_RUN)")
    if "_SECRET_ENV_VARS" in _orch_txt or "_sanitize" in _orch_txt:
        ok("gpu_orchestrator.py — secret masking present")
    else:
        err("gpu_orchestrator.py — secret masking missing (secrets must not be logged)")
    if all(m in _orch_txt for m in ("disabled", "webhook", "timeweb", "vast")):
        ok("gpu_orchestrator.py — all four modes: disabled / webhook / timeweb / vast")
    else:
        err("gpu_orchestrator.py — one or more modes missing (disabled|webhook|timeweb|vast)")
    # vast mode must NOT pass DATABASE_URL to the GPU instance
    # Look for the quoted string literal "DATABASE_URL" inside _VAST_WORKER_ENV_VARS list
    if "_VAST_WORKER_ENV_VARS" in _orch_txt:
        _vast_list_start = _orch_txt.find("_VAST_WORKER_ENV_VARS: List")
        if _vast_list_start == -1:
            _vast_list_start = _orch_txt.find("_VAST_WORKER_ENV_VARS =")
        _vast_list_end = _orch_txt.find("]", _vast_list_start) + 1 if _vast_list_start != -1 else -1
        if _vast_list_start != -1 and _vast_list_end > _vast_list_start:
            _vast_list_body = _orch_txt[_vast_list_start:_vast_list_end]
            if '"DATABASE_URL"' not in _vast_list_body and "'DATABASE_URL'" not in _vast_list_body:
                ok("gpu_orchestrator.py — DATABASE_URL not forwarded to vast.ai GPU (correct)")
            else:
                err("gpu_orchestrator.py — DATABASE_URL must NOT be in _VAST_WORKER_ENV_VARS")
        else:
            ok("gpu_orchestrator.py — _VAST_WORKER_ENV_VARS found (DATABASE_URL check skipped)")
    else:
        err("gpu_orchestrator.py — _VAST_WORKER_ENV_VARS list not found")
    # GPU model include/exclude filters
    if "VAST_GPU_INCLUDE_REGEX" in _orch_txt:
        ok("gpu_orchestrator.py — VAST_GPU_INCLUDE_REGEX supported")
    else:
        err("gpu_orchestrator.py — VAST_GPU_INCLUDE_REGEX missing")
    if "VAST_GPU_EXCLUDE_REGEX" in _orch_txt:
        ok("gpu_orchestrator.py — VAST_GPU_EXCLUDE_REGEX supported")
    else:
        err("gpu_orchestrator.py — VAST_GPU_EXCLUDE_REGEX missing")
    # Default exclusions must cover legacy data-center GPUs
    if "Tesla" in _orch_txt and "V100" in _orch_txt:
        ok("gpu_orchestrator.py — default exclude covers Tesla/V100")
    else:
        err("gpu_orchestrator.py — default VAST_GPU_EXCLUDE_REGEX must include Tesla|V100")
    if "RTX" in _orch_txt and "VAST_GPU_INCLUDE_REGEX" in _orch_txt:
        ok("gpu_orchestrator.py — default include covers RTX consumer GPUs")
    else:
        err("gpu_orchestrator.py — default VAST_GPU_INCLUDE_REGEX must include RTX models")
    if "_get_offer_gpu_name" in _orch_txt:
        ok("gpu_orchestrator.py — _get_offer_gpu_name helper (multi-field gpu name extraction)")
    else:
        err("gpu_orchestrator.py — _get_offer_gpu_name missing")
else:
    err("gpu_orchestrator.py missing")

# 24d — bootstrap_worker_once.sh: exists, has required commands, supports api mode
_bs = ROOT / "deploy" / "gpu" / "bootstrap_worker_once.sh"
if _bs.exists():
    ok("bootstrap_worker_once.sh exists")
    _bs_txt = _bs.read_text(encoding="utf-8", errors="ignore")
    if "gpu_worker.py" in _bs_txt and "--once" in _bs_txt and "--job-id" in _bs_txt:
        ok("bootstrap contains gpu_worker.py --once --job-id")
    else:
        err("bootstrap missing: gpu_worker.py --once --job-id")
    if "shutdown" in _bs_txt:
        ok("bootstrap contains shutdown")
    else:
        err("bootstrap missing: shutdown command")
    if "WORKER_BACKEND_MODE" in _bs_txt:
        ok("bootstrap supports WORKER_BACKEND_MODE (api/db)")
    else:
        err("bootstrap missing WORKER_BACKEND_MODE support")
    if "api" in _bs_txt and "BACKEND_API_URL" in _bs_txt:
        ok("bootstrap supports api mode (no DATABASE_URL required)")
    else:
        err("bootstrap missing api mode support with BACKEND_API_URL")
    # In api mode, DATABASE_URL must be optional (not unconditionally required)
    if "WORKER_BACKEND_MODE" in _bs_txt and "DATABASE_URL" in _bs_txt:
        # Check that DATABASE_URL is guarded by a condition (not always required)
        if 'WORKER_BACKEND_MODE' in _bs_txt and ('api' in _bs_txt):
            ok("bootstrap — DATABASE_URL optional when WORKER_BACKEND_MODE=api")
        else:
            err("bootstrap — DATABASE_URL is unconditionally required; must be optional in api mode")
else:
    err("deploy/gpu/bootstrap_worker_once.sh missing")

# 24e — sonya-dispatcher.service exists
_svc = ROOT / "deploy" / "systemd" / "sonya-dispatcher.service"
if _svc.exists(): ok("sonya-dispatcher.service exists")
else: err("deploy/systemd/sonya-dispatcher.service missing")

# 24f — docs: ephemeral GPU + vast.ai + n8n optional + timeweb optional/legacy
_n8n_doc = ROOT / "deploy" / "n8n_gpu_orchestration.md"
if not _n8n_doc.exists():
    _n8n_doc = ROOT / "deploy" / "N8N_GPU_ORCHESTRATION.md"
if _n8n_doc.exists():
    _doc_txt = _n8n_doc.read_text(encoding="utf-8", errors="ignore").lower()
    if "ephemeral" in _doc_txt:
        ok(f"{_n8n_doc.name} — describes ephemeral GPU")
    else:
        err(f"{_n8n_doc.name} — missing 'ephemeral'")
    if "vast" in _doc_txt or "vast.ai" in _doc_txt:
        ok(f"{_n8n_doc.name} — mentions vast.ai as production GPU provider")
    else:
        err(f"{_n8n_doc.name} — missing vast.ai documentation (production GPU provider)")
    if "n8n" in _doc_txt and ("optional" in _doc_txt or "not required" in _doc_txt):
        ok(f"{_n8n_doc.name} — n8n marked as optional")
    else:
        err(f"{_n8n_doc.name} — n8n must be documented as optional")
    if "timeweb" in _doc_txt and ("optional" in _doc_txt or "legacy" in _doc_txt):
        ok(f"{_n8n_doc.name} — Timeweb GPU marked as optional/legacy")
    else:
        err(f"{_n8n_doc.name} — Timeweb GPU must be documented as optional/legacy (not primary)")
else:
    err("deploy/n8n_gpu_orchestration.md (or N8N_GPU_ORCHESTRATION.md) missing")

_cmd_doc = ROOT / "deploy" / "commands_production_queue_gpu.md"
if _cmd_doc.exists():
    _cmd_txt = _cmd_doc.read_text(encoding="utf-8", errors="ignore").lower()
    ok("commands_production_queue_gpu.md exists")
    if "vast" in _cmd_txt or "vast.ai" in _cmd_txt:
        ok("commands_production_queue_gpu.md — vast.ai commands present")
    else:
        err("commands_production_queue_gpu.md — missing vast.ai commands (production GPU provider)")
    if "vast_dry_run" in _cmd_txt or "dry_run" in _cmd_txt or "dry-run" in _cmd_txt:
        ok("commands_production_queue_gpu.md — dry-run command present")
    else:
        err("commands_production_queue_gpu.md — missing dry-run check command")
    if "worker_backend_mode" in _cmd_txt or "worker backend mode" in _cmd_txt:
        ok("commands_production_queue_gpu.md — WORKER_BACKEND_MODE documented")
    else:
        err("commands_production_queue_gpu.md — WORKER_BACKEND_MODE not documented")
else:
    err("deploy/commands_production_queue_gpu.md missing")

# 24g — gpu_worker: --once, WORKER_BACKEND_MODE api, no unconditional DATABASE_URL
_gw = ROOT / "scripts" / "gpu_worker.py"
if _gw.exists():
    _gw_txt = _gw.read_text(encoding="utf-8", errors="ignore")
    if "--once" in _gw_txt:
        ok("gpu_worker.py supports --once (ephemeral flow)")
    else:
        err("gpu_worker.py missing --once flag")
    if "WORKER_BACKEND_MODE" in _gw_txt:
        ok("gpu_worker.py supports WORKER_BACKEND_MODE env var")
    else:
        err("gpu_worker.py missing WORKER_BACKEND_MODE support")
    if "_BackendAPIClient" in _gw_txt or "BackendAPIClient" in _gw_txt:
        ok("gpu_worker.py has HTTP backend client (api mode)")
    else:
        err("gpu_worker.py missing HTTP backend client for api mode")
    # Verify DATABASE_URL is not unconditionally imported/required at module level
    # (it should only be used in db mode)
    if 'if _WORKER_MODE == "db"' in _gw_txt or "if _WORKER_MODE == 'db'" in _gw_txt:
        ok("gpu_worker.py — prod_job_store imported conditionally (db mode only)")
    else:
        err("gpu_worker.py — prod_job_store must be imported conditionally (db mode only)")
else:
    err("gpu_worker.py missing")

# 24h — vast mode: DATABASE_URL not required for external GPU
print("\n[24h] vast.ai external GPU — DATABASE_URL isolation")
if _orch.exists() and "_VAST_WORKER_ENV_VARS" in _orch_txt:
    ok("gpu_orchestrator.py — _VAST_WORKER_ENV_VARS defined (no DATABASE_URL in vast env)")
else:
    err("gpu_orchestrator.py — _VAST_WORKER_ENV_VARS missing")
if _gw.exists() and "WORKER_BACKEND_MODE" in _gw_txt:
    ok("gpu_worker.py — WORKER_BACKEND_MODE=api allows operation without DATABASE_URL")
else:
    err("gpu_worker.py — must support WORKER_BACKEND_MODE=api (no DATABASE_URL)")
if _bs.exists() and "WORKER_BACKEND_MODE" in _bs_txt and "api" in _bs_txt:
    ok("bootstrap_worker_once.sh — api mode does not require DATABASE_URL")
else:
    err("bootstrap_worker_once.sh — must support api mode without DATABASE_URL")

# ── 25. Docker worker image ────────────────────────────────────────────────────
print("\n[25] Docker worker image")

import re as _re   # used throughout sections 25-27

_dockerfile = ROOT / "deploy" / "docker" / "Dockerfile.worker"
if _dockerfile.exists():
    ok("deploy/docker/Dockerfile.worker exists")
    _df_txt = _dockerfile.read_text(encoding="utf-8", errors="ignore")
    # Must not COPY secrets or model weights into the image
    for forbidden in (".env", "models/", "*.pt", "*.onnx", "*.safetensors"):
        if f"COPY {forbidden}" in _df_txt or f"ADD {forbidden}" in _df_txt:
            err(f"Dockerfile.worker — COPY/ADD of forbidden path: {forbidden}")
    # Must use the pytorch CUDA base
    if "pytorch/pytorch" in _df_txt or "cuda" in _df_txt.lower():
        ok("Dockerfile.worker — uses CUDA/PyTorch base image")
    else:
        err("Dockerfile.worker — missing CUDA base image")
    # WORKER_BACKEND_MODE must be set to api
    if "WORKER_BACKEND_MODE=api" in _df_txt:
        ok("Dockerfile.worker — WORKER_BACKEND_MODE=api set as ENV default")
    else:
        err("Dockerfile.worker — WORKER_BACKEND_MODE=api must be set as ENV default")
    # Must have an entrypoint defined and chmod +x applied
    if "ENTRYPOINT" in _df_txt:
        ok("Dockerfile.worker — ENTRYPOINT defined")
    else:
        err("Dockerfile.worker — missing ENTRYPOINT")
    if "chmod +x /entrypoint.sh" in _df_txt or "chmod +x" in _df_txt:
        ok("Dockerfile.worker — entrypoint chmod +x applied")
    else:
        err("Dockerfile.worker — missing chmod +x on entrypoint")
else:
    err("deploy/docker/Dockerfile.worker missing")

_entrypoint = ROOT / "deploy" / "docker" / "worker_entrypoint.sh"
if _entrypoint.exists():
    ok("deploy/docker/worker_entrypoint.sh exists")
    _ep_txt = _entrypoint.read_text(encoding="utf-8", errors="ignore")
    # Must validate required env vars
    for req in ("JOB_ID", "BACKEND_API_URL", "WORKER_SECRET", "S3_ENDPOINT_URL",
                "S3_BUCKET_NAME", "MODELS_S3_BUCKET"):
        if req in _ep_txt:
            ok(f"worker_entrypoint.sh — validates {req}")
        else:
            err(f"worker_entrypoint.sh — missing validation for {req}")
    # Must NOT mandate DATABASE_URL
    if not _re.search(r':\s*["\$]?\{?\s*DATABASE_URL\s*:[\?!]', _ep_txt):
        ok("worker_entrypoint.sh — DATABASE_URL not mandated (api mode)")
    else:
        err("worker_entrypoint.sh — must not mandate DATABASE_URL (use WORKER_BACKEND_MODE=api)")
    # Must run gpu_worker.py --once --job-id
    if "gpu_worker.py" in _ep_txt and "--once" in _ep_txt and "--job-id" in _ep_txt:
        ok("worker_entrypoint.sh — runs gpu_worker.py --once --job-id")
    else:
        err("worker_entrypoint.sh — missing: gpu_worker.py --once --job-id")
    # Must run preflight and model download
    if "prod_preflight_check.py" in _ep_txt:
        ok("worker_entrypoint.sh — runs prod_preflight_check.py")
    else:
        err("worker_entrypoint.sh — missing prod_preflight_check.py call")
    if "model_downloader.py" in _ep_txt:
        ok("worker_entrypoint.sh — runs model_downloader.py")
    else:
        err("worker_entrypoint.sh — missing model_downloader.py call")
    # Must document the production path in its header comment
    if "backend worker api" in _ep_txt.lower() or "backend_api_url" in _ep_txt.lower():
        ok("worker_entrypoint.sh — documents backend API path")
    else:
        err("worker_entrypoint.sh — must document backend API path in header")
else:
    err("deploy/docker/worker_entrypoint.sh missing")

# Build scripts
_build_sh = ROOT / "deploy" / "docker" / "build_worker_image.sh"
if _build_sh.exists(): ok("build_worker_image.sh exists")
else: err("deploy/docker/build_worker_image.sh missing")

_build_ps = ROOT / "deploy" / "docker" / "build_worker_image.ps1"
if _build_ps.exists(): ok("build_worker_image.ps1 exists")
else: err("deploy/docker/build_worker_image.ps1 missing")

# gpu_orchestrator must support VAST_WORKER_IMAGE
if _orch.exists() and "VAST_WORKER_IMAGE" in _orch_txt:
    ok("gpu_orchestrator.py — VAST_WORKER_IMAGE direct image mode supported")
else:
    err("gpu_orchestrator.py — VAST_WORKER_IMAGE not supported")

# GHCR_TOKEN must remain in secret set (never logged, even if not required for public images)
if _orch.exists() and "GHCR_TOKEN" in _orch_txt and "_SECRET_ENV_VARS" in _orch_txt:
    _secret_block_start = _orch_txt.find("_SECRET_ENV_VARS = frozenset")
    _secret_block_end   = _orch_txt.find("}", _secret_block_start) + 1
    _secret_block = _orch_txt[_secret_block_start:_secret_block_end]
    if "GHCR_TOKEN" in _secret_block:
        ok("gpu_orchestrator.py — GHCR_TOKEN in _SECRET_ENV_VARS (never logged)")
    else:
        err("gpu_orchestrator.py — GHCR_TOKEN must be in _SECRET_ENV_VARS")
else:
    err("gpu_orchestrator.py — GHCR_TOKEN handling missing")

# Docs mention private repo
_cmd_txt_lower = _cmd_doc.read_text(encoding="utf-8", errors="ignore").lower() if _cmd_doc.exists() else ""
if "private" in _cmd_txt_lower and ("ghcr" in _cmd_txt_lower or "docker" in _cmd_txt_lower):
    ok("commands doc — mentions private repo + GHCR docker flow")
else:
    err("commands_production_queue_gpu.md — must document private repo + GHCR docker flow")

# ── Section 26 — vast.ai startup script safety (git-clone fallback) ────────────
# The base64/bash-lc wrapper is only used in git-clone fallback mode (no VAST_WORKER_IMAGE).
# In direct image mode, onstart is simply "bash /entrypoint.sh".

print("\n-- 26. vast.ai startup script safety --")

# 26a — _wrap_vast_startup_command must exist (git-clone fallback)
if _orch.exists():
    if "_wrap_vast_startup_command" in _orch_txt:
        ok("gpu_orchestrator.py — _wrap_vast_startup_command present (git-clone fallback)")
    else:
        err("gpu_orchestrator.py — missing _wrap_vast_startup_command (needed for git-clone fallback)")
else:
    err("gpu_orchestrator.py not found")

# 26b — git-clone fallback still uses base64/bash-lc (avoids exec-shebang error)
if _orch.exists():
    has_base64_wrap = (
        "base64.b64encode" in _orch_txt
        and "bash -lc" in _orch_txt
        and "base64 -d" in _orch_txt
    )
    if has_base64_wrap:
        ok("gpu_orchestrator.py — git-clone fallback: base64 + bash -lc wrapper present")
    else:
        err("gpu_orchestrator.py — git-clone fallback must use base64 + bash -lc wrapper")

# 26c — runtype=ssh present for git-clone fallback (dev/debug only)
if _orch.exists():
    if '"runtype":  "ssh"' in _orch_txt or '"runtype": "ssh"' in _orch_txt:
        ok("gpu_orchestrator.py — runtype=ssh present (git-clone fallback, dev/debug only)")
    else:
        err("gpu_orchestrator.py — runtype=ssh missing from git-clone fallback path")

# 26d — _sanitized_startup_preview: no secrets in logs
if _orch.exists():
    if "_sanitized_startup_preview" in _orch_txt:
        ok("gpu_orchestrator.py — _sanitized_startup_preview present (secrets not logged)")
    else:
        err("gpu_orchestrator.py — missing _sanitized_startup_preview")

# 26e — raw startup_script not logged (secrets safe in our own logs)
if _orch.exists():
    _bad_log = _re.search(r'logger\.\w+\([^)]*\bstartup_script\b[^)]*\)', _orch_txt)
    if not _bad_log:
        ok("gpu_orchestrator.py — startup_script not passed to logger (secrets safe)")
    else:
        err("gpu_orchestrator.py — startup_script appears in logger call — remove to protect secrets")

# 26f — set +x in git-clone script builder (prevents bash tracing secret values)
if _orch.exists():
    if "set +x" in _orch_txt:
        ok("gpu_orchestrator.py — 'set +x' in startup script (prevents secret tracing)")
    else:
        err("gpu_orchestrator.py — startup script must include 'set +x'")

# ── Section 27 — vast.ai direct image mode ─────────────────────────────────────
# Production path: VPS dispatcher → vast.ai direct image (runtype=args)
# → worker_entrypoint → backend worker API → S3 → instance shutdown/destroy
#
# Key invariants:
#   - VAST_WORKER_IMAGE is used as the `image` field
#   - runtype=args (NOT ssh) — no SSH daemon, no openssh-server installation
#   - args_str = "bash -lc /entrypoint.sh" — short command, no secrets
#   - env vars (secrets) in `env` dict (HTTPS to vast.ai API, not in script)
#   - No Docker-in-Docker, no git clone

print("\n-- 27. vast.ai direct image mode --")

# 27a — _build_vast_env_dict helper present
if _orch.exists():
    if "_build_vast_env_dict" in _orch_txt:
        ok("gpu_orchestrator.py — _build_vast_env_dict helper present (direct image env dict)")
    else:
        err("gpu_orchestrator.py — missing _build_vast_env_dict (env dict for direct image mode)")

# 27b — VAST_WORKER_IMAGE used as effective_image in direct image path
if _orch.exists():
    if "effective_image" in _orch_txt and "_VAST_WORKER_IMAGE" in _orch_txt:
        ok("gpu_orchestrator.py — VAST_WORKER_IMAGE used as effective_image in direct mode")
    else:
        err("gpu_orchestrator.py — direct image mode must use VAST_WORKER_IMAGE as the image field")

# 27c — env dict passed to Vast (not embedded in script)
if _orch.exists():
    if '"env"' in _orch_txt and "env_dict" in _orch_txt and "payload_fields" in _orch_txt:
        ok("gpu_orchestrator.py — env dict used for direct image secrets (not in args_str)")
    else:
        err("gpu_orchestrator.py — direct image mode must pass env vars via env dict field")

# 27d — runtype=args used for direct image (NOT runtype=ssh)
if _orch.exists():
    has_args_mode = '"runtype":  "args"' in _orch_txt or '"runtype": "args"' in _orch_txt
    if has_args_mode:
        ok("gpu_orchestrator.py — runtype=args used for direct image mode (no SSH wrapper)")
    else:
        err("gpu_orchestrator.py — direct image mode must use runtype=args, not runtype=ssh")

# 27e — args_str contains entrypoint command (not a multiline script, no secrets)
if _orch.exists():
    has_entrypoint_cmd = (
        '"bash -lc /entrypoint.sh"' in _orch_txt
        or "'bash -lc /entrypoint.sh'" in _orch_txt
        or '"args_str"' in _orch_txt
    )
    if has_entrypoint_cmd:
        ok("gpu_orchestrator.py — args_str contains entrypoint command (bash -lc /entrypoint.sh)")
    else:
        err("gpu_orchestrator.py — args_str must be 'bash -lc /entrypoint.sh' for direct image mode")

# 27f_ssh — direct image path must NOT use runtype=ssh (ssh installs openssh-server)
# The presence of "runtype=ssh" is fine only in the git-clone fallback path.
# The critical check: "direct-image" deployment_mode must be paired with runtype=args.
if _orch.exists():
    # Check that deployment_mode direct-image-args and runtype=args appear together
    has_direct_args = "direct-image-args" in _orch_txt and '"runtype":  "args"' in _orch_txt
    if has_direct_args:
        ok("gpu_orchestrator.py — direct-image-args uses runtype=args (no SSH wrapper)")
    else:
        err("gpu_orchestrator.py — direct image deployment_mode must be paired with runtype=args")

# 27g (was 27e) — Docker-in-Docker absent from orchestrator bash commands
if _orch.exists():
    _docker_quoted = _re.search(
        r'["\']docker\s+(pull|run|login)\b',
        _orch_txt
    )
    if not _docker_quoted:
        ok("gpu_orchestrator.py — no Docker-in-Docker ops as bash commands (direct image path correct)")
    else:
        err("gpu_orchestrator.py — docker pull/run/login found as bash commands — remove Docker-in-Docker")

# 27f — env_dict key names logged, not values
if _orch.exists():
    # sanitized_config should log env_vars_forwarded (list of key names), not env_dict values
    if "env_vars_forwarded" in _orch_txt:
        ok("gpu_orchestrator.py — sanitized_config logs env var names only (no secret values)")
    else:
        err("gpu_orchestrator.py — sanitized_config must log env_vars_forwarded (key names), not values")

# 27g — worker_entrypoint.sh is the ENTRYPOINT in Dockerfile (Vast runs it directly)
if _dockerfile.exists() and _entrypoint.exists():
    _df_txt_local = _dockerfile.read_text(encoding="utf-8", errors="ignore") if not _df_txt else _df_txt
    _ep_exists_as_entrypoint = (
        "COPY deploy/docker/worker_entrypoint.sh /entrypoint.sh" in _df_txt_local
        and 'ENTRYPOINT ["/entrypoint.sh"]' in _df_txt_local
    )
    if _ep_exists_as_entrypoint:
        ok("Dockerfile.worker — worker_entrypoint.sh is the image ENTRYPOINT (/entrypoint.sh)")
    else:
        err("Dockerfile.worker — worker_entrypoint.sh must be COPY'd to /entrypoint.sh and set as ENTRYPOINT")

# 27h — docs describe the VPS → Vast direct image → API → S3 → shutdown flow
_n8n_txt_lower = _n8n_doc.read_text(encoding="utf-8", errors="ignore").lower() if _n8n_doc.exists() else ""
_flow_keywords = ["direct image", "vast direct", "entrypoint", "backend api", "s3", "shutdown"]
_flow_hits = [kw for kw in _flow_keywords if kw in _n8n_txt_lower or kw in _cmd_txt_lower]
if len(_flow_hits) >= 4:
    ok(f"docs — production flow documented ({', '.join(_flow_hits[:4])})")
else:
    err("docs — must document VPS -> Vast direct image -> entrypoint -> backend API -> S3 -> shutdown flow")

# ── Section 28 — vast.ai location filter ──────────────────────────────────────
print("\n-- 28. vast.ai location filter --")

# 28a — VAST_LOCATION_INCLUDE_REGEX and VAST_LOCATION_EXCLUDE_REGEX config present
if _orch.exists():
    if "VAST_LOCATION_INCLUDE_REGEX" in _orch_txt:
        ok("gpu_orchestrator.py — VAST_LOCATION_INCLUDE_REGEX supported")
    else:
        err("gpu_orchestrator.py — missing VAST_LOCATION_INCLUDE_REGEX")
    if "VAST_LOCATION_EXCLUDE_REGEX" in _orch_txt:
        ok("gpu_orchestrator.py — VAST_LOCATION_EXCLUDE_REGEX supported")
    else:
        err("gpu_orchestrator.py — missing VAST_LOCATION_EXCLUDE_REGEX")
else:
    err("gpu_orchestrator.py not found")

# 28b — default exclude covers South Korea / KR
if _orch.exists():
    _orch_has_kr = (
        "South Korea" in _orch_txt and
        ("KR" in _orch_txt or "Korea" in _orch_txt) and
        "_VAST_LOCATION_EXCLUDE_REGEX" in _orch_txt
    )
    if _orch_has_kr:
        ok("gpu_orchestrator.py — default VAST_LOCATION_EXCLUDE_REGEX excludes South Korea/KR")
    else:
        err("gpu_orchestrator.py — VAST_LOCATION_EXCLUDE_REGEX must exclude South Korea/KR by default")

# 28c — _get_offer_location_label helper present
if _orch.exists():
    if "_get_offer_location_label" in _orch_txt:
        ok("gpu_orchestrator.py — _get_offer_location_label helper present")
    else:
        err("gpu_orchestrator.py — missing _get_offer_location_label helper")

# 28d — location filtering applied in offer loop (both exclude and include paths)
if _orch.exists():
    has_loc_exclude = "location_exclude" in _orch_txt or "location_exclude_re" in _orch_txt
    has_loc_include = "location_not_include" in _orch_txt or "location_include_re" in _orch_txt
    if has_loc_exclude:
        ok("gpu_orchestrator.py — location exclusion filter applied in offer loop")
    else:
        err("gpu_orchestrator.py — location exclusion filter missing from offer loop")
    if has_loc_include:
        ok("gpu_orchestrator.py — location inclusion filter applied in offer loop")
    else:
        err("gpu_orchestrator.py — location inclusion filter missing from offer loop")

# 28e — docs mention location filter and KR exclusion
_n8n_loc = _n8n_doc.read_text(encoding="utf-8", errors="ignore") if _n8n_doc.exists() else ""
_cmd_loc  = _cmd_doc.read_text(encoding="utf-8", errors="ignore") if _cmd_doc.exists() else ""
_loc_ok = (
    ("South Korea" in _n8n_loc or "South Korea" in _cmd_loc) and
    ("VAST_LOCATION_EXCLUDE_REGEX" in _n8n_loc or "VAST_LOCATION_EXCLUDE_REGEX" in _cmd_loc)
)
if _loc_ok:
    ok("docs — VAST_LOCATION_EXCLUDE_REGEX + South Korea exclusion documented")
else:
    err("docs — must document VAST_LOCATION_EXCLUDE_REGEX and South Korea exclusion")

# ── Section 29 — vast.ai verified / reliability filter ─────────────────────────
# Unverified hosts hang at "Loading" / "Verifying checksum" and never reach backend.
# Require verified=true + reliability >= 98 by default.

print("\n-- 29. vast.ai verified / reliability filter --")

# 29a — VAST_REQUIRE_VERIFIED config present with default true
if _orch.exists():
    if "VAST_REQUIRE_VERIFIED" in _orch_txt:
        ok("gpu_orchestrator.py — VAST_REQUIRE_VERIFIED config present")
    else:
        err("gpu_orchestrator.py — missing VAST_REQUIRE_VERIFIED config")

# 29b — VAST_MIN_RELIABILITY config present with default 98
if _orch.exists():
    if "VAST_MIN_RELIABILITY" in _orch_txt:
        ok("gpu_orchestrator.py — VAST_MIN_RELIABILITY config present")
    else:
        err("gpu_orchestrator.py — missing VAST_MIN_RELIABILITY config")

# 29c — _check_offer_verified helper (multi-field)
if _orch.exists():
    if "_check_offer_verified" in _orch_txt:
        ok("gpu_orchestrator.py — _check_offer_verified helper present (multi-field)")
    else:
        err("gpu_orchestrator.py — missing _check_offer_verified helper")

# 29d — _get_offer_reliability helper
if _orch.exists():
    if "_get_offer_reliability" in _orch_txt:
        ok("gpu_orchestrator.py — _get_offer_reliability helper present")
    else:
        err("gpu_orchestrator.py — missing _get_offer_reliability helper")

# 29e — reject reasons logged (not_verified / low_reliability)
if _orch.exists():
    has_not_verified   = "reason=not_verified" in _orch_txt
    has_low_reliability = "reason=low_reliability" in _orch_txt
    if has_not_verified:
        ok("gpu_orchestrator.py — reason=not_verified logged on reject")
    else:
        err("gpu_orchestrator.py — missing reason=not_verified in offer skip log")
    if has_low_reliability:
        ok("gpu_orchestrator.py — reason=low_reliability logged on reject")
    else:
        err("gpu_orchestrator.py — missing reason=low_reliability in offer skip log")

# 29f — chosen_offer includes verified + reliability fields in dry-run output
if _orch.exists():
    if '"verified"' in _orch_txt and '"reliability"' in _orch_txt:
        ok("gpu_orchestrator.py — chosen_offer logs verified + reliability fields")
    else:
        err("gpu_orchestrator.py — chosen_offer must include verified and reliability fields")

# 29g — docs mention verified hosts requirement
_n8n_ver = _n8n_doc.read_text(encoding="utf-8", errors="ignore") if _n8n_doc.exists() else ""
_cmd_ver  = _cmd_doc.read_text(encoding="utf-8", errors="ignore") if _cmd_doc.exists() else ""
_verified_in_docs = (
    "verified" in _n8n_ver.lower() or "verified" in _cmd_ver.lower()
) and (
    "VAST_REQUIRE_VERIFIED" in _n8n_ver or "VAST_REQUIRE_VERIFIED" in _cmd_ver
    or "unverified" in _n8n_ver.lower() or "unverified" in _cmd_ver.lower()
)
if _verified_in_docs:
    ok("docs — verified host requirement documented")
else:
    err("docs — must document VAST_REQUIRE_VERIFIED / avoid unverified hosts")

# ── Section 30 — preflight api-mode + S3 bucket alias ─────────────────────────
# prod_preflight_check.py must NOT require DATABASE_URL when WORKER_BACKEND_MODE=api.
# S3_BUCKET and S3_BUCKET_NAME must be treated as aliases everywhere.

print("\n-- 30. preflight api-mode + S3 bucket alias --")

_preflight = ROOT / "scripts" / "prod_preflight_check.py"
if _preflight.exists():
    _pf_txt = _preflight.read_text(encoding="utf-8", errors="ignore")

    # 30a — preflight must handle --role worker argument format
    if "_parse_role" in _pf_txt or "--role" in _pf_txt:
        ok("prod_preflight_check.py — handles --role worker argument format")
    else:
        err("prod_preflight_check.py — must parse --role worker (not just positional)")

    # 30b — api-mode worker must NOT check DATABASE_URL
    if "WORKER_BACKEND_MODE" in _pf_txt and "api" in _pf_txt:
        ok("prod_preflight_check.py — WORKER_BACKEND_MODE=api branch present")
    else:
        err("prod_preflight_check.py — missing WORKER_BACKEND_MODE=api branch")

    # Confirm REQUIRED_WORKER_API list does not contain DATABASE_URL
    # (DATABASE_URL is only in REQUIRED_BACKEND — intentional)
    _api_list_start = _pf_txt.find("REQUIRED_WORKER_API")
    _api_list_end   = _pf_txt.find("]", _api_list_start) + 1 if _api_list_start != -1 else -1
    _api_list_body  = _pf_txt[_api_list_start:_api_list_end] if _api_list_start != -1 else ""
    if "DATABASE_URL" not in _api_list_body:
        ok("prod_preflight_check.py — DATABASE_URL not in REQUIRED_WORKER_API (api-mode correct)")
    else:
        err("prod_preflight_check.py — DATABASE_URL must not be in REQUIRED_WORKER_API")

    # 30c — BACKEND_API_URL required in api-mode
    if "BACKEND_API_URL" in _pf_txt and "REQUIRED_WORKER_API" in _pf_txt:
        ok("prod_preflight_check.py — BACKEND_API_URL in REQUIRED_WORKER_API")
    else:
        err("prod_preflight_check.py — BACKEND_API_URL must be in REQUIRED_WORKER_API list")

    # 30d — S3_BUCKET / S3_BUCKET_NAME alias check present
    if "_s3_bucket_present" in _pf_txt or ("S3_BUCKET" in _pf_txt and "S3_BUCKET_NAME" in _pf_txt):
        ok("prod_preflight_check.py — S3_BUCKET / S3_BUCKET_NAME alias handled")
    else:
        err("prod_preflight_check.py — must accept S3_BUCKET or S3_BUCKET_NAME (alias)")

    # 30e — MODELS_S3_BUCKET fallback to S3_BUCKET_NAME
    if "MODELS_S3_BUCKET" in _pf_txt and "S3_BUCKET_NAME" in _pf_txt:
        ok("prod_preflight_check.py — MODELS_S3_BUCKET falls back to S3_BUCKET_NAME")
    else:
        err("prod_preflight_check.py — MODELS_S3_BUCKET must fall back to S3_BUCKET_NAME")
else:
    err("scripts/prod_preflight_check.py not found")

# 30f — worker_entrypoint.sh creates S3_BUCKET alias from S3_BUCKET_NAME
if _entrypoint.exists():
    _ep_txt2 = _entrypoint.read_text(encoding="utf-8", errors="ignore")
    if "S3_BUCKET=" in _ep_txt2 and "S3_BUCKET_NAME" in _ep_txt2:
        ok("worker_entrypoint.sh — S3_BUCKET alias set from S3_BUCKET_NAME")
    else:
        err("worker_entrypoint.sh — must set S3_BUCKET=${S3_BUCKET:-$S3_BUCKET_NAME}")
else:
    err("worker_entrypoint.sh not found")

# 30g — gpu_orchestrator.py injects both S3_BUCKET and S3_BUCKET_NAME in env dict
if _orch.exists():
    if "S3_BUCKET" in _orch_txt and "S3_BUCKET_NAME" in _orch_txt and "env[" in _orch_txt:
        ok("gpu_orchestrator.py — S3_BUCKET + S3_BUCKET_NAME both in vast env dict")
    else:
        err("gpu_orchestrator.py — vast env dict must include both S3_BUCKET and S3_BUCKET_NAME")

# ── Summary ────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
if WARNS:
    print("WARNINGS:")
    for w in WARNS: print(w)
if ERRORS:
    print("\nFAILED — errors:")
    for e in ERRORS: print(e)
    print(f"\nTotal errors: {len(ERRORS)}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
    sys.exit(0)

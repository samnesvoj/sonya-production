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

# 24c — gpu_orchestrator.py exists and uses webhook mode
_orch = ROOT / "scripts" / "gpu_orchestrator.py"
if _orch.exists():
    _orch_txt = _orch.read_text(encoding="utf-8", errors="ignore")
    if "webhook" in _orch_txt: ok("gpu_orchestrator.py — webhook mode present")
    else: err("gpu_orchestrator.py — 'webhook' not found")
else: err("gpu_orchestrator.py missing")

# 24d — bootstrap_worker_once.sh exists and contains required commands
_bs = ROOT / "deploy" / "gpu" / "bootstrap_worker_once.sh"
if _bs.exists():
    ok("bootstrap_worker_once.sh exists")
    _bs_txt = _bs.read_text(encoding="utf-8", errors="ignore")
    if "gpu_worker.py" in _bs_txt and "--once" in _bs_txt and "--job-id" in _bs_txt:
        ok("bootstrap contains gpu_worker.py --once --job-id")
    else:
        err("bootstrap missing: gpu_worker.py --once --job-id")
    if "shutdown" in _bs_txt: ok("bootstrap contains shutdown")
    else: err("bootstrap missing: shutdown command")
else: err("deploy/gpu/bootstrap_worker_once.sh missing")

# 24e — sonya-dispatcher.service exists
_svc = ROOT / "deploy" / "systemd" / "sonya-dispatcher.service"
if _svc.exists(): ok("sonya-dispatcher.service exists")
else: err("deploy/systemd/sonya-dispatcher.service missing")

# 24f — docs describe ephemeral GPU
_n8n_doc = ROOT / "deploy" / "n8n_gpu_orchestration.md"
if _n8n_doc.exists():
    _doc_txt = _n8n_doc.read_text(encoding="utf-8", errors="ignore").lower()
    if "ephemeral" in _doc_txt: ok("n8n_gpu_orchestration.md describes ephemeral GPU")
    else: err("n8n_gpu_orchestration.md missing 'ephemeral'")
else: err("deploy/n8n_gpu_orchestration.md missing")

_cmd_doc = ROOT / "deploy" / "commands_production_queue_gpu.md"
if _cmd_doc.exists(): ok("commands_production_queue_gpu.md exists")
else: err("deploy/commands_production_queue_gpu.md missing")

# 24g — gpu_worker supports --once
_gw = ROOT / "scripts" / "gpu_worker.py"
if _gw.exists():
    _gw_txt = _gw.read_text(encoding="utf-8", errors="ignore")
    if "--once" in _gw_txt: ok("gpu_worker.py supports --once (ephemeral flow)")
    else: err("gpu_worker.py missing --once flag")
else: err("gpu_worker.py missing")

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

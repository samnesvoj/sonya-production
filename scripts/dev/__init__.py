"""
scripts/dev — local-only development/test harness tools.

Nothing in this package is imported by production code (scripts/prod_*.py,
scripts/gpu_worker.py, streamer-mode.*), and nothing here runs unless a
developer explicitly invokes one of these scripts. See scripts/dev/
local_e2e_server.py's module docstring for the NO-GPU local E2E harness
this package exists for.
"""

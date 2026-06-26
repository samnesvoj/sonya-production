# VPS Deployment Commands

## Requirements

- Ubuntu 22.04+
- Python 3.11+
- PostgreSQL 15+
- S3-compatible storage

## Initial deploy

```bash
# Clone repo
git clone <REPO_URL> /srv/sonya
cd /srv/sonya

# Install backend deps
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-backend.txt

# Set env vars
cp .env.example .env
nano .env

# Run migrations
python scripts/run_migrations.py

# Pre-flight check
python scripts/prod_preflight_check.py backend

# Start API (via systemd or screen)
uvicorn scripts.prod_generation_api:app --host 0.0.0.0 --port 8000
```

## Systemd unit example

```ini
[Unit]
Description=SONYA Production API
After=network.target

[Service]
WorkingDirectory=/srv/sonya
EnvironmentFile=/srv/sonya/.env
ExecStart=/srv/sonya/.venv/bin/uvicorn scripts.prod_generation_api:app --host 0.0.0.0 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Update

```bash
cd /srv/sonya
git pull
pip install -r requirements-backend.txt
python scripts/run_migrations.py
systemctl restart sonya-api
```

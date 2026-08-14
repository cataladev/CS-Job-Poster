# Single VPS deployment

This guide deploys the repo as one long-running Python bot on an Ubuntu VPS:

- the Discord bot runs under `systemd`
- Postgres runs in Docker and binds to `127.0.0.1:5432`
- the app code lives in a real git checkout so the server can fast-forward from `origin/main`
- deploys can happen either from a local `ssh` session, a polling timer on the VPS, or GitHub Actions

## What I need from you to SSH in and finish the deploy

To do the first deploy directly from your machine, I need:

- the exact SSH command that already works from this machine, for example `ssh -p 2222 ubuntu@203.0.113.10`
- a user with `sudo`
- the repo path you want on the server, or permission to use `/opt/job-bot/app`
- confirmation that the repo is public or, if private, a deploy key or other non-interactive git credential is already configured on the VPS
- the production env values:
  - `DISCORD_TOKEN`
  - `DISCORD_CHANNEL_ID`
  - `DISCORD_NEW_GRAD_CHANNEL_ID`
  - `DATABASE_URL`
  - `POSTGRES_PASSWORD`
- whether admin SSH stays on `22` or already moved to another port

The important operational constraint is that the SSH auth must already work non-interactively from this Mac. If your key needs a passphrase prompt, load it into `ssh-agent` first.

## Topology

```text
internet
  `- ssh your-user@your-server -> Ubuntu VPS
                                 |- systemd: job-bot.service
                                 |- docker: postgres:16 on 127.0.0.1:5432
                                 `- git checkout: /opt/job-bot/app
```

This bot does not need nginx, TLS, or any inbound web port. Only SSH has to be reachable.

## 1. Clone the repo onto the server

Suggested path:

```text
/opt/job-bot/app
```

First-time setup:

```bash
sudo mkdir -p /opt/job-bot
sudo chown "$USER":"$USER" /opt/job-bot
git clone https://github.com/cataladev/CS-Job-Poster.git /opt/job-bot/app
cd /opt/job-bot/app
```

If the repo is private, create a deploy key on the VPS and add the public key to GitHub as a read-only deploy key:

```bash
ssh-keygen -t ed25519 -C "job-bot deploy" -f ~/.ssh/id_ed25519
cat ~/.ssh/id_ed25519.pub
```

After GitHub trusts that key, verify the VPS can fetch the repo without prompts:

```bash
git -C /opt/job-bot/app fetch origin main
```

## 2. Run the bootstrap script

From the repo root on the server:

```bash
sudo APP_USER=jobbot \
  DEPLOY_USER="$USER" \
  APP_DIR=/opt/job-bot/app \
  BRANCH=main \
  bash deploy/bootstrap-single-vps.sh
```

What this installs:

- system packages: `git`, `python3`, `python3-venv`, Docker, `ufw`
- app user: `jobbot`
- env file: `/etc/job-bot/job-bot.env`
- systemd units:
  - `job-bot.service`
  - `job-bot-auto-update.service`
  - `job-bot-auto-update.timer`
- Python virtualenv with repo requirements installed
- passwordless `sudo` for the deploy user to restart the bot or trigger the auto-update unit from CI

## 3. Fill in the production env file

Use the generated example as the base:

```bash
sudoedit /etc/job-bot/job-bot.env
```

The required values are:

- `DISCORD_TOKEN`
- `DISCORD_CHANNEL_ID`
- `DISCORD_NEW_GRAD_CHANNEL_ID`
- `DATABASE_URL`
- `POSTGRES_PASSWORD`

Recommended baseline:

```dotenv
POSTGRES_BIND_HOST=127.0.0.1
POSTGRES_HOST_PORT=5432
POSTGRES_DB=jobbot
POSTGRES_USER=jobbot
POSTGRES_PASSWORD=<long-random-password>
DATABASE_URL=postgresql://jobbot:<long-random-password>@localhost:5432/jobbot
DISCORD_CHANNEL_ID=<internship-channel-id>
DISCORD_NEW_GRAD_CHANNEL_ID=<new-grad-channel-id>
POLL_INTERVAL_SECONDS=300
DIGEST_INTERVAL_SECONDS=14400
# Optional: mentioned only once per track digest; requires Mention Everyone.
DISCORD_INTERNSHIP_ALERT_ROLE_ID=
DISCORD_NEW_GRAD_ALERT_ROLE_ID=
RECENT_CATCHUP_HOURS=48
REQUEST_TIMEOUT_SECONDS=20
REQUEST_RETRIES=2
SOURCE_CONCURRENCY=6
PUBLISH_DELAY_SECONDS=1.0
```

Do not open port `5432` in the firewall. The compose file now binds Postgres to loopback only.

The Discord bot role must have **View Channel**, **Send Messages**, **Embed Links**,
and **Read Message History** in both configured channels. History access is mandatory:
the service refuses to publish to a channel without it because delivery reconciliation
would not be safe. Administrators can later change either destination with
`/set-job-channel`; the database override survives restarts and deployments. For
optional role pings in the periodic digest, grant **Mention Everyone** and configure
the role with `/set-job-alert-role`.

## 4. Verify the service

Once the env file is filled in:

```bash
sudo systemctl restart job-bot.service
sudo systemctl status job-bot.service
sudo journalctl -u job-bot.service -n 100 --no-pager
```

If you have existing SQLite production data you want to preserve, import it once before the final restart:

```bash
cd /opt/job-bot/app
sudo -u jobbot bash -lc 'cd /opt/job-bot/app && .venv/bin/python scripts/import_sqlite_to_postgres.py'
sudo systemctl restart job-bot.service
```

## 5. Enable automatic pull-based deploys on the VPS

Once started, the timer installed by bootstrap polls `origin/main` every minute and only deploys when the remote commit changes.

Useful commands:

```bash
sudo systemctl start job-bot-auto-update.timer
sudo systemctl status job-bot-auto-update.timer
sudo systemctl start job-bot-auto-update.service
sudo journalctl -u job-bot-auto-update.service -n 100 --no-pager
```

The update path is:

1. `git fetch origin main`
2. compare `HEAD` to `origin/main`
3. `git merge --ff-only`
4. `pip install -r requirements.txt`
5. restart `job-bot.service`

If the working tree is dirty, the auto-update script refuses to deploy.

## 6. Optional GitHub Actions deploys

The repo now includes [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml).

Required repository secrets:

- `DEPLOY_HOST`: VPS hostname or IP
- `DEPLOY_USER`: SSH user that can run `sudo systemctl start job-bot-auto-update.service`
- `DEPLOY_SSH_KEY`: private key matching that user on the VPS

Optional repository secrets:

- `DEPLOY_PORT`: use this if admin SSH is not on `22`
- `DEPLOY_AUTO_UPDATE_UNIT`: only needed if you renamed the default unit
- `DEPLOY_SERVICE_UNIT`: only needed if you renamed `job-bot.service`

Workflow behavior:

1. set up Python 3.11
2. run `pytest tests -q` with Postgres in CI
3. SSH into the VPS
4. trigger `job-bot-auto-update.service`

This means both deploy paths reuse the same server-side update logic.
GitHub Actions is a test-and-deploy gate only; do not add an Actions-based publisher
alongside the long-running service.

## 7. Firewall and operational checks

Only open the SSH port you actually use. Example if admin SSH is on `2222`:

```bash
sudo ufw allow 2222/tcp
sudo ufw enable
```

Useful checks:

```bash
ssh -p 2222 your-user@your-server
sudo systemctl status job-bot.service
sudo systemctl status job-bot-auto-update.timer
sudo journalctl -u job-bot.service -n 100 --no-pager
sudo journalctl -u job-bot-auto-update.service -n 100 --no-pager
docker ps
docker compose -f /opt/job-bot/app/docker-compose.yml ps
```

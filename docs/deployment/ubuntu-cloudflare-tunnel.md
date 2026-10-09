# Deploying CR+CRM on Ubuntu behind Cloudflare Tunnel

Target host: `192.168.0.13`
Public URL: `https://mailvex.in`
Orchestration: Docker Compose, one file, `docker-compose.yml`

This is the authoritative deployment guide. Everything else in `docs/` is
supporting reference.

---

## 1. Architecture

```
Internet
   |
   | HTTPS 443
   v
Cloudflare edge  -------->  mailvex.in DNS record
   |
   | mTLS tunnel (outbound only; no inbound port is ever opened)
   v
cloudflared  (systemd service ON THE HOST, not a container)
   |
   | HTTP to 127.0.0.1:8080
   v
nginx  (container, port published to loopback ONLY)
   |
   +-- /api/  ---> backend:8000
   +-- /health --> backend:8000
   +-- /      ---> frontend:80
                   |
                   +--> PostgreSQL 192.168.0.12:5432 (separate server)
                    +--> redis:6379   (internal Docker network)
```

### The deployed tunnel

| Item         | Value                                         |
| ------------ | --------------------------------------------- |
| Host         | `dmlit@192.168.0.13` (Ubuntu 22.04)           |
| Tunnel name  | `crcrm`                                       |
| Tunnel ID    | `42407e6b-1e81-4a47-8ed5-4d9337c6b39e`        |
| Credentials  | `~/.cloudflared/<Tunnel ID>.json`, mode `600` |
| Config       | `~/.cloudflared/config.yml`, mode `600`       |
| Unit         | `~/.config/systemd/user/cloudflared.service`  |
| Origin       | `http://127.0.0.1:8080`                       |
| Deploy path  | `/home/dmlit/crcrm`                           |

Inspect it with:

```bash
systemctl --user status cloudflared
cloudflared tunnel info crcrm
journalctl --user -u cloudflared -n 50 --no-pager
```

The tunnel ID is public; the credentials JSON is the secret and is never
committed.

Three properties of this layout are deliberate. Do not "simplify" them away:

1. **The host needs no open inbound ports.** `cloudflared` dials out to
   Cloudflare and maintains the tunnel. Your router and firewall need nothing
   opened, which is the main reason to use a tunnel rather than port forwarding.
2. **Only one port is published, and it is bound to `127.0.0.1`.** Other
   machines on `192.168.0.0/24` cannot reach the app even if they know the IP.
   Redis stays on the internal Docker network. PostgreSQL runs on the separate
   database host and should accept connections only from approved private hosts.
3. **TLS terminates at Cloudflare.** nginx speaks plain HTTP on loopback. The
   `cloudflared`-to-origin hop is local to the machine, so it never crosses a
   network you do not control.

### `cloudflared` runs on the host, deliberately

Earlier drafts of this setup ran `cloudflared` in a container via
`docker-compose.cloudflared.yml`. Those files have been removed. A host-level
systemd service is used instead because:

- The tunnel is infrastructure, not application code. It should start before and
  survive independently of the application stack.
- It keeps the container stack free of any Cloudflare credential.
- `docker compose down` must never sever the public entry point during a deploy.

---

## 2. Prerequisites

On the Ubuntu host:

```bash
hostnamectl                     # confirm you are on 192.168.0.13
docker --version && docker compose version
```

If Docker is absent, install it from the official repository:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) \
  signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"    # log out and back in afterwards
```

You also need a Cloudflare account with `mailvex.in` added as a zone, and
permission to create DNS records for it.

---

## 3. Get the code and configure secrets

```bash
cd /opt
git clone <your-repo-url> crcrm
cd crcrm
```

Create the environment file. It is the single source of truth for secrets and
is gitignored:

```bash
cp .env.production.example .env.production
```

Then edit it. Every value must be replaced; the app refuses to start in
`production` while any placeholder is still present.

| Variable | Notes |
| --- | --- |
| `RUNTIME_ENV_FILE` | Leave as `.env.production`. The file refers to itself so containers can mount the same config. |
| `POSTGRES_HOST` | Private PostgreSQL host, such as `192.168.0.12`. |
| `POSTGRES_DB` | App database, normally `crcrm`. |
| `POSTGRES_USER` | Least-privilege app role, normally `crcrm_app`; do not use the PostgreSQL superuser in the app. |
| `POSTGRES_PASSWORD` | Password for `POSTGRES_USER`; it must match `DATABASE_URL`. |
| `REDIS_PASSWORD` | Redis password. `REDIS_URL` is derived from it in Compose, so never set `REDIS_URL` by hand. A mismatch is a silent outage. |
| `DATABASE_URL` | Connect to `POSTGRES_HOST:5432` and embed the app role and same password as `POSTGRES_PASSWORD`. |
| `JWT_SECRET` | Long random value. Rotating it logs every user out. |
| `ENCRYPTION_KEY` | Rotating it makes existing stored credentials unreadable. |
| `ALLOWED_ORIGINS` | `https://mailvex.in` |
| `ALLOWED_HOSTS` | `mailvex.in,localhost,127.0.0.1,backend,frontend,nginx` |
| `PUBLIC_BASE_URL` | `https://mailvex.in` (used in OAuth redirect URIs and email links). |
| `VALIDATION_SMTP_ENABLED` | Leave `false` unless SMTP egress is confirmed. See section 10. |

Generate the two secrets rather than typing them:

```bash
openssl rand -hex 32   # JWT_SECRET
openssl rand -hex 32   # ENCRYPTION_KEY
```

Validate before deploying anything:

```bash
docker compose --env-file .env.production config --quiet
```

---

### Create the application database once

On the PostgreSQL server, create a dedicated role and database. Use a new
strong password for `crcrm_app`; do not reuse the PostgreSQL administrator
password. Put that app-role password in `POSTGRES_PASSWORD` and `DATABASE_URL`
in the app's `.env.production` file.

```bash
sudo -u postgres psql
```

At the `psql` prompt, create the app role and database once, and create a
separate backup account. Set passwords with `\password` so they are not
written into SQL history:

```sql
CREATE ROLE crcrm_app LOGIN;
\password crcrm_app
CREATE DATABASE crcrm OWNER crcrm_app;
CREATE ROLE crcrm_backup LOGIN CREATEDB;
\password crcrm_backup
GRANT CONNECT ON DATABASE crcrm TO crcrm_backup;
\c crcrm
GRANT USAGE ON SCHEMA public TO crcrm_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE crcrm_app IN SCHEMA public GRANT SELECT ON TABLES TO crcrm_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE crcrm_app IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO crcrm_backup;
```

Use the `crcrm_app` password in `POSTGRES_PASSWORD` and `DATABASE_URL`, and the
`crcrm_backup` password in `BACKUP_PGPASSWORD`. The backup login is separate
from the application login; it needs `CREATEDB` only to restore dumps into
isolated scratch databases for verification.

Configure PostgreSQL to listen on its private address and allow the app host
(`192.168.0.13/32`) in `pg_hba.conf`. If local development also needs access,
add that workstation's private IP as a separate rule. Restrict the firewall
accordingly; never expose port 5432 to the public internet. Restart PostgreSQL
and confirm port 5432 is reachable from the app host before deployment.

## 4. Build and start the application

```bash
docker compose --env-file .env.production build
docker compose --env-file .env.production up -d --remove-orphans
```

On an upgrade from the previous Compose file, `--remove-orphans` removes its
old PostgreSQL container. It keeps the old Docker volume; do not use `down -v`
as part of this change.

Startup is ordered by health dependencies, so `migrate` completes before the
API starts and the API becomes healthy before nginx and the frontend come up:

```bash
docker compose --env-file .env.production ps
```

Expected state: `redis`, `backend`, `worker`, `scheduler`, `frontend`, and
`nginx` healthy; `backup` running; `migrate` exited with status 0. PostgreSQL
is managed separately and does not appear in `docker compose ps`.

Confirm the migration landed and confirm the origin responds:

```bash
docker compose --env-file .env.production run --rm migrate alembic current
curl -sS http://127.0.0.1:8080/health          # {"status":"ok"}
curl -sS http://127.0.0.1:8080/nginx-health    # ok
```

`http://127.0.0.1:8080` is the exact URL the tunnel will target. Verify it from
the host before involving Cloudflare.

---

## 5. Install `cloudflared` on the host

```bash
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg > /dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] \
  https://pkg.cloudflare.com/cloudflared any main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install -y cloudflared
```

Confirm the tunnel credential before using the service:

```bash
cloudflared tunnel login
```

This writes a certificate to `~/.cloudflared/cert.pem`. On a headless server it
prints a URL instead of opening a browser: open that URL on any machine, approve
it, and the still-running `cloudflared` process downloads the certificate
itself. Leave the command running until it reports success.

---

## 6. Create the tunnel

```bash
cloudflared tunnel create crcrm
```

Note the printed `Tunnel ID`. Create the configuration file:

```bash
mkdir -p ~/.cloudflared && chmod 700 ~/.cloudflared
nano ~/.cloudflared/config.yml
```

```yaml
tunnel: <TUNNEL-ID>
credentials-file: /home/dmlit/.cloudflared/<TUNNEL-ID>.json

ingress:
  - hostname: mailvex.in
    service: http://127.0.0.1:8080
  - service: http_status:404
```

```bash
chmod 600 ~/.cloudflared/config.yml
cloudflared tunnel ingress validate
cloudflared tunnel ingress rule https://mailvex.in/anything
```

The credentials file is already `600` and owned by the deploy user, so nothing
needs to be copied into `/etc`.

---

## 7. Route DNS and run the service

Create the DNS record. Using `cloudflared tunnel route dns` avoids the common
mistake of leaving a proxied `A` record in place, which would send traffic to
the private IP and fail:

```bash
cloudflared tunnel route dns crcrm mailvex.in
```

If an `A`/`AAAA` record already exists for the hostname, Cloudflare serves it
instead of the tunnel and the site returns **HTTP 530**. Replace it explicitly:

```bash
cloudflared tunnel route dns --overwrite-dns crcrm mailvex.in
```

A proxied apex CNAME does not appear in a `dig CNAME` query; confirm it with
`dig +short A mailvex.in` showing Cloudflare edge addresses and with
`curl -I https://mailvex.in/` returning `200`.

Run it as a **systemd user service**, so the credentials stay in the deploy
user's home directory and no further `sudo` is needed:

```bash
mkdir -p ~/.config/systemd/user
nano ~/.config/systemd/user/cloudflared.service
```

```ini
[Unit]
Description=Cloudflare Tunnel for CR+CRM (mailvex.in)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/cloudflared --no-autoupdate tunnel run crcrm
Restart=on-failure
RestartSec=10s

[Install]
WantedBy=default.target
```

Linger is what makes the service survive logout and reboot. It needs `sudo`
once, at install time:

```bash
sudo loginctl enable-linger dmlit
```

Then, as the deploy user:

```bash
systemctl --user daemon-reload
systemctl --user enable --now cloudflared
systemctl --user status cloudflared
journalctl --user -u cloudflared -n 50 --no-pager
```

Confirm the connector registered before touching DNS:

```bash
cloudflared tunnel info crcrm    # CONNECTIONS must be non-empty
```

In the Cloudflare dashboard set **SSL/TLS → Edge Certificates → Always Use
HTTPS** to on. Consider enabling HTTP/3 and setting the security level to
Full (strict).

---

## 8. Verify end to end

```bash
curl -I https://mailvex.in/                 # 200, SPA shell
curl -sS https://mailvex.in/health          # {"status":"ok"}
curl -sS -o /dev/null -w '%{http_code}\n' \
  -X POST https://mailvex.in/api/v1/auth/login \
  -H 'Content-Type: application/json' -d '{}'   # 422, not 502
```

Then confirm the client IP survived the tunnel. This is the check most likely
to be silently wrong:

```bash
docker compose --env-file .env.production logs nginx --tail 20 | tail -5
```

The logged address must be your real public IP, **not** `127.0.0.1` and **not**
`172.x.x.x`. If it shows the Docker bridge gateway, the real-IP configuration
is not loaded; see section 11.

Log in through the browser and confirm the app is usable, then confirm imports
enqueue and complete.

---

## 9. Day-to-day operations

```bash
# status
docker compose --env-file .env.production ps

# logs
docker compose --env-file .env.production logs -f backend
docker compose --env-file .env.production logs -f worker

# update
git pull --ff-only
export APP_BUILD_REV="$(git rev-parse --short HEAD)"
docker compose --env-file .env.production build --pull frontend
docker compose --env-file .env.production up -d --no-deps --force-recreate frontend

# verify the frontend container and published app
docker compose --env-file .env.production ps frontend nginx
curl -fsS http://127.0.0.1:8080/health/ready

# rollback
git checkout <previous-tag>
export APP_BUILD_REV="$(git rev-parse --short HEAD)"
docker compose --env-file .env.production build --pull frontend
docker compose --env-file .env.production up -d --no-deps --force-recreate frontend

# restart one service
docker compose --env-file .env.production restart backend
```

### Backups

`backup` runs hourly: a compressed `pg_dump`, the upload store, and a copy of
`.env.production`.

```bash
docker compose --env-file .env.production exec backup ls -lh /backups
docker compose --env-file .env.production exec backup sh /scripts/backup.sh
docker compose --env-file .env.production cp backup:/backups/crcrm-db-<stamp>.sql.gz ./
```

Backups live in the `backups_data` Docker volume rather than a host directory:
the container runs as root, and a bind mount would leave root-owned files the
deploy user cannot delete.

`/backups/config/<stamp>/runtime.env` contains `JWT_SECRET`, `ENCRYPTION_KEY`,
and the database and Redis passwords. Treat the backup volume as secret
material: never commit it, never serve it, and copy it off-site.

Restore procedure: `docs/operations/backup-disaster-recovery.md`.

---

## 10. SMTP validation

`VALIDATION_SMTP_ENABLED` defaults to `false`. Contact verification then relies
on syntax, MX, and provider heuristics, and no message is ever sent to a
contact. This is the safe default and is sufficient to start.

Only enable it once all of the following are true: outbound port 25 is not
blocked by your provider, your sending domain has SPF, DKIM, and DMARC records,
and you accept that bulk verification mail will damage domain reputation if
your list is low quality. Do not enable it merely to make the status column
look better.

---

## 11. Troubleshooting

**`502 Bad Gateway` from the tunnel.** The origin is not answering. Check
`docker compose ps` and `curl http://127.0.0.1:8080/nginx-health` on the host.
A wrong `service:` address in `~/.cloudflared/config.yml` is the usual cause.

**Every request is `429 Too Many Requests` with `Retry-After: 60`.** The app
fails closed when it cannot reach Redis. Confirm the password is consistent:

```bash
docker compose --env-file .env.production exec backend \
  python -c "from redis import Redis; import os; print(Redis.from_url(os.environ['REDIS_URL']).ping())"
```

That must print `True`. If Redis is in protected mode with no password it
rejects the backend and the limiter denies every request, while the API still
reports healthy.

**All users share one rate-limit bucket, or logs show `172.x.x.x`.** The real-IP
snippet is not loaded. A host process reaching a published Docker port appears
to the container as the bridge gateway, not as `127.0.0.1`, which is why the
snippet trusts the private ranges. Reload after any change:

```bash
docker compose --env-file .env.production exec nginx nginx -t
docker compose --env-file .env.production exec nginx nginx -s reload
```

Cloudflare changes its edge ranges periodically. Refresh them with
`./scripts/update-cloudflare-ips.sh`, then reload nginx.

**Backup container is `Up` but the backup volume is empty.** Check
`docker compose logs backup`. A `set: illegal option` error means the scripts
were checked out with CRLF line endings. `.gitattributes` pins LF for this
repository; verify with `file infrastructure/backup/backup.sh` and rebuild the
image if needed.

**Database password rejected.** `POSTGRES_PASSWORD` is the dedicated app-role
password and must match `DATABASE_URL`. Do not put the PostgreSQL superuser
password in the app configuration. Change the app role password on the database
host and update both values in `.env.production` together.

```bash
sudo -u postgres psql -d crcrm
# At the psql prompt:
\password crcrm_app
```

---

## 12. Pre-flight checklist

- [ ] `docker compose --env-file .env.production config --quiet` is clean
- [ ] No `CHANGE_ME` placeholders remain in `.env.production`
- [ ] `POSTGRES_PASSWORD` and the password inside `DATABASE_URL` match
- [ ] `REDIS_PASSWORD` is set and `REDIS_URL` is not overridden
- [ ] `ALLOWED_HOSTS` and `ALLOWED_ORIGINS` contain `mailvex.in`
- [ ] All eight long-running services healthy, `migrate` exited `0`
- [ ] `curl http://127.0.0.1:8080/health` returns `{"status":"ok"}`
- [ ] `systemctl --user is-active cloudflared` returns `active`
- [ ] DNS routes to the tunnel: `cloudflared tunnel info crcrm` shows connections and `curl -I https://mailvex.in/` returns `200`
- [ ] `https://mailvex.in` loads and a real login succeeds
- [ ] nginx logs show real client IPs, not loopback or the bridge gateway
- [ ] At least one backup exists (`exec backup ls -lh /backups`) and a copy has been taken off-site

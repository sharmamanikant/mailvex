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
                   +--> postgres:5432  (no published port)
                    +--> redis:6379     (no published port)
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
   Postgres and Redis publish no port at all and exist only on the internal
   Docker network.
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
| `POSTGRES_PASSWORD` | The database password. |
| `REDIS_PASSWORD` | Redis password. `REDIS_URL` is derived from it in Compose, so never set `REDIS_URL` by hand. A mismatch is a silent outage. |
| `DATABASE_URL` | Must embed the same password as `POSTGRES_PASSWORD`. |
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

## 4. Build and start the application

```bash
docker compose --env-file .env.production build
docker compose --env-file .env.production up -d
```

Startup is ordered by health dependencies, so `migrate` completes before the
API starts and the API becomes healthy before nginx and the frontend come up:

```bash
docker compose --env-file .env.production ps
```

Expected state: `postgres`, `redis`, `backend`, `worker`, `scheduler`,
`frontend`, `nginx` all `(healthy)`, `backup` `Up`, `migrate` `Exited (0)`.

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
git pull
docker compose --env-file .env.production build
docker compose --env-file .env.production up -d

# rollback
git checkout <previous-tag>
docker compose --env-file .env.production build
docker compose --env-file .env.production up -d

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

**Database password rejected after changing `.env.production`.** `POSTGRES_PASSWORD`
only applies when the data directory is initialised. On an existing volume, set
the password inside the database instead:

```bash
docker compose --env-file .env.production exec postgres \
  psql -U app -d crcrm -c "ALTER USER app WITH PASSWORD 'new-password';"
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

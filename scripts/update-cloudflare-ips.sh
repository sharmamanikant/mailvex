#!/usr/bin/env bash
# Regenerate nginx/snippets/cloudflare-realip.conf from Cloudflare's published
# edge IP ranges. Run this when the list changes, then reload nginx:
#
#   sudo ./scripts/update-cloudflare-ips.sh
#   docker compose exec nginx nginx -s reload
#
# The ranges are required for correct client IP attribution behind Cloudflare
# Tunnel. A stale list means some requests lose their real client IP, which
# degrades rate limiting and audit log accuracy.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$REPO_ROOT/nginx/snippets/cloudflare-realip.conf"
V4="$(mktemp)"
V6="$(mktemp)"
trap 'rm -f "$V4" "$V6"' EXIT

curl -fsSL https://www.cloudflare.com/ips-v4 -o "$V4"
curl -fsSL https://www.cloudflare.com/ips-v6 -o "$V6"

grep -Eo '^[0-9.]+/[0-9]+$' "$V4" > "$V4.clean" || true
grep -Eo '^[0-9A-Fa-f:]+/[0-9]+$' "$V6" > "$V6.clean" || true

if [ ! -s "$V4.clean" ] || [ ! -s "$V6.clean" ]; then
  echo "Refusing to write: Cloudflare returned no usable ranges." >&2
  echo "Check network access to www.cloudflare.com and retry." >&2
  exit 1
fi

mkdir -p "$(dirname "$OUT")"
{
  echo "# Real client IP restoration for the Cloudflare Tunnel origin."
  echo "#"
  echo "# CRITICAL: the published origin port is bound to 127.0.0.1, but a host"
  echo "# process reaching a published Docker port is NOT seen as 127.0.0.1 by the"
  echo "# container. It is seen as the Docker bridge gateway (for example"
  echo "# 172.19.0.1). Trusting only 127.0.0.1 therefore silently fails, every"
  echo "# request is attributed to the gateway, and all users share one rate-limit"
  echo "# bucket and one audit-log identity. The private ranges below cover the"
  echo "# bridge gateway. This is safe only because the port is loopback-bound, so"
  echo "# nothing off-host can open the connection in the first place."
  echo "#"
  echo "# Cloudflare edge ranges are appended below by scripts/update-cloudflare-ips.sh"
  echo "# so the CF-Connecting-IP header is only honoured from Cloudflare itself."
  echo "# Source: https://www.cloudflare.com/ips-v4 and https://www.cloudflare.com/ips-v6"
  echo
  echo "set_real_ip_from 10.0.0.0/8;"
  echo "set_real_ip_from 172.16.0.0/12;"
  echo "set_real_ip_from 192.168.0.0/16;"
  echo "set_real_ip_from 127.0.0.1;"
  echo "set_real_ip_from ::1;"
  sed 's/^/set_real_ip_from /; s/$/;/' "$V4.clean"
  sed 's/^/set_real_ip_from /; s/$/;/' "$V6.clean"
  echo "real_ip_header CF-Connecting-IP;"
  echo "real_ip_recursive on;"
} > "$OUT"

rm -f "$V4.clean" "$V6.clean"
echo "Wrote $OUT ($(grep -c '^set_real_ip_from ' "$OUT") ranges)."
echo "Reload nginx: docker compose exec nginx nginx -s reload"

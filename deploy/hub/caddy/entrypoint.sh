#!/bin/sh
set -eu

mode="${AGENT_HUB_TLS_MODE:-self-signed}"
if [ "$mode" = "letsencrypt" ]; then
	: "${AGENT_HUB_DOMAIN:?AGENT_HUB_DOMAIN is required when AGENT_HUB_TLS_MODE=letsencrypt}"
	cp /etc/caddy/Caddyfile.domain /etc/caddy/Caddyfile
else
	cp /etc/caddy/Caddyfile.selfsigned /etc/caddy/Caddyfile
fi

exec caddy run --config /etc/caddy/Caddyfile

#!/bin/sh
set -eu

mode="${AGENT_HUB_CLOUDFLARE_MODE:-quick}"

if [ "$mode" = "named" ]; then
	: "${AGENT_HUB_CLOUDFLARE_TUNNEL_TOKEN:?AGENT_HUB_CLOUDFLARE_TUNNEL_TOKEN is required when AGENT_HUB_CLOUDFLARE_MODE=named}"
	exec cloudflared tunnel --no-autoupdate run --token "$AGENT_HUB_CLOUDFLARE_TUNNEL_TOKEN"
fi

# Quick Tunnel: no account/domain, random https://<id>.trycloudflare.com URL.
# The URL is printed to stdout on startup; copy it from:
#   docker compose logs cloudflared
exec cloudflared tunnel --no-autoupdate --url http://agent-hub:8090

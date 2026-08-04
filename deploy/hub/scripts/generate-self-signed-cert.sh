#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cert_dir="${1:-"$script_dir/../certs"}"
public_ip="${AGENT_HUB_PUBLIC_IP:-}"

if [ -z "$public_ip" ]; then
	echo "Usage: AGENT_HUB_PUBLIC_IP=<VPS public IP> $0 [cert-dir]" >&2
	exit 1
fi

mkdir -p "$cert_dir"

openssl req -x509 -newkey rsa:2048 -nodes \
	-keyout "$cert_dir/ca.key" -out "$cert_dir/ca.pem" \
	-days 3650 -subj "/CN=AgentSociety Hub Test CA" >/dev/null 2>&1

openssl req -newkey rsa:2048 -nodes \
	-keyout "$cert_dir/hub.key" -out "$cert_dir/hub.csr" \
	-subj "/CN=$public_ip" >/dev/null 2>&1

openssl x509 -req -in "$cert_dir/hub.csr" \
	-CA "$cert_dir/ca.pem" -CAkey "$cert_dir/ca.key" -CAcreateserial \
	-out "$cert_dir/hub.crt" -days 825 \
	-extfile <(printf 'subjectAltName = IP:%s, DNS:localhost\nextendedKeyUsage = serverAuth\n' "$public_ip") \
	>/dev/null 2>&1

rm -f "$cert_dir/hub.csr" "$cert_dir/ca.srl"
chmod 600 "$cert_dir/ca.key" "$cert_dir/hub.key"

echo "Generated self-signed TLS material in $cert_dir:"
echo "  ca.pem   -> distribute to every device (do NOT commit)"
echo "  hub.crt  -> server certificate"
echo "  hub.key  -> server private key (keep on the VPS)"
echo
echo "Device trust (Node.js agents): export NODE_EXTRA_CA_CERTS=$cert_dir/ca.pem"

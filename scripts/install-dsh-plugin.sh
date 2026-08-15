#!/bin/sh
# Install the AgentSociety dsh bundle for local development.
#
# 1. Creates the worker profile used by `dsh --profile agent-society-worker`.
# 2. Links the bundle into dsh-TUI's external plugin directory
#    ($DSH_HOME/plugins/agent-society) without modifying dsh or dsh-TUI.
set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
dsh_home=${DSH_HOME:-"$HOME/.dsh"}

dsh plugin --profile agent-society-worker add "$repository_root/dsh-plugin"

mkdir -p "$dsh_home/plugins"
ln -sfn "$repository_root/dsh-plugin" "$dsh_home/plugins/agent-society"

echo "AgentSociety dsh bundle installed."
echo "Worker profile: dsh --profile agent-society-worker"
echo "TUI plugin link: $dsh_home/plugins/agent-society"

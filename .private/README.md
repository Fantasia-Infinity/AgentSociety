# .private/

This directory holds machine-local, private runtime files. It is fully ignored
by Git (except this README) and must never be committed or shared.

Layout:

```text
.private/
├── env/       environment files (agent.env, hub.env, core.env, gateway.env)
├── state/     local databases (core-state.sqlite3, hub-state.sqlite3, ...)
├── logs/      worker and gateway logs
├── certs/     self-signed certificates
├── launchd/   generated launchd plists (contain real user paths)
└── agenthub/  optional local task envelope cache
```

The repository keeps only `*.example` templates. Code reads the `.private/`
paths first and falls back to the legacy root-level paths (`.env.agent`,
`.env`, `.env.gateway`, `.env.hub`) so existing installs keep working during
the transition.

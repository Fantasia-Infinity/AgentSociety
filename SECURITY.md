# Security Policy

AgentSociety coordinates tasks that run with the privileges of the signed-in
user on each device. Please report security issues responsibly.

## Reporting a vulnerability

- **Preferred**: open a private vulnerability report at
  <https://github.com/Fantasia-Infinity/AgentSociety/security/advisories/new>.
- If you cannot use GitHub, email the maintainers with the subject
  `[AgentSociety Security]` (address is listed on the GitHub profile).

Do not open public issues for security problems.

## What to include

- Affected component (Hub, agent-host, WeChat gateway, deployment scripts)
  and version/commit.
- Steps to reproduce, including whether the attack requires an authenticated
  Hub account or node credential.
- Impact and any suggested fix, if known.

## Scope

We treat these as in scope:

- Authentication or authorization bypass (accounts, sessions, node
  credentials, tenant/principal isolation).
- Credential leakage or unintended secret exposure.
- Remote code execution reachable through Hub task dispatch or agent
  adapters.
- Data isolation violations between users or tenants.

Out of scope: UI-automation risks inherent to third-party clients such as
WeChat/wxauto, and general dependency CVEs without a demonstrated exploit
path in this project.

## Response expectations

- We aim to acknowledge reports within 5 business days.
- We will coordinate disclosure after a fix is available.

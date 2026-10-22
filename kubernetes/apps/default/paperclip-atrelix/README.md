# Paperclip (Atrelix)

[Paperclip](https://github.com/paperclipai/paperclip) orchestrates AI agents as
a "company": org chart, goals, budgets, approvals, with agents waking on
heartbeats to work issues. Deployed with app-template at
`paperclip.${SECRET_DOMAIN_2}`, **LAN only**: the agents run unsandboxed inside
this pod.

## What runs where

| Piece | Where | Notes |
|---|---|---|
| Server + UI | `Deployment/paperclip-atrelix`, image `ghcr.io/paperclipai/paperclip` (stable channel, digest-pinned) | One replica, `Recreate`. Local adapters (`claude_local`, `codex_local`, `opencode_local`, `gemini_local`) spawn their CLIs, which ship in the image, as child processes in the same container |
| Postgres | shared CNPG cluster `postgres`, database `paperclip_atrelix` | Role and database created by the `postgres-init` initContainer. Migrations auto-apply at start. Needs `pg_trgm` and `fuzzystrmatch`; both are trusted extensions, so the owning role creates them itself. Covered by the cluster's daily `ScheduledBackup`; Paperclip's own dumps are off (`PAPERCLIP_DB_BACKUP_ENABLED=false`) |
| Data | `PVC/paperclip-atrelix-data` (20Gi `ceph-block`) at `/paperclip` (= `PAPERCLIP_HOME` = `$HOME`) | Agent workspaces, attachments (`local_disk` storage), run logs, `instances/default/config.json`, CLI logins (`~/.claude`, `~/.codex`). **Not backed up** |
| Secrets | `ExternalSecret/paperclip-atrelix` → `paperclip-atrelix-ext-secret` | 1Password items `paperclip-atrelix` (below) and `cloudnative-pg` (superuser, init container only) |
| Ingress | class `internal`; LAN DNS: unifi-dns CNAME → `internal.${SECRET_DOMAIN}` | Not on cloudflared. TLS from cert-manager over DNS-01 (the `dnsNames` solver in the ClusterIssuer) |

### 1Password item `paperclip-atrelix` (vault `Home-Ops Default`)

| Field | Value | Rotation |
|---|---|---|
| `paperclip_postgres_user` | `paperclip_atrelix` | Don't: postgres-init would create a second role |
| `paperclip_postgres_password` | `openssl rand -hex 24` | Fine: postgres-init re-applies it on the next start |
| `paperclip_better_auth_secret` | `openssl rand -hex 32` | Signs everyone out and invalidates in-flight agent JWTs |
| `paperclip_tool_action_signing_secret` | `openssl rand -hex 32` | Only with no tool-action approvals pending |
| `paperclip_secrets_master_key` | `openssl rand -hex 32` | **Never.** It encrypts every secret Paperclip stores in the database; a restored database is unreadable without this exact key |

Hex values keep the password usable verbatim in postgres-init's SQL and in
`DATABASE_URL`.

## First start

1. From the LAN, open `https://paperclip.${SECRET_DOMAIN_2}`, create an
   account, and choose **Claim this instance**. In `authenticated` + `private`
   mode the first signed-in browser to claim becomes instance admin, so do this
   right after the first deploy.
2. Close sign-up: add `PAPERCLIP_AUTH_DISABLE_SIGN_UP: "true"` to the
   HelmRelease env. Add people through invites from then on.
3. **Apps → Connections**: sign in to the Claude and Codex subscriptions, or
   add API keys. Logins land on the data volume and in the database (encrypted
   with the master key).

## Decisions and gotchas

- **Agents see the server's environment.** The local adapters spawn agent CLIs
  with `{ ...process.env, ...env }`, so every agent can read `DATABASE_URL` and
  `BETTER_AUTH_SECRET`, which also signs sessions and agent JWTs. Treat any
  agent here as able to act as instance admin. Mitigations in place: the CNPG
  superuser reaches only the init container; the master key is a mounted file,
  not env (still readable on disk by the same UID); no service-account token is
  mounted. Isolating untrusted work needs a sandboxed execution target, not
  this pod.
- **Probes send `Host: localhost`.** In `private` exposure the
  private-hostname guard returns 403 for any Host other than the public one or
  loopback, and kubelet sends the pod IP.
- **Liveness is TCP.** `/api/health` returns 503 while Postgres is unreachable;
  restarting on that would kill in-flight agent runs during a CNPG failover.
- **Claude sign-in behind nginx** needs `CLAUDE_LOGIN_TRUSTED_PROXIES` (the pod
  CIDR). Paperclip's setup-token login sends its confidential responses only
  over direct TLS or from an allowlisted TLS-terminating proxy; `TRUST_PROXY`
  doesn't count.
- **TLS over DNS-01.** The internal ingress's default certificate covers only
  `*.${SECRET_DOMAIN}`, and this zone's HTTP-01 solver needs a tunnel route. The
  cert-manager Cloudflare token (`kubernetes`) therefore has DNS:Edit and
  Zone:Read on this zone as well, and the ClusterIssuer pins only this host to
  DNS-01. Other secondary-domain hosts keep HTTP-01.
- **Upgrades.** Bump tag and digest together (stable tags look like
  `2026.1012.0`; the digest is the multi-arch index). Migrations apply on
  start; read the release notes for breaking changes first. A downgrade after
  migrations ran needs a database restore.

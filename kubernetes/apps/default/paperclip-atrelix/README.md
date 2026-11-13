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
| Data | `PVC/paperclip-atrelix-data` (20Gi `ceph-block`) at `/paperclip` (= `PAPERCLIP_HOME` = `$HOME`) | Agent workspaces, attachments (`local_disk` storage), run logs, `instances/default/config.json`, CLI state (`~/.claude`, `~/.codex`), pre-upgrade database dumps (`backups/`). **Not backed up** |
| Secrets | `ExternalSecret/paperclip-atrelix` → `paperclip-atrelix-ext-secret` | 1Password items `paperclip-atrelix` (below) and `cloudnative-pg` (superuser, init container only) |
| Route | `HTTPRoute` on `envoy-internal` (namespace `network`, listener `https`); LAN DNS: unifi-dns A record → envoy-internal; VPN clients resolve it through k8s-gateway | Not on cloudflared. TLS: the secondary-domain wildcard from `cert-manager/tls` |

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
2. Close sign-up with `PAPERCLIP_AUTH_DISABLE_SIGN_UP: "true"` in the
   HelmRelease env (set since the claim on 2026-10-22). Add people through
   invites from then on. Starting over on a fresh database means removing the
   flag again until the claim is done.
3. **Apps → Connections**: sign in to the Claude and Codex subscriptions, or
   add API keys. Credentials are stored in the database, encrypted with the
   master key; each sign-in's temporary home on the data volume is deleted
   once it completes. Connect Claude on 2026.1106.0 or later (see "Claude
   subscription tokens" below).

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
- **Claude subscription tokens.** Through 2026.1012.0 the Claude sign-in ran
  `claude auth login` and stored only the short-lived access token, dropping
  the refresh token, so the connection died when that token expired (runs fail
  with `401 OAuth access token has expired`). From 2026.1106.0 the sign-in runs
  `claude setup-token` in the browser and stores that long-lived token. It has
  only the `user:inference` scope, so the usage panel shows no quota for it.
  Reconnect any Claude connection made on an older version.
- **Claude sign-in behind envoy** needs `CLAUDE_LOGIN_TRUSTED_PROXIES` (the pod
  CIDR, which holds the envoy-internal pods). Paperclip's setup-token login
  sends its confidential responses only over direct TLS or from an allowlisted
  TLS-terminating proxy; `TRUST_PROXY` doesn't count.
- **TLS.** envoy-internal's https listener serves the `*.${SECRET_DOMAIN_2}`
  wildcard, issued over DNS-01 with the cert-manager Cloudflare token
  (`kubernetes`), which has DNS:Edit and Zone:Read on both zones. There is no
  per-host certificate.
- **Harmless startup noise.** The banner's `Agent JWT: missing (run npx
  paperclipai onboard)` only checks `PAPERCLIP_AGENT_JWT_SECRET`; agent JWTs
  are signed with the `BETTER_AUTH_SECRET` fallback instead.
- **Upgrades.** Read the release notes for breaking changes, then dump the
  database onto the data volume. Restoring the CNPG cluster backup instead
  would roll back every app's database.

  ```sh
  PRIMARY=$(kubectl -n database get pods -l cnpg.io/cluster=postgres,cnpg.io/instanceRole=primary -o name)
  kubectl -n database exec "$PRIMARY" -c postgres -- pg_dump -Fc -d paperclip_atrelix \
    | kubectl -n default exec -i deploy/paperclip-atrelix -c app -- \
        sh -c 'umask 077; mkdir -p /paperclip/backups; cat > /paperclip/backups/paperclip_atrelix-pre-<version>.dump'
  ```

  Bump tag and digest together (stable tags look like `2026.1106.0`; the
  digest is the multi-arch index). Migrations apply on start. A downgrade after
  migrations ran needs that dump restored.

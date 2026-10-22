# dario

[dario](https://github.com/askalf/dario) turns the Claude subscription into an
Anthropic- and OpenAI-compatible API, so tools that want an API key (Claude
Code, Cursor, Aider, the Agent SDK) run on the subscription instead of
per-token billing.

| | |
|---|---|
| URL | `https://dario.turner.haus` (internal ingress: LAN or Tailscale only) |
| Manifests | `kubernetes/apps/default/dario/`: `app/` is the proxy, `notify/` the alerts |
| State | PVC `dario`, mounted at exactly `/home/dario/.dario`; seats live in `accounts/*.json` |
| Secrets | `app/secret.sops.yaml`: `DARIO_API_KEY` (clients), `DARIO_ADMIN_TOKEN` (`/admin/*`) |
| Helpers | `task dario:status`, `task dario:login`, `task dario:complete` |
| Alerts | CronJob `dario-notify`, daily 09:00 PT: email + ntfy topic `homelab` |
| Updates | Renovate merges new minor/patch releases to `main` daily, 02:00-07:59 PT (`.renovaterc.json5`); a major arrives as a PR |

## Using it from a client

```sh
export ANTHROPIC_BASE_URL=https://dario.turner.haus
export ANTHROPIC_API_KEY=$(cd /mnt/c/dev/home-ops && SOPS_AGE_KEY_FILE=age.key sops -d kubernetes/apps/default/dario/app/secret.sops.yaml | yq -r .stringData.DARIO_API_KEY)
```

That key is dario's own client key, not an Anthropic one. OpenAI-shaped tools
take `OPENAI_BASE_URL=https://dario.turner.haus/v1` and the same key.

## The monthly re-grant

dario holds an OAuth grant per Claude account, which it calls a seat (ours is
`main`). Anthropic ends a grant's refresh token about 28 days after it was
approved, however often dario refreshes it. Once it lapses, every request gets

```
401 authentication_error: OAuth access token has expired. Re-authenticate to continue.
```

`/health` turns 503, and the pod logs `invalid_grant: Refresh token expired`
every few minutes. Nothing in the cluster can renew a grant: a person has to
approve a new one in a browser signed in to Claude. So plan on re-granting
about every four weeks. dario counts the days (WARN from day 21, URGENT from
day 26) and `dario-notify` passes that on to you.

### Re-grant (about a minute)

```sh
task dario:login                 # SEAT=main by default
```

1. It prints an approval link. Open it in a browser signed in to the Claude
   account that should back the seat. A grant on the wrong account succeeds,
   and dario then serves (and bills) that account's subscription.
2. Approve. The page shows a code shaped `code#state`: copy all of it.
3. Paste it at the prompt within 10 minutes. The task completes the grant and
   prints the status table: expect `/health: 200` and the seat at `ok`, `0d`.

There is no restart: the pool hot-reloads, and clients work again at once.

No terminal to paste into (a script, a `!` command in Claude Code)? The task
prints the link and exits; finish within 10 minutes with
`task dario:complete CODE='<code#state>'`.

### By hand

What the task runs, and what the commands in your shell history were:

```sh
cd /mnt/c/dev/home-ops
T=$(SOPS_AGE_KEY_FILE=age.key sops -d kubernetes/apps/default/dario/app/secret.sops.yaml | yq -r .stringData.DARIO_ADMIN_TOKEN)
curl -s -X POST -H "authorization: Bearer $T" https://dario.turner.haus/admin/login/start -d '{"alias":"main"}'
# open authorize_url from the reply, approve, copy the code#state, then within 10 minutes:
curl -s -X POST -H "authorization: Bearer $T" https://dario.turner.haus/admin/login/complete -d '{"alias":"main","code":"<code#state>"}'
# -> {"alias":"main","status":"added",...}
```

### When `login/complete` refuses

| Reply | Meaning |
|---|---|
| `invalid_grant: Invalid 'code'` | The code was truncated, mistyped, or already used |
| `400 state mismatch` | The code came from an older link |
| `410` or `no pending login` | The link is over 10 minutes old, or the pod restarted |

A pending login is single-use and held in the pod's memory, so the fix for all
three is the same: `task dario:login` again. A new link replaces the old one.

## Checking on it

```sh
task dario:status
```

prints `/health` and one row per seat: grant level (`ok`, `warn`, `urgent`,
`unknown`), age, the date of the ~28-day wall, access-token lifetime and
consecutive auth failures. The raw data is
`curl -s -H "authorization: Bearer $T" https://dario.turner.haus/admin/accounts | jq`.

## When it's rate-limited

dario serves within your subscription's usage limits. When Anthropic answers
429 (the 5-hour or weekly window is used up), dario parks the seat until that
window resets, and until then answers 429 itself without asking Anthropic:
`/status` says `all accounts rate-limited`, `task dario:status` shows the seat
`rate-limited until <time>`, and the pod logs `pool parked ... nothing sent
upstream`. It resumes on its own at the reset time.

To make it retry sooner (say, after raising the limit), send one request
pinned to the seat. A pinned request skips the parked check, and a successful
answer replaces the parked reading:

```sh
curl -s https://dario.turner.haus/v1/messages -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "x-dario-account: main" -H "x-dario-admin-token: $T" \
  -H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' \
  -d '{"model":"claude-haiku-5-5","max_tokens":8,"messages":[{"role":"user","content":"ping"}]}'
```

If the limit was lifted by turning on extra usage, that answer bills as extra
usage, and dario's overage guard halts the whole proxy. That is deliberate:
it never spends money quietly. The halt clears by itself after 30 minutes, or
at once with
`curl -X POST -H "authorization: Bearer $T" https://dario.turner.haus/admin/resume`.
For dario to keep serving on paid extra usage, set `DARIO_OVERAGE_BEHAVIOR: warn`
in `app/helmrelease.yaml`.

## Alerts

`notify/` runs `notify.py` daily at 09:00 PT. It stays silent while `/health`
is 200 and every seat is `ok`. Otherwise it sends one message two ways:

- **Email** to `tturner@${SECRET_DOMAIN_2}`, through the Gmail relay that n8n
  and Documenso use (1Password item `n8n`).
- **ntfy push** to topic `homelab` on your public ntfy server. Its URL lives
  in `notify/secret.sops.yaml` (the in-cluster `ntfy.turner.haus` isn't
  used). Subscribe to `homelab` on that server in the ntfy app. Tapping the
  notification opens this page.

| Level | Trigger | ntfy priority |
|---|---|---|
| warn | a grant 21-25 days old, or of unknown age | default |
| urgent | a grant 26+ days old, or 3+ auth failures in a row | high |
| down | `/health` not 200, or the check can't read dario | urgent |

A seat that's only rate-limited doesn't alert, even though `/health` is 503
then: dario resumes it by itself (see below).

Run it now:

```sh
kubectl -n default create job --from=cronjob/dario-notify dario-notify-manual
kubectl -n default logs -f job/dario-notify-manual
kubectl -n default delete job dario-notify-manual
```

To mute it, patch `spec.suspend` to `true` (and back to `false`). Git doesn't
set the field, so Flux leaves it alone:
`kubectl -n default patch cronjob dario-notify --type=merge -p '{"spec":{"suspend":true}}'`

## Gotchas

- **Never move the PVC off `/home/dario/.dario`.** The image declares that path
  a `VOLUME`. A mount anywhere else (it sat at `/home/dario` until 2026-09-13)
  leaves the seats in a per-container directory that dies with the pod.
- One replica with `Recreate`: two pods would race token refreshes on one file.
- The probes are TCP on purpose. `/health` is 503 whenever no seat can serve,
  which would crash-loop a liveness probe and, on readiness, cut off the
  ingress you re-grant through.
- `No Claude Code device identity found` at startup is harmless in pool mode.
- **A new model answers `400 claude_code_version_too_old`** ("Claude Code
  2.1.270 does not support this model"): Anthropic gates new models on the
  Claude Code version dario's bundled template claims, so dario needs a newer
  release. Renovate merges those daily; to get one sooner, edit the image tag
  in `app/helmrelease.yaml`. Opus 5.5 needed 2.1.280+, which came with the
  move to 6.13.28 (CC 2.1.306).

## History

- 2026-09-14: seat `main` granted.
- 2026-10-05 to 10-11: dario logged a daily WARN, then URGENT, to stdout only.
- 2026-10-13, about 10:24 PT: the last access token expired, and clients got
  401s for nine days until it was noticed on 2026-10-22. `dario-notify` exists
  because of this.

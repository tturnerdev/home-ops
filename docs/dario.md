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

## Alerts

`notify/` runs `notify.py` daily at 09:00 PT. It stays silent while `/health`
is 200 and every seat is `ok`. Otherwise it sends one message two ways:

- **Email** to `tturner@${SECRET_DOMAIN_2}`, through the Gmail relay that n8n
  and Documenso use (1Password item `n8n`).
- **ntfy push** to topic `homelab` on `https://ntfy.turner.haus`. To receive
  it, add a subscription in the ntfy app, choose "use another server", and
  enter server `https://ntfy.turner.haus`, topic `homelab`. The server is
  internal, so the phone has to be at home or on Tailscale. Tapping the
  notification opens this page.

| Level | Trigger | ntfy priority |
|---|---|---|
| warn | a grant 21-25 days old, or of unknown age | default |
| urgent | a grant 26+ days old, or 3+ auth failures in a row | high |
| down | `/health` not 200, or the check can't read dario | urgent |

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

## History

- 2026-09-14: seat `main` granted.
- 2026-10-05 to 10-11: dario logged a daily WARN, then URGENT, to stdout only.
- 2026-10-13, about 10:24 PT: the last access token expired, and clients got
  401s for nine days until it was noticed on 2026-10-22. `dario-notify` exists
  because of this.

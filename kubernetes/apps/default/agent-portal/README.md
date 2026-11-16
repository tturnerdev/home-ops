# agent-portal

The Atrelix Agent Portal (`atrelixtech/atrelix-support-portal`): which computers run Atrelix
Support, whether they are online, and what they are. One pod, the shared CNPG Postgres, no volume.

## Two hostnames, on purpose

| Hostname | Gateway | Serves |
|---|---|---|
| `agent-portal.<domain 2>` | `envoy-internal` | The web UI and the whole API. LAN and tailnet only. |
| `agent.<domain 2>` | `envoy-external` (tunnel) | Device check-in only: `/api/v1/sync/*`. |

Only check-in is public. The route admits nothing else, and the portal answers 404 for everything
else on that hostname by itself (`PORTAL_DEVICE_HOSTS`), so a mistake here cannot publish the
management API. A hostname that is neither of the two (the pod's own address, which the probes
ask) gets the health check and nothing else (`PORTAL_MANAGEMENT_HOSTS`): add a name there before
people or scripts use it.

`agent.<domain 2>` is also the update relay of Atrelix Support, a Cloudflare Worker that every
installed client depends on. The hostname is split at Cloudflare, by path:

- a Workers route `agent.<domain 2>/api/*` with **no Worker** sends those requests to the origin,
  which is this cluster through the tunnel;
- the Worker's own route `agent.<domain 2>/*` keeps everything else (`/updates`, `/v1`, `/health`).

## What has to exist outside this repository

1. **1Password item `agent-portal`** in "Home-Ops Default" with `agent_portal_postgres_user`,
   `agent_portal_postgres_password`, `agent_portal_secret_key`, `agent_portal_admin_email`,
   `agent_portal_admin_password`. Every one of the five needs a value: a field that exists and
   is empty syncs without an error, and the pod then stops in `Init:CrashLoopBackOff` (`init-db`
   logs `INIT_POSTGRES_PASS: unset`). That repository's `docs/deploy.md` (the first deployment,
   step 3) says how to see it and what to do once the fields are filled.
2. **The image**: private package `ghcr.io/atrelixtech/atrelix-support-portal`, pushed by that
   repository's Image workflow. `ghcr-pull` must be able to read it; a pod stuck in
   `ImagePullBackOff` with a 401 or 403 means it cannot (Package settings > Manage access).
3. **Cloudflare**, done on 2026-11-16. For whoever has to repeat it, in this order (that
   repository's `docs/deploy.md`, the first deployment, steps 4 to 8, has the calls and the
   reasons):
   1. DNS: `agent` is one proxied CNAME to the tunnel, the same target `sign` has. It replaced two
      placeholder records (A `192.0.2.1`, AAAA `100::`) without the name ever being without a
      record: delete the AAAA, then overwrite the A record in place as the CNAME. A CNAME cannot
      sit next to an A or AAAA record, which is why the AAAA goes first.
   2. Check at once: `curl https://agent.<domain 2>/health` and `/updates/latest.json` still answer
      from the Worker. If not, put the two placeholder records back.
   3. Workers Routes: `agent.<domain 2>/api/*` with Worker: none. Only then do those paths skip
      the Worker; until then it answers them by itself, with 404. The route comes after the DNS
      change because computers that already check in would otherwise be sent to the placeholder.
   4. Request body buffering is left at its default for this hostname, and the zone's one rate
      limiting rule is on `/api/v1/sync`.

## Checking a deployment

```sh
# From the LAN: the portal itself
curl https://agent-portal.<domain 2>/api/v1/health
# The public path: check-in is reachable (401 without a device token), nothing else is. The name
# resolves publicly on the LAN too, so this goes through Cloudflare from anywhere. The Agent Portal
# reads the body before it asks for the token: a POST without a body answers 400, not 401.
curl -i -X POST -H 'content-type: application/json' -d '{"state":{"hostname":"PROBE"}}' \
  https://agent.<domain 2>/api/v1/sync                          # 401
curl -i https://agent.<domain 2>/api/v1/devices                 # 404
curl -i -X DELETE https://agent.<domain 2>/api/v1/devices       # 404 as well, never 405 or 401
curl -i https://agent.<domain 2>/updates/latest.json            # the Worker, as before
```

The first administrator is created from the two `agent_portal_admin_*` fields while the database
has no account: sign in at `agent-portal.<domain 2>` and change the password.

## Rolling out a new version

Run the Image workflow in `atrelixtech/atrelix-support-portal` (or push a `v*` tag there), copy
the tag and digest from its summary into `app/helmrelease.yaml`, commit. Migrations run when the
pod starts.

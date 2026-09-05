# ERPNext

ERPNext at `https://erpnext.${SECRET_DOMAIN}` (internal ingress only). Test instance.

Eight containers in **one pod** — Valkey as a native sidecar, two init containers for
bootstrap, then gunicorn / nginx / socket.io / scheduler / two RQ workers — with the
database on the cluster's **CloudNativePG** PostgreSQL.

## Why one pod

Upstream splits these across six Deployments and therefore needs a **ReadWriteMany**
`sites` volume. This cluster has no RWX storage at all (Ceph RBD and TopoLVM are both
RWO). RWO restricts a volume to one *node*, not one pod — so collapsing everything
into a single pod removes the RWX requirement entirely. One RWO PVC, eight containers,
no shared-filesystem problem.

## Image

`docker.io/frappe/erpnext:develop@sha256:dbe1dcbf…`

`develop` is the dev/nightly stream and currently builds **v17.0.0-dev**. It is
rebuilt **weekly** (Mondays 03:17 UTC), and only after the compose integration suite
passes. Both `develop` and `latest` are mutable and there is no `v17.x` tag, so the
only way to pin is by digest.

**There is no stable fallback for this deployment.** ERPNext on PostgreSQL only reached
parity on `develop` (frappe/erpnext#24389, closed 2026-06-21); `version-16` and
`version-15` never got it. Swapping in a v16 digest would not just downgrade — it would
not install at all against this database.

**Renovate is disabled for this image** (`.renovaterc.json5`). Nothing in this repo
automerges, so an un-disabled Renovate would only have opened PRs — but a digest bump on
a mutable `develop` tag is a schema migration waiting to be merged by reflex, and
`03-create-site` runs `bench migrate` on every restart once the sentinel exists.

`upgrade.remediation` is set to `retries: 0` / `remediateLastFailure: false` for the same
reason: by the time a failed upgrade is noticed the database is already migrated, and
Flux's default rollback would put **old code on a new schema**. A failed upgrade stays
failed, visibly, for a human.

## PostgreSQL on CloudNativePG

The site's database lives in the shared CNPG cluster (`postgres-rw.database`,
PostgreSQL 17). What that buys and costs:

- **Backups for free.** The database is inside the cluster-wide daily barman backup with
  continuous WAL archiving to Ceph RGW (`ScheduledBackup postgres-daily`). That is a
  materially better backup than a local dump on the same PVC — and, as it happens, the
  only one available: see *`bench backup` does not work* below.
- **`bench new-site --db-type postgres`** still prints "experimental". The upstream
  Postgres CI is develop-only, label-gated and explicitly not a required check, and
  Postgres-only bugs in reports/patches/desk queries have landed after parity. Read
  `apps/erpnext/.github/POSTGRES_COMPATIBILITY.md` in the image for the class of thing to
  expect: loose `GROUP BY`, MySQL-only functions, case-sensitive text equality, integer
  division. This is a test instance; that is the point of it.
- **PostgreSQL 17 vs upstream's 15.** frappe_docker pins `postgres:15.17` and the CI
  takes whatever the runner ships. The code path explicitly handles `> 15` (it runs
  `ALTER DATABASE … OWNER TO` for the site role), and psycopg2 2.9.12 is fine; nothing
  else in the driver is version-gated.

### What `bench new-site` does as the superuser

`frappe/database/postgres/setup_db.py` `setup_database()`, connecting as the root login
to the `postgres` database:

```sql
DROP DATABASE IF EXISTS "<db_name>";          -- a fresh random name: no-op
CREATE USER "<db_name>" WITH PASSWORD '…';   -- or ALTER USER if it exists
CREATE DATABASE "<db_name>";
GRANT ALL PRIVILEGES ON DATABASE "<db_name>" TO "<db_name>";
ALTER DATABASE "<db_name>" OWNER TO "<db_name>";  -- server_version_num > 150000
GRANT ALL ON SCHEMA public TO "<db_name>";        -- connected to the new DB
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;  -- best-effort; needs
GRANT pg_read_all_stats TO "<db_name>";             -- shared_preload_libraries
```

`<db_name>` is `_<16 hex>` for both the role and the database, same as on MariaDB. The
last two statements are wrapped in `try/except` and silently skipped here — CNPG's
`shared_preload_libraries` is empty. The bootstrap SQL is then restored with **`psql`**,
which is why a 15.x client against a 17.x server is fine (only `pg_dump` is
version-strict).

The superuser is needed for `CREATE USER` / `CREATE DATABASE` / `ALTER … OWNER` and is
mounted **only** into `03-create-site` (`erpnext-cnpg-secret`, from the shared
`cloudnative-pg` 1Password item). The long-running containers never see it; the site
connects as its own generated role with the password frappe writes into
`sites/<site>/site_config.json`.

Connection details: `db_host` / `db_port` / `db_type` come from `common_site_config.json`
(written by `02-configurator`); the driver defaults `gssencmode` to `disable` (the krb5
path is not fork-safe under RQ) and leaves `sslmode` at libpq's `prefer`, so CNPG
negotiates TLS. pg_hba on the cluster is `host all all all scram-sha-256`.

### `bench backup` does not work

The image ships **pg_dump 15.19**; the server is **17.6**. pg_dump refuses a newer
server outright — verified: `aborting because of server version mismatch`. So the
sentinel path deliberately runs **no** `bench backup` before `bench migrate` (it did on
MariaDB), and `bench backup` from inside the pod will fail until the image ships a PG17
client. Use CNPG: barman base backups + WAL for point-in-time recovery, or
`pg_dump` from a PG17 client outside the pod against `postgres-rw.database`.

## Ordering

`01-valkey` is a Kubernetes **native sidecar**: an init container with
`restartPolicy: Always` **plus a `startupProbe`**. The kubelet gates the next init
container on that probe succeeding — and only if a startupProbe is present
(`if container.StartupProbe != nil`). Omitting it removes the ordering gate entirely.
The gate is never on *readiness*; readiness probes don't even run during the init phase.

`custom: true` on that probe is mandatory, for a reason that is easy to get backwards:
a **non-custom** probe is built from the controller's *primary Service port*, and this
controller has one (service `app`, port 8080). The sidecar would end up start-probing
nginx's port instead of its own, so the gate would open at the wrong moment or never.
(The chart's drop-the-probe-entirely path applies only to a controller with no Service
at all — not this one.)

SidecarContainers is Beta-on-by-default on Kubernetes 1.32 (GA in 1.33), so
`03-create-site` also waits on `127.0.0.1:6379` as a backstop, and on
`postgres-rw.database:5432` because CNPG is outside the pod.

## Bootstrap

`02-configurator` must do two things a Docker Compose deployment gets for free:

1. `ls -1 apps > sites/apps.txt` — **first**. `get_file_items(sites/apps.txt,
   raise_not_found=True)` is reached from `bench_helper.get_apps()`, which builds the
   click command tree for *every* bench invocation. Without it the bench CLI itself
   cannot start.
2. `[ -f sites/common_site_config.json ] || echo '{}' > …` — `bench set-config -g`
   opens that file in **read** mode with no existence check, so the first call raises
   `FileNotFoundError` on an empty PVC.

Both are needed because Kubernetes does not seed an empty PVC from image content the
way Docker named volumes do; mounting the fresh PVC over `sites/` hides the copies
the image ships.

`03-create-site` is guarded by a **sentinel file**, not by `test -d sites/<site>`.
`_new_site()` writes `site_config.json` *before* it contacts the database, and its
rollback is gated on `sys.__stdin__.isatty()` — false in a container. So one
transient failure would poison a directory guard permanently: every later restart
skips creation and gunicorn 500s on every request. With the sentinel, a failed attempt
leaves no sentinel, the directory is removed, and the next start retries cleanly.

A half-built attempt also leaves an orphaned database and role behind on CNPG (both
named `_<16 hex>`), which nothing else cleans up. The retry path reads `db_name` out
of the stale `site_config.json` and runs `DROP DATABASE IF EXISTS` / `DROP ROLE IF
EXISTS` as the superuser — the same statements frappe's own `drop_user_and_database()`
issues — before starting over.

`--force` is **never** used: `setup_database()` drops the database first, so a pod
restart would wipe the ERP.

`bench enable-scheduler` is **mandatory**. `_new_site()` probes a database that does
not exist yet, swallows the exception, and disables the scheduler. Without the
explicit enable, `bench schedule` runs forever and enqueues nothing — no email queue,
no auto-repeat, no scheduled reports — and logs no error anywhere.

## `args:` not `command:`

Every long-running container uses `args:` only. Docker Compose's `command:` maps to
CMD and leaves the ENTRYPOINT in place; Kubernetes `command:` **replaces** it. The
ENTRYPOINT is:

```sh
rm -rf sites/assets && ln -s /home/frappe/frappe-bench/assets sites/assets
```

Since frappe_docker `64e6536` the built assets exist **only** in the image layer, so
that symlink is the only way nginx can find them. Setting `command:` here makes every
CSS/JS bundle 404 — an unstyled login page that looks like a CSS or ingress fault.

The two bootstrap init containers *do* set `command:`, deliberately — they have no
business rewriting the symlink.

## Site name

The site is named **exactly** `erpnext.${SECRET_DOMAIN}` and
`FRAPPE_SITE_NAME_HEADER` is left at its default `$host`. That makes
Host == Origin == `X-Frappe-Site-Name` == the site directory name == the socket.io
namespace, and all four checks line up with no special-casing.

Pinning the header to a literal that differs from the browser's Host makes nginx emit
a mismatched `Origin` and socket.io rejects every connection with "Invalid origin" —
which is exactly why upstream's own `pwd.yml` has broken realtime.

A Host that matches no directory under `sites/` makes `frappe.init()` raise
`frappe.exceptions.IncorrectSitePath`, a subclass of werkzeug's `NotFound` — so the
response is **HTTP 404** with body `<host> does not exist.` This is why the frontend's
probes must send an explicit `Host` header — otherwise the kubelet sends the pod IP
and the probe 404s forever.

## Redis

One Valkey sidecar serves both frappe endpoints. Frappe needs exactly two —
`redis_cache` and `redis_queue`; `redis_socketio` has been dead since v15 and is set
only for backward compatibility.

Both point at `redis://127.0.0.1:6379` with **no database index**. The keyspaces
cannot collide: cache keys are `{db_name}|…`, queue keys are `rq:…`.
`--maxmemory-policy noeviction` is deliberate — an LRU policy could evict `rq:*` job
keys under cache pressure and silently drop background jobs.

The cluster Dragonfly would also work, but its eviction policy isn't under this app's
control and it only supports database 0 (emulated cluster mode). A 64Mi sidecar
removes both as variables.

## Network exposure

There are no NetworkPolicies on this cluster, and every pod can reach every other pod.
So Valkey is bound to **loopback only** (`--bind 127.0.0.1`). This matters more than
it looks: RQ deserializes job payloads with `pickle`, so an unauthenticated Valkey
reachable from other pods is remote code execution in the workers.

Only the nginx frontend (8080) is reachable from outside the pod, which is what the
Service targets.

**Consequence for probes:** a `tcpSocket` probe has no `host` field, so the kubelet dials
`status.PodIP` *from the node* — a loopback-bound server never answers it. Valkey's
startup probe is therefore an `exec` (`valkey-cli -h 127.0.0.1 ping`), not a tcpSocket.
Getting this wrong hangs the pod in `Init` forever, because the native-sidecar gate
never opens.

The scheduler and worker liveness probes check **only** the in-pod Valkey: restarting a
worker cannot fix an unreachable database, and a bare TCP probe against CNPG every 15s
from three containers would only add noise to the Postgres log.

## Hardening

All eight containers drop `ALL` capabilities and set `allowPrivilegeEscalation: false`,
with `runAsNonRoot` and `seccompProfile: RuntimeDefault` at pod level.
`readOnlyRootFilesystem` is **not** set: the frappe ENTRYPOINT rewrites `sites/assets`
and nginx writes `/var/lib/nginx` and `/run/nginx.pid`.

The `pod:` block sets **only** `terminationGracePeriodSeconds` (so an in-flight RQ job
can finish on rollout). Adding `securityContext` there would replace
`defaultPodOptions.securityContext` wholesale — app-template's `getOption` is a plain
assignment, not a merge — silently dropping `fsGroup` and leaving the PVC unwritable.

## Secrets

`erpnext-secret` (the `Administrator` password) is SOPS today because the cluster's
1Password Connect token can read items but not create them; `externalsecret.yaml` is
the staged replacement. It is only read on the boot that creates the site — change the
password in the UI afterwards, not here.

`erpnext-cnpg-secret` (the CNPG superuser) is already an ExternalSecret, drawn from the
shared `cloudnative-pg` item, and is mounted only into `03-create-site`.

The per-site PostgreSQL role and password are generated by frappe and written to
`sites/<site>/site_config.json` on the PVC. Do not set `FRAPPE_DB_USER` /
`FRAPPE_DB_PASSWORD` / `FRAPPE_DB_NAME` — they would override every site's own
credentials.

## Post-deploy

1. Log in at `https://erpnext.${SECRET_DOMAIN}` as **`Administrator`** with
   `ADMIN_PASSWORD` from the secret.
2. Set the time zone. `get_system_timezone()` falls back to `Asia/Kolkata` and System
   Settings ships no default, so every timestamp is IST-shifted until this is done —
   via the Setup Wizard, or:
   ```
   bench --site erpnext.turner.haus execute frappe.db.set_single_value \
     --args "['System Settings','time_zone','America/Los_Angeles']"
   ```
3. Check the devtools console for `Invalid origin` / `Invalid namespace` — realtime is
   effectively untested upstream (the weekly CI brings the websocket service up but
   asserts nothing about it), so this is the only real signal.

## Known issues

- **Assets symlink race.** Every frappe container's ENTRYPOINT independently runs
  `rm -rf sites/assets; ln -s …` under `set -e` against the shared PVC. The kubelet
  starts containers sequentially so the window is tiny, but on a loaded node a
  loser's `ln -s` can fail with "File exists" and that container exits 1 — one
  self-healing CrashLoopBackOff cycle that looks like a config error. Deterministic
  fix if it recurs: `command: ["/bin/bash","-c"]` with
  `args: ["ln -sfn /home/frappe/frappe-bench/assets /home/frappe/frappe-bench/sites/assets; exec <real command>"]`
  (`ln -sfn` is atomic via rename, and there is no `rm -rf`).
- **Node vs Python realtime.** frappe develop ships a Python socket.io server alongside
  the Node one, but `bench socketio` does **not** probe for availability: it reads
  `socketio_backend` from `common_site_config.json` (default `"node"`) and execs
  `apps/frappe/socketio.js` with no existence check. It only ever falls back
  python → node, never the reverse. If a future weekly build drops `socketio.js`, this
  container crash-loops until `bench set-config -g socketio_backend python` is added to
  `02-configurator`.
- **`chromium_path`.** `/usr/bin/chromium-headless-shell` is present in the image
  (verified); the setting matches upstream compose.
- **Secret exposure.** The CNPG superuser password reaches `bench new-site` on the
  command line, so it is visible in `/proc` for the life of that init container, and the
  generated per-site DB password sits in plaintext in `site_config.json` on the PVC.
  Acceptable on a LAN test instance; note it before reusing this pattern.
- **Never run `bench build` in a running container** — it writes new assets into the
  writable layer, desynchronises `assets.json`, and needs pod *recreation* to recover.

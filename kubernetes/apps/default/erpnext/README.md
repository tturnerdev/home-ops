# ERPNext

ERPNext at `https://erpnext.${SECRET_DOMAIN}` (internal ingress only). Test instance.

Nine containers in **one pod** — Valkey as a native sidecar, two init containers for
bootstrap, then gunicorn / nginx / socket.io / scheduler / two RQ workers (seven running
at steady state) — with the database on the cluster's **CloudNativePG** PostgreSQL.

## Why one pod

Upstream splits these across six Deployments and therefore needs a **ReadWriteMany**
`sites` volume. This cluster has no RWX storage at all (Ceph RBD and TopoLVM are both
RWO). RWO restricts a volume to one *node*, not one pod — so collapsing everything
into a single pod removes the RWX requirement entirely. One RWO PVC, nine containers,
no shared-filesystem problem.

## Image

`docker.io/frappe/erpnext:develop@sha256:dbe1dcbf…`

`develop` is the dev/nightly stream and currently builds **v17.0.0-dev**. It is
rebuilt **weekly** (Mondays 03:17 UTC), and only after the compose integration suite
passes. Both `develop` and `latest` are mutable and there is no `v17.x` tag, so the
only way to pin is by digest.

**There is no stable fallback for this deployment.** ERPNext on PostgreSQL reached parity
on `develop` (frappe/erpnext#24389, closed 2026-06-21). A `v16` image *can* run
`bench new-site --db-type postgres` (verified against PG 17 in review), but Postgres is
untested and unsupported on `version-16`/`version-15` — and on this deployment a v16
digest would not be a fresh install anyway: the sentinel path would run v16's
`bench migrate` against the v17-dev schema, which is exactly the downgrade above.

**Renovate is disabled for this image** (`.renovaterc.json5`). Nothing in this repo
automerges, so an un-disabled Renovate would only have opened PRs — but a digest bump on
a mutable `develop` tag is a schema migration waiting to be merged by reflex, and
`03-create-site` runs `bench migrate` on every restart once the sentinel exists.

`upgrade.remediation` is set to `retries: 0` / `remediateLastFailure: false` for the same
reason: by the time a failed upgrade is noticed the database is already migrated, and
Flux's default rollback would put **old code on a new schema**. A failed upgrade stays
failed, visibly, for a human.

## bench: why not its `develop` branch

The `bench` CLI in the image is the **PyPI 5.31.0 wheel** (system python; not a git
install). It is not on the PostgreSQL path at all: every database-touching command this
pod runs — `new-site`, `migrate`, `set-config`, `execute`, `enable-scheduler`,
`set-maintenance-mode`, `schedule`, `worker` — is a frappe command that bench merely
`os.execv`s into `env/bin/python -m frappe.utils.bench_helper frappe …`, so the code that
runs is frappe **17.0.0-dev**, which the pinned digest already builds from frappe
`develop`. The installed wheel contains zero PostgreSQL code (its only database-flavoured
code is MariaDB-specific and never runs here). The one bench-native command in the pod is
`bench socketio`.

bench's `develop` is also not an edge channel: releases are cut from `v5.x`
(v5.31.0 = PyPI 5.31.0 = latest on both, as of 2026-09-05), `develop` is 10 commits
ahead / 277 behind, and its delta is shell completions, `easy-install.py`, an
nginx-template regex and a git-worktree filter — every file on this pod's executed path
(`cli.py`, `commands/socketio.py`, `utils/*`, `config/common_site_config.py`) is
byte-identical. It self-labels `5.0.0-dev`, which pip sorts *below* 5.31.0. No upstream
bleeding-edge image installs bench from git: frappe_docker's production Containerfile does
an unpinned `pip3 install frappe-bench` (a PR adding a `BENCH_VERSION` arg was closed), and
Frappe Cloud pins a PyPI version. So bench is left exactly as shipped.

If a from-git bench were ever genuinely needed, the right shape is a one-layer derived
image `FROM` the pinned digest that `pip install`s a pinned bench **SHA** as root, built in
GitHub Actions and digest-pinned like `atrelix-sign` — not a frappe_docker fork, and never
a boot-time install.

The genuinely bleeding-edge knob bench 5.31.0 exposes is the realtime backend — see
*Realtime* below.

## PostgreSQL on CloudNativePG

The site's database lives in the shared CNPG cluster (`postgres-rw.database`,
PostgreSQL 17). What that buys and costs:

- **Backups for free.** The database is inside the cluster-wide daily barman backup with
  continuous WAL archiving to Ceph RGW (`ScheduledBackup postgres-daily`). That is a
  materially better backup than a local dump on the same PVC — and, as it happens, the
  only one available: see *`bench backup` does not work* below.
- **`bench new-site --db-type postgres`** still prints "experimental". The upstream
  Postgres suite runs nightly only on `develop` (`version-15`/`16` carry a label-gated PR
  copy that has never run) and is label-gated on PRs; Postgres-only bugs in
  reports/patches/desk queries have landed after parity. Read
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
last two statements are "best-effort" (`try/except`) — but on CNPG **both succeed**:
`CREATE EXTENSION` needs only superuser (the preload is checked at *query* time, so the
extension installs but is inert and frappe's Query Stats report raises SQLSTATE 55000),
and `GRANT pg_read_all_stats` needs nothing at all. That grant lets the site role read
`pg_stat_activity` query text for **every database on the shared cluster** — a
cross-tenant boundary no other app role here has. So `03-create-site` immediately
`REVOKE`s `pg_read_all_stats` and drops the inert extension. The bootstrap SQL is then
restored with **`psql`**, which is why a 15.x client against a 17.x server is fine (only
`pg_dump` is version-strict).

The superuser is needed for `CREATE USER` / `CREATE DATABASE` / `ALTER … OWNER` and is
mounted **only** into `03-create-site` (`erpnext-cnpg-secret`, from the shared
`cloudnative-pg` 1Password item). It is fed to `bench new-site` on **stdin**, not argv:
bench logs every invocation's full argv to `logs/bench.log` — a CRI host directory that
outlives the container — and argv is readable in `/proc`. The long-running containers
never see it; the site connects as its own generated role with the password frappe
writes into `sites/<site>/site_config.json`.

Connection details: `db_host` / `db_port` / `db_type` come from `common_site_config.json`
(written by `02-configurator`); the driver defaults `gssencmode` to `disable` (the krb5
path is not fork-safe under RQ) and leaves `sslmode` at libpq's `prefer`, so CNPG
negotiates TLS. pg_hba on the cluster is `host all all all scram-sha-256`.

### `bench backup` does not work

The image ships **pg_dump 15.19**; the server is **17.6**. pg_dump refuses a newer
server outright — verified: `aborting because of server version mismatch`. So the
sentinel path deliberately runs **no** `bench backup` before `bench migrate` (it did on
MariaDB), and `bench backup` from inside the pod will fail until the image ships a PG17
client. The same failure reaches every path that takes a pre-flight dump: the desk
**Download Backups** page (`/app/backups`), `bench drop-site` (use `--no-backup`),
`bench uninstall-app --no-backup`, and the other bench commands that back up first.
frappe schedules no periodic backup of its own, so nothing fails nightly. Use CNPG:
barman base backups + WAL for point-in-time recovery (cluster-scoped — a per-app
rollback means restoring to a side cluster), or `pg_dump` from a PG17 client outside the
pod against `postgres-rw.database`.

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

The time zone is set **during bootstrap**, not afterwards, for a related reason. System
Settings ships no `time_zone` and frappe falls back to `Asia/Kolkata`, so every
Scheduled Job Type is stamped in IST at creation; the scheduler's cold start computes
each job's next run from that stamp. Set the zone to America/Los_Angeles *later* and
the site clock moves back 12.5 h — every job is "not due" for 12.5 h, silently, with
the scheduler enabled and the workers idle. `03-create-site` sets the zone right after
`bench new-site` and re-bases `last_execution` to the same local clock. Check:
`SELECT count(last_execution) FROM "tabScheduled Job Type"` should be `> 0` within
minutes of Ready.

Both of those post-sentinel steps are non-fatal by design: a failure there must not
re-enter the destructive retry path.

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
worker cannot fix an unreachable database, and a `wait-for-it` exec probe against CNPG
every 15s from three containers would write one `incomplete startup packet` LOG line per
probe (its `/dev/tcp` path sends a newline before closing). A kubelet `tcpSocket` probe
would be silent — but equally pointless.

## Hardening

All eight containers drop `ALL` capabilities and set `allowPrivilegeEscalation: false`,
with `runAsNonRoot` and `seccompProfile: RuntimeDefault` at pod level.
`readOnlyRootFilesystem` is **not** set: the frappe ENTRYPOINT rewrites `sites/assets`
and nginx writes `/var/lib/nginx` and `/run/nginx.pid`.

The `pod:` block sets **only** `terminationGracePeriodSeconds` (so an in-flight RQ job
can finish on rollout). Adding `securityContext` there would replace
`defaultPodOptions.securityContext` wholesale — app-template's `getOption` is a plain
assignment, not a merge — silently dropping `fsGroup` and leaving the PVC unwritable.

## Shutdown: three containers ignore SIGTERM

A process running as **PID 1 gets no default signal action** — SIGTERM is silently
ignored unless the process installs a handler. Three of the upstream entrypoints do not:

| container | PID 1 | why SIGTERM is ignored | fix |
| --- | --- | --- | --- |
| `frontend` | `/bin/bash nginx-entrypoint.sh` | the script ends with a bare `nginx -g 'daemon off;'`, no `exec` | preStop `nginx -s stop` |
| `scheduler` | `python … frappe schedule` | `start_scheduler()` is `while True: sleep`, no handler; Python catches only SIGINT | preStop `kill -INT 1` |
| `websocket` (node fallback) | `node apps/frappe/socketio.js` | `socketio.js` registers no listeners; Node's own handler resets the disposition to default and re-raises, which the kernel ignores for PID 1 | `args:` wraps `bench socketio` in a bash `trap` so the child gets a real SIGTERM (the default Python backend handles it natively; the wrapper covers the node fallback) |

Without these, each ran out the **entire** 180s grace on every `Recreate` rollout —
measured from the node's CRI view (`talosctl containers -k`), because the API server does
not update container states until the whole kill sequence ends: nginx still answering 502s
two minutes after the pod was deleted, and the RWO volume `Multi-Attach`-blocked for the
full 180s before the replacement could start.

The websocket one cannot be fixed with a preStop hook: the image ships no `tini` or
`dumb-init`, and `kill -KILL 1` from *inside* a pid namespace is ignored by the kernel as
well — only the parent namespace (the kubelet) may SIGKILL an init. Hence the wrapper,
which keeps the ENTRYPOINT (it is `args:`, not `command:`) and `bench socketio`'s backend
selection.

`nginx -s stop` rather than the graceful `quit` is deliberate: open ERPNext tabs hold
socket.io websockets through nginx, and a graceful quit waits for those.

gunicorn (`start.sh` does `exec`), both RQ workers (warm shutdown: finish the current job)
and Valkey (under `tini`) handle SIGTERM correctly. `grep SigCgt /proc/1/status` is a hint
(SIGTERM is bit 15, mask `0x4000`) but not proof — node shows the bit *set* (its handler)
and still does not exit, and the bit reads clear once that handler has fired and reset
itself. The node-level container list during a delete is the only reliable check.

The two init containers run their long `bench` calls in the background and `wait` on
them for the same reason: bash only runs a trap once the foreground command returns, so
without that a delete during a 76s bootstrap would also ride out the grace.

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
   `ADMIN_PASSWORD` from the secret, and run the Setup Wizard (the time zone is already
   America/Los_Angeles — see Bootstrap).
2. Check the devtools console for `Invalid origin` / `Invalid namespace` — realtime is
   effectively untested upstream (the weekly CI brings the websocket service up but
   asserts nothing about it), so this is the only real signal.
3. Confirm the scheduler is really running: `SELECT count(last_execution) FROM
   "tabScheduled Job Type"` on the site database should be `> 0`.

## Known issues

- **Assets symlink race.** Every frappe container's ENTRYPOINT independently runs
  `rm -rf sites/assets; ln -s …` under `set -e` against the shared PVC. The kubelet
  starts containers sequentially so the window is tiny, but on a loaded node a
  loser's `ln -s` can fail with "File exists" and that container exits 1 — one
  self-healing CrashLoopBackOff cycle that looks like a config error. Deterministic
  fix if it recurs: `command: ["/bin/bash","-c"]` with
  `args: ["ln -sfn /home/frappe/frappe-bench/assets /home/frappe/frappe-bench/sites/assets; exec <real command>"]`
  (`ln -sfn` is atomic via rename, and there is no `rm -rf`).
- **Realtime backend.** `02-configurator` sets `socketio_backend: python`, so
  `bench socketio` execs frappe's **Python realtime server**
  (`python -m frappe.realtime.server`, asyncio + uvicorn, rewritten 2026-08-25 — five
  days before this image was built; no upstream image, compose file or CI runs it). It
  reads the same keys as node (`socketio_port`, `redis_queue`, `webserver_host`/`port`)
  and, unlike node, exits cleanly on SIGTERM even as PID 1 (uvicorn installs handlers).
  Verified in-pod: authenticated namespace connect with a session cookie and
  `publish_realtime` → delivered event. `bench socketio` only ever falls back
  python → node (if `frappe.realtime.server` vanishes from a build), never the reverse;
  the bash wrapper stays so that fallback is signal-safe too. Revert = set the key to
  `node` in `02-configurator` (it persists on the PVC, so removing the line is not enough).
- **`chromium_path`.** `/usr/bin/chromium-headless-shell` is present in the image
  (verified); the setting matches upstream compose.
- **Secret exposure.** The generated per-site DB password sits in plaintext in
  `site_config.json` on the PVC. frappe's DDL log also wrote it (`CREATE USER … WITH
  PASSWORD`) into `sites/<site>/logs/database.log*` during bootstrap, so `03-create-site`
  deletes those files once the sentinel is written. The `Administrator` password is still
  on `bench new-site`'s argv (visible in `/proc` and `logs/bench.log` for that init
  container) — it is bootstrap-only and rotated in the UI. Acceptable on a LAN test
  instance; note it before reusing this pattern.
- **Never run `bench build` in a running container** — it writes new assets into the
  writable layer, desynchronises `assets.json`, and needs pod *recreation* to recover.

# ERPNext

ERPNext at `https://erpnext.${SECRET_DOMAIN}` (internal ingress only). Test instance.

Ten containers in **one pod**: MariaDB and Valkey as native sidecars, two ordinary
init containers for bootstrap, then gunicorn / nginx / socket.io / scheduler / two
RQ workers.

## Why one pod

Upstream splits these across six Deployments and therefore needs a **ReadWriteMany**
`sites` volume. This cluster has no RWX storage at all (Ceph RBD and TopoLVM are both
RWO). RWO restricts a volume to one *node*, not one pod — so collapsing everything
into a single pod removes the RWX requirement entirely. Two RWO PVCs, ten containers,
no shared-filesystem problem.

The cost: the whole pod cycles together, so a frappe image bump also restarts
MariaDB. If that becomes annoying, split MariaDB into its own controller —
`--mariadb-user-host-login-scope='%'` is already passed, which is what makes that
split safe (otherwise `DbManager.get_current_host()` binds the site's DB user to the
frappe pod's ephemeral IP and the site loses access on the first reschedule).

## Image

`docker.io/frappe/erpnext:develop@sha256:dbe1dcbf…`

`develop` is the dev/nightly stream and currently builds **v17.0.0-dev**. It is
rebuilt **weekly** (Mondays 03:17 UTC), and only after the compose integration suite
passes. Both `develop` and `latest` are mutable and there is no `v17.x` tag, so the
only way to pin is by digest.

No upstream compose or helm file pins `develop` — frappe_docker's `pwd.yml` and
frappe/helm both pin `v16.34.1`. The deployment *contract* (entrypoint, `start.sh`,
`nginx-entrypoint.sh`, `bench worker/schedule/socketio`, the `common_site_config`
keys) is unchanged on develop, which is why this works.

Stable fallback, a one-line swap:
`"v16.34.1@sha256:eee240a179ff494cb82ad067d853c8db3f87171d86324f4b326892654e342144"`.
**Downgrading an existing site is not safe** — the revert path is: delete both PVCs,
flip the digest, let `04-create-site` rebuild.

**Renovate is disabled for this image** (`.renovaterc.json5`). Nothing in this repo
automerges, so an un-disabled Renovate would only have opened PRs — but a digest bump on
a mutable `develop` tag is a schema migration waiting to be merged by reflex, and
`04-create-site` runs `bench migrate` on every restart once the sentinel exists.

`upgrade.remediation` is set to `retries: 0` / `remediateLastFailure: false` for the same
reason: by the time a failed upgrade is noticed the database is already migrated, and
Flux's default rollback would put **old code on a new schema**. A failed upgrade stays
failed, visibly, for a human.

## Ordering

`01-mariadb` and `02-valkey` are Kubernetes **native sidecars**: init containers with
`restartPolicy: Always` **plus a `startupProbe`**. The kubelet gates the next init
container on that probe succeeding — and only if a startupProbe is present
(`if container.StartupProbe != nil`). Omitting it removes the ordering gate entirely.
The gate is never on *readiness*; readiness probes don't even run during the init
phase.

`custom: true` on those probes is mandatory, for a reason that is easy to get backwards:
a **non-custom** probe is built from the controller's *primary Service port*, and this
controller has one (service `app`, port 8080). Both sidecars would end up start-probing
nginx's port instead of their own, so the gate would open at the wrong moment or never.
(The chart's drop-the-probe-entirely path applies only to a controller with no Service
at all — not this one.)

SidecarContainers is Beta-on-by-default on Kubernetes 1.32 (GA in 1.33), so
`04-create-site` also opens with `wait-for-it -t 300 127.0.0.1:3306` as a backstop.

## Bootstrap

`03-configurator` must do two things a Docker Compose deployment gets for free:

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

`04-create-site` is guarded by a **sentinel file**, not by `test -d sites/<site>`.
`_new_site()` writes `site_config.json` *before* it contacts MariaDB, and its
rollback is gated on `sys.__stdin__.isatty()` — false in a container. So one
transient MariaDB timeout would poison a directory guard permanently: every later
restart skips creation and gunicorn 500s on every request. With the sentinel, a
failed attempt leaves no sentinel, the directory is removed, and the next start
retries cleanly.

`--force` is **never** used: `setup_database()` drops the database first, so a pod
restart would wipe the ERP.

A half-built attempt also leaves an orphaned MariaDB database and user behind (frappe
names them `_<16 hex>`), which nothing else cleans up. The retry path reads `db_name`
out of the stale `site_config.json` and drops both before starting over.

On the sentinel path, `bench backup` (database only) runs **before** `bench migrate`,
keeping the newest 5 dumps in `sites/<site>/private/backups`. `bench migrate` is one-way
and Flux is configured not to roll the image back (below), so this dump is the only
thing standing between a bad migration and a rebuild.

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
response is **HTTP 404** with body `<host> does not exist.` The `assert conf.db_name`
in `app.py` is never reached. `currentsite.txt` is not consulted on the HTTP path.
This is why the frontend's probes must send an explicit `Host` header — otherwise the
kubelet sends the pod IP and the probe 404s forever.

## Redis

One Valkey sidecar serves both frappe endpoints. Frappe needs exactly two —
`redis_cache` and `redis_queue`; `redis_socketio` has been dead since v15 (grepping
frappe develop returns zero hits) and is set only for backward compatibility.

Both point at `redis://127.0.0.1:6379` with **no database index**. The keyspaces
cannot collide: cache keys are `{db_name}|…`, queue keys are `rq:…`.
`--maxmemory-policy noeviction` is deliberate — an LRU policy could evict `rq:*` job
keys under cache pressure and silently drop background jobs.

The cluster Dragonfly would also work, but its eviction policy isn't under this app's
control and it only supports database 0 (emulated cluster mode). A 64Mi sidecar
removes both as variables.

## MariaDB

11.8 is the top of Frappe's tested window (`check_compatible_versions()` warns below
10.6 and above 11.8). Five server flags, and an explicit do-not-add list in the
manifest — in particular **not** `--skip-innodb-read-only-compressed`, which is an
obsolete MariaDB 10.6.0–10.6.5 workaround that frappe_docker removed and frappe/helm
still ships.

`innodb_read_only_compressed` must stay **OFF** (the 11.8 default). Frappe v16 still
creates `ROW_FORMAT=COMPRESSED` tables for seven core doctypes — `tabVersion`,
`tabCommunication`, `tabView Log`, `tabWeb Page View`, `tabDeleted Document`,
`tabWebhook Request Log`, `tabEmail Queue` — and with `innodb_strict_mode` on (the
default) turning it ON aborts `install-app frappe` partway through. Check with:

```sql
SELECT @@global.innodb_read_only_compressed, @@innodb_strict_mode;  -- expect OFF, ON
```

The frappe containers reach MariaDB over TCP at `127.0.0.1:3306`. **Never** set
`MYSQL_UNIX_PORT` or `FRAPPE_DB_SOCKET`: containers don't share `/run`, and frappe
unconditionally prefers a socket over host/port. `FRAPPE_DB_SOCKET` is worse — it is
persisted into `site_config.json`, so a one-time leak becomes permanent.

### Why not CloudNativePG

ERPNext on Postgres genuinely improved — frappe/erpnext#24389 closed 2026-06-21, and
develop now runs a 4-shard nightly Postgres CI that installs erpnext on a real
Postgres site. But that CI is develop-only, label-gated, explicitly not a required
check, and ~85% green; frappe_docker pins `postgres:15.17` while this cluster is PG17;
`bench new-site --db-type postgres` still prints "experimental"; and Postgres-only
bugs in reports, patches and desk queries kept landing after parity. Revisit in a year.

## Network exposure

There are no NetworkPolicies on this cluster, and every pod can reach every other pod.
So MariaDB and Valkey are both bound to **loopback only** (`--bind-address=127.0.0.1`
and `--bind 127.0.0.1`). Every consumer is inside this pod's network namespace and
already dials `127.0.0.1`, so nothing is lost.

This matters more than it looks for Valkey: RQ deserializes job payloads with `pickle`,
so an unauthenticated Valkey reachable from other pods is remote code execution in the
workers. MariaDB's `root@'%'` — which the image provisions on first init — was likewise
answering cluster-wide.

Only the nginx frontend (8080) is reachable from outside the pod, which is what the
Service targets.

**Consequence for probes:** a `tcpSocket` probe has no `host` field, so the kubelet dials
`status.PodIP` *from the node* — a loopback-bound server never answers it. Valkey's
startup probe is therefore an `exec` (`valkey-cli -h 127.0.0.1 ping`), not a tcpSocket.
MariaDB's was already an in-container `exec`. Getting this wrong hangs the pod in `Init`
forever, because the native-sidecar gate never opens.

## Mixed-uid pod

frappe runs as 1000, MariaDB as 999, and both write their PVCs. `fsGroup` does not
match by uid — the kubelet chowns each volume to `root:1000` setgid `g+rwx` **and**
adds gid 1000 to the supplementary groups of every container in the pod.

The MariaDB uid override is per-**container**. Putting it at
`controllers.erpnext.pod.securityContext` would replace the pod securityContext
wholesale (app-template's `getOption` is a plain assignment, not a merge), silently
dropping `fsGroup` and leaving both PVCs unwritable. The same caution applies to the
`pod:` block that sets `terminationGracePeriodSeconds` — it deliberately sets *only*
that key.

All ten containers drop `ALL` capabilities and set `allowPrivilegeEscalation: false`,
with `runAsNonRoot` and `seccompProfile: RuntimeDefault` at pod level.
`readOnlyRootFilesystem` is **not** set: the frappe ENTRYPOINT rewrites `sites/assets`,
nginx writes `/var/lib/nginx` and `/run/nginx.pid`, and MariaDB writes `/run/mysqld`
and `/tmp`.

`terminationGracePeriodSeconds: 180` exists because the 30s default is shared across all
ten containers and the kubelet terminates restartable init containers **last** — a busy
RQ worker can otherwise leave MariaDB the ~2s floor and an unclean InnoDB shutdown.

## Secrets

`erpnext-secret` is SOPS today because the cluster's 1Password Connect token can read
items but not create them. `externalsecret.yaml` is the staged replacement.

`MARIADB_ROOT_PASSWORD` is only honoured on the **first** init of an empty datadir —
changing it later does not re-provision root. Rotate inside MariaDB with
`ALTER USER` and update the secret to match.

The per-site MariaDB user and password are generated by frappe and written to
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
  `03-configurator`.
- **`chromium_path`.** Set to `/usr/bin/chromium-headless-shell` to match upstream
  compose; the bench Dockerfile installs it behind an `INSTALL_CHROMIUM` ARG. Harmless
  if absent, but chromium-based print/PDF would fail at use time.
- **Secret exposure.** The MariaDB root password reaches `bench new-site` on the
  command line, so it is visible in `/proc` for the life of that init container.
  Acceptable on a LAN test instance; note it before reusing this pattern.
- **Never run `bench build` in a running container** — it writes new assets into the
  writable layer, desynchronises `assets.json`, and needs pod *recreation* to recover.

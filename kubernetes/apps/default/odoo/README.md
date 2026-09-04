# Odoo Community

Odoo 19 Community at `https://odoo.${SECRET_DOMAIN}` (internal ingress only).
Test instance.

## Image

`docker.io/library/odoo:19.0-20260817@sha256:f99ffac9…`

The official `library/odoo` dated tags **are** the nightly builds — `odoo/docker`'s
`19.0/Dockerfile` installs
`nightly.odoo.com/19.0/nightly/deb/odoo_19.0.${ODOO_RELEASE}_all.deb`. So
`19.0-20260817` is the 2026-08-17 nightly of the 19.0 series, which was the newest
published image at deploy time. There is no `master`/20.0 image; the true master
stream (`19.5a1`) would need a custom build with no upgrade path.

Stable fallback, a one-line swap: `"18.0-20260817"` — but see the PGDATABASE note
below first.

**Renovate is disabled for this image** (`.renovaterc.json5`). The nightly stream
carries schema changes and Odoo does not auto-migrate; a bump needs
`odoo -u all --stop-after-init` after a backup.

## Bootstrap

Two init containers, and the design hinges on one rule: **never leave an existing
database empty.**

1. `init-db-role` (postgres image) creates only the PostgreSQL role.
2. `init-db-schema` (odoo image) runs `odoo db init odoo --username admin --password …`
   if — and only if — the database does not already exist.

Step 2 is what keeps the admin password out of Odoo's hands: `base`'s
`res_users_data.xml` hardcodes `admin`/`admin` and does **not** randomize it, and
`odoo db init` is the only supported way to set it non-interactively at creation
time. `--force` is never used (it drops the database).

It also does not reintroduce the trap below, because `odoo db init` leaves a
**fully initialized** database — the app container's `DatabaseExists` path then finds
`is_initialized()` true and loads normally, in ~0.2s with 0 queries.

`odoo/cli/server.py` `main()` calls `_create_empty_database(db)` and, *on success*,
sets `config['init']['base'] = True`. That flag is what makes `preload_registries()`
run with `update_module=True` and actually install `base`. If the database already
exists, `DatabaseExists` is raised *before* the assignment, so `init` is never set
and `modules/loading.py` does a bare `return` after logging one ERROR — the process
exits 0 and the pod sits **Running and Ready serving an empty registry forever**.

So `init-db-role` creates only:

```sql
CREATE ROLE odoo LOGIN CREATEDB;
```

and the database is created *and initialized together* by `odoo db init`
(`ENCODING 'unicode' LC_COLLATE 'C' TEMPLATE template0`, plus `CREATE EXTENSION
pg_trgm`). Every later boot swallows `DatabaseExists` and skips module loading —
idempotent.

This is why the house's usual `ghcr.io/onedr0p/postgres-init` is **not** used here:
it would create an *empty* database, which is precisely the broken state. The role
keeps `CREATEDB` because `odoo db init` runs as that role.

The role also must not be named `postgres`: `check_postgres_user()` does
`sys.exit(1)` when `db_user == 'postgres'`. The CNPG superuser is used exactly once,
by the initContainer.

Detect the broken state with
`SELECT to_regclass('public.ir_module_module') IS NOT NULL;` on the `odoo` database;
recover with a one-shot `odoo -d odoo -i base --stop-after-init`.

## Configuration

Everything is env vars — no `odoo.conf` ConfigMap. The stock `/etc/odoo/odoo.conf`
supplies `data_dir` and `addons_path`, and env outranks the file
(ChainMap: runtime > CLI > env > file > defaults).

Two families:

- **`PG*`** (libpq) for the database. There is no `ODOO_DB_HOST`. `entrypoint.sh`'s
  `check_config` returns early only when the named `PG*` var is set; with all of them
  set it appends no CLI args at all. `PGDATABASE` binding to `-d` is **19.0+ only** —
  on 18.x and older it is silently ignored and the pod boots to the database manager.
- **`ODOO_<DEST>`** for every other option (`odoo/tools/config.py`:
  `self.env_name = 'ODOO_' + self.dest.upper()`). Undocumented but covered by
  upstream CI. `ODOO_LIST_DB: "false"` is the DEST boolean, not inverted, and
  disables `/web/database/manager`.

`enableServiceLinks: false` matters here: a Service named `odoo-http` would inject
`ODOO_HTTP_PORT=tcp://10.43.x.y:8069` and Odoo would fail to parse it as an int.

## Networking

One container port, one Service port, one ingress path.

With `workers = 0` nothing binds the gevent port 8072 — `GeventServer` is only
constructed under `odoo.evented` or by `PreforkServer.long_polling_spawn()`. The
threaded server's `RequestHandler.make_environ()` injects `environ['socket']`
unconditionally, so `/websocket` is served in-thread on **8069**. Most Odoo/nginx
tutorials say otherwise because they assume `workers > 0`.

**Raising `ODOO_WORKERS` above 0 requires** a second Service port (`8072`), a second
ingress path `/websocket -> 8072`, and an explicit `MALLOC_ARENA_MAX=2`.

The 3600s proxy timeouts are required or the bus websocket is torn down every idle
minute (nginx defaults to 60s). `proxy-body-size` is already `0` globally.

## Memory

The 3Gi container limit is the design, not a guess. Odoo applies
`limit_memory_hard` (2560 MiB) as `RLIMIT_AS` in the first statement of
`ThreadedServer.start()`, and RSS ≤ VMS always — so a hard VMS cap strictly below
the cgroup ceiling makes the kernel raise `MemoryError` synchronously at
`mmap`/`brk` instead of OOMKilling the pod. **Keep hard < container limit** if you
change either.

In threaded mode the *soft* limit triggers a `SIGHUP`/`_reexec` — a full ~30s server
restart, not a graceful worker recycle. A pod that appears to restart itself
periodically with no OOMKill is this.

## First login

Username **`admin`**, password = `ADMIN_LOGIN_PASSWORD` from `secret.sops.yaml`:

```
sops -d kubernetes/apps/default/odoo/app/secret.sops.yaml | yq -r .stringData.ADMIN_LOGIN_PASSWORD
```

That key is read **only** on the boot that creates the database. Changing it later has
no effect — change the password in the UI instead.

Note this host is reachable from the whole tailnet, not only the LAN: the `home-lan`
Tailscale connector advertises 10.13.32.0/21, which contains the internal ingress VIP.

Then, in Settings → Technical → System Parameters:

| Parameter | Value |
| --- | --- |
| `web.base.url` | `https://odoo.${SECRET_DOMAIN}` |
| `web.base.url.freeze` | `True` |
| `report.url` | `http://localhost:8069` |

## Probes

Readiness and startup use `/web/health`; **liveness is a bare `tcpSocket` on 8069**, and
that difference is deliberate. `Registry.new()` holds a process-wide lock for the whole
of a module install, and every request — `/web/health` included — blocks behind it; with
`ODOO_WORKERS=0` there is no second process to answer. An HTTP liveness probe would race
installing an app from the Apps screen against its own `failureThreshold`, and losing
that race means a SIGKILL mid-install: modules stranded in state `to install`, every
later module operation raising `UserError`, and `ir.cron` skipping the database
entirely — with no automatic recovery (`button_reset_state` has no UI action).

`ThreadedServer` binds the socket for the process lifetime, so a hung or dead process is
still caught. Readiness on `/web/health` still pulls the pod out of the Service while an
install is in flight.

`report.url` keeps wkhtmltopdf fetching report assets in-pod instead of hairpinning
out to the ingress VIP and inheriting its timeouts.

Smoke test: print a quotation to PDF (wkhtmltopdf) and open Discuss (the in-thread
websocket through the ingress).

## Secrets

`odoo-secret` is SOPS today because the cluster's 1Password Connect token can read
items but not create them. `externalsecret-app.yaml` is the staged 1Password
replacement — see its header. `odoo-cnpg-secret` (the CNPG superuser, for the
initContainer only) is already an ExternalSecret.

## Known issues

- odoo/docker#562 — "Apps don't appear on homepage after installing" on Odoo 19
  images, open since 2025-10-09 against the 20250930/20251008 builds. Check it
  before debugging the manifest if installed apps don't show in the home menu.
- odoo/docker#576 — missing PyJWT makes `l10n_be_intervat` log an ERROR. Benign.
- The `addons paths: /mnt/extra-addons … invalid addons directory, skipped` warning
  at startup is benign; no volume is mounted there.
- uid 100 / gid 101 is a base-image property, not an Odoo constant (odoo:17.0 on
  jammy is uid 101). Re-check `/etc/passwd` on any tag whose base image changes, or
  `fsGroup` drift gives EACCES on `/var/lib/odoo/sessions` and a CrashLoop.

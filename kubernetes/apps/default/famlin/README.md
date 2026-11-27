# Famlin

[Famlin](https://github.com/TimVanOnckelen/famlin) (MIT) is a private,
self-hosted social network for a family: posts, photos and videos, albums,
chat. One Node.js container serves the family web app at `/`, the admin UI at
`/admin` and the API; the database is `famlin` on the shared CloudNativePG
cluster (PostgreSQL 17), created by the usual `postgres-init` initContainer.

Once the first-run setup is done (below), its route,
`family.${SECRET_DOMAIN}` on `envoy-external`, serves the internet through
the Cloudflare tunnel and the home network straight from the gateway
(split-horizon DNS), so one address works everywhere.

## Storage

| Path | Volume | Holds |
| --- | --- | --- |
| `/app/uploads` | PVC `famlin-uploads`, 100Gi `ceph-block` | photos and videos |
| `/app/data` | PVC `famlin-data`, 1Gi `ceph-block` | generated secrets (nearly empty: `JWT_SECRET` is set from SOPS) |
| `/app/backups` | NFS share on the NAS, `/mnt/storage/backups/famlin` | Famlin's scheduled backups |
| `/tmp` | `emptyDir`, 5Gi | uploads being processed, npm's cache for the boot-time migration |
| `/app/media-cache` | `emptyDir`, 1Gi | thumbnails for the optional local-folder media source |

The rest of the root filesystem is read-only.

Both PVCs carry `kustomize.toolkit.fluxcd.io/prune: disabled`, so removing the
app from git leaves them in place; delete them by hand if that is the intent.

The NFS share has to exist before the first rollout, writable by uid/gid 1000
(the image's `node` user; NFS ignores `fsGroup`). The pod does not start
while the mount fails.

## First run

On a fresh database, whoever opens `/admin` first creates the admin account.
That is why the app first ships **without** a route:

1. Roll out without the route and wait for the pod to become ready.
2. `kubectl -n default port-forward svc/famlin 3000:3000`, open
   `http://localhost:3000/admin` and create the admin account. Plain HTTP on
   localhost is fine: Famlin authenticates with bearer tokens, not cookies.
3. Leave invitations and the mobile-app QR code for later. Famlin builds
   those links from the address in the browser, which is `localhost` at this
   point.
4. Add the route. Then, from `https://family.${SECRET_DOMAIN}/admin`, set
   **Server settings → Public URL** to that address (emails only carry links
   when it is set) and create the invitations.

## Configuration

The HelmRelease only sets what the process needs to start. Everything else
lives in the database and is changed in `/admin`: SMTP (Integrations →
Email), the public URL, branding, login methods and allowed emails, backups,
and upload limits.

`secret.sops.yaml` holds the generated database password (also inside
`DATABASE_URL`) and `JWT_SECRET`. The CNPG superuser password for
`postgres-init` comes from 1Password through `externalsecret.yaml` and is
never given to the app container.

## Backups

The first-run setup switches on Famlin's scheduled backups: daily at 03:00 in
the pod's time zone, keeping 7, adjustable on the **Backups** page. Each run
writes to the NAS share:

- `famlin-backup-<timestamp>.zip`: accounts, families, posts, comments and
  settings, without media (`.zip.age` when backup encryption is on);
- `uploads/`: one incremental mirror of all photos and videos, with deleted
  files kept for 30 days under `uploads/.deleted/`;
- `db/famlin-<timestamp>.dump`: a `pg_dump`, until Famlin 1.0 and only while
  backups are unencrypted.

**System status** in `/admin` shows the last result and has **Run backup
now**. Backups leave out the SMTP password, the OIDC client secret and API
keys; enter them again after a restore. The database is also covered by the
CNPG cluster's own backups. The uploads PVC has no other backup than the
mirror.

Backup encryption (optional, **Backups** page) protects the copies on the
NAS. Its emergency kit is the only key outside the server, so keep it
somewhere else, such as the password manager.

## Restore

- **Running server:** **Backups** page → restore. Famlin takes a safety
  backup of the current state first.
- **Empty database** (new install, or after losing the database): the
  first-run setup screen offers **Restore a backup** and lists what it finds
  on the share, or takes an uploaded file. It is refused once anyone has
  signed up.

The running image must be the same version as the one that made the backup,
or newer.

## Upgrades

Famlin is pre-1.0. Every start applies its Prisma migrations, which only go
forward, and an image older than the database refuses to start. So there is
no rollback by image tag:

- Renovate opens a PR for each release and never merges it on its own.
- Before merging, read the release notes and run a backup (**Run backup
  now**).
- The HelmRelease does not roll back automatically either (upgrade
  remediation is off, timeout 15 minutes, longer than the startup probe), so
  a migration is never cut short.
- Going back means restoring: drop the `famlin` database (`postgres-init`
  recreates it empty at the next start), deploy the old version, then
  restore the pre-upgrade backup from the setup screen.

## Limits

- Uploads over the internet go through Cloudflare, which caps request bodies
  (100 MB on the Free and Pro plans) and gives the origin 100 seconds to
  start answering. The web app sends a whole selection in one request, so
  large videos are best uploaded from home, where traffic goes straight to
  the gateway.
- `/tmp` holds a whole upload request while it is processed. If the upload
  limits in `/admin` grow past 5Gi per request, raise the `emptyDir` size
  limit, because exceeding it evicts the pod.

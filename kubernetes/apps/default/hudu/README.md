# Hudu

IT documentation platform (Hudu 2.46.1, self-hosted) deployed with Hudu's
official Helm chart at `docs.${SECRET_DOMAIN_2}`. It took over from the
docker-compose instance on lab1 (10.13.37.20) on 2026-10-08; the "Migration
from lab1" section below records how, and doubles as the restore runbook.

## What runs where

| Piece | Where | Notes |
|---|---|---|
| Chart | `OCIRepository/hudu` → `oci://public.ecr.aws/q4n0g0t7/hudu-helm-chart` 0.3.4 | ECR Public, anonymous pulls. Image `hududocker/hudu:2.46.1` pinned in the HelmRelease |
| Web / worker | `Deployment/hudu-web`, `Deployment/hudu-worker` | `fullnameOverride: hudu`. The worker must stay at 1 replica (it owns Sidekiq's cron scheduler; the chart refuses more) |
| Migrations | `Job/hudu-migrate` (Helm hook, `post-install,pre-upgrade`) | Runs `rails db:prepare`. The completed Job stays around until the next upgrade, so its logs are inspectable |
| Postgres | shared CNPG cluster `postgres` (PG 17.6), database `hudu_production`, role `hudu` | Created by `Job/hudu-db-init`, which also creates the extensions `vector`, `pg_trgm`, `unaccent`, `fuzzystrmatch` as superuser. Covered by the cluster's daily `ScheduledBackup` (02:00 → Ceph RGW) like every other app DB |
| Redis | shared Dragonfly (`dragonfly.database.svc.cluster.local:6379`, db 0) | Sidekiq 7 + Rails cache. Dragonfly runs in emulated cluster mode (db 0 only); Sidekiq keys don't collide with the Bull/BullMQ apps already on it |
| Uploads | the existing MinIO bucket of the lab1 instance (`https://minio.${SECRET_DOMAIN_2}`, path-style) | Hudu hands browsers **presigned S3 URLs** (`AUTHENTICATE_UPLOADS` only applies to local-filesystem storage), so the object store must be reachable from LAN and WAN — the in-cluster Ceph RGW is not. Sharing the bucket with lab1 is safe: every key carries a random hash, nothing is overwritten |
| Secrets | `Secret/hudu-app-secret` (sops) and `ExternalSecret/hudu` → `hudu-ext-secret` | See below |
| Ingress | `Ingress/hudu`, class `external`, cert-manager `letsencrypt-production` | LAN: unifi-dns publishes the host → ingress LB IP. Public: manual proxied CNAME in the Cloudflare zone → `<tunnel-id>.cfargotunnel.com` plus the cloudflared rules in `network/external/cloudflared/configs/config.yaml` (both `hudu.` and `docs.` are routed) |

### Secrets

`hudu-app-secret` (sops, `app/secret.sops.yaml`) is the only Secret the app
reads. `secrets.existingSecret` makes the chart skip rendering its own Secret,
so this one carries everything, under the chart's default key names:

- `SECRET_KEY_BASE`, `PASSWORD_KEY`, `TWO_FACTOR_KEY` — **copied verbatim from
  lab1's `/opt/hudu/.env`**. They encrypt stored passwords, 2FA seeds and
  sessions; the restored database only decrypts with these exact values.
  Never rotate them.
- `DISCOVERY_CLIENT_SECRET` — empty; the chart's env wiring requires the key to exist.
- `SMTP_USERNAME` / `SMTP_PASSWORD` — the SendGrid credentials lab1 uses.
- `REDIS_URL`, `S3_BUCKET`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` — the
  bucket name also feeds `storage.s3.bucket` through `valuesFrom`, because it
  embeds the domain.
- `INIT_POSTGRES_HOST/DBNAME/USER/PASS` — consumed by `hudu-db-init`; `INIT_POSTGRES_PASS`
  doubles as the chart's `externalDatabase` password (`existingSecretPasswordKey`).

`hudu-ext-secret` (1Password item `cloudnative-pg`) only supplies the CNPG
superuser to `hudu-db-init`.

Edit: `SOPS_AGE_KEY_FILE=age.key sops kubernetes/apps/default/hudu/app/secret.sops.yaml`.
Reloader (`commonAnnotations`) restarts web/worker when the Secret changes.

## Decisions and gotchas

- **PostgreSQL 17 vs Hudu's stated baseline.** Hudu's docs call PostgreSQL 18
  + pgvector 0.8 a hard requirement from Core v2.46. The code disagrees:
  `db/migrate/20260722000003_create_vector_embeddings.rb` skips the semantic
  search table when pgvector is missing, and lab1 runs 2.46.1 on plain
  PostgreSQL 17.2 with no pgvector at all. The shared CNPG cluster is 17.6
  *with* pgvector 0.8.0 (the `standard` CNPG image ships it), so the
  `vector_embeddings` table and its HNSW index are created normally — verified
  in the migration rehearsal below. When the shared cluster moves to 18
  (Renovate branch `renovate/ghcr.io-cloudnative-pg-postgresql-18.x`), Hudu
  rides along; no dedicated cluster needed.
- **pgvector is not a "trusted" extension**, so only a superuser can
  `CREATE EXTENSION vector`; the app role cannot. That is why `hudu-db-init`
  runs as the CNPG superuser instead of the usual `postgres-init`
  initContainer (the official chart has no initContainer hook anyway).
- **Chart install deadlock under Flux.** Hudu's web and worker crash until the
  schema exists (boot queries `accounts`), but the chart loads the schema in a
  `post-install` hook, which Helm only runs after the Deployments are ready
  when it waits. With Flux's default wait that never happens; the first
  install sat in CrashLoopBackOff for 10 minutes and timed out. The
  HelmRelease therefore sets `install.disableWait: true`: the hook runs while
  the pods crash-loop, and they recover on the next restart (install took ~20 s
  after that). Upgrades keep the wait — migrations are a `pre-upgrade` hook there.
- **Fresh install has no `vector_embeddings` table.** `db:prepare` on an empty
  database loads `db/schema.rb`, which deliberately omits pgvector objects.
  Only relevant if Hudu is ever started from scratch instead of restored:
  `kubectl exec -n default deploy/hudu-web -- sh -c 'cd /var/www/hudu2 && bundle exec rails hudini:ensure_vector_embeddings'`.
  The restore path runs real migrations, which create the table.
- **TLS needs the public path.** HTTP-01 for this zone validates through the
  cloudflared tunnel (the Cloudflare token is scoped to the primary zone, so
  no DNS-01), so the certificate only issues once the public CNAME points at
  the tunnel and cloudflared routes the host. Until then the ingress serves
  the default certificate; LAN access works with a browser warning.
- **cloudflared was hand-patched during the cutover.** Its config is
  Flux-managed from `main`, which did not have the `hudu.`/`docs.` rules on
  2026-10-08, so the `cloudflared` Kustomization (namespace `network`) was
  suspended and the live `cloudflared-configmap` applied from the branch for
  a few hours; it was resumed right after `feat/hudu` merged. Nothing to do.
- `config.domain` must match the ingress host: it drives every generated URL
  and the CSRF/ActionCable origin checks. `behindTlsProxy: true` because
  ingress-nginx terminates TLS (plain HTTP is 308-redirected by nginx and
  `force_ssl` stays on in Rails).
- `SMTP_OPENSSL_VERIFY_MODE=peer` is set explicitly: the app's own default is
  `none`.

## How it was first applied (before the merge)

Flux tracks `main` only. The app was applied by hand from `feat/hudu` with the
field manager Flux claims on merge, so adoption was clean; the same recipe
works for any future unmerged change:

```sh
export KUBECONFIG=/mnt/c/dev/home-ops/kubeconfig
D=kubernetes/apps/default/hudu/app
SOPS_AGE_KEY_FILE=age.key sops -d $D/secret.sops.yaml \
  | kubectl apply -n default --server-side --field-manager=kustomize-controller --force-conflicts -f -
for f in externalsecret.yaml ocirepository.yaml db-init-job.yaml helmrelease.yaml; do
  sed -e 's|\${SECRET_DOMAIN_2}|<secondary domain>|g' -e 's|\${SECRET_DOMAIN}|<primary domain>|g' $D/$f \
    | kubectl apply -n default --server-side --field-manager=kustomize-controller --force-conflicts -f -
done
```

`ks.yaml` is skipped on purpose (its path does not exist on `main` until the
merge). The cloudflared hostname rules and the `default/kustomization.yaml`
entry only take effect on merge.

## Verification

```sh
kubectl get hr,ocirepository -n default hudu
kubectl get pods -n default -l app.kubernetes.io/instance=hudu
kubectl logs -n default job/hudu-migrate | tail
curl -sk https://docs.<domain>/users/sign_in -o /dev/null -w '%{http_code}\n'   # 200
```

In-pod wiring check (DB, extensions, Redis, S3) — paste into
`kubectl exec -i -n default deploy/hudu-web -- sh -c 'cat >/tmp/c.rb && cd /var/www/hudu2 && bundle exec rails runner /tmp/c.rb'`:

```ruby
c = ActiveRecord::Base.connection
puts c.select_values("select extname||'='||extversion from pg_extension order by 1").join(", ")
puts "schema #{c.select_value('select max(version) from schema_migrations')}, vector_embeddings=#{c.table_exists?(:vector_embeddings)}"
puts "redis #{Sidekiq.redis { |r| r.call('PING') }}"
s = Shrine.storages[:store]
puts "s3 #{s.bucket.name}: #{s.bucket.objects(prefix: 'uploads/upload/').count} upload objects"
```

## Upgrades

Bump `ref.tag` in `app/ocirepository.yaml` and `image.tag` in the HelmRelease
together (the chart pins its `appVersion` image; check the chart's changelog
on Artifact Hub). The pre-upgrade hook migrates the schema before the new pods
roll. `helm rollback` does not undo a migration — restore the database from
the CNPG backup instead.

## Migration from lab1 (`docs.${SECRET_DOMAIN_2}`)

### Findings (2026-10-08)

- **lab1's live database is empty.** Its docker volumes (`hudu_postgres_data`,
  `hudu_app_data`, `hudu_redis_data`) were recreated on 2026-10-08 at 09:35
  PDT; the app initialised a blank schema (0 companies, 0 users) and
  `docs.${SECRET_DOMAIN_2}` now shows the first-account sign-up page.
- **The data source is the last dump:** `lab1:/opt/hudu/backup/hudu_backup.2026-09-28.sql`
  (86.7 MB, md5 `f4e5c7d7c202417d6c1ea0a0e1f895da`, pg_dump 17.2, ends with
  "PostgreSQL database dump complete"). Contents: 11 companies, 63 articles,
  30 assets, 11 users, 11 uploads, schema `20260310135828`, account
  "Atrelix Technologies" with its `self_hosted` license key. Older dumps:
  2026-05-08 and 2026-06-12 in the same directory, plus the June copy on
  nas202 (`/var/nfs/shared/Vault/backup/Hudu-docs-atrelix-com_2026-06-12/`).
  Anything entered on lab1 between Sep 28 and Oct 8 exists in no dump.
- **Uploads need no copying.** All 357 objects (96 MB, including the 11
  `uploads/upload/<id>/file/...` attachments the dump references) are in the
  MinIO bucket the new instance already uses.
- **Rehearsed on 2026-10-08** against a scratch database on the CNPG cluster:
  the filtered dump restores as role `hudu` in 31 s with zero errors, Hudu
  2.46.1 then runs 49 migrations forward in ~3 s (including the pgvector
  table), and all counts and the license row survive. The scratch database was
  dropped afterwards.
- **License.** The key lives in `accounts.licensing_key` and comes with the
  restore. Hudu HQ records the instance URL with the activation key and the
  nightly `CheckLicensingJob` only reports the licensed-user count. After
  cutover open Admin → License Key → Refresh; if HQ rejects the new URL, Hudu
  support can update it (they ask for the HQ account email, a screenshot of
  the License Key page, the billing email and the card's last four digits).
  An expired/invalid license puts the instance in read-only "Limited Mode",
  not off.
- Hudu's own guide (support article 33570468016791) is the docker-compose
  version of the same three ingredients: the `.env` (our sops Secret), the SQL
  dump, and the object storage. There is no application-level export/import
  path; CSV exports exist for individual asset layouts only.

### Executed on 2026-10-08

lab1 had meanwhile been restored from the Sep-28 dump by hand, so the source
became a fresh dump of that restored database (schema already at
`20260910150000`, two rows newer than the file): `hudu_backup.2026-10-08-final.sql`,
copied to `~/Downloads/hudu_backup.2026-10-08-final-from-lab1.sql` on the
workstation. lab1's `app` and `worker` containers were stopped before the
dump and stay stopped; its `db`, `redis` and `letsencrypt` containers and all
volumes are untouched. Steps 1–5 below ran as written (restore 31 s, zero
errors, counts 11/63/30/11/11, license row present). Because the dump's
schema was already current, the migrate hook had nothing to do and the
pgvector table was created with `rails hudini:ensure_vector_embeddings`.
The hostname was then switched to `docs.${SECRET_DOMAIN_2}` (step 6) and
cloudflared was patched as described under "Decisions and gotchas".

Still to do by hand: the Cloudflare DNS record (`docs` → proxied CNAME to the
tunnel, see step 6), then `docker-compose down` on lab1 after a week.

### Runbook (as executed; reusable for any future restore)

Maintenance window: ~10 minutes. Nothing here touches lab1.

**0. Prepare (any time before)**

```sh
export KUBECONFIG=/mnt/c/dev/home-ops/kubeconfig
scp root@10.13.37.20:/opt/hudu/backup/hudu_backup.2026-09-28.sql ~/hudu-dump/   # or ssh ... cat > file
md5sum ~/hudu-dump/hudu_backup.2026-09-28.sql     # f4e5c7d7c202417d6c1ea0a0e1f895da
tail -3 ~/hudu-dump/hudu_backup.2026-09-28.sql     # "PostgreSQL database dump complete"
# lab1 objects belong to its superuser; here the app role owns everything, so drop the
# ownership statements (and the extension comments, which need extension ownership),
# and run the whole restore as the app role from a superuser session:
{ echo "SET ROLE hudu;"; grep -vE '^ALTER [A-Z ]+ [^ ]+ OWNER TO postgres;$|^COMMENT ON EXTENSION' ~/hudu-dump/hudu_backup.2026-09-28.sql; } > ~/hudu-dump/hudu-restore.sql
PRIMARY=$(kubectl get cluster -n database postgres -o jsonpath='{.status.currentPrimary}')
```

**1. Freeze the new instance** (nobody is using it yet, but Sidekiq must not hold connections):

```sh
kubectl scale -n default deploy/hudu-web deploy/hudu-worker --replicas=0
```

**2. Recreate the database** (superuser; `WITH (FORCE)` drops leftover connections):

```sh
kubectl exec -n database $PRIMARY -c postgres -- psql -U postgres -v ON_ERROR_STOP=1 \
  -c "DROP DATABASE hudu_production WITH (FORCE);" \
  -c "CREATE DATABASE hudu_production OWNER hudu;" \
  -c "\c hudu_production" \
  -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_trgm; CREATE EXTENSION IF NOT EXISTS unaccent; CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;"
```

**3. Restore** (streams the file into psql on the primary; ~30 s; one transaction, so a failure leaves an empty database rather than a half-restore):

```sh
kubectl exec -i -n database $PRIMARY -c postgres -- \
  psql -q -v ON_ERROR_STOP=1 --single-transaction -U postgres -d hudu_production < ~/hudu-dump/hudu-restore.sql
kubectl exec -n database $PRIMARY -c postgres -- psql -At -U postgres -d hudu_production -c \
  "select (select count(*) from companies)||' companies, '||(select count(*) from users)||' users, '||(select count(*) from uploads)||' uploads, schema '||(select max(version) from schema_migrations)"
# expect: 11 companies, 11 users, 11 uploads, schema 20260310135828
```

**4. Migrate forward and start** — a forced reconcile replays the chart's own
pre-upgrade migration hook (`rails db:prepare`; 49 migrations in seconds for
the Sep-28 schema, a no-op for a dump that is already current) and, as
verified on 2026-10-08, puts the Deployments back to 1 replica; the explicit
scale below is just a safety net. If the dump already carries the pgvector
migration as applied but was taken from a server without pgvector (lab1), the
table does not exist yet — the rake task creates it:

```sh
flux reconcile helmrelease hudu -n default --force --timeout 10m
kubectl logs -n default job/hudu-migrate | grep -E "migrated|Skipping|rror" | tail
kubectl scale -n default deploy/hudu-web deploy/hudu-worker --replicas=1
kubectl get pods -n default -l app.kubernetes.io/instance=hudu     # web 1/1, worker 1/1
kubectl exec -n default deploy/hudu-web -- sh -c 'cd /var/www/hudu2 && bundle exec rails hudini:ensure_vector_embeddings'
```

**5. Verify** — the counts query again should report schema `20260910150000`
(and `select to_regclass('vector_embeddings')` non-null); then in a browser:
log in with an existing lab1 account (passwords and 2FA are decrypted with the
carried-over keys), open an article with an attachment (download = presigned
MinIO URL), upload a new file, open a password record, check Admin → License Key.

**6. Cutover to `docs.${SECRET_DOMAIN_2}`** (same window or later; one commit):

- `app/helmrelease.yaml`: `config.domain` and `ingress.host` → `docs.${SECRET_DOMAIN_2}`
  (keep `tls.secretName: hudu-tls`; cert-manager re-issues for the new host).
  Re-apply as in "Deploying from this branch", or merge. **Done 2026-10-08.**
- Public DNS (Cloudflare dashboard, zone of the secondary domain): edit the
  existing `docs` record from `CNAME dc2.infra.atrelix.net` (DNS only) to
  `CNAME a7a31f2c-ec52-4d5f-94e7-0acc8a461744.cfargotunnel.com`, **Proxied**
  (the tunnel ID is `TunnelID` in `cloudflare-tunnel.json`). No cluster token
  can edit this zone. The cloudflared rule for `docs.` is live (hand-patched,
  see "Decisions and gotchas"). Certificate `hudu-tls` issues within minutes
  of the change.
- LAN DNS: remove the manual override `docs.${SECRET_DOMAIN_2} → 10.13.37.20`
  (local DNS server / UniFi) so the unifi-dns record (→ 10.13.38.80) wins; a
  pre-existing manual record of the same name blocks external-dns from
  creating it.
- Stop lab1 (`ssh root@10.13.37.20 'cd /opt/hudu && docker-compose stop app worker letsencrypt'`)
  so only one instance talks to Hudu HQ, then Admin → License Key → Refresh
  on the new one. The old `hudu.` hostname can stay routed or be removed.
- After a week: `docker-compose down` on lab1 (keep `/opt/hudu/backup`),
  remove the docs route from lab1's Traefik and the WAN 443 port-forward.

**Rollback** before step 6: `kubectl scale --replicas=0`, repeat steps 2–4 with
another dump (or none, to return to the blank install). After step 6: revert
the DNS records and restart lab1's containers (it is a blank install now, so
only do that with a dump restored there as well). The new database is in the
CNPG cluster's daily object-store backups from the first night after restore.

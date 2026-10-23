# Updates (Renovate)

The Renovate GitHub App scans this repo and `.renovaterc.json5` decides what
merges itself. Self-merging updates go straight to `main` as a commit, with no
PR and no CI (Flux Local only runs on PRs, and it fails intermittently on
registry rate limits), and Flux rolls them out within minutes.

| What | How it updates |
|---|---|
| List B apps (below) | minor, patch and digest updates merge daily, 02:00-07:59 PT; majors open a PR |
| dario | minor and patch updates merge daily, 02:00-07:59 PT (see `docs/dario.md`) |
| CLI tools in `.mise.toml` | minor and patch updates merge on weekends, except talosctl, talhelper, kubectl, flux and cilium-cli |
| GitHub Actions | every update merges on weekends, once it is 3 days old |
| Everything else (list A) | a PR on weekends, for review |

## List A: review by hand

- Cluster platform: Talos and Kubernetes (`talos/talenv.yaml`), Cilium,
  CoreDNS, Spegel, metrics-server, Reloader, csi-driver-nfs, TopoLVM, Flux,
  Rook-Ceph, CloudNativePG (operator, cluster, Barman plugin), Dragonfly,
  cert-manager, external-secrets, both ingress-nginx, envoy-gateway,
  external-dns and the UniFi webhook, k8s-gateway, cloudflared, Tailscale,
  and the CRDs in `bootstrap/`.
- Atrelix business apps: Hudu, Documenso, n8n-atrelix, Paperclip,
  workflow-console, RustDesk.
- Madsonic: a pinned 2022 beta build.
- The shared app-template chart, and any image a list-A app shares with a
  list-B app: Renovate merges an update only when every file it touches is
  in list B.

## List B: update unattended

Media: Plex, Sonarr, Radarr, Prowlarr, Readarr, SABnzbd, Overseerr,
Audiobookshelf. Dashboards: Homarr, Homepage, Organizr. Home apps: Mealie,
Paperless-ngx, Photoprism, n8n (personal), ntfy, Shlink and its web client.
Test apps: echo (all three), podinfo (all three), httpbin (both).

Homebox belongs here but is held out for now. A switch to hay-kot/homebox
v0.10.3, uncommitted on 2026-10-22, edits the same lines as its pending
update (PR #180). Restore its folder in the rule once that switch lands.

## Version holds

Two rules cap a package with `allowedVersions`: Sonarr's waits on
upstream, PostgreSQL's on a planned major upgrade. Each rule's comment
carries the detail.

| Rule | Caps | Why | Relax or drop |
|---|---|---|---|
| Hold Sonarr on v4 | `ghcr.io/home-operations/sonarr` at `<5` | v5 is still a pre-release (GitHub flags every 5.0.0.x build as one, as of 2026-10-22) with a one-way database migration, and the home-operations image publishes those builds under plain tags | drop once Sonarr ships v5 as a stable release |
| Hold PostgreSQL on 17 | `ghcr.io/cloudnative-pg/postgresql` at `<18` | a major in CloudNativePG is an offline in-place upgrade (pg_upgrade job, minutes of downtime) that needs a fresh backup and its own window | raise when PostgreSQL 18 is planned |

Relax a hold by raising its `allowedVersions`, drop one by deleting its
rule, and check the edit with the validator below either way.

## Changing it

Moving an app between lists means adding or removing its folder in the
`matchFileNames` of the "Auto-merge less critical apps" rule. Check an edit
with the current validator (an older cached Renovate rejects this config):

```sh
npx --yes --package renovate@latest -- renovate-config-validator --strict .renovaterc.json5
```

The Renovate Dashboard (issue #1) lists every pending update.

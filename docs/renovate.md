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

Five rules cap a package with `allowedVersions`. Four wait on the cluster
(Talos v1.10.9 / Kubernetes v1.33.13 in `talos/talenv.yaml`, as of
2026-10-23) and lift during the Talos/Kubernetes upgrade; Sonarr's waits
on upstream. Each rule's comment carries the detail.

| Rule | Caps | Why | Relax or drop |
|---|---|---|---|
| Hold Sonarr on v4 | `ghcr.io/home-operations/sonarr` at `<5` | v5 is still a pre-release (GitHub flags every 5.0.0.x build as one, as of 2026-10-22) with a one-way database migration, and the home-operations image publishes those builds under plain tags | drop once Sonarr ships v5 as a stable release |
| Hold Spegel on 0.6 | `ghcr.io/spegel-org/helm-charts/spegel` at `<0.7.0` | 0.7 dropped containerd 2.0 (it advertises images only through content events, new in containerd 2.1); 0.8 refuses to start below containerd 2.2 and its registry Service sets `trafficDistribution: PreferSameNode`, which Kubernetes 1.32 rejects; the nodes run containerd 2.0.3 | raise to `<0.8.0` at Talos 1.11+ (containerd 2.1); drop at Talos 1.13+ (containerd 2.2.3) with Kubernetes 1.34+ |
| Hold Flux on 2.8 | `controlplaneio-fluxcd/distribution` and the flux CLI (`aqua:fluxcd/flux2`), which moves with it, at `<2.9.0` | 2.9 needs Kubernetes 1.34.1, and flux-operator cannot roll a distribution back a minor | drop at Kubernetes 1.34.1 |
| Hold cert-manager on 1.20 | `quay.io/jetstack/charts/cert-manager` at `<1.21.0` | 1.21 is supported on Kubernetes 1.33-1.36; 1.20 is the last line for 1.32 | drop at Kubernetes 1.33 |
| Hold Envoy Gateway on 1.8 | `mirror.gcr.io/envoyproxy/gateway-helm` at `<1.9.0` | 1.9 is supported on Kubernetes 1.33-1.36; 1.8 is the last line for 1.32 | drop at Kubernetes 1.33; the CRDs go first (see the envoy-gateway `ocirepository.yaml`) |

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

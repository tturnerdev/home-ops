# Home-Ops Atlas: plan and design

A single page that explains the cluster: what it is, how it is laid out, how a
request travels through it, and what is running right now. Published as a
Claude artifact and kept in this repo under `docs/atlas/`.

Status: implemented 2026-10-24. Section 9 records the decisions; section 10
is the runbook. The page is served three ways from one file:

| Copy | Where | Refresh source |
|---|---|---|
| Claude artifact | link in the session that built it | GitHub connector, then the snapshot bundled into the page |
| GitHub Pages | `https://tturnerdev.github.io/home-ops/` (after the first `Atlas` workflow run) | raw.githubusercontent.com, then the files next to the page |
| In-cluster | `https://atlas.turner.haus` (envoy-internal, LAN and VPN only) | raw.githubusercontent.com, then the ConfigMap-served files; `config.json` fills in the secondary zone |

## 1. What the page has to do

1. **Overview**: the cluster in one screen, with the numbers that matter
   (nodes, apps, hostnames, LoadBalancer IPs) and one architecture drawing.
2. **Design**: how the pieces fit, with a drawing per topic: network and the
   three DNS horizons, the two Envoy Gateways, GitOps (Flux) structure,
   storage, data services, secrets and TLS, the Talos nodes.
3. **Flow Explorer** (the centrepiece): pick a client location, a protocol or
   port and a destination, and see every stop the traffic makes, UniFi-style,
   with the reason for each stop and the object in this repo that causes it.
4. **Current state**: versions, image tags, chart sources, hostnames, IPs,
   Renovate policy, tool pins, and (optionally) live facts from the cluster.
   A **Refresh** button reloads this from the repo's `main` branch on demand.

## 2. What the research found

Everything below comes from the manifests and docs in this repo (commit
`556a79c`); `scripts/atlas/build-state.py` extracts the same facts mechanically.

- **Platform**: 3 Talos control-plane VMs (`home-ops-01/02/03`,
  10.13.38.101-103, QEMU disks, so Proxmox or similar), Talos 1.14.4 on the
  1.13 config contract, Kubernetes 1.36.5, API VIP 10.13.38.82, KubePrism
  on 127.0.0.1:7445. CNI `none` + Cilium 1.20.3 (kube-proxy replacement,
  native routing, L2 announcements, LB-IPAM, DSR + maglev). CoreDNS and
  kube-proxy from Talos are disabled; CoreDNS comes from its Helm chart.
- **GitOps**: Flux 2.9.6 via flux-operator 0.61.0. `kubernetes/flux/cluster`
  defines `cluster-meta` (shared Helm repos) and `cluster-apps`
  (`./kubernetes/apps`). The apps path has no kustomization.yaml, so
  kustomize-controller generates one from the namespace directories that do;
  each namespace kustomization lists `./<app>/ks.yaml`, each `ks.yaml` is a
  Flux Kustomization pointing at `<app>/app`, which holds the HelmRelease.
  SOPS (age) decrypts secrets; `cluster-secrets` feeds `${SECRET_DOMAIN}`,
  `${SECRET_DOMAIN_2}`, `${SECRET_NFS_IP}`, `${POSTGRES_IPV4}` and
  `${ONEPASSWORD_CONNECT_HOST}` into manifests by postBuild substitution.
  GitHub pushes reach Flux through a Receiver behind `flux-webhook.<d>` on
  the external Gateway.
- **Ingress**: Envoy Gateway 1.9.2, one GatewayClass, two Gateways:
  `envoy-internal` (10.13.38.83) and `envoy-external` (10.13.38.84), each
  with an http listener (301 to https only) and an https listener serving two
  wildcard certificates (primary and secondary zone, Let's Encrypt via
  DNS-01 on Cloudflare) through a ReferenceGrant. App routes are
  `HTTPRoute`s attached to the https listener. External's
  ClientTrafficPolicy trusts one X-Forwarded-For hop (cloudflared); internal's
  uses the TCP source address (Cilium preserves it). ingress-nginx is gone.
- **Three DNS horizons** for `*.turner.haus`:
  - LAN: `unifi-dns` (external-dns + UniFi webhook) publishes every HTTPRoute
    host: internal routes as `A 10.13.38.83`, external routes as
    `CNAME external.turner.haus`, which is a manually kept UniFi A record to
    10.13.38.84. Both zones are published to UniFi.
  - Public: `external-dns` (Cloudflare provider) publishes only routes on
    `envoy-external`, as proxied CNAMEs to `external.turner.haus`, which a
    DNSEndpoint CNAMEs to the Cloudflare Tunnel. Secondary-zone hosts are
    manual proxied CNAMEs in that zone. Internal apps have no public record.
  - VPN: Tailscale split DNS sends `turner.haus` to `k8s-gateway`
    (10.13.38.53), a CoreDNS that answers from Services and HTTPRoutes and
    forwards everything else to the UniFi resolver 10.13.37.1.
- **Remote access**: cloudflared (QUIC, post-quantum, 1 replica) connects
  the tunnel to `https://envoy-external.network.svc:443` with SNI
  `external.turner.haus`; the tunnel ingress rules list `turner.haus`,
  `*.turner.haus` and five secondary-zone hosts. The Tailscale operator runs a
  2-replica Connector advertising `10.13.32.0/21` (deliberately wider than
  the LAN's `10.13.36.0/22`, so at home the directly connected route wins).
- **Other LoadBalancer IPs** (Cilium pool 10.13.38.53, .80-.81, .83-.100):
  k8s-gateway .53, Plex .91 (:32400, direct), postgres-lb .92 (:5432, primary
  only, externalTrafficPolicy Local), RustDesk .93 (raw TCP/UDP 21114-21119).
- **Storage**: Rook 1.21.0 / Ceph 20.2.4 Tentacle, one OSD per node
  (`ceph-block`, RBD, 3x replicated, default class, zstd compression) plus an
  RGW object store (`ceph-bucket`, Retain) used for Postgres backups;
  TopoLVM 17.3.0 thin pool on a separate disk per node (`topolvm-thin-provisioner`,
  node-local, used only by the three Postgres instances); NFS from the NAS
  (`${SECRET_NFS_IP}`, `/mnt/storage/media/...`) mounted directly by the
  media apps. The csi-driver-nfs HelmRelease exists but is not wired in.
- **Data services**: CloudNativePG 1.30 (chart 0.29.1) cluster `postgres`,
  3 instances on TopoLVM, synchronous `any 1`, switchover updates, daily
  02:00 Barman Cloud backup to Ceph RGW with 30-day retention; consumers:
  documenso, homebox, hudu, mealie, n8n, n8n-atrelix, paperclip-atrelix,
  paperless-ngx (and shlink, not deployed). Dragonfly 2.0.2 (3 replicas,
  emulated cluster mode, no persistence) for hudu and n8n.
- **Secrets**: SOPS/age in git; external-secrets 2.13 with a ClusterSecretStore
  on 1Password Connect (vault "Home-Ops Default").
- **Apps**: 57 HelmReleases in 11 namespaces; 45 deployed, 6 suspended test
  apps, 6 folders not referenced by any namespace kustomization
  (httpbin, httpbin2, shlink, shlink-web-client, audiobookshelf,
  csi-driver-nfs). 33 hostnames: 24 on the internal Gateway, 9 on the
  external one. Most apps use bjw-s app-template 5.3.0; Hudu uses its own
  chart. Renovate auto-merges "list B" apps daily in a 02:00-08:00 PT
  window and dario daily; everything else is a weekend PR; holds on Sonarr
  v4 and PostgreSQL 17.
- **Outside the cluster** (from comments and docs): UniFi gateway/resolver at
  10.13.37.1 (default route; the troubleshooting doc also names
  10.13.36.1 as the router), NAS at 10.13.37.60 ("nas201", NFS), `lab1`
  Docker host at 10.13.37.20 (former Hudu, still hosts the MinIO bucket Hudu
  uploads to), a standalone Tailscale subnet-router container (break-glass),
  Cloudflare (zone, tunnel, DNS-01), GitHub (repo, webhook, Renovate),
  1Password Connect, a public ntfy server and a Gmail/SendGrid relay for
  alerts. The page will state these as "outside the cluster" and mark the
  ones it cannot verify from the repo.

Two things the research turned up that the page will show as findings rather
than hide: the six unwired app folders above, and the audiobookshelf release
still carrying an `ingress:` block for the retired `internal` class.

## 3. Approach for the visualizations

Options considered:

| Option | Verdict |
|---|---|
| Mermaid diagrams | Quick, but no control over layout, styling or interaction; renders differently per host. Rejected for the main drawings. |
| A graph library (cytoscape, d3-force) | Good for exploring a graph, bad for explaining one: auto-layouts move every reload and hide the layering that is the point. Rejected. |
| React Flow or similar | Heavy, needs a bundler, and the page must run from a single file under a strict CSP. Rejected. |
| Hand-laid SVG drawn by the page from `state.json`, plus a DOM "hop rail" for flows | Deterministic layout that mirrors the real layers; theme-aware; every box can link to the manifest that defines it. **Chosen.** |

The Flow Explorer is a small rule engine, not a picture: given
`(location, protocol/port, destination)` it walks the real decision points
(which resolver answers, what the record is, which IP, who answers ARP for
it, which Gateway listener, which HTTPRoute, which Service and pod) and emits
an ordered list of stops with a verdict: reachable, redirected, blocked, or not
configured. The rail renders those stops with a moving packet marker
(disabled under reduced-motion). Because it is data-driven, adding an app or
a hostname in git updates the explorer on the next refresh without touching
the page.

Preset flows (chips above the controls): LAN browser to an internal app;
WAN browser to an external app; WAN to an internal-only app (blocked, and
why); Tailscale away from home; plain http (redirect); kubectl to the VIP;
talosctl to a node; SSH to a node (Talos has none); Plex direct :32400;
RustDesk client; psql over the VPN; DNS lookup itself; and system flows:
git push to Flux reconcile, certificate issuance, DNS record publication,
image pull through Spegel, nightly Postgres backup, Renovate update.

## 4. Deliverable layout

```
kubernetes/apps/infra/atlas/
  ks.yaml                     Flux Kustomizations atlas (site) and atlas-live (CronJob)
  app/
    helmrelease.yaml          static-web-server serving /public from three ConfigMaps
    kustomization.yaml        configMapGenerator: atlas-site (index.html), atlas-state (state.json, live.json)
    configmap.yaml            atlas-config: config.json with ${SECRET_DOMAIN_2} substituted by Flux
    site/
      index.html              the page: one file, vanilla JS + inline SVG, no build step
      state.json              repo-derived state, rebuilt by the Atlas workflow on every push to main
      live.json               facts from the running cluster (task atlas:live or the CronJob)
  cronjob/
    live.sh                   read-only kubectl/jq collector; --push commits live.json to main
    cronjob.yaml              CronJob atlas-live (staged: suspend: true), every 6 h
    rbac.yaml                 ServiceAccount + read-only ClusterRole (no secrets, no exec)
    externalsecret.yaml       GITHUB_TOKEN from the 1Password item "atlas"
scripts/atlas/
  build-state.py              manifests -> state.json (no cluster access needed)
  bundle.py                   embeds state.json into index.html for the artifact (--unwrap)
.taskfiles/atlas/Taskfile.yaml  task atlas:state | atlas:live | atlas:refresh | atlas:serve
.github/workflows/atlas.yaml    rebuild state.json on push to main, deploy the site dir to GitHub Pages
docs/atlas.md                   this document
```

The site lives under the app directory because kustomize may only read files
below the kustomization that references them; the GitHub Action and GitHub
Pages read the same directory. One `index.html` serves all three copies: the
repo copy is a full document; `bundle.py --unwrap` strips the document
wrapper and embeds the data for the artifact host, which adds its own.

## 5. The Refresh button

The page loads its state in this order and shows which source won, with the
commit and generation time:

1. **GitHub connector** (inside claude.ai): the artifact declares the `mcp`
   capability for the viewer's GitHub connector and calls
   `get_file_contents` for `kubernetes/apps/infra/atlas/app/site/state.json`
   (and `live.json`) on `refs/heads/main`. This is what makes Refresh work
   inside the artifact: the page itself may not fetch arbitrary URLs there.
   The connector is only asked when the button is pressed, never on load.
2. **raw.githubusercontent.com** (outside claude.ai, e.g. GitHub Pages or a
   local file): a plain `fetch` of the same files; the repo is public and
   GitHub sends `Access-Control-Allow-Origin: *`.
3. **Sibling file** (`./state.json` next to the page).
4. **Bundled snapshot** embedded in the page at publish time.

Refresh re-runs 1 to 3 and the Settings drawer logs which source answered.
Nothing polls. The last successful payload is cached in the browser as a
convenience only. In-cluster, Flux also updates the served `state.json` and
`live.json` within its reconcile interval (or at once after the webhook), so
that copy follows `main` even without pressing anything.

What "fresh" means per fact:

- Repo-derived facts are as fresh as `main`: the Action regenerates
  `state.json` on every push that touches `kubernetes/`, `talos/`,
  `bootstrap/`, `.mise.toml` or `.renovaterc.json5`.
- Live facts are as fresh as the last `task atlas:live` run that was
  committed. The page shows that timestamp separately and never mixes the
  two.

## 6. State model (`state.json`, schema 1)

`platform` (versions), `talos` (contract, installer, flags), `nodes`,
`network` (CIDRs, VIP, LB pool, LB IPs with owners, Gateways with listeners,
certs and client-IP policy, the three DNS horizons, tunnel, Tailscale, TLS),
`storage` (classes, Ceph, TopoLVM, NFS exports, PVCs by class), `databases`
(Postgres topology, backup, consumers; Dragonfly), `apps` (per HelmRelease:
chart and source, images with tag and digest, routes with Gateway and
exposure, services with LB IPs, PVCs, NFS mounts, dependencies, DB usage,
deployed/suspended, Homepage metadata), `hostnames` (host, app, exposure,
and the expected LAN, public and VPN answers), `flux` (Kustomizations with
dependencies, sources, git sync), `renovate`, `tools`, `bootstrap`, `errors`.

`live.json` (schema 1): nodes (Talos and kubelet versions, kernel, Ready),
Flux Kustomization/HelmRelease readiness, pods not Running per namespace,
certificate expiry dates, Ceph health summary, CNPG primary and phase,
LoadBalancer Services with assigned IPs, Cilium L2 lease holders (which node
answers for each LB IP), generated time. No secret values, ever.

## 7. Secrets and the public repo

The repo is public. The page and `state.json` contain nothing that the repo
does not already contain in the open: `turner.haus` is written in `docs/`,
LAN addresses are in the manifests. `${SECRET_DOMAIN_2}` stays a token in the
committed files; the page lets the viewer type the real zone once and keeps
it in their own browser storage. `${SECRET_NFS_IP}` and
`${ONEPASSWORD_CONNECT_HOST}` stay tokens. `live.sh` emits names, versions,
dates and health words only.

## 8. Implementation steps

1. `build-state.py` (done in research), `live.sh`, the Taskfile and the
   Action.
2. `index.html`: tokens and layout, Overview, Design drawings, Flow Explorer
   engine and rail, Current State tables, Refresh with provenance.
3. Publish the artifact; verify the GitHub-connector refresh and the fallback
   order; commit everything on the working branch and push.
4. Write the refresh runbook into this file.

## 9. Decisions (2026-10-24)

1. **Hosting**: all three. The Claude artifact, the committed site deployed
   to GitHub Pages by the `Atlas` workflow, and an in-cluster copy at
   `atlas.turner.haus` on envoy-internal (app `infra/atlas`).
2. **Live facts**: both. `task atlas:live` for a manual snapshot, plus the
   `atlas-live` CronJob, shipped suspended until its token exists.
3. **Secondary zone**: tokenized in git. The page asks for it once (kept in
   the browser); the in-cluster copy reads it from `config.json`, which Flux
   fills in at apply time.
4. **LAN facts**: confirmed as read from the repo: UniFi gateway at
   10.13.37.1 (router, DNS, port-forward), three VMs on one Proxmox host,
   NAS at 10.13.37.60 serving NFS, lab1 at 10.13.37.20 running MinIO, and a
   standalone Tailscale subnet router as the break-glass path.

## 10. Runbook

### Rebuild the declared state

Happens by itself: every push to `main` that touches `kubernetes/`, `talos/`,
`bootstrap/`, `.mise.toml`, `.renovaterc.json5` or `scripts/atlas/` runs
`.github/workflows/atlas.yaml`, which regenerates `state.json` and commits it
when anything but the provenance changed, then deploys the site directory to
GitHub Pages. By hand:

```sh
task atlas:state          # python3 scripts/atlas/build-state.py
git add kubernetes/apps/infra/atlas/app/site/state.json && git commit -m "chore(atlas): refresh state.json"
```

### Collect live facts

```sh
task atlas:live           # read-only kubectl (and talosctl if TALOSCONFIG is set) -> live.json
task atlas:refresh        # both, then git status
git add kubernetes/apps/infra/atlas/app/site/live.json && git commit -m "chore(atlas): refresh live.json" && git push
```

`live.json` holds names, versions, dates, health words and which node holds
each Cilium L2 lease. It never contains Secret values.

### Enable the CronJob

1. Create a fine-grained GitHub personal access token for `tturnerdev/home-ops`
   only, with **Contents: Read and write** and nothing else, 1-year expiry.
2. Put it in 1Password, vault "Home-Ops Default", item `atlas`, field
   `github_token`. external-secrets renders `atlas-live-secret` from it.
3. Set `suspend: false` in `kubernetes/apps/infra/atlas/cronjob/cronjob.yaml`
   and push. It runs at :17 every six hours (America/Los_Angeles) and pushes
   `live.json` only when more than the timestamps changed. Run once by hand:

```sh
kubectl -n infra create job --from=cronjob/atlas-live atlas-live-manual
kubectl -n infra logs -f job/atlas-live-manual
kubectl -n infra delete job atlas-live-manual
```

Rotate the token by replacing the 1Password field; Reloader is not involved
(the CronJob reads the Secret at each run).

### GitHub Pages

The workflow's `configure-pages` step tries to enable Pages with "GitHub
Actions" as the source on its first run. If that step fails for lack of
permission, enable it once in the repository Settings → Pages → Source:
GitHub Actions, then re-run the workflow. The site is served from the
repository root path, so the address is `https://tturnerdev.github.io/home-ops/`.

### The in-cluster copy

Flux applies `infra/atlas`; unifi-dns publishes `atlas.turner.haus` as an A
record to envoy-internal, k8s-gateway serves it to VPN clients. The pod runs
static-web-server from scratch with a read-only root; the three ConfigMaps
(page, state files, config) are a projected volume at `/public`, so a push
that changes any of them is live within about a minute of Flux applying it,
with no restart. Local preview of the same directory: `task atlas:serve`.

### Republish the artifact

```sh
python3 scripts/atlas/bundle.py --unwrap --out /tmp/atlas.html
```

then publish `/tmp/atlas.html` from a Claude session with the `mcp`
capability for the GitHub connector (`get_file_contents`), as the first
publish did. The artifact's own Refresh pulls `main` without a republish;
republishing is only needed when `index.html` itself changes.

### Adding to the page

- A new app, hostname or LoadBalancer address needs nothing: the next
  `state.json` rebuild carries it and the flow explorer picks it up.
- Facts the manifests cannot express (the LAN side, the UniFi port-forward,
  the hand-kept DNS records) live in the `LAN` object and the resolver rules
  near the top of the script in `index.html`.
- New preset flows go in `PRESETS`; new system flows in `systemFlows()`.
- Pin a different image or chart version only through the manifests;
  `platform` in `state.json` is derived, never edited.

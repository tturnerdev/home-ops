# Phase 4 runbook — Talos 1.9.5 / Kubernetes 1.32.3 → Talos 1.13.12 / Kubernetes 1.36.5

Prepared 2026-10-23 from a read-only survey of the cluster and the repo (Flux tracks `main`). Plan only: nothing here had been executed when it was written.
Nothing in this document has been executed. Every command below is for the operator to run; the
"evidence" lines say where a claim comes from.

---

## 0. Executive summary

| # | Step | Reboots | Expected impact | Hold changes (`.renovaterc.json5`) |
|---|------|---------|-----------------|------------------------------------|
| 0 | Pre-flight: tool pins, repo fixes (Flux v2beta2 file, Cilium pool API, Cilium chart source), TopoLVM 16.0.1, Cilium 1.18.15 + Rook 1.18.11 patches, backups, baseline | none | none (rolling restarts of Cilium agents / Rook operator / TopoLVM pods) | none |
| 1 | Talos 1.9.5 → **1.10.9** (K8s stays 1.32.3) | 3 | ~25–40 min window; each node ~5–8 min out; single-replica apps on the drained node restart elsewhere (1–3 min); CNPG switchover when the primary's node drains (seconds) | none |
| 2 | Kubernetes 1.32.3 → **1.33.13**, then Flux 2.8.8, CoreDNS chart 1.49.0, cert-manager 1.21.2, Envoy Gateway 1.9.2 (CRDs first) | none | ~15 min; API blips of seconds | Flux `<2.8.0` → `<2.9.0`; **drop** cert-manager hold; **drop** Envoy Gateway hold; **drop** CoreDNS hold (after Flux 2.8 is live) |
| 3 | Talos 1.10.9 → **1.11.6** (etcd 3.5 → 3.6, containerd 2.1), then Spegel 0.7.4 | 3 | as step 1; etcd storage moves to 3.6 (one-way once all 3 nodes are done) | Spegel `<0.7.0` → `<0.8.0` |
| 4 | Cilium 1.18.15 → **1.19.9**, then Kubernetes 1.33.13 → **1.34.12**, then Flux 2.9.6 | none | Cilium rollout: seconds of datapath reload per node; K8s as step 2 | **drop** Flux hold |
| 5 | Talos 1.11.6 → **1.12.12** (kernel 6.18, KSPP sysctls, hardened API-server ciphers) | 3 | as step 1 | none |
| 6 | Rook 1.18.11 → **1.19.12** (pin Ceph image), Ceph 19.2.3 → **20.2.4 Tentacle** (optional-but-urgent: Squid EOL 2026-10-31), TopoLVM 17.3.0, then Kubernetes 1.34.12 → **1.35.9** | none | Rook: rolling daemon restarts, IO pauses of seconds; Ceph: 15–30 min rolling, no downtime; K8s as step 2 | none |
| 7 | Talos 1.12.12 → **1.13.12** (containerd 2.2.3, new upgrade API with client-side drain), then Spegel 0.8.0 | 3 | as step 1 | **drop** Spegel hold |
| 8 | Rook 1.19.12 → **1.20.9** (ceph-csi-drivers chart becomes mandatory), Cilium 1.19.9 → **1.20.3**, decide ingress-nginx, then Kubernetes 1.35.9 → **1.36.5** | none | as steps 4/6 | none |
| 9 | Rook 1.20.9 → **1.21.0** (OCI tag loses the `v`; StorageClass recreate rule) | none | Rook rolling restarts | none |
| — | Kubernetes **1.37 is not reachable on Talos 1.13**: the Talos 1.13 support matrix stops at 1.36; 1.37 needs Talos 1.14 (1.14.4 is current), and today Cilium (1.20 tested ≤1.36, no 1.21 release), cert-manager 1.21 (≤1.36), Envoy Gateway 1.9 (≤1.36) and Flux 2.9 (≤1.36) all stop at 1.36. Stop at 1.36 for this phase. | | | |

Decisions the user must make are collected in §11.

---

## 1. Current state (verified 2026-10-23, read-only)

* Nodes: `home-ops-01/02/03` = 10.13.38.101/102/103, all control-plane, schedulable, Talos **v1.9.5**, Kubernetes **v1.32.3**, containerd **2.0.3**, kernel 6.12.18 (`kubectl get nodes -o wide`, `talosctl version`).
* etcd **3.5.19**, 3 members, same raft index, DB ~130 MB (`talosctl etcd status/members`).
* Boot: legacy BIOS + GRUB (`/sys/firmware/efi` absent; cmdline `BOOT_IMAGE=/A/vmlinuz talos.platform=metal`). Platform `metal` (Proxmox/QEMU disks). KubeSpan disabled. VIP 10.13.38.82 on `ens18`. KubePrism on 7445 (Cilium uses `k8sServiceHost: 127.0.0.1:7445`). hostDNS with `forwardKubeDNSToHost: true`.
* Image Factory schematic `376567988ad370138ad8b2698212367b8edcb69b5fd68c80be1f2ec7d603b4ba` with **no system extensions** (`talosctl get extensions` lists only the schematic). Installer tags for v1.10.9, v1.11.6, v1.12.12, v1.13.12 exist under both `installer/` and `metal-installer/` (registry HEAD = 200).
* talconfig: legacy `machine.network.interfaces` (deviceSelector by MAC, static IP, VIP), `machine.files` CRI part (`discard_unpacked_layers = false`), kernel modules `dm_thin_pool`/`dm_mod`, kubelet `serializeImagePulls: false` + `nodeIP.validSubnets`, sysctls (inotify, rmem/wmem), NTP, nameservers, `cluster.proxy.disabled`, `coreDNS.disabled`, CNI `none`, etcd `advertisedSubnets`, API server `enable-aggregator-routing`, patch removing `cluster.apiServer.admissionControl`. No `extraKernelArgs`, no `machine.disks`, no `.machine.install.extensions`, no `machine.registries`, no `machine.env`.
* Disks per node: `sda` 275 GB system, `sdb` 1.1 TB Rook OSD (`devicePathFilter: /dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_drive-scsi1`), `sdc` 107 GB TopoLVM VG `topolvm-vg` (thin pool `dm-4`, created out-of-band; Talos does not manage it).
* Storage consumers: 30 PVs on `ceph-block` (RWO RBD), 3 PVs on `topolvm-thin-provisioner` (the three CNPG instances). No RWX volumes. No workload is node-pinned.
* Ceph **19.2.3 Squid**, HEALTH_OK, 3 mon / 2 mgr / 3 OSD (one per host, failure domain host, size 3) / 1 RGW, `require_osd_release squid`, `requireMsgr2: true`, `csi.readAffinity.enabled: true`. mgr modules enabled: dashboard, diskprediction_local, insights, iostat, nfs, prometheus, **restful**, **rook**. `disruptionManagement: {managePodBudgets: true, osdMaintenanceTimeout: 30}`; `mon_osd_down_out_interval = 600`. PDBs: `rook-ceph-osd`, `rook-ceph-mon-pdb`, `rook-ceph-mgr-pdb` (maxUnavailable 1 each). The ceph-csi-operator CRs already exist and are Rook-managed: `operatorconfig/ceph-csi-operator-config`, `driver/rook-ceph.rbd.csi.ceph.com`.
* CNPG operator chart **0.29.1 = CNPG 1.30.1** (upgraded 2026-10-23 in 6f9b6c5, while this survey ran), Barman Cloud plugin 0.15.1 (current). Cluster `postgres`: 3 instances on TopoLVM, `synchronous: {method: any, number: 1}`, `primaryUpdateMethod: switchover`, `nodeMaintenanceWindow: {inProgress: false, reusePVC: true}`. PDBs: `postgres` (minAvailable 1, allowed disruptions 1) and `postgres-primary` (allowed disruptions **0**).
* Dragonfly 3 replicas, operator 1.7.1, PDB maxUnavailable 1, no persistence. CoreDNS 2 replicas, PDB maxUnavailable 1, hostname topology spread, control-plane affinity.
* Flux distribution **2.7.5** via flux-operator/flux-instance **0.61.0**; CRDs still serve `helmreleases/v2beta2` and `ocirepositories/v1beta2`. `kubectl get --raw /metrics | grep apiserver_requested_deprecated_apis` shows only CRD versions: `helmreleases v2beta2`, `ocirepositories v1beta2` (both from kustomize-controller applying git manifests — commit `a57aa01` 2026-10-22 already migrated them; the only remaining v2beta2 manifest is the **uncommitted** `kubernetes/apps/default/homebox/app/helmrelease.yaml`), and `ciliumnodeconfigs v2alpha1` (Cilium's own list call). No removed built-in API is in use.
* Other platform versions: Cilium 1.18.6 (chart from `ghcr.io/home-operations/charts-mirror/cilium`, whose **last published tag is 1.18.6** — the mirror is deprecated for charts that moved to upstream OCI), Spegel 0.6.0, CoreDNS chart 1.44.3, cert-manager v1.20.4, Envoy Gateway v1.8.5 (**EOL 2026-11-08**), TopoLVM chart 15.6.1 (tested Kubernetes 1.30–1.32 only), ingress-nginx chart 4.15.1 (project retired; last tested K8s 1.35), external-dns 1.23, k8s-gateway 4.0.1, Tailscale operator 1.104.2, external-secrets 2.13.0, metrics-server 3.14.0.
* Tool pins (`.mise.toml`): talosctl 1.12.1, talhelper 3.1.2, kubectl 1.35.0, cilium-cli 0.19.0, flux 2.7.5, helm 4.x.
* `talosctl health` (server-side) passes; Rook/CNPG/Flux all Ready.

---

## 2. Compatibility matrix used for the ordering

### Talos ↔ Kubernetes (official support matrix; `docs.siderolabs.com/talos/<ver>/getting-started/support-matrix`, older rows from `website/content/<ver>/introduction/support-matrix.md`)

| Talos | Kubernetes supported | Ships (latest patch) | Upgrade from |
|-------|----------------------|----------------------|--------------|
| 1.9   | 1.27–1.32 | etcd 3.5.21, K8s default 1.32.4 | — |
| 1.10 (1.10.9) | **1.28–1.33** | etcd **3.5.26**, containerd 2.0.x, K8s default 1.33.6, kernel 6.12 | 1.9 |
| 1.11 (1.11.6) | **1.29–1.34** | etcd **3.6.5**, containerd **2.1.x**, K8s default 1.34.1 | 1.10 |
| 1.12 (1.12.12) | **1.30–1.35** | etcd 3.6.14, containerd 2.1.x, K8s default 1.35.8, kernel **6.18** | 1.11 |
| 1.13 (1.13.12) | **1.31–1.36** | etcd 3.6.14, containerd **2.2.3**, K8s default 1.36.3, kernel 6.18 | 1.12 |
| 1.14 (1.14.4) | 1.33–1.37 | etcd 3.7.2, K8s default 1.37.1 | 1.13 (out of scope) |

(etcd/K8s defaults read from `pkg/machinery/constants/constants.go` at each tag; containerd/kernel from the `.0` release notes.)
Talos upgrades are tested only between adjacent minors (latest patch of each). Talos validates the Kubernetes version against its supported range on upgrade, provisioning and config apply (1.11 release notes), so Kubernetes must stay inside the target Talos range at every step. Kubernetes minors cannot be skipped (`talosctl upgrade-k8s` works one minor at a time; kube-apiserver skew policy).

Kubernetes patch targets (latest as of 2026-10-23, kubelet images `ghcr.io/siderolabs/kubelet:<tag>` verified present): **v1.33.13, v1.34.12, v1.35.9, v1.36.5** (v1.37.1 exists but needs Talos 1.14).

### Platform components ↔ Kubernetes (tested/supported windows)

| Component | Current | Window | Target | Window | Must happen |
|-----------|---------|--------|--------|--------|-------------|
| Cilium | 1.18.6 | 1.30–1.33 | 1.19.9 | 1.32–1.35 | before K8s 1.34 |
| Cilium | — | — | 1.20.3 | 1.33–1.36 | before K8s 1.36 |
| Rook | 1.18.8 | 1.29–1.34 | 1.19.12 | 1.30–1.35 (Ceph ≥19.2.0) | before K8s 1.35 |
| Rook | — | — | 1.20.9 | 1.31–1.36 (ceph-csi-operator mandatory) | before K8s 1.36 |
| Rook | — | — | 1.21.0 | ≥1.32 | after 1.20 (Helm users must be ≥1.19.5 before 1.20) |
| Ceph | 19.2.3 Squid (EOL 2026-10-31) | — | 20.2.4 Tentacle (EOL 2027-06-01) | Rook ≥1.19 (≥20.2.2 with readAffinity) | after Rook 1.19 |
| Flux | 2.7.5 | ≥1.32 | 2.8.8 | 1.33–1.35 (min 1.33) | after K8s 1.33 |
| Flux | — | — | 2.9.6 | 1.34(≥1.34.1)–1.36 | after K8s 1.34.1+ |
| cert-manager | 1.20.4 | 1.32–1.35 | 1.21.2 | 1.33–1.36 | after K8s 1.33 |
| Envoy Gateway | 1.8.5 | 1.32–1.35, EOL 2026-11-08 | 1.9.2 | 1.33–1.36, Gateway API v1.6.1 | after K8s 1.33 (CRDs first) |
| CoreDNS chart | 1.44.3 | — | 1.49.0 (CoreDNS 1.14.7) | charts ≥1.45 are cosign-v3-bundle signed → Flux ≥2.8 | after Flux 2.8 |
| Spegel | 0.6.0 | containerd 2.0 | 0.7.4 | containerd ≥2.1 (support for 1.7/2.0 removed) | after Talos 1.11 |
| Spegel | — | — | 0.8.0 | containerd ≥2.2 (hard check in code) **and** `trafficDistribution: PreferSameNode` → K8s ≥1.34 (gate on by default in 1.34, alpha in 1.33) | after Talos 1.13 (K8s will be 1.35) |
| CNPG operator | 1.30.1 (chart 0.29.1) | 1.34–1.36 supported, 1.32/1.33/1.37 tested | — | — | done 2026-10-23 |
| TopoLVM | chart 15.6.1 (0.37.0) | 1.30–1.32 | 16.0.1 (0.40.2) | 1.32–1.34 | before K8s 1.33 (ideally now); 16.1.x and 17.0–17.2 are 1.33–1.35 |
| TopoLVM | — | — | 17.3.0 (0.42.0) | 1.34–1.36 | after K8s 1.34, before 1.35 |
| ingress-nginx | 4.15.1 (v1.15.1) | 1.31–1.35, project retired (no releases after March 2026) | — | — | decision before K8s 1.36 (§11) |

Evidence: Cilium `docs.cilium.io/en/v1.18|v1.19|v1.20/network/kubernetes/compatibility`; Rook release notes v1.18.0/v1.19.0/v1.20.0/v1.21.0 and `rook.io/docs/rook/v1.21/Upgrade/rook-upgrade`; Rook `Upgrade/ceph-upgrade` (Tentacle ≥20.2.1, 20.2.0 read-affinity corruption, disable `rook` mgr module); Ceph releases index (Squid EOL 2026-10-31, Tentacle 20.2.4); Flux v2.8.0/v2.9.0 release notes "Kubernetes compatibility"; cert-manager `docs/releases`; Envoy Gateway `news/releases/matrix`; Spegel v0.7.0 release notes (#1168) and `pkg/oci/containerd/containerd.go` at v0.8.0 (`version.AtLeast("2.2")`), chart `templates/service.yaml` (`trafficDistribution: PreferSameNode`); Kubernetes CHANGELOG-1.33/1.34 (`PreferSameTrafficDistribution`); CNPG `docs/src/supported_releases.md`; TopoLVM README at each chart tag; ingress-nginx README.

---

## 3. Conventions used in the commands

```sh
cd /mnt/c/dev/home-ops
export KUBECONFIG=/mnt/c/dev/home-ops/kubeconfig
export TALOSCONFIG=/mnt/c/dev/home-ops/talos/clusterconfig/talosconfig   # task sets this itself
# zsh: quote globs/brackets; unquoted $vars do not word-split (use "${=VAR}" if you must).
```

* `task talos:generate-config` = `talhelper genconfig` (writes `talos/clusterconfig/*`, gitignored).
* `task talos:apply-node IP=<ip> MODE=auto` = `talosctl apply-config --nodes <ip> --file clusterconfig/kubernetes-home-ops-0N.yaml --mode=auto`.
* `task talos:upgrade-node IP=<ip>` renders `talosctl upgrade --talosconfig=./clusterconfig/talosconfig --nodes=<ip> --image=<talosImageURL>:<talenv talosVersion> --image='<same>' --timeout=10m` (talhelper already adds `--image`; the Taskfile's extra `--image` is a harmless duplicate). The raw form used below is equivalent and lets you choose `--timeout`.
* `task talos:upgrade-k8s` renders `talosctl upgrade-k8s --talosconfig=... --to=<talenv kubernetesVersion> --to '<same>' --nodes=10.13.38.101`.
* `talosctl upgrade` keeps etcd membership and the EPHEMERAL partition on every version in this plan: the upgrade sequencer (1.9.6, 1.10.9, 1.13.12) has no etcd-leave or wipe phase, and the installer logs "system disk wipe on upgrade is not supported anymore, option ignored" when `--preserve` is false. `--preserve/--stage/--force/--insecure` are legacy no-ops (deprecated in 1.13, removed in 1.18).
* `talosctl` client should match the **target** Talos version at each step (bump `.mise.toml` first). A newer client only warns about older servers (`ClientVersionCheck` collects warnings); the 1.13 client falls back to the legacy upgrade API for servers <1.13.
* Flux alerts (`flux-system/ntfy`) will fire during every step; that is expected.

Drain mechanics you will see (source: `pkg/kubernetes/kubernetes.go`, `cmd/talosctl/pkg/talos/nodedrain`, Rook PDB design doc, CNPG `kubernetes_upgrade.md`):

* Talos ≤1.12 (server-side drain): cordon → evict every non-DaemonSet pod with a 60 s grace, retry PDB-blocked (HTTP 429) evictions every 5 s, give up after **5 minutes**, log `WARNING: failed to evict pod` and **reboot anyway**.
* Talos 1.13 (`talosctl upgrade` new API): image is pulled and written first, then **talosctl** cordons+drains (`--drain-timeout`, default 5 m); if the drain times out the command **returns an error before rebooting** — the node stays cordoned with the new image staged; fix the blocker, then `talosctl -n <ip> reboot` (the staged upgrade boots) and uncordon.
* CNPG: the primary's eviction is refused by `postgres-primary` (allowed disruptions 0); the operator sees the node cordoned and performs a switchover ("If a node is to be drained and contains a cluster's primary instance, a switchover happens ahead of the drain"), after which the demoted instance is evicted. The replica pod on the drained node stays `Pending` until the node is back (TopoLVM volume is node-bound; `reusePVC: true` default). With sync `any 1`, the two surviving instances keep accepting writes.
* Rook: when a node is cordoned the operator replaces the shared `rook-ceph-osd` PDB with blocking PDBs (`maxUnavailable 0`) for the other hosts and sets `noout` on the drained host for up to `osdMaintenanceTimeout` (30 min); it restores the shared PDB when all PGs are `active+clean`. Mons: `rook-ceph-mon-pdb` maxUnavailable 1. OSDs are marked `out` only after `mon_osd_down_out_interval` (600 s) unless `noout` is set — set it manually as well (below) in case a reboot takes longer than planned.
* Dragonfly: if the master pod is evicted the operator promotes a replica and repoints the Service selector; un-replicated writes are lost (no persistence, cache only).
* Single-replica apps (`Recreate`) on `ceph-block`: evicted, RBD detaches, pod reschedules on another node: 1–3 min each.
* API VIP 10.13.38.82 follows a healthy control plane; expect a few seconds of API unavailability when the holder reboots; in-cluster clients use KubePrism.

---

## 4. Pre-flight (step 0) — do once, on Talos 1.9.5 / Kubernetes 1.32.3

### 4.1 Tooling

Edit `.mise.toml` (all of these are Renovate-manual by design):

```toml
"aqua:budimanjojo/talhelper" = "3.1.17"   # 3.1.9 added Talos 1.13 machinery; 3.1.17 tracks 1.13.8
"aqua:siderolabs/talos" = "1.10.9"        # bump again at each Talos step
"aqua:kubernetes/kubernetes/kubectl" = "1.33.13"  # keep within ±1 of the server at each step
"aqua:fluxcd/flux2" = "2.9.6"             # CLI ≥2.9.0 is needed for `flux migrate -f` (backward compatible with a 2.7 cluster)
"aqua:cilium/cilium-cli" = "0.20.1"       # 0.20.x knows Cilium 1.20
```

```sh
mise install && talosctl version --client && talhelper --version && kubectl version --client && flux version --client && cilium version --client
```

`kubectl-cnpg` (for deterministic switchovers): download `kubectl-cnpg_1.30.1_linux_x86_64.tar.gz` from the CloudNativePG v1.30.1 release and put `kubectl-cnpg` on `$PATH` (not in the aqua registry).

### 4.2 Repo fixes to merge before anything else

1. `kubernetes/apps/default/homebox/app/helmrelease.yaml` (currently modified, uncommitted) still declares `apiVersion: helm.toolkit.fluxcd.io/v2beta2` → change to `helm.toolkit.fluxcd.io/v2`. Flux 2.8 removes `v2beta2` from the CRD and kustomize-controller would fail to apply it. The committed version on `main` is `v2beta2` too (homebox was left out of a57aa01 because of that uncommitted rewrite), so this must land, with or without the rewrite, before step 2's Flux 2.8. Then check: `flux migrate -v 2.6 -f kubernetes/` (idempotent; should report nothing to do) and `grep -rn 'v2beta2\|v1beta2' kubernetes/ bootstrap/` must be empty.
2. `kubernetes/apps/kube-system/cilium/app/networks.yaml`: change the `CiliumLoadBalancerIPPool` to `apiVersion: cilium.io/v2` (the CRD already serves and stores `v2`; Cilium 1.19 deprecates `v2alpha1` and tells you to migrate before upgrading). Leave `CiliumL2AnnouncementPolicy` on `cilium.io/v2alpha1` (still the only version in 1.20).
3. Cilium chart source: `ghcr.io/home-operations/charts-mirror/cilium` stops at 1.18.6. Switch `kubernetes/apps/kube-system/cilium/app/ocirepository.yaml` `url` to `oci://quay.io/cilium/charts/cilium` (verified: 1.18.6, 1.18.15, 1.19.9, 1.20.3 all resolve) and the same chart in `bootstrap/helmfile.d/01-apps.yaml`. Keep the tag at 1.18.6 in that commit, then bump to **1.18.15** in a second commit (Cilium requires the latest patch before a minor upgrade). Verify after each: `kubectl -n kube-system exec ds/cilium -c cilium-agent -- cilium status --brief`, `kubectl get ciliumloadbalancerippool pool`, LB IPs 10.13.38.53/80/81/83/84 and the `postgres-lb` IP still answer.
4. Rook patch: `kubernetes/apps/rook-ceph/rook-ceph/app/ocirepository.yaml` and `cluster/ocirepository.yaml` `v1.18.8` → `v1.18.11` (tags verified). Watch `kubectl -n rook-ceph get deploy -l rook_cluster=rook-ceph -o jsonpath='{range .items[*]}{.metadata.name}{" rook="}{.metadata.labels.rook-version}{" ceph="}{.metadata.labels.ceph-version}{"\n"}{end}'` until a single rook-version remains and `ceph status` is HEALTH_OK.
5. TopoLVM: `kubernetes/apps/topolvm/topolvm/app/helmrelease.yaml` chart `15.6.1` → `16.0.1` (app 0.40.2, tested K8s 1.32–1.34 — the last chart whose window still includes 1.32; 16.0 breaking change = removal of "delete pods when their PVC is deleted", unused here; `helm template` of 17.3.0 with the repo values rendered cleanly, so 16.x will too). Verify `kubectl -n topolvm get ds,deploy` and `kubectl get sc topolvm-thin-provisioner`.
6. (Optional, saves one manual promote per node) nothing to change for CNPG; keep PDBs enabled as CNPG recommends.

### 4.3 Backups and baseline (repeat the first three before every Talos/Kubernetes step)

```sh
mkdir -p ~/backups/home-ops && \
talosctl -n 10.13.38.101 -e 10.13.38.101 etcd snapshot ~/backups/home-ops/etcd-$(date +%F-%H%M).snapshot
talosctl -n 10.13.38.101,10.13.38.102,10.13.38.103 -e 10.13.38.101 etcd status
talosctl -n 10.13.38.101 -e 10.13.38.101 health                     # server-side, whole cluster
kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph status     # HEALTH_OK, 3 osd up/in, pgs active+clean
kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph versions
kubectl get cluster -n database                                      # 3/3, "Cluster in healthy state"
kubectl get dragonflies -n database                                  # Ready
flux get ks -A && flux get hr -A                                     # all Ready
kubectl get pdb -A
kubectl get --raw /metrics | grep '^apiserver_requested_deprecated_apis'
```

Also take a CNPG on-demand backup (Barman plugin) so a fresh base backup exists: create a `Backup` CR with `method: plugin`, `pluginConfiguration.name: barman-cloud.cloudnative-pg.io`, `cluster.name: postgres`, and wait for `status.phase: completed`.

Keep the generated `talos/clusterconfig/*.yaml` and `talsecret.sops.yaml` backed up as well (the talosconfig cert expires 2027-05-14).

---

## 5. Generic Talos minor procedure (used by steps 1, 3, 5, 7)

Per minor:

```sh
# a) pins and config
#    .mise.toml: talosctl = <target>; mise install
#    talos/talenv.yaml: talosVersion: v<target>
task talos:generate-config                       # talhelper genconfig; must succeed without validation errors
git diff --stat                                   # only talenv.yaml / .mise.toml should change (clusterconfig is gitignored)
```

If `talhelper genconfig` ever fails on `patches/controller/admission-controller-patch.yaml` ("remove operation does not apply") the target Talos no longer generates `cluster.apiServer.admissionControl` by default; drop the patch.

Then, **one node at a time** (order 03 → 02 → 01 is fine; do the node holding the VIP / etcd leader last if you like — `talosctl etcd status` shows the leader):

```sh
IP=10.13.38.103; NODE=home-ops-03
# b) push the new machine config (install.image changes do not reboot)
task talos:apply-node IP=$IP MODE=auto           # or: talosctl -n $IP -e $IP apply-config --mode=auto --file talos/clusterconfig/kubernetes-$NODE.yaml
talosctl -n $IP -e $IP get machineconfig -o yaml | grep 'image: factory'   # shows the new installer tag

# c) protect Ceph for the reboot window
kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph osd set noout

# d) CNPG: if this node hosts the primary, switch over deterministically (optional; CNPG also does it on cordon)
kubectl get cluster postgres -n database -o jsonpath='{.status.currentPrimary}{"\n"}'
kubectl get pods -n database -l cnpg.io/cluster=postgres -o wide
kubectl cnpg promote postgres <instance-on-another-node> -n database     # only if the primary is on $IP
kubectl cnpg status postgres -n database

# e) upgrade (talosctl client == target version)
talosctl -n $IP -e $IP upgrade \
  --image factory.talos.dev/installer/376567988ad370138ad8b2698212367b8edcb69b5fd68c80be1f2ec7d603b4ba:v<target> \
  --wait --timeout 15m
# step 7 (talosctl 1.13) adds client-side drain; prefer:  --drain-timeout 10m
```

Verification per node (all must pass before the next node):

```sh
talosctl -n $IP -e $IP version | grep Tag
talosctl -n $IP -e $IP get machinestatus -o yaml | grep -E 'stage|ready'   # running / ready: true
talosctl -n $IP -e $IP get extensions                                    # schematic only, unchanged
talosctl -n $IP -e $IP etcd status                                       # member back, raft index converging, no errors
kubectl get node $NODE -o wide                                           # Ready, OS-IMAGE Talos (v<target>), not SchedulingDisabled
kubectl get pods -A --field-selector spec.nodeName=$NODE | grep -vE 'Running|Completed'
kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph status           # wait for HEALTH_OK, all OSDs up/in, pgs active+clean
kubectl -n rook-ceph get pdb                                             # blocking rook-ceph-osd-host-* PDBs gone again
kubectl get cluster postgres -n database                                 # 3/3 ready (the replica rejoins once the node is back)
kubectl get dragonflies -n database
kubectl -n kube-system exec ds/cilium -c cilium-agent -- cilium status --brief
talosctl -n 10.13.38.101 -e 10.13.38.101 health
```

After the last node: `ceph osd unset noout`; `talosctl -n 10.13.38.101,10.13.38.102,10.13.38.103 -e 10.13.38.101 version | grep Tag`; `kubectl get nodes -o wide`; `talosctl -n <each> images list | head` (images preserved); commit `talenv.yaml` + `.mise.toml`.

Rollback (per node): `talosctl -n $IP -e $IP rollback` boots the previous A/B image (automatic if the new image fails to boot). Caveats: after **step 3** (etcd 3.6) a rollback of a node is only clean while at least one member is still on 3.5; once all three run 3.6 the cluster storage version is 3.6 and a 1.10 image (etcd 3.5) will not start etcd — use `talosctl etcd downgrade` (Talos 1.11 API that mirrors `etcdctl downgrade enable 3.5`) before rolling back, or restore the pre-step snapshot (`talosctl etcd recover`, then re-bootstrap per the Talos disaster-recovery guide). Kubernetes is never downgraded by `talosctl rollback` (kubelet image comes from the machine config), so Talos rollback is safe with respect to Kubernetes as long as the old Talos still supports the running Kubernetes minor (true at every step here).

---

## 6. Generic Kubernetes minor procedure (used by steps 2, 4, 6, 8)

```sh
# pins: .mise.toml kubectl = <target>; talos/talenv.yaml kubernetesVersion: v<target>
talosctl -n 10.13.38.101 -e 10.13.38.101 upgrade-k8s --to v<target> --dry-run
```

The dry run prints "checking for resource APIs to be deprecated in version …" and lists any resource stored in an API version that the target removes. For 1.33–1.36 the Kubernetes deprecation guide lists **no removals**, so expect an empty table (built-in APIs); CRD versions are not in scope of that check.

```sh
talosctl -n 10.13.38.101 -e 10.13.38.101 upgrade-k8s --to v<target>      # == task talos:upgrade-k8s
```

What happens (Talos "Upgrading Kubernetes" guide): images pre-pulled; each control-plane node's machine config is patched with the new kube-apiserver / controller-manager / scheduler images (static pods restart one node at a time — seconds of API unavailability each, the VIP may move); kube-proxy is disabled here so that step is skipped; kubelet is updated on every node (kubelet restart only, running pods are untouched); bootstrap manifests re-applied (from Talos 1.13: server-side apply with inventory and pruning of resources Talos no longer manages). "If the command fails for any reason, it can be safely restarted".

Verification:

```sh
kubectl get nodes -o wide                      # VERSION v<target> on all three
kubectl version
talosctl -n 10.13.38.101 -e 10.13.38.101 get machineconfig -o yaml | grep -E 'kube-(apiserver|controller-manager|scheduler|proxy)|kubelet:v'
flux check && flux get ks -A && flux get hr -A
kubectl get --raw /metrics | grep '^apiserver_requested_deprecated_apis'
kubectl -n kube-system exec ds/cilium -c cilium-agent -- cilium status --brief
kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph status
kubectl get cluster -n database
```

Because `upgrade-k8s` patches the **live** machine config, re-run `task talos:generate-config` and `talosctl -n <ip> -e <ip> apply-config --mode=auto --dry-run --file talos/clusterconfig/kubernetes-home-ops-0N.yaml` for each node to confirm talhelper's output now matches (no diff) — this keeps the next `apply-node` from silently downgrading component images (Talos docs warn about exactly this drift).

Rollback: Kubernetes does not support downgrading kube-apiserver across a minor once storage versions move; treat the pre-step etcd snapshot as the only rollback (`talosctl etcd recover` + bootstrap), and lean on `--dry-run` plus the component windows in §2 to avoid needing it.

---

## 7. Step details

### Step 1 — Talos 1.9.5 → 1.10.9 (Kubernetes stays 1.32.3; 1.10 supports 1.28–1.33)

Breaking/notable changes for THIS config (Talos v1.10.0 release notes; 1.10 support matrix):

* cgroups v1 dropped — irrelevant (Talos 1.9 already runs cgroups v2; kubelet 1.35 later requires it anyway).
* Fresh UEFI installs move to systemd-boot/UKI; "Upgrades to Talos 1.10 will preserve the existing bootloader (GRUB for non-secureboot)" — these nodes are BIOS/GRUB, nothing changes. `extraKernelArgs` would need an Image Factory schematic from 1.10 on; none are used.
* `.machine.install.extensions` has no effect (not used; schematic has no extensions). Root filesystem now unified `/usr` — only matters for extensions (none).
* `.machine.disks` deprecated in favour of `UserVolumeConfig` — not used (TopoLVM VG and the OSD disk are unmanaged).
* iSCSI initiator / NVMe host NQN files are now derived from node identity — no iSCSI/NVMe-oF here.
* `cluster.apiServer.authorizationConfig` ordering — not used.
* etcd 3.5.26 (minor patch bump), containerd 2.0.x (Spegel 0.6 stays), Linux 6.12.
* talconfig edits: **none** (only `talenv.yaml`).

Run §5 for all three nodes. Expected window ~25–40 min, impacts as in §3.

### Step 2 — Kubernetes 1.32.3 → 1.33.13, then the 1.33-gated platform upgrades

Kubernetes 1.33 notes: no removed APIs; urgent notes concern CSI driver `IsLikelyNotMountPoint` semantics, scheduler plugin renames and `ReduceDefaultCrashLoopBackOffDecay` (opt-in) — nothing to change. `PreferSameTrafficDistribution` is alpha/off (matters for Spegel 0.8 later). Cilium 1.18 (≤1.33), Rook 1.18 (≤1.34), CNPG 1.30 (tested on 1.33), TopoLVM 16.0.1 (1.32–1.34), ingress-nginx 1.15 (≤1.35) all cover 1.33.

Run §6 with `--to v1.33.13`. Then, in this order:

1. **Flux 2.7.5 → 2.8.8** (needs K8s ≥1.33). Confirm no `v2beta2/v1beta2` manifests remain (§4.2.1). Edit `kubernetes/apps/flux-system/flux-instance/app/helmrelease.yaml` and `helm/values.yaml`: `instance.distribution.version: 2.8.8`. The operator (0.61.0 ≥ 0.53 required) upgrades controllers and CRDs and migrates stored API versions in-cluster. Verify: `kubectl -n flux-system get fluxinstance,fluxreport`, `flux check`, `kubectl get crd helmreleases.helm.toolkit.fluxcd.io -o jsonpath='{.spec.versions[*].name}'` (only `v2`), `flux get ks -A`, `flux get hr -A`. Rollback: the operator cannot roll a distribution back a minor — fix forward (reason this is gated on a healthy 1.33 first).
2. **CoreDNS chart 1.44.3 → 1.49.0** (CoreDNS 1.14.7). `kubernetes/apps/kube-system/coredns/app/helmrelease.yaml` tag + `bootstrap/helmfile.d/01-apps.yaml` + `bootstrap/helmfile.yaml`. The OCIRepository has `verify.provider: cosign`; charts ≥1.45 ship cosign v3 bundles, which source-controller reads from Flux 2.8 ("Support for Cosign v3 for verifying OCI artifacts"). `helm template` of 1.49.0 with the repo values renders the `kube-dns` Service at 10.43.0.10, 2 replicas, PDB maxUnavailable 1 and the hostname spread unchanged. Verify `flux get ocirepository coredns -n kube-system` (verified), `kubectl -n kube-system rollout status deploy/coredns`, `kubectl run -it --rm dns-test --image=busybox:1.36 --restart=Never -- nslookup kubernetes.default`. Rollback: tag back to 1.44.3.
3. **cert-manager v1.20.4 → v1.21.2** (K8s 1.33–1.36). `ocirepository.yaml` tag + both bootstrap helmfiles. 1.21 breaking changes (release notes): Helm values `prometheus.servicemonitor.targetPort/path` and `prometheus.podmonitor.path` removed (not set here; `helm template` with the repo values passes the chart's strict schema), default `tokenrequest` Role removed (not used), `cert-manager-edit` aggregate role loses create on challenges/orders (not used). Verify `kubectl get clusterissuer letsencrypt-production` Ready, `kubectl get certificate -A` Ready, `kubectl -n cert-manager logs deploy/cert-manager --since=10m | grep -i error`.
4. **Envoy Gateway v1.8.5 → v1.9.2** (K8s 1.33–1.36; Gateway API bundle v1.6.1; 1.8 is EOL 2026-11-08). CRDs first, exactly as the comment in `kubernetes/apps/network/envoy-gateway/app/ocirepository.yaml` says:
   ```sh
   helm template envoy-gateway oci://mirror.gcr.io/envoyproxy/gateway-helm --version v1.9.2 -n network --include-crds --no-hooks \
     | yq ea 'select(.kind == "CustomResourceDefinition")' | kubectl apply --server-side --force-conflicts -f -
   ```
   (the render contains 41 CRDs incl. `gateways.gateway.networking.k8s.io` at bundle-version v1.6.1). Then bump the tag in `ocirepository.yaml` and `bootstrap/helmfile.d/00-crds.yaml`. Verify `kubectl get gateway -n network` (Programmed=True), `kubectl get httproute -A` (Accepted), `kubectl -n network rollout status deploy/envoy-gateway`, the envoy-internal/-external Deployments roll (2 replicas each), curl an HTTPRoute host through 10.13.38.83/84.
5. **Done 2026-10-23 (6f9b6c5), skip.** ~~CNPG operator chart 0.26.0 → 0.29.1 (CNPG 1.27.0 → 1.30.1)~~ before K8s 1.34 (1.27 is EOL and ends at 1.33; 1.30 is tested on 1.33 and supported on 1.34–1.36). Read the 1.30.0 upgrade notes (operator-side SCRAM password encoding `CVE-2026-55765`, `search_path` pinning `CVE-2026-55769`, Lease-based primary election, instance-manager status-port auth); "Most versions are directly upgradable". Edit `kubernetes/apps/database/cloudnative-pg/operator/helmrelease.yaml` version `"0.29.1"` (`helm template` with the repo values renders `cloudnative-pg:1.30.1`). The operator then rolls the instance manager through all three instances and finishes with a **switchover** (`primaryUpdateMethod: switchover`) — a write pause of seconds. Verify `kubectl get cluster -n database` healthy, `kubectl cnpg status postgres -n database`, `kubectl get backup -n database` (next scheduled backup completes), apps using `postgres-lb` reconnect. Barman plugin 0.15.1 is already the current release.
6. **Hold changes** in `.renovaterc.json5`: `Hold Flux on 2.7` → `allowedVersions: '<2.9.0'` (rename to "Hold Flux on 2.8"); delete `Hold cert-manager on 1.20`; delete `Hold Envoy Gateway on 1.8`; delete `Hold CoreDNS chart below 1.45`. Note: Renovate's `aqua:fluxcd/flux2` CLI pin is under the same Flux rule.

Expected impact: K8s ~15 min with API blips; CNPG one switchover; everything else rolling/no downtime.

### Step 3 — Talos 1.10.9 → 1.11.6 (K8s 1.33 is inside 1.29–1.34), then Spegel 0.7.4

Breaking/notable (Talos v1.11.0 release notes):

* **etcd 3.5.26 → 3.6.5**. A mixed cluster runs the 3.5 protocol until all members are 3.6; "The cluster is only considered upgraded once all of its members are upgraded"; after that "binary rollback is no longer possible" — only `talosctl etcd downgrade` (new in 1.11) or a snapshot restore. Upstream etcd's 3.6 upgrade page now says 3.5 members should be on ≥3.5.32 (fixes for upgrade blockers landed in 3.5.24–3.5.32); Talos 1.10.x only ever shipped up to 3.5.26 and the 1.10 → 1.11 path is the one Sidero tests, so: take the snapshot, upgrade one node, check `talosctl etcd status` (no `ERRORS`, raft index converging) and `talosctl -n <ip> logs etcd | tail` before the next node. Talos never enabled the etcd v2 API, so the v2store checks do not apply.
* Kubernetes version validation now also runs on config apply; image references must carry a tag (they do: `ghcr.io/siderolabs/kubelet:v1.33.13`).
* containerd **2.1.x** (Spegel 0.7 prerequisite), runc 1.3, lvm2 2.03.33 (host-side only; TopoLVM brings its own lvm in the pod with `LVM_SYSTEM_DIR=/tmp`).
* IMA removed (the current cmdline carries `ima_*` args from the old kernel; they vanish with the new boot entry — harmless).
* BOOT partition grows to 2 GiB on **new** installs only; upgrades keep the current layout. Raw/existing user volumes, swap/zswap, early inline config, `talosctl disk wipe` in maintenance mode — none used.
* talconfig edits: **none**.

Run §5 for all three nodes. Then **Spegel 0.6.0 → 0.7.4**: `kubernetes/apps/kube-system/spegel/app/helmrelease.yaml` tag + both bootstrap helmfiles. 0.7 "Remove testing and support for Containerd 1.7 and 2.0" (#1168) — fine on 2.1. Values keys (`service.registry.hostPort`, `spegel.containerdSock`, `spegel.containerdRegistryConfigPath: /etc/cri/conf.d/hosts`, `serviceMonitor`) all still exist in 0.7.4. Verify `kubectl -n kube-system rollout status ds/spegel`, `kubectl -n kube-system logs ds/spegel --since=5m | grep -iE 'error|containerd'`, and that an image pull on a node still succeeds. Hold change: `Hold Spegel on 0.6` → `allowedVersions: '<0.8.0'` (rename "Hold Spegel on 0.7"; keep the comment that 0.8 needs containerd 2.2 **and Kubernetes 1.34** — the current comment says 1.33, which is wrong: `PreferSameNode` is only valid when the `PreferSameTrafficDistribution` gate is on, and that gate is on by default from 1.34).

### Step 4 — Cilium 1.19.9, Kubernetes 1.33.13 → 1.34.12, Flux 2.9.6

1. **Cilium 1.18.15 → 1.19.9** (tested K8s 1.32–1.35; must precede 1.34). Upgrade notes that apply: `CiliumLoadBalancerIPPool v2alpha1` deprecated (already moved to `v2` in §4.2.2); BGPv1 removed (unused); eBPF host routing auto-enables only with IPsec (unused) — keep `bpf.hostLegacyRouting: true`: Talos issue #10002 ("Cilium 1.16.5 breaks external DNS resolution with forwardKubeDNSToHost") was closed as a known incompatibility between Cilium eBPF host routing and Talos host DNS on `lo`, with the legacy-routing workaround now documented by Cilium; `--unmanaged-pod-watcher-interval` becomes a duration (Helm converts ints); Kafka policy deprecated (unused). Removed options (`--enable-node-port` family, session-affinity flags, recorder, custom-calls, egress multi-home compat) are not in the values. Every key in `helm/values.yaml` still exists in 1.20.3 (checked with `helm show values` and a clean `helm template`), so no values change for 1.19. Bump `ocirepository.yaml` tag (quay source) + `bootstrap/helmfile.d/01-apps.yaml` + `bootstrap/helmfile.yaml`. Rollout: `kubectl -n kube-system rollout status ds/cilium` (rollOutCiliumPods is on), `cilium status`, `kubectl -n kube-system get leases | grep cilium-l2announce`, curl 10.13.38.81/83/84, `kubectl get svc -A --field-selector spec.type=LoadBalancer` (all have IPs). Rollback: revert the tag (tested one minor back).
2. Run §6 with `--to v1.34.12`. 1.34 notes: `PreferSameTrafficDistribution` on by default; kubelet `--cloud-config` removed (Talos-managed kubelet); apiserver/etcd metric label changes (`resource_prefix` → `group`/`resource`) — only matters for custom dashboards; static pods referencing API objects are now rejected (Talos static pods don't).
3. **Flux 2.8.8 → 2.9.6** (K8s ≥1.34.1): same edit as step 2.1. 2.9 removes `image.toolkit.fluxcd.io/v1beta2` and `notification.toolkit.fluxcd.io/v1beta2`; this repo uses `notification…/v1beta3` (Alert/Provider) and `…/v1` (Receiver) and no image automation. Verify as in step 2.1. Hold change: delete `Hold Flux on 2.7`.

### Step 5 — Talos 1.11.6 → 1.12.12 (K8s 1.34 is inside 1.30–1.35)

Breaking/notable (Talos v1.12.0 release notes; "What's new in Talos 1.12"):

* Linux **6.18** (from 6.12). Ceph krbd and dm-thin keep working; nothing in the config references kernel specifics.
* **Feature lock**: `machine.features.rbac`, `machine.features.apidCheckExtKeyUsage`, `cluster.apiServer.disablePodSecurityPolicy` are ignored (locked to true). talhelper still emits them for older targets; harmless.
* `.machine.network` (legacy) is **deprecated but supported**; talhelper keeps generating it from `networkInterfaces`. No action; optional later migration to `LinkConfig`/`HostnameConfig`/`ResolverConfig` documents.
* `.machine.registries` deprecated — unused (`machine.files` CRI part is unaffected).
* kube-apiserver TLS cipher suites hardened (CIS 1.12 list). All API clients here are modern Go (kubectl, Flux, operators, Tailscale, cloudflared); if an old client breaks, extend via `cluster.apiServer.extraArgs.tls-cipher-suites`.
* **KSPP sysctls now default-on** (`pkg/kernel/kspp/kspp.go`): `dev.tty.ldisc_autoload=0`, `dev.tty.legacy_tiocsti=0`, `fs.protected_{symlinks,hardlinks}=1`, `fs.protected_{fifos,regular}=2`, `fs.suid_dumpable=0`, `kernel.kptr_restrict=2`, `kernel.dmesg_restrict=1`, `kernel.perf_event_paranoid=3`, `kernel.randomize_va_space=2`, `kernel.yama.ptrace_scope=2`, **`user.max_user_namespaces=0`**, `kernel.unprivileged_bpf_disabled=1`, `net.core.bpf_jit_harden=2`, `vm.unprivileged_userfaultfd=0`. Cilium runs privileged (unaffected; `bpf_jit_harden=2` adds a small eBPF cost). `max_user_namespaces=0` breaks any pod that creates user namespaces (rootless container tooling inside pods — check dario/n8n sandboxes); override in `talos/patches/global/machine-sysctls.yaml` if needed. Inspect after the first node: `talosctl -n <ip> -e <ip> get kernelparamstatus`.
* etcd image now `registry.k8s.io/etcd` (3.6.14); GRUB `grubUseUKICmdline` defaults to **false** for upgraded installs (legacy cmdline kept — nothing to do, no extraKernelArgs); UEFI boot order untouched (BIOS here); persistent logs under `/var/log`; `nft` binary; force reboot mode (`talosctl reboot --mode force`); kernel module signature enforcement stays on.
* talconfig edits: **none** (optional sysctl override).

Run §5 for all three nodes.

### Step 6 — Rook 1.19.12, Ceph Tentacle, TopoLVM 17.3.0, then Kubernetes 1.34.12 → 1.35.9

1. **Rook 1.18.11 → 1.19.12** (K8s 1.30–1.35; Ceph ≥19.2.0 ✓). Helm breaking change: the cluster chart now defines the Ceph image as top-level `cephImage: {repository, tag}` (`cephClusterSpec.cephVersion` must not be set — it isn't). **Pin it**, because chart defaults change underneath you (1.18.8 → `v19.2.3`, 1.19.12 → `v19.2.6`, 1.20.9 and 1.21.0 → **`v20.2.4`**; an unpinned bump to 1.20 would silently start a Ceph major upgrade). In `kubernetes/apps/rook-ceph/rook-ceph/cluster/helmrelease.yaml` add:
   ```yaml
   cephImage:
     repository: quay.io/ceph/ceph
     tag: v19.2.6      # Squid patch (19.2.7 is latest; Rook 1.19.12's tested default is 19.2.6)
   ```
   Order: operator `app/ocirepository.yaml` tag → wait Ready → cluster `cluster/ocirepository.yaml` tag (+ the pin). Watch `kubectl -n rook-ceph get deploy -l rook_cluster=rook-ceph -o jsonpath=…rook-version/ceph-version…` until one rook-version and `ceph versions` shows 19.2.6 everywhere; `ceph status` HEALTH_OK; `kubectl -n rook-ceph get pods` all Running/Completed. Also bump the toolbox image if Rook doesn't (`kubectl -n rook-ceph get deploy rook-ceph-tools -o jsonpath='{.spec.template.spec.containers[0].image}'`).
2. **Ceph 19.2.6 → 20.2.4 Tentacle** (Squid EOL 2026-10-31; Rook 1.19+ supports Tentacle ≥20.2.1; with `readAffinity` enabled you must be ≥20.2.2 — 20.2.0 corrupts data). Prerequisites from Rook/Ceph docs: HEALTH_OK, no down/recovering/undersized PGs; cluster already on Squid ✓ (`require_osd_release squid`); **disable the `rook` mgr module** ("several known stability issues"; only some dashboard pages go away): set `enabled: false` for `name: rook` in `cephClusterSpec.mgr.modules`; Tentacle **removed the `restful` and `zabbix` mgr modules** — `restful` is enabled on this cluster, so run `kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph mgr module disable restful` first (and check `diskprediction_local`/`insights` still exist after the upgrade: `ceph mgr module ls`). Then set `cephImage.tag: v20.2.4` in a separate commit. The operator refuses if HEALTH_ERR; it upgrades mon → mgr → osd → rgw one daemon at a time (15–30 min, no downtime). Verify `ceph versions` (all 20.2.4), `ceph osd dump | grep require_osd_release` (Rook/Ceph sets `tentacle`; if still `squid`, run `ceph osd require-osd-release tentacle`), `ceph health detail`, PVC provisioning and RGW (CNPG backups to `cnpg-backups-*` still succeed). Ceph cannot be downgraded — this step is forward-only; the Rook docs' note on RGW multisite settings for 19.2.6/20.2.4 does not apply (single zone).
3. **TopoLVM 16.0.1 → 17.3.0** (K8s 1.34–1.36, so it must land while on 1.34 and before 1.35, where 16.0.1's window ends; 17.0 breaking change `image.tag` → `image.reference`, not set here; `helm template` 17.3.0 with the repo values renders the `topolvm-thin-provisioner` default StorageClass unchanged). Verify as in §4.2.5.
4. Run §6 with `--to v1.35.9`. 1.35 notes: kubelet `--pod-infra-container-image` removed (Talos owns kubelet flags); kubelet now **errors on cgroups v1** (Talos is v2-only since 1.10). Rook 1.19 (≤1.35), Cilium 1.19 (≤1.35), CNPG 1.30, TopoLVM 17, cert-manager 1.21, Envoy 1.9, Flux 2.9 all cover 1.35. ingress-nginx 1.15.1's last tested minor is 1.35.

### Step 7 — Talos 1.12.12 → 1.13.12 (K8s 1.35 is inside 1.31–1.36), then Spegel 0.8.0

Breaking/notable (Talos v1.13.0 release notes; 1.13 "Upgrading Talos" page):

* **New `LifecycleService` upgrade API** — `talosctl` 1.13 pulls the image on the node, writes it, then performs the drain itself (`--drain`, `--drain-timeout`, `--no-reboot`), reboots and uncordons. Use `--drain-timeout 10m` so a CNPG switchover never trips the 5 m default; on a drain error the node is left cordoned with the upgrade staged — fix and `talosctl -n <ip> reboot`, then `kubectl uncordon`. `--preserve/--stage/--force/--insecure` are deprecated legacy flags.
* The 1.13 page's `extraKernelArgs`/`grubUseUKICmdline` warning (args not re-applied on GRUB nodes under the new API) does not apply: no `extraKernelArgs`.
* containerd **2.2.3** (Spegel 0.8 prerequisite), etcd 3.6.14, Linux 6.18.24 (Clang/ThinLTO build), runc 1.4, CDI enabled by default, `proc_mem.force_override=never`, dynamic preemption (`preempt=` cmdline).
* `extraArgs` fields accept lists (strings still fine); protobuf shape of `*Configs` resources changed (only matters to custom tooling).
* `EnvironmentConfig` replaces `.machine.env` (unused); `KubeSpanConfig` document (KubeSpan disabled); `ResolverConfig`: nameservers in the machine config now **overwrite** lower layers instead of merging — this config pins 1.1.1.1/1.0.0.1, which is what you get.
* Bootstrap manifests applied with server-side apply + inventory; `upgrade-k8s` prunes resources Talos no longer owns (kube-proxy/CoreDNS are disabled in config, nothing else is Talos-owned).
* NVIDIA/CDI notes, `ImageVerificationConfig`, external volumes, routing rules, VRF, link aliases — unused.
* talconfig edits: **none**.

Run §5 (with `--drain-timeout 10m`). Then **Spegel 0.7.4 → 0.8.0** (containerd ≥2.2 hard check: "containerd version … is too old, version 2.2 or later is required"; K8s 1.35 ≥ 1.34 so `trafficDistribution: PreferSameNode` is accepted). Values: delete `service.registry.hostPort` (0.8 "Remove host port and only depend on prefer same node service", #1313) and `grafanaDashboard` (dashboard removed) — both are ignored by the 0.8.0 chart anyway (clean `helm template` with the repo values; args keep `--containerd-registry-config-path=/etc/cri/conf.d/hosts`). Optional: Cilium honours EndpointSlice traffic-distribution hints only with `loadBalancer.serviceTopology: true` (Cilium kube-proxy-free docs); without it Spegel still works, pulls just aren't guaranteed node-local. Verify the DaemonSet, logs (version validation passes), and `kubectl -n kube-system get svc spegel-registry -o jsonpath='{.spec.trafficDistribution}'`. Hold change: delete `Hold Spegel on 0.6`.

### Step 8 — Rook 1.20.9, Cilium 1.20.3, ingress-nginx decision, then Kubernetes 1.35.9 → 1.36.5

1. **Rook 1.19.12 → 1.20.9** (K8s 1.31–1.36; Helm users must already be ≥1.19.5 ✓). Breaking: "CSI drivers are admin-managed via the ceph-csi-operator. Rook no longer deploys CSI drivers"; the `csi.*` settings leave the operator chart (`csi.enableCephfsDriver`, `enableCephfsSnapshotter`, `enableLiveness`, `cephFSKernelMountOptions` in `app/helmrelease.yaml` become dead keys — remove them; `csi.serviceMonitor.enabled` and `enableDiscoveryDaemon`/`monitoring`/`image.repository` remain). The **`ceph-csi-drivers` chart must be installed** ("otherwise the CSI driver will be in a failed state due to missing service accounts"), order `rook-ceph` → `ceph-csi-drivers` → `rook-ceph-cluster`. It is a classic Helm repo, not OCI: add a `HelmRepository` (`https://ceph.github.io/ceph-csi-operator`) + `HelmRelease ceph-csi-drivers` in `rook-ceph` (chart `ceph-csi-drivers`, version `1.1.0` for Rook 1.21 / the version matching the `ceph-csi-operator` subchart Rook 1.20.9 depends on, `1.0.4`), using Rook's recommended values file (`deploy/charts/ceph-csi-drivers/values.yaml` at the Rook tag) with `drivers.cephfs.enabled: false`, `drivers.nfs.enabled: false`, `drivers.rbd.name: rook-ceph.rbd.csi.ceph.com`, `operatorConfig.namespace: rook-ceph`. Before upgrading, export the Rook-created CRs so settings are preserved ("retrieve these CRs to get your desired settings"): `kubectl -n rook-ceph get operatorconfig ceph-csi-operator-config -o yaml` and `kubectl -n rook-ceph get driver rook-ceph.rbd.csi.ceph.com -o yaml`. Because those objects already exist with the same names and are not Helm-owned, expect helm-controller to refuse with "invalid ownership metadata"; adopt them: `kubectl -n rook-ceph annotate operatorconfig/ceph-csi-operator-config driver/rook-ceph.rbd.csi.ceph.com meta.helm.sh/release-name=ceph-csi-drivers meta.helm.sh/release-namespace=rook-ceph` and `kubectl -n rook-ceph label … app.kubernetes.io/managed-by=Helm`, then reconcile. Verify `kubectl -n rook-ceph get operatorconfig,driver`, `kubectl get csidriver rook-ceph.rbd.csi.ceph.com`, `kubectl -n rook-ceph get pods | grep csi` (ctrlplugin ×2, nodeplugin ×3 Running), a test PVC binds, and `readAffinity` still shows in the CephCluster (`spec.csi.readAffinity` is a CephCluster field and stays). Keep `cephImage.tag` pinned at `v20.2.4` (or `v19.2.6` if Tentacle was deferred).
2. **Cilium 1.19.9 → 1.20.3** (K8s 1.33–1.36). Upgrade notes that apply: `CiliumNodeConfig v2alpha1` removed (none defined); CNI spec 1.0.0 default (Talos CNI `none`, Cilium writes its own conflist); Gateway API ≥1.6.1 only if Cilium's Gateway API were enabled (it isn't; Envoy Gateway owns `gateway.networking.k8s.io`); with `socketLB.hostNamespaceOnly=true` in-cluster NodePort traffic is now balanced at the client's egress (no NetworkPolicies here); Mutual Auth deprecated, proxylib/Kafka removed, `bpf.datapathMode=auto` new (stays veth), `configDriftDetection` on by default. Removed options are not in the values. Procedure/verification as step 4.1; bump cilium-cli pin if not already 0.20.x.
3. **ingress-nginx**: 4.15.1 is the final release of a retired project ("no further releases, no bugfixes, and no updates to resolve any security vulnerabilities") and is tested only up to Kubernetes 1.35. Decide (§11) before 1.36: migrate the remaining `Ingress` objects (internal class default, external via cloudflared) to Envoy Gateway `HTTPRoute`s and retire both controllers, or accept running it untested on 1.36.
4. Run §6 with `--to v1.36.5`. 1.36 notes: kube-controller-manager metric `volume_operation_total_errors` renamed `volume_operation_errors_total` (dashboards only); scheduler PreBind parallelism (plugin authors only). cert-manager 1.21, Envoy 1.9, Flux 2.9, Cilium 1.20, Rook 1.20, CNPG 1.30, TopoLVM 17 all cover 1.36.

### Step 9 — Rook 1.20.9 → 1.21.0

* OCI chart tags **drop the `v`**: `ref.tag: 1.21.0` in both OCIRepositories (`helm show chart oci://ghcr.io/rook/rook-ceph --version 1.21.0` resolves; `v1.21.0` does not).
* msgr2 required by default — already `requireMsgr2: true`. "Default CSI driver container images are no longer set by Rook" (ceph-csi-operator defaults apply; no custom images here). NFS pool change only affects new NFS installs.
* StorageClass rule in the 1.21 guide: "Since a StorageClass's parameters field is immutable, the storage class must be deleted before upgrading … `kubectl delete storageclass ceph-block` … Deleting the StorageClass is safe. Existing PersistentVolumes are unaffected". A `helm template` of 1.21.0 with this repo's values renders `ceph-block` with exactly the parameter keys the live object has, so the Helm upgrade may well succeed without it; only if the `rook-ceph-cluster` HelmRelease fails with `parameters: Forbidden` do: `flux suspend hr rook-ceph-cluster -n rook-ceph`, `kubectl delete storageclass ceph-block`, `flux resume hr rook-ceph-cluster -n rook-ceph` (provisioning is unavailable for the seconds in between; bound PVCs are unaffected).
* Order and verification as step 6.1, including `kubectl -n rook-ceph get operatorconfig,driver` after.

---

## 8. Hold / pin changes by step (recap)

| Step | `.renovaterc.json5` | `.mise.toml` | `talos/talenv.yaml` |
|------|---------------------|--------------|---------------------|
| 0 | — | talhelper 3.1.17, talosctl 1.10.9, kubectl 1.33.13, flux 2.9.6 (CLI), cilium-cli 0.20.1 | — |
| 1 | — | — | talosVersion v1.10.9 |
| 2 | Flux `<2.8.0`→`<2.9.0`; drop cert-manager, Envoy Gateway, CoreDNS holds | — | kubernetesVersion v1.33.13 |
| 3 | Spegel `<0.7.0`→`<0.8.0` (comment: needs Talos 1.13 **and K8s 1.34**) | talosctl 1.11.6 | talosVersion v1.11.6 |
| 4 | drop Flux hold | kubectl 1.34.12 | kubernetesVersion v1.34.12 |
| 5 | — | talosctl 1.12.12 | talosVersion v1.12.12 |
| 6 | — | kubectl 1.35.9 | kubernetesVersion v1.35.9 |
| 7 | drop Spegel hold | talosctl 1.13.12 | talosVersion v1.13.12 |
| 8 | — | kubectl 1.36.5 | kubernetesVersion v1.36.5 |

`docs/renovate.md` lists Talos/Kubernetes/Cilium/Rook/Flux/Spegel/… as list A (manual PRs), so Renovate will only propose the follow-ups; nothing auto-merges in this plan.

---

## 9. Downtime / risk estimates

| Operation | Duration | User-visible impact |
|-----------|----------|---------------------|
| Talos node upgrade (×3 per minor) | 5–8 min per node, 25–40 min per minor | per node: apps on it restart elsewhere (1–3 min each, 20+ single-replica apps spread over 3 nodes ⇒ ~7 per node); one CNPG switchover if the primary was there (seconds); Dragonfly master promotion (seconds, cache loss for un-replicated keys); API VIP blip (seconds); Ceph degraded (1 OSD down) until the node returns — no IO interruption |
| Kubernetes minor | 10–15 min | 3 × seconds of API-server restart; kubelet restarts (pods untouched) |
| Cilium minor | 5–10 min | per-node datapath reload (sub-second to seconds of packet loss), L2 lease hand-offs |
| Rook minor | 10–20 min | mon/mgr/osd/rgw/CSI rolling restarts; IO stalls of seconds; CSI provisioning paused briefly |
| Ceph major | 15–30 min | none (rolling), forward-only |
| Flux minor | 2–5 min | none for apps; reconciles pause |
| CoreDNS / cert-manager / Envoy Gateway / Spegel / TopoLVM | 1–5 min each | none (PDBs / 2 replicas / DaemonSets) |
| CNPG operator minor | 5–10 min | one switchover (seconds of write pause) |

Biggest single risks: etcd 3.5→3.6 one-way transition (step 3, mitigated by snapshot + per-node verification), Ceph major (step 6, forward-only), Rook 1.20 CSI hand-over (step 8, Helm adoption of existing CRs), and running retired ingress-nginx beyond its tested window (step 8 decision).

---

## 10. Evidence index

* Talos release notes: `gh release view v1.10.0|v1.11.0|v1.12.0|v1.13.0 -R siderolabs/talos`; constants per tag (`pkg/machinery/constants/constants.go`); support matrices (`docs.siderolabs.com/talos/v1.13|v1.14/getting-started/support-matrix`, `website/content/v1.10|v1.11/introduction/support-matrix.md`); upgrade guide (`docs.siderolabs.com/talos/v1.13/configure-your-talos-cluster/lifecycle-management/upgrading-talos`); Kubernetes upgrade guide (`docs.siderolabs.com/kubernetes-guides/advanced-guides/upgrading-kubernetes.md`); sequencer/installer/drain code at v1.9.6, v1.10.9, v1.12.12, v1.13.12 (`internal/app/machined/pkg/runtime/v1alpha1/v1alpha1_sequencer.go`, `cmd/installer/pkg/install/install.go`, `pkg/kubernetes/kubernetes.go`, `cmd/talosctl/cmd/talos/upgrade.go|drain.go|upgrade-k8s.go`, `cmd/talosctl/pkg/talos/nodedrain`, `cmd/talosctl/pkg/talos/helpers/checks.go`, `pkg/kernel/kspp/kspp.go`); Talos issue #10002; Image Factory registry HEADs.
* etcd: `etcd.io/docs/v3.6/upgrades/upgrade_3_6`.
* Kubernetes: deprecation guide (no removals 1.33–1.37), CHANGELOG-1.33…1.37 "Urgent Upgrade Notes", `PreferSameTrafficDistribution` entries (1.33 alpha, 1.34 default on).
* Cilium: compatibility pages v1.18/v1.19/v1.20, upgrade guides v1.19/v1.20, L2-announcements and LB-IPAM docs v1.20 (API versions), kube-proxy-free docs (`loadBalancer.serviceTopology`); chart availability (`helm show chart oci://quay.io/cilium/charts/cilium`, mirror tag list); `helm template` 1.20.3 with repo values.
* Rook: release notes v1.18.0–v1.21.0; upgrade guides v1.19/v1.20/v1.21; `Upgrade/ceph-upgrade`, `Upgrade/node-maintenance`, `Upgrade/health-verification`; CephCluster CRD doc; `design/ceph/ceph-managed-disruptionbudgets.md`; chart values per tag (`deploy/charts/rook-ceph(-cluster)/values.yaml`, `Chart.yaml` dependency on `ceph-csi-operator`), ceph-csi-operator Helm index; `helm template` of 1.21.0 charts with repo values; live `ceph` outputs.
* Ceph: releases index (Squid EOL 2026-10-31, Tentacle 20.2.4 2026-08-19), Tentacle release notes (restful/zabbix removal, `require-osd-release tentacle`).
* Flux: v2.8.0/v2.9.0 release notes, discussion #5572 (upgrade procedure), live CRD versions, kube-apiserver audit log attribution of the v2beta2/v1beta2 requests, repo commit `a57aa01`.
* cert-manager: `docs/releases`, v1.21.0 release notes; `helm template` v1.21.2 with repo values (schema).
* Envoy Gateway: compatibility matrix, v1.9.0 release notes, `helm template` v1.9.2 (`--include-crds`).
* CoreDNS: chart tags/release notes 1.45.0, `helm template` 1.49.0 with repo values.
* Spegel: v0.7.0/v0.8.0 release notes, chart values/templates, `pkg/oci/containerd/containerd.go`; `helm template` 0.8.0 with repo values.
* CNPG: `supported_releases.md`, `installation_upgrade.md`, `kubernetes_upgrade.md`; chart appVersions; `helm template` 0.29.1 with repo values; release assets (kubectl-cnpg).
* TopoLVM: README per chart tag, chart release notes 16.0.0/17.0.0, Helm index; `helm template` 17.3.0 with repo values.
* ingress-nginx: README (retirement notice, supported versions table).
* Dragonfly: HA docs (operator reconfigures replication; Service follows the master).
* Live cluster: `kubectl` / `talosctl` read-only outputs captured 2026-10-23.

---

## 11. Decisions the user must make

1. **Stop at Talos 1.13.12 / Kubernetes 1.36.5** (recommended) — Kubernetes 1.37 needs Talos 1.14 and today none of Cilium (1.20 tested ≤1.36; no 1.21 release), cert-manager 1.21 (≤1.36; 1.22 due ~Nov 2026), Envoy Gateway 1.9 (≤1.36) or Flux 2.9 (≤1.36) support it. Revisit as a Phase 5.
2. **ingress-nginx**: migrate to Envoy Gateway before Kubernetes 1.36, or knowingly run a retired, untested controller.
3. **Ceph Tentacle timing** (Squid EOL 2026-10-31): right after Rook 1.19 (this plan) or deferred; either way `rook` and `restful` mgr modules must be disabled first (some dashboard pages disappear).
4. **Cilium chart source**: `oci://quay.io/cilium/charts/cilium` (keeps the OCIRepository pattern; verified) vs. `https://helm.cilium.io` (HelmRepository). The home-operations mirror will prune the 1.18 tags.
5. **Resolved 2026-10-23** (CNPG 1.30.1 is live). ~~CNPG 1.27 → 1.30 directly~~ (documented as directly upgradable; read the 1.30.0 security-change notes) and installing `kubectl-cnpg` for manual switchovers (optional — CNPG switches over automatically on cordon).
6. **etcd 3.6 transition**: accept Talos 1.10.9's etcd 3.5.26 as the starting point (upstream now recommends ≥3.5.32, which no Talos 1.10 ships) with the snapshot/downgrade-API safety net.
7. **KSPP `user.max_user_namespaces=0`** (Talos 1.12): confirm no workload needs user namespaces, or add a sysctl override before step 5.
8. **Rook 1.20 CSI hand-over**: accept Helm adoption of the existing OperatorConfig/Driver CRs (annotate/label) rather than letting the driver fail and recreate.
9. Scheduling: each Talos step ≈ one 45-min window with three reboots; Kubernetes/component steps ≈ 30 min each. Steps 1–2 are time-sensitive (Envoy Gateway 1.8 EOL 2026-11-08, Ceph Squid EOL 2026-10-31).

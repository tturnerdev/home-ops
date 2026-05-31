# Investigation: `plex.turner.haus` unreachable — `external.turner.haus` DNS tangle

- **Date:** 2026-05-30
- **Investigator:** Claude Code (Opus 4.8) session, working from `/Users/static/dev/home-ops`
- **Status:** ⚠️ Investigation / documentation only. **No DNS or app-ingress changes were made.** This
  document exists so the findings can be independently reviewed before anyone acts on them.
- **Reported symptom:** `plex.turner.haus:32400/web` → `ERR_ADDRESS_UNREACHABLE`; ping to `10.13.38.81` fails.

> **Read this first if you only read one thing:** Plex itself is healthy. Two *separate, pre-existing*
> DNS misconfigurations combine here, and one of them is **accidentally load-bearing** — naively
> "fixing" `external.turner.haus` to point at the external ingress (`.80`) will fix Plex **but break
> ~14 internal apps** (Sonarr, Radarr, Mealie, Paperless, Homarr, …). Do not repoint that record in
> isolation. See [§7](#7-the-trap-the-wrong-record-is-load-bearing).

---

## 0. TL;DR

| # | Finding | Confidence |
|---|---------|-----------|
| 1 | Plex is **up and healthy**; `http://10.13.38.91:32400/web` works right now. | **Verified** |
| 2 | `plex.turner.haus:32400` is wrong two ways: `:32400` only exists on the Plex LB IP (`.91`), not on a hostname; and the hostname resolves to the wrong ingress. | **Verified** |
| 3 | `external.turner.haus` resolves to **`10.13.38.81` (internal ingress)** on the LAN/Tailscale path, but to **`10.13.38.80` (external ingress)** via the cluster's own `k8s-gateway` (`.53`). Split-horizon DNS is **inconsistent**. | **Verified** |
| 4 | Plex's ingress route lives **only on the external ingress (`.80`)**. The internal ingress (`.81`) returns 404 for it. So resolving to `.81` = broken. | **Verified** |
| 5 | The `external.turner.haus → A 10.13.38.81` record is **orphaned**: external-dns refuses to reconcile it because the ownership TXT doesn't match (`found: "", required: "default"`). | **Verified (log)** |
| 6 | **~14 internal-class apps mis-target `external.turner.haus`** (they should target `internal.turner.haus`). They currently work *only because* the orphaned record happens to point at `.81` = the internal ingress. | **Verified** |
| 7 | Therefore repointing `external.turner.haus` → `.80` in isolation breaks those ~14 apps. The correct fix is two-step and ordered. | **Reasoned from verified facts** |
| 8 | Neither external-dns instance watches Services or Gateways (`sources: ["crd","ingress"]`), so the LB-service `external-dns.../hostname` annotations are **inert**. This matters for who actually publishes `external.turner.haus`. | **Verified** |

---

## 1. IP / name map (ground truth)

Source: `cluster.yaml` (generator vars), `talos/talconfig.yaml`, and live `kubectl get svc -A`.

| IP | Role | Source |
|----|------|--------|
| `10.13.36.1`  | LAN router / default gateway | `talconfig.yaml` routes |
| `10.13.38.53` | `k8s-gateway` (in-cluster split-DNS server) | `cluster.yaml: cluster_dns_gateway_addr` |
| `10.13.38.80` | **external** ingress-nginx | `cluster.yaml: cloudflare_ingress_addr` |
| `10.13.38.81` | **internal** ingress-nginx | `cluster.yaml: cluster_ingress_addr` |
| `10.13.38.82` | **Kube API VIP** (control-plane floating IP) | `cluster.yaml: cluster_api_addr` + `talconfig.yaml vip.ip` |
| `10.13.38.83` | envoy-internal Gateway | `envoy.yaml` |
| `10.13.38.91` | **Plex** LoadBalancer (`:32400`) | `media/plex/app/helmrelease.yaml:88` |
| `.101/.102/.103` | the three Talos nodes | `talconfig.yaml` |

`turner.haus` = `${SECRET_DOMAIN}`. There are two naming anchors:
- `internal.turner.haus` → meant to be the **internal** ingress (`.81`).
- `external.turner.haus` → meant to be the **external** path (external ingress `.80` and/or the Cloudflare Tunnel).

Apps publish a per-app hostname as a **CNAME to one of those two anchors**, chosen via the
`external-dns.alpha.kubernetes.io/target` annotation, and are *served* by whichever ingress matches
their `ingressClassName`. **The bug class in this report is: target anchor and ingress class disagree.**

---

## 2. Plex is healthy (rule out the app)

```console
$ curl -sS -m6 -o /dev/null -w "HTTP %{http_code}\n" http://10.13.38.91:32400/identity
HTTP 200

$ nc -z -G3 10.13.38.91 32400 && echo OPEN
OPEN
```

Plex's pod, service, and LB IP (`10.13.38.91:32400`) are all working. `PLEX_ADVERTISE_URL` even
includes `http://10.13.38.91:32400` (`media/plex/app/helmrelease.yaml:43`).

**Immediate access that works today:** `http://10.13.38.91:32400/web`

> Why `ERR_ADDRESS_UNREACHABLE` for `plex.turner.haus:32400`: the hostname resolves to an ingress IP
> (`.81`), and ingress-nginx listens on `80/443` only — nothing listens on `:32400` there, so the
> browser can't connect. `:32400` belongs *only* on `10.13.38.91`. Also note: **ping is the wrong
> test** for any of these IPs — Cilium L2-announced LoadBalancer IPs do not answer ICMP (e.g. `.53`
> fails ping yet serves DNS). `10.13.38.82` answers ping only because it's the real Talos node VIP.

---

## 3. DNS resolution evidence (the split-horizon inconsistency)

```console
# Default resolver on this Mac = 100.100.100.100 (Tailscale MagicDNS → LAN/UniFi)
$ dig +short plex.turner.haus
external.turner.haus.
10.13.38.81                 # ← internal ingress (WRONG for an external-class app)

$ dig +short external.turner.haus            # default resolver
10.13.38.81
$ dig +short external.turner.haus @1.1.1.1   # "public"
10.13.38.81
$ dig +short external.turner.haus @10.13.38.53   # cluster k8s-gateway
10.13.38.80                 # ← external ingress (CORRECT)

$ dig +short internal.turner.haus            # default resolver
10.13.38.81                 # ← internal ingress (correct)
```

**Key observation:** the cluster's own `k8s-gateway` (`.53`) returns the *correct* `.80` for
`external.turner.haus`, but the path the browser actually uses (Tailscale → LAN/UniFi) returns the
*wrong* `.81`. The two horizons disagree.

> Caveat worth re-checking by hand: `@1.1.1.1` also returned the **private** `.81`. That is suspicious
> for a genuinely public Cloudflare answer (Cloudflare can't proxy an RFC-1918 address). Two
> possibilities, not disambiguated here: (a) the LAN intercepts/transparently redirects `1.1.1.1`
> so we never really reached Cloudflare; or (b) there is a real DNS-only (grey-cloud) `A 10.13.38.81`
> record in the Cloudflare zone. **Verify against the Cloudflare dashboard + UniFi DNS records
> directly.** This was not possible from the CLI in-session.

---

## 4. Which ingress actually serves what (Host-header probes)

These bypass DNS by setting the `Host` header explicitly, isolating *routing* from *resolution*:

```console
# Plex's route exists ONLY on the external ingress (.80):
$ curl -skS -m6 -o/dev/null -w "%{http_code}\n" -H "Host: plex.turner.haus" https://10.13.38.80/
401      # reached a backend (Plex/auth) — route EXISTS on .80
$ curl -skS -m6 -o/dev/null -w "%{http_code}\n" -H "Host: plex.turner.haus" https://10.13.38.81/
404      # default backend — NO route on .81

# An internal app (homarr @ dash.turner.haus) is the exact opposite:
$ curl -skS -m6 -o/dev/null -w "%{http_code}\n" -H "Host: dash.turner.haus" https://10.13.38.81/
200      # served on .81
$ curl -skS -m6 -o/dev/null -w "%{http_code}\n" -H "Host: dash.turner.haus" https://10.13.38.80/
404      # NOT served on .80
```

**Conclusion:** external-class apps (Plex) must resolve to `.80`; internal-class apps (Homarr) must
resolve to `.81`. They are served by *different* nginx controllers (different `ingressClassName`).

---

## 5. Root cause A — the orphaned `external.turner.haus` record

From the **unifi-dns** controller logs (`kubectl -n network logs deploy/unifi-dns`):

```
level=debug msg="Skipping endpoint external.turner.haus 0 IN A 10.13.38.81 []
   because owner id does not match, found: \"\", required: \"default\""
```

Interpretation: a record `external.turner.haus → A 10.13.38.81` exists in the provider (UniFi DNS),
but it carries **no external-dns ownership TXT** (`found: ""`). external-dns is configured with
`txtOwnerId: default`, so it will only modify/delete records it owns. It therefore **cannot correct
or remove** this record. It is effectively a stale/hand-created A record that is stuck.

Relevant config:
- `kubernetes/apps/network/external/external-dns/helmrelease.yaml` — provider `cloudflare`,
  `sources: ["crd","ingress"]`, `--ingress-class=external`, `--cloudflare-proxied`, `policy: sync`,
  `txtOwnerId: default`, `txtPrefix: k8s.`, `domainFilters: ["${SECRET_DOMAIN}"]`.
- `kubernetes/apps/network/internal/unifi-dns/helmrelease.yaml` — provider `webhook` (UniFi),
  `sources: ["crd","ingress"]`, **`--ingress-class=internal` AND `--ingress-class=external`**,
  `policy: sync`, `txtOwnerId: default`.
- `kubernetes/apps/network/external/cloudflared/dnsendpoint.yaml:9-11` — the only in-repo definition
  of `external.turner.haus` is a **CNAME to the Cloudflare Tunnel** (`…cfargotunnel.com`), *not* an
  A record. So the `A 10.13.38.81` did not come from the repo.

---

## 6. Root cause B — internal apps mis-targeting `external.turner.haus`

Live cluster (`kubectl get ingress -A`) cross-referenced with the repo annotations
(`grep -rn external-dns.alpha.kubernetes.io/target kubernetes/apps`). Every row below is an ingress
whose **class is `internal`** but whose **DNS target is `external.turner.haus`** — a mismatch:

| App | ns | class | target | repo source |
|-----|----|-------|--------|-------------|
| echo-internal | default | internal | **external** | `default/echo-internal/app/helmrelease.yaml:85` |
| homarr | default | internal | **external** | `default/homarr/app/helmrelease.yaml:64` |
| homebox | default | internal | **external** | `default/homebox/app/helmrelease.yaml:85` |
| homepage | default | internal | **external** | `default/homepage/app/helmrelease.yaml:98` |
| mealie | default | internal | **external** | `default/mealie/app/helmrelease.yaml:74` |
| n8n | default | internal | **external** | `default/n8n/app/helmrelease.yaml:149` |
| paperless-ngx | default | internal | **external** | `default/paperless-ngx/app/helmrelease.yaml:117` |
| photoprism | default | internal | **external** | `default/photoprism/app/helmrelease.yaml:109` |
| overseerr | media | internal | **external** | `media/overseerr/app/helmrelease.yaml:65` |
| prowlarr | media | internal | **external** | `media/prowlarr/app/helmrelease.yaml:85` |
| radarr | media | internal | **external** | `media/radarr/app/helmrelease.yaml:84` |
| readarr | media | internal | **external** | `media/readarr/app/helmrelease.yaml:87` |
| sabnzbd | media | internal | **external** | `media/sabnzbd/app/helmrelease.yaml:74` |
| sonarr | media | internal | **external** | `media/sonarr/app/helmrelease.yaml:81` |

That's **14 apps**. The clearest tell that this is copy-paste drift, not intent: **`echo-internal`** —
an app literally named *internal* — targets `external.turner.haus`.

For contrast, the apps that get it **right** (class `internal` → target `internal.turner.haus`):
`infra/ntfy:46`, `infra/organizr:49`, `infra/podinfo2:47`, `infra/podinfo3:46`, `default/podinfo:46`,
`default/httpbin:45`, `default/httpbin2:39`. And genuinely external apps correctly target external:
`media/plex:128`, `default/echo:85`, `flux-system/flux-instance/app/ingress.yaml:7`,
`media/audiobookshelf:78`, `monitoring/kube-prometheus-stack` (grafana/prometheus/alertmanager).

unifi-dns confirms it is generating these CNAMEs into local DNS, e.g.:
```
msg="Endpoints generated from ingress: media/plex:   [plex.turner.haus 0 IN CNAME external.turner.haus []]"
msg="Endpoints generated from ingress: default/homarr:[dash.turner.haus 0 IN CNAME external.turner.haus []]"
```

---

## 7. The trap: the wrong record is load-bearing

Put §3–§6 together:

```
dash.turner.haus  ──CNAME──▶  external.turner.haus  ──A──▶  10.13.38.81 (internal nginx)  ──▶ Homarr ✓ (works!)
plex.turner.haus  ──CNAME──▶  external.turner.haus  ──A──▶  10.13.38.81 (internal nginx)  ──▶ 404  ✗ (Plex broken)
```

Because the orphaned record points `external.turner.haus → .81`, and the 14 mis-targeted internal
apps are *served* by the internal ingress (`.81`), **those apps work by coincidence.** Genuinely
external apps (Plex, and likely Grafana/Prometheus/echo/flux-webhook publicly) are the only ones that
break, because they need `.80`.

**Therefore:**
- ❌ Fixing **only** the record (`external.turner.haus → .80`) fixes Plex but **breaks all 14 internal
  apps** (they'd resolve to `.80`, which 404s for class `internal` — proven for `dash` in §4).
- ✅ Fixing the **targets first** (the 14 apps → `internal.turner.haus`), letting external-dns
  reconcile, *then* correcting the `external.turner.haus` record is non-breaking.

This is very likely **why nobody noticed for ~300 days** (Plex ingress age is 304d): the majority of
apps are internal and silently rode the wrong record.

---

## 8. Who actually publishes `external.turner.haus`? (architecture note)

This is subtle and easy to get wrong:

- Both external-dns instances use `sources: ["crd","ingress"]`. **Neither watches `service` or
  `gateway-httproute`.** So:
  - The `external-ingress-nginx` **Service** annotation `external-dns.../hostname: external.turner.haus`
    (`external/ingress-nginx/helmrelease.yaml:29`) is **inert** — nothing consumes it. So nginx does
    **not** publish the `external.turner.haus` A record.
  - The `envoy-external` **Gateway/Service** `external-dns` annotations are likewise inert today.
  - The `postgres-lb` Service `external-dns.../hostname` is inert too.
- The only in-repo publisher of `external.turner.haus` is the **DNSEndpoint CRD** → `CNAME` to the
  Cloudflare Tunnel (`cloudflared/dnsendpoint.yaml`), consumed by external-dns via the `crd` source
  (Cloudflare side).
- That leaves the **local/UniFi** `external.turner.haus = A 10.13.38.81` record with **no in-repo
  owner** — consistent with the "orphaned, owner-less" log in §5. Its true origin (manual entry in
  UniFi? legacy external-dns run with a different `txtOwnerId`/`txtPrefix`?) was **not determined**
  and should be confirmed in the UniFi controller.

---

## 9. Alternative interpretations / open questions (for the skeptic)

You flagged that this "doesn't seem right." Honest list of things that could change the conclusion,
and how to test each:

1. **Maybe `external.turner.haus` is *intended* to be `.81` locally** (i.e., local split-DNS sends all
   app traffic to one ingress and the names are just labels). → Against this: the template convention
   is `internal.*`→internal ingress, `external.*`→external ingress, and `echo-internal` targeting
   `external` is clearly accidental. But confirm what *you* intended the two anchors to mean.
   *Test:* decide the intended meaning of `external.turner.haus`, then everything else follows.
2. **Maybe external apps are deliberately tunnel-only** (no local hostname access; use direct IPs on
   LAN). → If so, Plex-by-hostname "failing" on LAN is expected, and the real bug is just the 14
   mis-targeted internal apps + the orphaned record. *Test:* from off-LAN, does `https://plex.turner.haus`
   work through the Cloudflare Tunnel? (Tunnel CNAME path, §5.)
3. **Is the `1.1.1.1` answer real or intercepted?** (§3 caveat). *Test:* `dig external.turner.haus @8.8.8.8`
   from a network that does **not** route through your UniFi gateway, and check the Cloudflare zone
   editor directly.
4. **Did external-dns ever own this record?** *Test:* in UniFi DNS, look for a `k8s.external.turner.haus`
   TXT next to the A record. Its absence/mismatch is the blocker in §5.

None of these change findings #1–#6 (all verified); they only affect what the *desired* end state is.

---

## 10. Recommended remediation (NOT performed)

Do these in order; each step is independently safe:

1. **Repoint the 14 mis-targeted internal apps** (table in §6) from `external.${SECRET_DOMAIN}` →
   `internal.${SECRET_DOMAIN}` in their `helmrelease.yaml`. Commit, let Flux + external-dns reconcile.
   After this, those apps resolve via `internal.turner.haus → .81` and **no longer depend on the
   `external.turner.haus` record**.
2. **Clear the orphaned record.** In the **UniFi** DNS controller, delete the `external.turner.haus`
   A record (and any stale `k8s.external.turner.haus` TXT). Also verify the **Cloudflare** zone has no
   conflicting DNS-only `A 10.13.38.81`. With the blocker gone and `policy: sync`, external-dns can
   manage `external.turner.haus` correctly.
3. **Decide the intended meaning of `external.turner.haus`** (§9.1) and make the publishers agree:
   external-class apps should resolve to the external ingress `.80` locally (or the tunnel off-LAN).
4. **Re-test:** `dig +short plex.turner.haus` should yield `.80`; `https://plex.turner.haus` (port 443,
   **no** `:32400`) should load; spot-check 2–3 internal apps still work.

### Quick LAN access in the meantime
`http://10.13.38.91:32400/web` (Plex direct LB IP — verified working).

---

## Appendix — reproduce every check

```bash
export KUBECONFIG=/Users/static/dev/home-ops/kubeconfig

# Plex health
curl -sS -m6 -o/dev/null -w "%{http_code}\n" http://10.13.38.91:32400/identity

# Resolution (note the split horizon)
for r in "" "@1.1.1.1" "@10.13.38.53"; do echo "external.turner.haus $r:"; dig +short external.turner.haus $r; done
dig +short plex.turner.haus ; dig +short internal.turner.haus

# Routing (DNS-independent)
for ip in 10.13.38.80 10.13.38.81; do
  echo -n "plex via $ip -> "; curl -skS -m6 -o/dev/null -w "%{http_code}\n" -H "Host: plex.turner.haus" https://$ip/
done

# Class vs target for every ingress
kubectl get ingress -A -o json | jq -r '.items[] |
  [.metadata.namespace,.metadata.name,.spec.ingressClassName,
   (.metadata.annotations["external-dns.alpha.kubernetes.io/target"]//"(none)")]|@tsv' | column -t

# The orphaned-record log line
kubectl -n network logs deploy/unifi-dns --tail=500 | grep -i 'external.turner.haus.*owner id'
```

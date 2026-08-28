# Tailscale VPN access

Remote access to every app at its real `*.turner.haus` hostname, with valid TLS,
via a subnet router running in-cluster. No per-app configuration: DNS for VPN
clients is served by the existing k8s-gateway CoreDNS straight from Ingress
state, and traffic rides the tunnel only when away from home.

## Architecture

- **Operator + Connector** (`kubernetes/apps/network/tailscale/`): the Tailscale
  Kubernetes operator runs a 2-replica `Connector` subnet router advertising
  **`10.13.32.0/21`**.
- **Why /21 and not the real /22:** the LAN is `10.13.36.0/22`. Advertising the
  *containing* `/21` means a client at home holds a more-specific connected
  route, so longest-prefix match sends traffic directly over the LAN even with
  the VPN up; away from home the `/21` is the only route and traffic uses the
  tunnel. This is Tailscale's documented pattern for LAN-first behavior.
- **Split DNS:** the tailnet is configured (admin console, below) to resolve
  `turner.haus` via **k8s-gateway at `10.13.38.53`**, which answers from live
  Ingress status (internal apps → `10.13.38.81`, external apps → `10.13.38.80`,
  no Cloudflare hairpin). Non-cluster names fall through to the UniFi resolver
  at `10.13.36.1`. LAN clients without VPN keep using UniFi DNS as before.
- Because the `/21` also covers the Kube API VIP (`10.13.38.82`) and
  postgres-lb (`10.13.38.92`), `kubectl` and `psql` work over the VPN with no
  extra setup.

## One-time setup (Tailscale admin console)

1. **ACL policy** (Access controls → Edit file) — add:

   ```json
   "tagOwners": {
     "tag:k8s-operator": ["autogroup:admin"],
     "tag:k8s": ["tag:k8s-operator"]
   },
   "autoApprovers": {
     "routes": {
       "10.13.32.0/21": ["tag:k8s"]
     }
   }
   ```

2. **OAuth client** (Settings → OAuth clients → Generate): scopes
   `Devices: Core` (write), `Keys: Auth Keys` (write), and tag
   `tag:k8s-operator`. Then put the credentials into the sops secret:

   ```sh
   # SOPS_AGE_KEY_FILE is normally set by mise/task; the explicit prefix makes
   # this work in any shell (run from the repo root)
   SOPS_AGE_KEY_FILE=age.key sops edit kubernetes/apps/network/tailscale/app/secret.sops.yaml
   # replace the PLACEHOLDER_ values of client_id / client_secret
   ```

   The operator crash-loops (and the Connector Kustomization stays blocked)
   until real credentials land — that ordering is intentional.

3. **Deploy:** commit and push; Flux installs the operator, then the
   Connector once the operator is Ready. The operator crash-loops on
   placeholder credentials, so step 2 must land in the same (or an earlier)
   push. Expect three new devices in Machines: `home-ops-k8s-operator`,
   `home-ops-connector-0`, `home-ops-connector-1` — the connectors' route
   auto-approved via `autoApprovers`.

4. **Split DNS** (DNS → Nameservers → Add nameserver → Custom):
   nameserver `10.13.38.53`, **Restrict to domain** `turner.haus`.
   Do this after step 3 so the k8s-gateway fallthrough change is live.
   Never add it as a global (unrestricted) nameserver, and leave
   "Override DNS servers" off.

   *Optional:* the secondary domain (`SECRET_DOMAIN_2`, e.g. `sign.`/
   `automate.` hosts) is not served by k8s-gateway. For LAN-direct access to
   it over VPN, add a second split-DNS entry: that domain → `10.13.36.1`
   (UniFi already holds its records via unifi-dns `domainFilters`). Without
   it, those hosts still work over the VPN via public DNS + cloudflared.

5. **Existing subnet-router container:** verify (Machines → the container
   device) that it advertises and is approved for **exactly `10.13.32.0/21`**.
   Identical prefixes make the three routers (container + 2 connector
   replicas) an automatic failover set, and the container remains the
   break-glass path when the cluster is down. A different prefix (e.g. the
   LAN `/22`) would shadow the Connector (more-specific route wins),
   re-break LAN-first routing at home, and prevent failover.

## Verification

```sh
# Connector devices joined and routes approved (admin console → Machines):
kubectl -n network get connector home-lan     # expects ConnectorReady

# From a remote network with VPN up:
dig +short echo.turner.haus                   # expect 10.13.38.80/.81, not a Cloudflare IP
curl -sI https://echo.turner.haus | head -3   # valid LE cert, no tunnel hop
kubectl get nodes                             # API VIP 10.13.38.82 over the tunnel

# At home with VPN up (LAN-first check):
route get 10.13.38.81 | grep interface        # expect en0/LAN, not utun
```

## Notes

- Linux clients must run `tailscale set --accept-routes` (macOS/iOS/Windows
  accept automatically).
- The Connector SNATs: over the VPN, apps see the connector pod/node as the
  client IP — don't build LAN-IP allowlists for VPN traffic.
- iOS has had version-specific bugs with subnet routes overlapping the local
  LAN (tailscale/tailscale#16082); worst case traffic hairpins through the
  connector at LAN speed.
- If the cluster is down, `turner.haus` resolution for VPN clients is down with
  it (split DNS targets k8s-gateway). The standalone container still provides
  the route to reach node IPs / Talos API for recovery.

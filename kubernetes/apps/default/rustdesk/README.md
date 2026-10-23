# RustDesk server

Self-hosted [RustDesk](https://github.com/rustdesk/rustdesk) ID/relay server
(`hbbs` + `hbbr` from [rustdesk-server](https://github.com/rustdesk/rustdesk-server),
open-source edition) at `rdesk.${SECRET_DOMAIN_2}`, plus
[lejianwen/rustdesk-api](https://github.com/lejianwen/rustdesk-api) as the
API server (accounts, synced address books, web admin) that the open-source
binaries lack. Replaces the TrueNAS TrueCharts install (1.1.10-3, host
networking) with a **new key pair**, so every client has to be pointed at the
new server and key.

## Shape

- One app-template pod, three containers, all built from our own source
  mirrors (see *Source forks* below). `hbbs` (ID/rendezvous) and `hbbr`
  (relay) come from `ghcr.io/atrelixtech/rustdesk-server`, a build of the
  lejianwen fork of rustdesk-server with the same `FROM scratch` layout as
  upstream's classic image: no shell, PATH or entrypoint, hence
  `command: ["/usr/bin/hbbs"]`. `api` is `ghcr.io/atrelixtech/rustdesk-api`
  (alpine, one Go binary). All non-root, read-only root fs, no capabilities,
  pulled with the `ghcr-pull` secret (private packages). One pod means an
  image bump on any of the three restarts all of them (a few seconds of
  ID/relay downtime).
- `Service` of type LoadBalancer at **10.13.38.93** (Cilium LB-IPAM + L2
  announcement). RustDesk is raw TCP/UDP, so neither the nginx ingresses nor
  the cloudflared tunnel (HTTP only) can front it — this is the one app on the
  secondary domain that gets its own VIP instead of a tunnel route.
- PVC `rustdesk` (1Gi, ceph-block) at `/data`, the working dir of both
  binaries: `hbbs` keeps peer registrations (id → device uuid + public key) in
  `db_v2.sqlite3` there; `hbbr` reads optional `blacklist.txt` /
  `blocklist.txt` from it. Losing the volume only loses registrations
  (clients re-register on their next heartbeat).
- PVC `rustdesk-api` (1Gi, ceph-block) at `/app/data`: the API's SQLite
  (users, address books, tokens, audit). Separate claim because a
  kubelet-created subPath directory would be root-owned. Losing it loses
  accounts and address books.
- Signing key in sops Secret `rustdesk-secret` (`KEY` = base64 64-byte
  ed25519 secret key, libsodium `seed||public` layout). It is injected as env
  `KEY` into **both** containers: each derives the public key, `hbbs` signs
  with it, and both reject clients that don't present that public key (the
  `-k` semantics; `hbbr`'s upstream default is *no* validation, which would
  make it an open relay). No `id_ed25519*` files are read or written. `KEY_PUB`
  in the same secret feeds the API (`RUSTDESK_API_RUSTDESK_KEY`, handed to
  clients as part of the server config). `externalsecret.yaml` is the staged
  1Password variant (see its header).
- Single replica (`strategy: Recreate`): registrations and hole-punching state
  live in one process.

## Ports

| Port  | Proto   | Server | Purpose                                   |
|-------|---------|--------|-------------------------------------------|
| 21115 | TCP     | hbbs   | NAT type test (`PORT-1`)                  |
| 21116 | TCP+UDP | hbbs   | ID registration / heartbeat / hole punch  |
| 21117 | TCP     | hbbr   | Relay                                     |
| 21118 | TCP     | hbbs   | WebSocket ID server (web client only; also behind `/ws/id` on the API ingress) |
| 21119 | TCP     | hbbr   | WebSocket relay (web client only; also behind `/ws/relay` on the API ingress) |
| 21114 | TCP     | api    | HTTP API + web admin `/_admin/` (LAN only) |

No `-r`/`RELAY-SERVERS`: the relay is on the same hostname at the standard
port, which clients derive from the ID server address. The API is additionally
served with TLS at `https://rdesk-api.${SECRET_DOMAIN_2}` through the external
ingress and the cloudflared tunnel (next section).

## Name resolution and WAN exposure

Split horizon, same model as the other `${SECRET_DOMAIN_2}` apps:

- **LAN**: `dnsendpoint.yaml` is a `DNSEndpoint` A record
  `rdesk.${SECRET_DOMAIN_2} → 10.13.38.93`; unifi-dns (sources `crd`+`ingress`,
  domain filter includes the secondary domain) publishes it to the UniFi
  gateway. The Cloudflare external-dns instance filters on `${SECRET_DOMAIN}`
  and ignores it.
- **WAN** (manual, like the other names on this zone — no cluster token covers
  it):
  1. Cloudflare DNS, zone of `${SECRET_DOMAIN_2}`: `A rdesk → <UniFi WAN IP>`,
     **DNS only (grey cloud)**. A proxied record would only carry HTTP(S) and
     break every RustDesk port.
  2. UniFi Network → Settings → Firewall & Security (or Routing) → Port
     Forwarding: `TCP 21115-21119 → 10.13.38.93` and `UDP 21116 → 10.13.38.93`
     (ports 21118/21119 are optional — web client only). Use the WAN interface
     the A record points at. Do **not** forward 21114: it is plain HTTP, and
     remote clients use the TLS name below instead.
  If the WAN address is dynamic, point UniFi's DDNS at the Cloudflare record
  or expect to update the A record by hand.
- **API over HTTPS**, `rdesk-api.${SECRET_DOMAIN_2}`: an ordinary external
  Ingress (`ingress` in the HelmRelease, cert from `letsencrypt-production`)
  plus a hostname rule in
  `kubernetes/apps/network/external/cloudflared/configs/config.yaml`. LAN:
  unifi-dns publishes the Ingress host → external ingress IP 10.13.38.80.
  WAN (manual): Cloudflare `CNAME rdesk-api → <tunnel-id>.cfargotunnel.com`,
  **proxied**. The certificate is issued by HTTP-01 through the tunnel, so it
  sits at `Ready=False` ("NXDOMAIN") until that CNAME exists; afterwards force
  a retry with `kubectl -n default delete secret rustdesk-api-tls` (re-issued
  within about a minute). Until then the ingress serves the default wildcard
  certificate and clients refuse the HTTPS address.
  Why a sibling name instead of `rdesk.` itself: that name has to stay a plain
  A record to the WAN IP for the raw ports, and this cluster can only issue
  certificates for the secondary domain by HTTP-01 through the tunnel. A TLS
  sidecar on the VIP would also have needed a WAN port-80 forward for ACME.
- **Web client WebSockets**, same hostname: the Ingress also routes `/ws/id`
  and `/ws/relay` to hbbs's and hbbr's WebSocket ports (21118/21119), and
  `RUSTDESK_API_RUSTDESK_WS_HOST=wss://rdesk-api.${SECRET_DOMAIN_2}` tells
  the bundled web client to use them. Without that setting a web client
  served over HTTPS derives `wss://<ID server host>/ws/id`, i.e. port 443 on
  `rdesk.${SECRET_DOMAIN_2}`, where nothing listens — "Failed to connect to
  rendezvous server, wss://rdesk…/ws/id" (hit 2026-10-20). The setting
  reaches the browser through `/webclient-config/index.js`, which upstream
  served without cache headers — Cloudflare caches `.js` by default and the
  first fix was invisible behind a cached copy; our API image sends it with
  `Cache-Control: no-store` (fork commit `f023cfc`). Sessions are relayed for every
  web client (browsers cannot hole-punch); the relay traffic rides the
  tunnel, and the ingress read/send timeouts are raised to an hour so an idle
  session is not cut at nginx's 60 s default.

Why a VIP works for NAT traversal here: Cilium's load balancer runs in DSR
mode (`bpf-lb-mode: dsr`), so `hbbs` sees each client's real public
address/port even when the pod is on a node other than the L2 lease holder.
`externalTrafficPolicy: Cluster` keeps every node able to answer for the VIP.

## Client setup

Settings → Network → ID/Relay server:

- **ID server**: `rdesk.${SECRET_DOMAIN_2}`
- **Relay server**: blank (derived) or `rdesk.${SECRET_DOMAIN_2}:21117`
- **Key**: the public key — read it with
  `sops -d kubernetes/apps/default/rustdesk/app/secret.sops.yaml | yq .stringData.KEY_PUB`,
  or from the server log: `kubectl -n default logs deploy/rustdesk -c hbbs | grep 'Key:'`.
- **API server** (same dialog): `https://rdesk-api.${SECRET_DOMAIN_2}`, which
  works on the LAN and the WAN once its CNAME exists. Left empty, the client
  assumes `http://rdesk.${SECRET_DOMAIN_2}:21114` — the plain VIP port, fine
  for machines that never leave the LAN.

Clients on the LAN resolve the name to the VIP directly; remote clients go
through the port-forward. A client that connects without (or with a wrong)
key gets "Key mismatch".

**Web client**: `https://rdesk-api.${SECRET_DOMAIN_2}/webclient2` (sign in
with an API account; the admin can also share a device to a guest from
`/_admin/`). It takes the ID server and key from the API, connects over
`wss://rdesk-api.${SECRET_DOMAIN_2}/ws/{id,relay}` (see "Name resolution and
WAN exposure") and is always relayed. The controlled side still connects to
`rdesk.${SECRET_DOMAIN_2}:21117` for the relay, so it needs the native
client set up as above.

A camera button (bottom-right, during a session) takes a native-resolution
screenshot of the remote screen, downloads it as PNG and copies it to the
clipboard. The bundled web client ships no such UI and its source is not
rebuildable, so the button is injected by our `rustdesk-api` fork: it drives
the web client's own connection object (`window.curConn`) to send a protocol
`ScreenshotRequest` and catches the `ScreenshotResponse`, the same mechanism
the native client uses. It is appended to the server-rendered
`/webclient-config/index.js` (fork `atrelix_screenshot.go`), so no change to
the prebuilt bundle and no service-worker cache bust. Needs the remote on
1.4.0 or newer; older peers ignore the request and the button reports a
timeout.

Accounts live in the API server. **Login** (Address Book tab, Settings →
Account) signs in against it; the address book, groups and the device list
then sync between that user's clients. Users are created in the web admin at
`/_admin/` on either API address (user `admin`; the generated first-start
password was reset immediately and handed over out of band — change it
there). Self sign-up is off (`RUSTDESK_API_APP_REGISTER=false`). Access to a
device is still the key above plus that device's own permanent password; the
account only unlocks the address book and audit features.

**Why hbbs is the lejianwen fork and not upstream.** Clients since 1.4.1,
when signed in to an account *and* configured with a key, first run a
key-exchange handshake with the ID server so their login token is not sent in
clear (`legacy_secure` in the client's `client.rs`). Only RustDesk Server Pro
and the lejianwen fork implement the server side; the stock open-source
`hbbs` ignores the message, the client waits 18 s and fails with
`Failed to secure tcp: deadline has elapsed` while signing out makes the same
connection work (hit here on 2026-10-19 with upstream 1.1.16). Upstream
closed the client-side fixes
([rustdesk#15743](https://github.com/rustdesk/rustdesk/pull/15743),
[#15809](https://github.com/rustdesk/rustdesk/pull/15809)) and has left the
server-side implementations open
([rustdesk-server#706](https://github.com/rustdesk/rustdesk-server/pull/706),
[#699](https://github.com/rustdesk/rustdesk-server/pull/699), [#691](https://github.com/rustdesk/rustdesk-server/pull/691),
[#689](https://github.com/rustdesk/rustdesk-server/pull/689)); the
rustdesk-api project tracks the same symptom
([lejianwen/rustdesk-api#482](https://github.com/lejianwen/rustdesk-api/issues/482),
[#514](https://github.com/lejianwen/rustdesk-api/issues/514)). The API
server cannot fix it: the handshake runs on hbbs's TCP port and the API is
never part of that connection. Resolution (2026-10-20): hbbs/hbbr are built
from the API project's companion fork
[lejianwen/rustdesk-server](https://github.com/lejianwen/rustdesk-server)
at its tag `v0.1.2` (branch `0.1.x`), which implements the handshake with one
per-process exchange key. Trade-off: it is based on upstream 1.1.14 and lacks
1.1.16's fix for unauthenticated UDP punch-hole reflection, so revisit before
UDP 21116 is forwarded from the internet; building upstream 1.1.16 plus one of
the open PRs stays the follow-up. Rejected alternatives: the client's "Use
WebSocket" switch (skips the handshake but forces relay for every session,
sends the token over plain `ws://`, and stock `hbbs` answers the WebSocket
registration with `NOT_SUPPORT`, so such a machine is unreachable itself);
signing out (no address book); Server Pro. Diagnosed from the client log in
`%AppData%\RustDesk\log\` (no `TCP punch attempt` line after
`rendezvous server:` when signed in) and `hbbr` logging no relay request.

## Key rotation

Rotating invalidates every configured client — do it deliberately.

```sh
# libsodium layout: id_ed25519 = base64(seed||public); regenerate while the
# public key's base64 contains '/' or ':' (upstream does the same, the key is
# embedded in client config strings).
python3 - <<'PY'
import base64
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
while True:
    sk = Ed25519PrivateKey.generate()
    seed, pk = sk.private_bytes_raw(), sk.public_key().public_bytes_raw()
    pub = base64.b64encode(pk).decode()
    if '/' not in pub and ':' not in pub: break
print("KEY     =", base64.b64encode(seed + pk).decode())
print("KEY_PUB =", pub)
PY
cd kubernetes/apps/default/rustdesk/app
sops set secret.sops.yaml '["stringData"]["KEY"]' '"<KEY>"'
sops set secret.sops.yaml '["stringData"]["KEY_PUB"]' '"<KEY_PUB>"'
```

Reloader restarts the pod when the Secret changes. (`rustdesk-utils genkeypair`
from the release zip produces the same two values.)

## Operations

```sh
kubectl -n default get hr rustdesk; kubectl -n default get svc rustdesk
kubectl -n default logs deploy/rustdesk -c hbbs   # "The key is a crypto private key", "Key: <pub>", "Listening on tcp/udp 0.0.0.0:21116"
kubectl -n default logs deploy/rustdesk -c hbbr   # "Listening on tcp :21117"
nc -vz 10.13.38.93 21116 && nc -vz 10.13.38.93 21117
```

- Start-up self-test: `hbbs` keeps sending itself a UDP `RegisterPeer` on
  127.0.0.1:21116 and exits (CrashLoop) if it goes 12 s without a reply —
  a crash loop right after start means the UDP listener is broken, not the
  key.
- Runtime consoles (loopback only, one command per TCP connection; see
  upstream `docs/environment-variables.md`). `hbbs` answers on 21115 — `h`
  lists `relay-servers`, `reload-geo`, `ip-blocker`, `ip-changes`,
  `punch-requests`, `always-use-relay`, `test-geo`; `hbbr` answers on 21117 —
  `h` lists blacklist/blocklist add-remove-list, `limit-speed`,
  `total-bandwidth`, `single-bandwidth`, `downgrade-*`, `usage`. The image has
  no shell, so run them from an ephemeral container in the pod's network
  namespace, keeping stdin open briefly so `nc` is still around for the reply:
  ```sh
  POD=$(kubectl -n default get pod -l app.kubernetes.io/name=rustdesk -o name)
  kubectl -n default debug -it $POD --image=busybox:1.37 --target=hbbr -- sh
  / # (printf 'u'; sleep 2) | nc 127.0.0.1 21117    # hbbr usage
  / # (printf 'ib'; sleep 2) | nc 127.0.0.1 21115   # hbbs ip-blocker table
  ```
  Non-interactive `kubectl debug -i … -- sh -c '…'` ran fine but streamed no
  output here; read it with `kubectl logs $POD -c debugger-<id>`. Ephemeral
  containers stay listed on the pod until the next rollout. Console changes
  are in-memory only; persistent settings go in the HelmRelease env
  (`ALWAYS_USE_RELAY`, `SINGLE_BANDWIDTH`, `TOTAL_BANDWIDTH`, `LIMIT_SPEED`)
  or in `/data/blacklist.txt` (throttled IPs) and `/data/blocklist.txt`
  (refused IPs), which hbbr reads at start.
- There is no admin UI and no user database in the open-source server. Device
  registrations live in the `peer` table of `/data/db_v2.sqlite3` (ID, uuid,
  public key). A reinstalled device that keeps its ID but gets a new uuid is
  refused (`UUID_MISMATCH` in the hbbs log); the fix is deleting its row with
  sqlite3 from a one-off pod that mounts the `rustdesk` claim while the
  deployment is scaled to 0 (RWO volume).
- `hbbs` runs with `HOME=/tmp` on an emptyDir: its daily version check loads
  RustDesk's client-side config library, which insists on writing
  `~/.config/rustdesk/*.toml` and logged two `Failed to store config` errors
  per start on the read-only root fs. Nothing server-side reads that file.
- `RUST_LOG=debug` on a container logs every connection with its source
  address — the quick way to confirm DSR is delivering real client IPs.
- API server: `kubectl -n default logs deploy/rustdesk -c api` (the first
  start prints the generated admin password). Reset it with
  `kubectl -n default exec deploy/rustdesk -c api -- ./apimain reset-admin-pwd '<new>'`.
  Logins are recorded per client IP (`/_admin/` → Login log) and feed a
  per-IP captcha after 3 failures, so the API must see real addresses: on the
  VIP port DSR delivers them; on the HTTPS path envoy-external forwards them
  (X-Forwarded-For, one trusted hop) and `RUSTDESK_API_GIN_TRUST_PROXY=10.42.0.0/16`
  makes the API accept the header. A request from a Tailscale
  node shows the `ts-home-lan` connector pod instead — that is the connector,
  not a bug. Admin file upload (custom client builds) writes under
  `/app/resources/upload` on the read-only root fs and fails; give that path
  a volume if the feature is ever wanted.
- Upgrades: both images are built from our mirrors (next section), so an
  update means merging upstream into the mirror, pushing `prod`, and copying
  the new digest into the HelmRelease; Renovate cannot see the private
  packages. Schema migrations are automatic (hbbs for `db_v2.sqlite3`, the
  API's "Migrating" step at start). Any bump restarts the whole pod.

## Source forks and image builds

Private mirrors in the `atrelixtech` org (GitHub cannot fork a public repo
privately, so these are mirror-pushes, same model as `atrelix-sign` and
`atrelix-automate`); local clones live in `~/dev-wsl/<name>` with `origin` =
the mirror and `upstream` = lejianwen (push URL disabled).

| Mirror | Upstream | Pinned at | Image |
|--------|----------|-----------|-------|
| `atrelixtech/rustdesk-server` | `lejianwen/rustdesk-server` (fork of `rustdesk/rustdesk-server`, hbbs 1.1.14 + signed-in key exchange, MUST_LOGIN, ws) | tag `v0.1.2` = branch `0.1.x` | `ghcr.io/atrelixtech/rustdesk-server` |
| `atrelixtech/rustdesk-api` | `lejianwen/rustdesk-api` | commit `e318f1fd` = upstream tag **v2.6.29** (2025-09-12, "feat: WebClient 1.4.2", the last release that still bundles the web client; the commit is no longer on upstream master after the DMCA history rewrite and is kept as branch `mirror/e318f1fd`; the admin frontend `lejianwen/rustdesk-api-web` is pinned at `3998c2a9` inside the workflow) | `ghcr.io/atrelixtech/rustdesk-api` |

Branch model in both: `master` (+ all upstream branches/tags) = pristine
mirror; `dev` = integration, cut from the pinned point; `prod` = build
trigger. The only enabled workflow is `.github/workflows/atrelix-image.yml`
(every upstream workflow is `disabled_manually`); it runs on push to `prod`
or on dispatch, builds linux/amd64 only (static musl binaries: `cross` for the
Rust server, `musl-gcc` + `CGO_ENABLED=1` for the Go API) and pushes the tags
`prod`, `<version>-atrelix` (`0.1.2-atrelix` / `e318f1f-atrelix`) and
`sha-<commit>`. The HelmRelease pins digests. Packages are private; the
cluster pulls with the `ghcr-pull` secret in `default` (built from a personal
token with `read:packages`). Commits on the mirrors are signed with the
no-reply author like everywhere else; GitHub rejects the company address.

To update: `git fetch upstream && git merge upstream/<branch>` into the mirror
branch, merge the wanted upstream point into `dev` (`--no-ff`), fast-forward
`prod`, wait for the run, then put the new digest into `helmrelease.yaml`.

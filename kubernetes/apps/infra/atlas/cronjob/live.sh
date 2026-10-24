#!/usr/bin/env bash
# Collect read-only facts from the running cluster into live.json for the
# Atlas page. Runs on a workstation (task atlas:live) or in the atlas-live
# CronJob (--push). Needs kubectl and jq; talosctl is optional and only used
# locally. It never reads Secret values.
#
#   live.sh --out <file>   write the JSON to a file (default: stdout)
#   live.sh --push         clone $GIT_REPO, write $LIVE_PATH, commit and push
#                          when anything but the timestamp changed
set -euo pipefail

OUT=""
PUSH=0
GIT_REPO="${GIT_REPO:-https://github.com/tturnerdev/home-ops.git}"
GIT_BRANCH="${GIT_BRANCH:-main}"
LIVE_PATH="${LIVE_PATH:-kubernetes/apps/infra/atlas/app/site/live.json}"
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --push) PUSH=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
for bin in kubectl jq; do
  command -v "$bin" >/dev/null || { echo "missing: $bin" >&2; exit 1; }
done

# kubectl ... -o json, or JSON null when the kind or object is missing.
kj() {
  local out
  if out=$(kubectl "$@" -o json 2>/dev/null) && [ -n "$out" ]; then
    printf '%s' "$out"
  else
    echo null
  fi
}
cond() { # jq helper: status of a condition type (fully parenthesized: it is an object value)
  printf '((((.status.conditions // [])[] | select(.type=="%s") | .status) // "Unknown"))' "$1"
}

nodes=$(kj get nodes | jq '[ (.items // [])[] | {
  name: .metadata.name,
  ip: (([.status.addresses[]? | select(.type=="InternalIP") | .address])[0]),
  ready: (((.status.conditions // [])[] | select(.type=="Ready") | .status) == "True"),
  schedulable: (.spec.unschedulable != true),
  kubelet: .status.nodeInfo.kubeletVersion,
  os: .status.nodeInfo.osImage,
  kernel: .status.nodeInfo.kernelVersion,
  runtime: .status.nodeInfo.containerRuntimeVersion,
  roles: ([.metadata.labels | to_entries[] | select(.key | startswith("node-role.kubernetes.io/")) | .key | sub("node-role.kubernetes.io/"; "")]),
  cpu: .status.capacity.cpu, memory: .status.capacity.memory } ]')

pods=$(kj get pods -A | jq '{
  total: ((.items // []) | length),
  running: ([(.items // [])[] | select(.status.phase == "Running" or .status.phase == "Succeeded")] | length),
  by_namespace: ((.items // []) | group_by(.metadata.namespace) | map({key: .[0].metadata.namespace, value: {total: length, running: ([.[] | select(.status.phase == "Running" or .status.phase == "Succeeded")] | length)}}) | from_entries),
  not_running: [ (.items // [])[] | select(.status.phase != "Running" and .status.phase != "Succeeded") | {
    namespace: .metadata.namespace, name: .metadata.name, phase: .status.phase, node: .spec.nodeName,
    reason: (.status.reason // ([.status.containerStatuses[]?.state | to_entries[]? | .value.reason // empty][0]) // null) } ],
  restarts: ([ (.items // [])[] | {namespace: .metadata.namespace, name: .metadata.name, node: .spec.nodeName,
    restarts: ([.status.containerStatuses[]?.restartCount] | add // 0)} | select(.restarts > 3) ] | sort_by(-.restarts) | .[:12]),
  nodes: ((.items // []) | map(select(.spec.nodeName != null)) | group_by(.spec.nodeName) | map({key: .[0].spec.nodeName, value: length}) | from_entries) }')

ks=$(kj get kustomizations.kustomize.toolkit.fluxcd.io -A | jq "[ (.items // [])[] | {
  namespace: .metadata.namespace, name: .metadata.name, suspended: (.spec.suspend == true),
  ready: $(cond Ready), message: (((.status.conditions // [])[] | select(.type==\"Ready\") | .message) // null),
  revision: .status.lastAppliedRevision } ]")
hr=$(kj get helmreleases.helm.toolkit.fluxcd.io -A | jq "[ (.items // [])[] | {
  namespace: .metadata.namespace, name: .metadata.name, suspended: (.spec.suspend == true),
  ready: $(cond Ready), message: (((.status.conditions // [])[] | select(.type==\"Ready\") | .message) // null),
  chart_version: (.status.history[0]?.chartVersion // .status.lastAttemptedRevision // null),
  app_version: (.status.history[0]?.appVersion // null),
  last_deployed: (.status.history[0]?.lastDeployed // null) } ]")
gitrepo=$(kj -n flux-system get gitrepositories.source.toolkit.fluxcd.io flux-system | jq "{
  revision: .status.artifact.revision, ready: $(cond Ready), last_update: .status.artifact.lastUpdateTime }")

lbs=$(kj get svc -A --field-selector spec.type=LoadBalancer | jq '[ (.items // [])[] | {
  namespace: .metadata.namespace, name: .metadata.name,
  ip: (.status.loadBalancer.ingress[0]?.ip // null),
  ports: [ .spec.ports[] | "\(.port)/\(.protocol)" ],
  external_traffic_policy: .spec.externalTrafficPolicy } ]')
leases=$(kj -n kube-system get leases | jq '[ (.items // [])[] | select(.metadata.name | startswith("cilium-l2announce-")) | {
  service: (.metadata.name | sub("^cilium-l2announce-"; "")), holder: .spec.holderIdentity, renewed: .spec.renewTime } ]')

certs=$(kj get certificates.cert-manager.io -A | jq "[ (.items // [])[] | {
  namespace: .metadata.namespace, name: .metadata.name, secret: .spec.secretName,
  dns_names: .spec.dnsNames, ready: $(cond Ready), not_after: .status.notAfter, renewal_time: .status.renewalTime } ]")

ceph=$(kj -n rook-ceph get cephclusters.ceph.rook.io rook-ceph | jq 'if . == null then null else {
  phase: .status.phase, health: .status.ceph.health, version: .status.version.version,
  last_checked: .status.ceph.lastChecked,
  details: ((.status.ceph.details // {}) | to_entries | map({code: .key, severity: .value.severity, message: .value.message})),
  capacity: .status.ceph.capacity,
  osds: (.status.storage.osd // null) } end')

pg=$(kj -n database get clusters.postgresql.cnpg.io postgres | jq 'if . == null then null else {
  phase: .status.phase, primary: .status.currentPrimary, instances: .status.instances, ready: .status.readyInstances,
  image: .status.image, last_backup: .status.lastSuccessfulBackup, first_recoverability: .status.firstRecoverabilityPoint,
  timeline: .status.timelineID } end')
pg_pods=$(kj -n database get pods -l cnpg.io/cluster=postgres | jq '[ (.items // [])[] | {
  name: .metadata.name, node: .spec.nodeName, role: (.metadata.labels["cnpg.io/instanceRole"] // .metadata.labels.role // null), phase: .status.phase } ]')
df=$(kj -n database get dragonflies.dragonflydb.io dragonfly | jq 'if . == null then null else {phase: .status.phase} end')
df_pods=$(kj -n database get pods -l app=dragonfly | jq '[ (.items // [])[] | {name: .metadata.name, node: .spec.nodeName, role: (.metadata.labels.role // null), phase: .status.phase} ]')

gws=$(kj get gateways.gateway.networking.k8s.io -A | jq "[ (.items // [])[] | {
  namespace: .metadata.namespace, name: .metadata.name,
  addresses: [ .status.addresses[]?.value ], programmed: $(cond Programmed),
  listeners: [ .status.listeners[]? | {name: .name, attached_routes: .attachedRoutes} ] } ]")
routes=$(kj get httproutes.gateway.networking.k8s.io -A | jq '[ (.items // [])[] | {
  namespace: .metadata.namespace, name: .metadata.name, hostnames: (.spec.hostnames // []),
  parents: [ .spec.parentRefs[]?.name ],
  accepted: ((([.status.parents[]?.conditions[]? | select(.type=="Accepted") | .status]) | all(. == "True")) and ((.status.parents // []) | length > 0)) } ]')
connectors=$(kj get connectors.tailscale.com -A | jq "[ (.items // [])[] | {name: .metadata.name, hostname: .status.hostname, routes: .status.subnetRoutes, ready: $(cond ConnectorReady)} ]")

k8s_version=$(kubectl version -o json 2>/dev/null | jq -r '.serverVersion.gitVersion // empty' || true)
mode="workstation"; [ "$PUSH" = 1 ] && mode="cronjob"

talos=null
if [ "$PUSH" = 0 ] && command -v talosctl >/dev/null && [ -n "${TALOSCONFIG:-}" ]; then
  ips=$(echo "$nodes" | jq -r '[.[].ip] | join(",")')
  if [ -n "$ips" ]; then
    ver=$(talosctl -n "$ips" version --short 2>/dev/null | awk '/Tag:/ {print $2}' | sort -u | paste -sd, - || true)
    etcd=$(talosctl -n "${ips%%,*}" etcd status 2>/dev/null | tail -n +2 | awk '{print $1":"$2":"$3}' | paste -sd' ' - || true)
    talos=$(jq -n --arg v "${ver:-}" --arg e "${etcd:-}" '{versions: $v, etcd_status: $e}')
  fi
fi

json=$(jq -n \
  --arg gen "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg by "$mode" --arg kv "$k8s_version" \
  --argjson nodes "$nodes" --argjson pods "$pods" --argjson ks "$ks" --argjson hr "$hr" --argjson gitrepo "$gitrepo" \
  --argjson lbs "$lbs" --argjson leases "$leases" --argjson certs "$certs" --argjson ceph "$ceph" \
  --argjson pg "$pg" --argjson pg_pods "$pg_pods" --argjson df "$df" --argjson df_pods "$df_pods" \
  --argjson gws "$gws" --argjson routes "$routes" --argjson connectors "$connectors" --argjson talos "$talos" \
  '{schema: 1, generated_at: $gen, generated_by: $by, kubernetes_server: $kv,
    nodes: $nodes, pods: $pods,
    flux: {git: $gitrepo, kustomizations: $ks, helmreleases: $hr,
           not_ready: ([($ks[] | select(.ready != "True" and .suspended != true) | "Kustomization \(.namespace)/\(.name)"),
                        ($hr[] | select(.ready != "True" and .suspended != true) | "HelmRelease \(.namespace)/\(.name)")])},
    services_lb: $lbs, l2_leases: $leases, certificates: $certs, ceph: $ceph,
    postgres: ($pg + {pods: $pg_pods}), dragonfly: ($df + {pods: $df_pods}),
    gateways: $gws, httproutes: $routes, tailscale: {connectors: $connectors}, talos: $talos}')

if [ "$PUSH" = 0 ]; then
  if [ -n "$OUT" ]; then
    printf '%s\n' "$json" > "$OUT"
    echo "wrote $OUT ($(echo "$json" | wc -c | tr -d ' ') bytes)" >&2
  else
    printf '%s\n' "$json"
  fi
  exit 0
fi

# --push: commit live.json to the repo when more than the timestamp changed.
: "${GITHUB_TOKEN:?GITHUB_TOKEN is required for --push}"
export GIT_TERMINAL_PROMPT=0
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
# The token goes through a credential helper, never into a URL or the config.
git -c credential.helper='!f() { echo "username=x-access-token"; echo "password=${GITHUB_TOKEN}"; }; f' \
  clone --quiet --depth 1 --branch "$GIT_BRANCH" "$GIT_REPO" "$work/repo"
cd "$work/repo"
mkdir -p "$(dirname "$LIVE_PATH")"
printf '%s\n' "$json" > "$LIVE_PATH"
if git diff --quiet -I '"generated_at"' -I '"renewed"' -I '"last_checked"' -- "$LIVE_PATH"; then
  echo "live.json unchanged apart from timestamps; nothing to push"
  exit 0
fi
git -c user.name="atlas-live" -c user.email="atlas-live@users.noreply.github.com" \
  commit --quiet -m "chore(atlas): refresh live.json" -- "$LIVE_PATH"
git -c credential.helper='!f() { echo "username=x-access-token"; echo "password=${GITHUB_TOKEN}"; }; f' \
  push --quiet origin "HEAD:$GIT_BRANCH"
echo "pushed live.json to $GIT_BRANCH"

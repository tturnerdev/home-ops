#!/usr/bin/env python3
"""Build kubernetes/apps/infra/atlas/app/site/state.json: the cluster's declared state, read from this repo.

Everything here comes from the manifests under kubernetes/, talos/, bootstrap/
and the tool pins, so it runs anywhere the repo is checked out (a laptop, the
GitHub Action) and needs no cluster access. Live facts (node versions, pod
health, certificate expiry, Ceph health) come from
kubernetes/apps/infra/atlas/cronjob/live.sh (task atlas:live).

Usage: python3 scripts/atlas/build-state.py [--out <path>]   (task atlas:state)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required: pip install pyyaml (or `mise install`, the repo venv has it)")

ROOT = Path(__file__).resolve().parents[2]
PRIMARY_DOMAIN = "turner.haus"          # ${SECRET_DOMAIN}; already public in docs/
SECONDARY_TOKEN = "${SECRET_DOMAIN_2}"  # stays a token: the page fills it in locally
# Values of cluster-secrets substitutions that the docs already state in the
# open; anything not listed stays a ${TOKEN}. Override with --var NAME=VALUE.
KNOWN_VARS = {
    "POSTGRES_IPV4": "10.13.38.92",   # docs/tailscale.md
}

# --------------------------------------------------------------------------- helpers

class Loader(yaml.SafeLoader):
    pass

# Tolerate unknown tags (!ENC etc.) and keep going.
Loader.add_multi_constructor("", lambda loader, suffix, node: None)


def _compose_node(self, parent, index):
    # kustomize/yq accept an anchor that is defined twice (the later one wins);
    # PyYAML refuses, so forget the earlier definition first.
    event = self.peek_event()
    anchor = getattr(event, "anchor", None)
    if anchor is not None and not isinstance(event, yaml.events.AliasEvent):
        self.anchors.pop(anchor, None)
    return yaml.composer.Composer.compose_node(self, parent, index)


Loader.compose_node = _compose_node  # type: ignore[assignment]


def json5_to_json(text: str) -> str:
    """Enough JSON5 for .renovaterc.json5: comments, single quotes, bare keys, trailing commas."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c in "\"'":
            q, j, buf = c, i + 1, []
            while j < n and text[j] != q:
                if text[j] == "\\":
                    buf.append(text[j:j + 2]); j += 2; continue
                buf.append(text[j]); j += 1
            body = "".join(buf)
            if q == "'":
                body = body.replace('\\\'', "'").replace('"', '\\"')
            out.append('"' + body + '"'); i = j + 1; continue
        if text.startswith("//", i):
            j = text.find("\n", i); i = n if j < 0 else j; continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2); i = n if j < 0 else j + 2; continue
        m = re.match(r"[A-Za-z_$][\w$]*(?=\s*:)", text[i:])
        if m and (not out or out[-1].rstrip()[-1:] in "{,"):
            out.append('"' + m.group(0) + '"'); i += len(m.group(0)); continue
        out.append(c); i += 1
    j = "".join(out)
    return re.sub(r",(\s*[}\]])", r"\1", j)


def load_docs(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []
    out = []
    try:
        for doc in yaml.load_all(text, Loader=Loader):
            if isinstance(doc, dict):
                out.append(doc)
    except yaml.YAMLError as exc:  # record, don't abort
        ERRORS.append({"file": rel(path), "error": str(exc).splitlines()[0]})
    return out


def rel(p: Path) -> str:
    return str(p.relative_to(ROOT))


def dig(obj, *keys, default=None):
    for k in keys:
        if isinstance(obj, dict) and k in obj:
            obj = obj[k]
        elif isinstance(obj, list) and isinstance(k, int) and k < len(obj):
            obj = obj[k]
        else:
            return default
    return obj


def subst(value, release: str | None = None, app: str | None = None):
    """Resolve the substitutions Flux/Helm would apply to a hostname-ish string."""
    if not isinstance(value, str):
        return value
    v = value
    v = v.replace("${SECRET_DOMAIN/./-}", PRIMARY_DOMAIN.replace(".", "-"))
    v = v.replace("${SECRET_DOMAIN}", PRIMARY_DOMAIN)
    v = v.replace("${SECRET_DOMAIN_2/./-}", "secret-domain-2")
    for k, val in KNOWN_VARS.items():
        v = v.replace("${" + k + "}", val)
    if release:
        v = re.sub(r"\{\{\s*\.Release\.Name\s*\}\}", release, v)
    if app:
        v = v.replace("${APP}", app)
    return v


def split_image(repo: str | None, tag) -> dict:
    tag = str(tag) if tag is not None else None
    digest = None
    if tag and "@" in tag:
        tag, digest = tag.split("@", 1)
    return {"repository": repo, "tag": tag, "digest": digest}


def git(*args) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()
    except Exception:
        return ""


ERRORS: list[dict] = []

# --------------------------------------------------------------------------- walk the repo

def walk_yaml(base: Path):
    for p in sorted(base.rglob("*.yaml")):
        if p.name.endswith(".sops.yaml"):
            continue
        if "/templates/" in str(p):
            continue
        yield p


def collect_objects() -> list[tuple[Path, dict]]:
    objs = []
    for p in walk_yaml(ROOT / "kubernetes"):
        for d in load_docs(p):
            if "kind" in d:
                objs.append((p, d))
    return objs


def namespace_for(path: Path, obj: dict) -> str | None:
    ns = dig(obj, "metadata", "namespace")
    if ns:
        return ns
    parts = path.relative_to(ROOT).parts
    if len(parts) > 2 and parts[0] == "kubernetes" and parts[1] == "apps":
        return parts[2]
    return None


def app_dir_for(path: Path) -> str | None:
    parts = path.relative_to(ROOT).parts
    if len(parts) > 3 and parts[0] == "kubernetes" and parts[1] == "apps":
        return parts[3]
    return None


# --------------------------------------------------------------------------- extractors

def gateway_exposure(parent_name: str | None) -> str | None:
    return {"envoy-internal": "internal", "envoy-external": "external"}.get(parent_name or "")


def routes_from_values(values: dict, release: str, app: str) -> list[dict]:
    routes = []
    for key, r in (dig(values, "route") or {}).items():
        if not isinstance(r, dict):
            continue
        hosts = [subst(h, release, app) for h in (r.get("hostnames") or [])]
        if not hosts and dig(r, "host", "name"):
            hosts = [subst(dig(r, "host", "name"), release, app)]
        parents = r.get("parentRefs") or []
        parent = parents[0] if parents else {}
        rules = r.get("rules") or []
        backends = []
        for rule in rules:
            for b in rule.get("backendRefs") or []:
                backends.append({"identifier": b.get("identifier"), "name": b.get("name"), "port": b.get("port")})
        hp = {}
        for ak, av in (r.get("annotations") or {}).items():
            if ak.startswith("gethomepage.dev/"):
                hp[ak.split("/", 1)[1]] = av
        routes.append({
            "name": key,
            "hostnames": hosts,
            "gateway": parent.get("name"),
            "gateway_namespace": parent.get("namespace"),
            "listener": parent.get("sectionName"),
            "exposure": gateway_exposure(parent.get("name")),
            "backends": backends,
            "homepage": hp or None,
        })
    return routes


def legacy_ingress_from_values(values: dict, release: str, app: str) -> list[dict]:
    out = []
    ing = dig(values, "ingress") or {}
    # app-template shape: ingress.<key>.hosts[]; plain chart shape: ingress.hosts[]
    candidates = ing.values() if all(isinstance(v, dict) and ("hosts" in v or "className" in v) for v in ing.values()) and ing else [ing]
    for i in candidates:
        if not isinstance(i, dict) or not i.get("hosts"):
            continue
        hosts = [subst(h.get("host") if isinstance(h, dict) else h, release, app) for h in i["hosts"]]
        out.append({
            "class": i.get("className"),
            "hostnames": hosts,
            "target": subst(dig(i, "annotations", "external-dns.alpha.kubernetes.io/target")),
            "enabled": i.get("enabled", True),
        })
    return out


def images_from_values(values: dict) -> list[dict]:
    imgs = []
    for cname, c in (dig(values, "controllers") or {}).items():
        if not isinstance(c, dict):
            continue
        for role_key, role in (("containers", "app"), ("initContainers", "init")):
            for name, cont in (c.get(role_key) or {}).items():
                img = dig(cont, "image") or {}
                if img.get("repository"):
                    imgs.append({**split_image(img.get("repository"), img.get("tag")), "controller": cname, "container": name, "role": role})
    return imgs


def services_from_values(values: dict) -> list[dict]:
    svcs = []
    for name, s in (dig(values, "service") or {}).items():
        if not isinstance(s, dict):
            continue
        ports = []
        for pname, p in (s.get("ports") or {}).items():
            if isinstance(p, dict):
                ports.append({"name": pname, "port": p.get("port"), "protocol": p.get("protocol", "TCP")})
        svcs.append({
            "name": name,
            "type": s.get("type", "ClusterIP"),
            "lb_ip": subst(dig(s, "annotations", "lbipam.cilium.io/ips")),
            "ports": ports,
        })
    return svcs


def persistence_from_values(values: dict, app: str) -> tuple[list[dict], list[dict]]:
    claims, nfs = [], []
    for name, p in (dig(values, "persistence") or {}).items():
        if not isinstance(p, dict):
            continue
        if p.get("existingClaim"):
            claims.append({"volume": name, "claim": subst(p["existingClaim"], app=app)})
        elif p.get("type") == "nfs":
            mounts = [m.get("path") for m in (p.get("globalMounts") or []) if isinstance(m, dict)]
            nfs.append({"volume": name, "server": p.get("server"), "path": p.get("path"), "mounts": mounts})
    return claims, nfs


def values_text_flags(values: dict) -> dict:
    blob = json.dumps(values, default=str)
    return {
        "uses_postgres": "postgres-rw.database" in blob or "postgres-lb" in blob or "postgresdb" in blob or "DB_ENGINE\": \"postgres" in blob or "HBOX_DATABASE_DRIVER\": \"postgres" in blob or "postgres-init" in blob,
        "uses_dragonfly": "dragonfly.database" in blob or "dragonfly" in blob.lower(),
    }


def chart_of(hr: dict, oci_index: dict, ns: str) -> dict:
    spec = hr.get("spec") or {}
    if "chartRef" in spec:
        ref = spec["chartRef"]
        key = (ref.get("namespace") or ns, ref.get("name"))
        src = oci_index.get(key) or oci_index.get(("*", ref.get("name"))) or {}
        return {"kind": "OCIRepository", "name": ref.get("name"), "url": src.get("url"), "version": src.get("tag") or src.get("semver")}
    cs = dig(spec, "chart", "spec") or {}
    return {"kind": dig(cs, "sourceRef", "kind"), "name": cs.get("chart"), "repo": dig(cs, "sourceRef", "name"),
            "repo_namespace": dig(cs, "sourceRef", "namespace"), "version": cs.get("version")}


# --------------------------------------------------------------------------- main build

def build() -> dict:
    objs = collect_objects()
    by_kind: dict[str, list[tuple[Path, dict]]] = {}
    for p, o in objs:
        by_kind.setdefault(o["kind"], []).append((p, o))

    # Sources ---------------------------------------------------------------
    oci_index = {}
    sources = []
    for p, o in by_kind.get("OCIRepository", []):
        ns = namespace_for(p, o)
        name = dig(o, "metadata", "name")
        entry = {"kind": "OCIRepository", "name": name, "namespace": ns, "url": dig(o, "spec", "url"),
                 "tag": dig(o, "spec", "ref", "tag"), "semver": dig(o, "spec", "ref", "semver"),
                 "verify": dig(o, "spec", "verify", "provider"), "file": rel(p)}
        oci_index[(ns, name)] = entry
        # components/repos are shared by every namespace that includes them
        if "/components/" in str(p):
            oci_index[("*", name)] = entry
        sources.append(entry)
    for p, o in by_kind.get("HelmRepository", []):
        sources.append({"kind": "HelmRepository", "name": dig(o, "metadata", "name"), "namespace": namespace_for(p, o),
                        "url": dig(o, "spec", "url"), "file": rel(p)})

    # Flux Kustomizations ------------------------------------------------------
    kustomizations = []
    for p, o in by_kind.get("Kustomization", []):
        if o.get("apiVersion", "").startswith("kustomize.toolkit.fluxcd.io"):
            kustomizations.append({
                "name": dig(o, "metadata", "name"),
                "namespace": namespace_for(p, o),
                "target_namespace": dig(o, "spec", "targetNamespace"),
                "path": dig(o, "spec", "path"),
                "depends_on": [f"{d.get('namespace') or namespace_for(p, o)}/{d.get('name')}" for d in (dig(o, "spec", "dependsOn") or [])],
                "interval": dig(o, "spec", "interval"),
                "wait": dig(o, "spec", "wait"),
                "suspend": dig(o, "spec", "suspend", default=False),
                "health_checks": bool(dig(o, "spec", "healthChecks") or dig(o, "spec", "healthCheckExprs")),
                "file": rel(p),
            })

    # PVCs ----------------------------------------------------------------------
    pvcs = {}
    for p, o in by_kind.get("PersistentVolumeClaim", []):
        ns = namespace_for(p, o)
        pvcs[(ns, dig(o, "metadata", "name"))] = {
            "name": dig(o, "metadata", "name"), "namespace": ns,
            "storage_class": dig(o, "spec", "storageClassName"),
            "size": dig(o, "spec", "resources", "requests", "storage"),
            "access_modes": dig(o, "spec", "accessModes"), "file": rel(p), "app": app_dir_for(p),
        }

    # Standalone HTTPRoutes / Services -----------------------------------------
    standalone_routes = []
    for p, o in by_kind.get("HTTPRoute", []):
        parents = dig(o, "spec", "parentRefs") or []
        parent = parents[0] if parents else {}
        # only routes that are actually included by a kustomization.yaml
        kust = p.parent / "kustomization.yaml"
        included = True
        if kust.exists():
            included = any(("./" + p.name) == r or p.name == r for r in (dig(load_docs(kust)[0], "resources") or [])) if load_docs(kust) else True
        backends = []
        for rule in dig(o, "spec", "rules") or []:
            for b in rule.get("backendRefs") or []:
                backends.append({"name": b.get("name"), "namespace": b.get("namespace"), "port": b.get("port")})
            for m in rule.get("matches") or []:
                if dig(m, "path", "value"):
                    backends and backends[-1].setdefault("path", dig(m, "path", "value"))
        standalone_routes.append({
            "name": dig(o, "metadata", "name"), "namespace": namespace_for(p, o), "app": app_dir_for(p),
            "hostnames": [subst(h) for h in (dig(o, "spec", "hostnames") or [])],
            "gateway": parent.get("name"), "gateway_namespace": parent.get("namespace"), "listener": parent.get("sectionName"),
            "exposure": gateway_exposure(parent.get("name")), "backends": backends, "included": included,
            "redirect": any(f.get("type") == "RequestRedirect" for r in (dig(o, "spec", "rules") or []) for f in (r.get("filters") or [])),
            "file": rel(p),
        })
    standalone_services = []
    for p, o in by_kind.get("Service", []):
        if "/plugin-barman/" in str(p):
            continue
        standalone_services.append({
            "name": dig(o, "metadata", "name"), "namespace": namespace_for(p, o), "app": app_dir_for(p),
            "type": dig(o, "spec", "type", default="ClusterIP"),
            "lb_ip": subst(dig(o, "metadata", "annotations", "lbipam.cilium.io/ips")),
            "hostname": subst(dig(o, "metadata", "annotations", "external-dns.kubernetes.io/hostname")),
            "external_traffic_policy": dig(o, "spec", "externalTrafficPolicy"),
            "ports": [{"name": pt.get("name"), "port": pt.get("port"), "protocol": pt.get("protocol", "TCP")} for pt in (dig(o, "spec", "ports") or [])],
            "selector": dig(o, "spec", "selector"), "file": rel(p),
        })

    # Which app dirs the namespace kustomizations actually include ------------------
    included_dirs: set[tuple[str, str]] = set()
    for nk in sorted((ROOT / "kubernetes/apps").glob("*/kustomization.yaml")):
        ns_dir = nk.parent.name
        for r in dig((load_docs(nk) or [{}])[0], "resources") or []:
            m = re.match(r"^\./([^/]+)/", str(r))
            if m:
                included_dirs.add((ns_dir, m.group(1)))

    def file_included(p: Path) -> bool:
        kust = p.parent / "kustomization.yaml"
        if not kust.exists():
            return False
        docs = load_docs(kust)
        res = dig(docs[0] if docs else {}, "resources") or []
        return any(str(r).lstrip("./") == p.name for r in res)

    # HelmReleases -> apps --------------------------------------------------------
    apps = []
    for p, o in by_kind.get("HelmRelease", []):
        ns = namespace_for(p, o)
        name = dig(o, "metadata", "name")
        app = app_dir_for(p) or name
        values = dig(o, "spec", "values") or {}
        # app/helm/values.yaml via valuesFrom ConfigMap
        values_file = p.parent / "helm" / "values.yaml"
        merged_from_file = None
        if dig(o, "spec", "valuesFrom") and values_file.exists():
            docs = load_docs(values_file)
            merged_from_file = docs[0] if docs else {}
            values = {**merged_from_file, **values}
        images = images_from_values(values)
        # non app-template charts: common image keys
        for key in ("image", "manager"):
            img = dig(values, key, "image") if key == "manager" else dig(values, key)
            if isinstance(img, dict) and img.get("repository") and not images:
                images.append({**split_image(img.get("repository"), img.get("tag")), "controller": key, "container": key, "role": "app"})
        if dig(values, "cephImage"):
            images.append({**split_image(dig(values, "cephImage", "repository"), dig(values, "cephImage", "tag")), "controller": "ceph", "container": "ceph", "role": "app"})
        if dig(values, "provider", "webhook", "image", "repository"):
            images.append({**split_image(dig(values, "provider", "webhook", "image", "repository"), dig(values, "provider", "webhook", "image", "tag")), "controller": "webhook", "container": "webhook", "role": "sidecar"})
        if dig(values, "instance", "distribution", "version"):
            images.append({"repository": "flux distribution", "tag": dig(values, "instance", "distribution", "version"), "digest": None, "controller": "flux", "container": "flux", "role": "app"})
        claims, nfs = persistence_from_values(values, app)
        storage = []
        for c in claims:
            pv = pvcs.get((ns, c["claim"]))
            storage.append({**c, **({"storage_class": pv["storage_class"], "size": pv["size"]} if pv else {"storage_class": None, "size": None})})
        routes = routes_from_values(values, name, app)
        routes += [r for r in standalone_routes if r["app"] == app and r["namespace"] == ns and r["included"] and not r["redirect"]]
        svcs = services_from_values(values)
        svcs += [s for s in standalone_services if s["app"] == app and s["namespace"] == ns]
        flags = values_text_flags(values)
        ks = [k for k in kustomizations if k["path"] and f"/apps/{ns}/{app}/" in k["path"] + "/"]
        depends = sorted({d for k in ks for d in k["depends_on"]} | {f"{d.get('namespace') or ns}/{d.get('name')}" for d in (dig(o, "spec", "dependsOn") or [])})
        hp = None
        for r in routes:
            if r.get("homepage"):
                hp = r["homepage"]
        ns_dir = p.relative_to(ROOT).parts[2] if len(p.relative_to(ROOT).parts) > 2 else ns
        apps.append({
            "name": name, "namespace": ns, "dir": app, "file": rel(p),
            "deployed": (ns_dir, app) in included_dirs and file_included(p),
            "chart": chart_of(o, oci_index, ns),
            "suspended": bool(dig(o, "spec", "suspend")) or dig(values, "enabled") is False or any(k["suspend"] for k in ks),
            "interval": dig(o, "spec", "interval"),
            "images": images,
            "routes": [{k: v for k, v in r.items() if k not in ("file", "included", "redirect", "app")} for r in routes],
            "legacy_ingress": legacy_ingress_from_values(values, name, app),
            "services": svcs,
            "storage": storage,
            "nfs": nfs,
            "depends_on": depends,
            "flux_kustomizations": [k["name"] for k in ks],
            "homepage": hp,
            **flags,
            "secrets": {
                "sops": any(f.name.endswith(".sops.yaml") for f in p.parent.glob("*.yaml")),
                "external": any("ExternalSecret" in d.get("kind", "") for f in p.parent.glob("*.yaml") if not f.name.endswith(".sops.yaml") for d in load_docs(f)),
            },
        })

    # CNPG / Dragonfly ------------------------------------------------------------
    cnpg = next((o for _, o in by_kind.get("Cluster", []) if o.get("apiVersion", "").startswith("postgresql.cnpg.io")), {})
    sched = next((o for _, o in by_kind.get("ScheduledBackup", [])), {})
    objstore = next((o for _, o in by_kind.get("ObjectStore", [])), {})
    pg_lb = next((s for s in standalone_services if s["name"] == "postgres-lb"), {})
    dragonfly = next((o for _, o in by_kind.get("Dragonfly", [])), {})
    databases = {
        "postgres": {
            "instances": dig(cnpg, "spec", "instances"),
            "image": dig(cnpg, "spec", "imageName"),
            "storage_class": dig(cnpg, "spec", "storage", "storageClass"),
            "size": dig(cnpg, "spec", "storage", "size"),
            "sync_replication": dig(cnpg, "spec", "postgresql", "synchronous"),
            "primary_update": dig(cnpg, "spec", "primaryUpdateMethod"),
            "lb_ip": pg_lb.get("lb_ip"), "lb_hostname": pg_lb.get("hostname"),
            "external_traffic_policy": pg_lb.get("external_traffic_policy"),
            "backup": {"schedule": dig(sched, "spec", "schedule"), "retention": dig(objstore, "spec", "retentionPolicy"),
                        "destination": dig(objstore, "spec", "configuration", "destinationPath"),
                        "endpoint": dig(objstore, "spec", "configuration", "endpointURL"), "method": dig(sched, "spec", "method")},
            "consumers": sorted(a["name"] for a in apps if a["uses_postgres"] and a["namespace"] != "database"),
        },
        "dragonfly": {
            "replicas": dig(dragonfly, "spec", "replicas"), "image": dig(dragonfly, "spec", "image"),
            "args": dig(dragonfly, "spec", "args"),
            "consumers": sorted(a["name"] for a in apps if a["uses_dragonfly"] and a["namespace"] != "database"),
        },
    }

    # Gateways / network -----------------------------------------------------------
    gateways = []
    for p, o in by_kind.get("Gateway", []):
        ann = dig(o, "metadata", "annotations") or {}
        infra = dig(o, "spec", "infrastructure", "annotations") or {}
        gateways.append({
            "name": dig(o, "metadata", "name"), "namespace": namespace_for(p, o),
            "class": dig(o, "spec", "gatewayClassName"),
            "lb_ip": infra.get("lbipam.cilium.io/ips"),
            "k8s_gateway_hostname": subst(infra.get("external-dns.kubernetes.io/hostname")),
            "dns_target": subst(ann.get("external-dns.kubernetes.io/target")),
            "listeners": [{"name": l.get("name"), "protocol": l.get("protocol"), "port": l.get("port"),
                           "routes_from": dig(l, "allowedRoutes", "namespaces", "from"),
                           "certs": [subst(c.get("name")) for c in (dig(l, "tls", "certificateRefs") or [])]} for l in (dig(o, "spec", "listeners") or [])],
        })
    ctps = {}
    for p, o in by_kind.get("ClientTrafficPolicy", []):
        tgt = dig(o, "spec", "targetRefs", 0, "name")
        ctps[tgt] = {"client_ip_detection": list((dig(o, "spec", "clientIPDetection") or {}).keys()),
                     "trusted_hops": dig(o, "spec", "clientIPDetection", "xForwardedFor", "numTrustedHops"),
                     "tls_min": dig(o, "spec", "tls", "minVersion"), "http3": "http3" in (o.get("spec") or {}),
                     "alpn": dig(o, "spec", "tls", "alpnProtocols")}
    for g in gateways:
        g["client_traffic_policy"] = ctps.get(g["name"])
    btp = next((o for _, o in by_kind.get("BackendTrafficPolicy", [])), {})
    envoy_proxy = next((o for _, o in by_kind.get("EnvoyProxy", [])), {})

    pool = next((o for _, o in by_kind.get("CiliumLoadBalancerIPPool", [])), {})
    l2 = next((o for _, o in by_kind.get("CiliumL2AnnouncementPolicy", [])), {})
    connector = next((o for _, o in by_kind.get("Connector", [])), {})
    dnsendpoints = [{"name": e.get("dnsName") and subst(e["dnsName"]), "type": e.get("recordType"), "targets": e.get("targets")}
                    for _, o in by_kind.get("DNSEndpoint", []) for e in (dig(o, "spec", "endpoints") or [])]
    certs = [{"name": subst(dig(o, "metadata", "name")), "secret": subst(dig(o, "spec", "secretName")),
              "dns_names": [subst(n) for n in (dig(o, "spec", "dnsNames") or [])], "issuer": dig(o, "spec", "issuerRef", "name")}
             for p, o in by_kind.get("Certificate", []) if "/plugin-barman/" not in str(p)]
    issuer = next((o for _, o in by_kind.get("ClusterIssuer", [])), {})

    cloudflared_cfg = {}
    cfg_path = ROOT / "kubernetes/apps/network/external/cloudflared/configs/config.yaml"
    if cfg_path.exists():
        d = load_docs(cfg_path)
        cloudflared_cfg = d[0] if d else {}

    def hr_values(ns, name):
        for a in apps:
            if a["namespace"] == ns and a["name"] == name:
                return a
        return {}

    k8s_gateway_hr = next((o for p, o in by_kind.get("HelmRelease", []) if dig(o, "metadata", "name") == "k8s-gateway"), {})
    kg_plugins = dig(k8s_gateway_hr, "spec", "values", "servers", 0, "plugins") or []
    kg = next((pl for pl in kg_plugins if pl.get("name") == "k8s_gateway"), {})
    kg_forward = next((pl.get("parameters") for pl in kg_plugins if pl.get("name") == "forward"), None)
    unifi_hr = next((o for p, o in by_kind.get("HelmRelease", []) if dig(o, "metadata", "name") == "unifi-dns"), {})
    cf_dns_hr = next((o for p, o in by_kind.get("HelmRelease", []) if dig(o, "metadata", "name") == "external-dns"), {})
    tailscale_hr = next((o for p, o in by_kind.get("HelmRelease", []) if dig(o, "metadata", "name") == "tailscale-operator"), {})

    lb_ips = []
    for g in gateways:
        lb_ips.append({"ip": g["lb_ip"], "owner": f"Gateway {g['name']}", "namespace": g["namespace"], "ports": [l["port"] for l in g["listeners"]], "kind": "gateway"})
    for a in apps:
        for s in a["services"]:
            if s.get("lb_ip"):
                lb_ips.append({"ip": s["lb_ip"], "owner": a["name"], "namespace": a["namespace"], "ports": [pt.get("port") for pt in s["ports"]], "kind": "service"})
    kg_ip = dig(k8s_gateway_hr, "spec", "values", "service", "annotations", "lbipam.cilium.io/ips")
    if kg_ip:
        lb_ips.append({"ip": kg_ip, "owner": "k8s-gateway", "namespace": "network", "ports": [53], "kind": "dns"})
    if pg_lb.get("lb_ip"):
        lb_ips.append({"ip": pg_lb["lb_ip"], "owner": "postgres-lb", "namespace": "database", "ports": [5432], "kind": "service"})

    seen, deduped = set(), []
    for e in lb_ips:
        key = (e["ip"], e["kind"], e["owner"] if e["kind"] != "service" else e["ip"])
        if key not in seen:
            seen.add(key); deduped.append(e)
    lb_ips = sorted(deduped, key=lambda e: [int(x) if x.isdigit() else 999 for x in str(e["ip"]).split(".")])

    network = {
        "lan_cidr": "10.13.36.0/22", "lan_gateway": "10.13.37.1",
        "pod_cidr": None, "service_cidr": None, "api_vip": None,
        "lb_pool": {"blocks": [f"{b.get('start')}-{b.get('stop')}" for b in (dig(pool, "spec", "blocks") or [])],
                    "l2_announcements": bool(l2), "mode": None},
        "lb_ips": lb_ips,
        "gateways": gateways,
        "envoy": {"replicas": dig(envoy_proxy, "spec", "provider", "kubernetes", "envoyDeployment", "replicas"),
                  "external_traffic_policy": dig(envoy_proxy, "spec", "provider", "kubernetes", "envoyService", "externalTrafficPolicy"),
                  "request_timeout": dig(btp, "spec", "timeout", "http", "requestTimeout"),
                  "compression": [c.get("type") for c in (dig(btp, "spec", "compressor") or [])]},
        "dns": {
            "k8s_gateway": {"ip": kg_ip, "zone": subst(kg.get("parameters")), "resources": kg.get("configBlock"), "forward": kg_forward},
            "unifi_dns": {"provider": "UniFi (external-dns webhook)", "sources": dig(unifi_hr, "spec", "values", "sources"),
                          "domains": [subst(d) for d in (dig(unifi_hr, "spec", "values", "domainFilters") or [])],
                          "policy": dig(unifi_hr, "spec", "values", "policy"), "txt_owner": dig(unifi_hr, "spec", "values", "txtOwnerId")},
            "cloudflare_dns": {"provider": "cloudflare", "sources": dig(cf_dns_hr, "spec", "values", "sources"),
                               "domains": [subst(d) for d in (dig(cf_dns_hr, "spec", "values", "domainFilters") or [])],
                               "gateway_filter": next((a.split("=", 1)[1] for a in (dig(cf_dns_hr, "spec", "values", "extraArgs") or []) if a.startswith("--gateway-name=")), None),
                               "proxied": any(a == "--cloudflare-proxied" for a in (dig(cf_dns_hr, "spec", "values", "extraArgs") or [])),
                               "policy": dig(cf_dns_hr, "spec", "values", "policy")},
            "dnsendpoints": dnsendpoints,
            "node_resolvers": None,
        },
        "tunnel": {
            "cname": next((e["targets"][0] for e in dnsendpoints if e.get("type") == "CNAME" and e.get("targets")), None),
            "origin": subst(dig(cloudflared_cfg, "ingress", 0, "service")),
            "origin_sni": subst(dig(cloudflared_cfg, "originRequest", "originServerName")),
            "hostnames": [subst(i.get("hostname")) for i in (dig(cloudflared_cfg, "ingress") or []) if i.get("hostname")],
            "transport": {k: v for k, v in (dig(next((o for p, o in by_kind.get("HelmRelease", []) if dig(o, "metadata", "name") == "cloudflared"), {}), "spec", "values", "controllers", "cloudflared", "containers", "app", "env") or {}).items() if str(k).startswith("TUNNEL_")},
        },
        "tailscale": {
            "operator_hostname": dig(tailscale_hr, "spec", "values", "operatorConfig", "hostname"),
            "connector": {"name": dig(connector, "metadata", "name"), "replicas": dig(connector, "spec", "replicas"),
                          "routes": dig(connector, "spec", "subnetRouter", "advertiseRoutes"), "tags": dig(connector, "spec", "tags"),
                          "hostname_prefix": dig(connector, "spec", "hostnamePrefix")},
        },
        "tls": {"issuer": dig(issuer, "metadata", "name"), "acme": dig(issuer, "spec", "acme", "server"),
                "solver": "dns01/cloudflare" if dig(issuer, "spec", "acme", "solvers", 0, "dns01") else "http01",
                "certificates": certs},
    }

    # Talos -----------------------------------------------------------------------
    talenv = (load_docs(ROOT / "talos/talenv.yaml") or [{}])[0]
    talconfig_text = (ROOT / "talos/talconfig.yaml").read_text()
    talconfig = yaml.load(talconfig_text.replace("${talosVersion}", str(talenv.get("talosVersion"))).replace("${kubernetesVersion}", str(talenv.get("kubernetesVersion"))), Loader=Loader) or {}
    nodes = []
    for n in talconfig.get("nodes") or []:
        ni = (n.get("networkInterfaces") or [{}])[0]
        nodes.append({"hostname": n.get("hostname"), "ip": n.get("ipAddress"), "control_plane": n.get("controlPlane"),
                      "install_disk": n.get("installDisk"), "mac": dig(ni, "deviceSelector", "hardwareAddr"),
                      "address": dig(ni, "addresses", 0), "gateway": dig(ni, "routes", 0, "gateway"), "vip": dig(ni, "vip", "ip"),
                      "image": n.get("talosImageURL")})
    network["api_vip"] = next((n["vip"] for n in nodes if n.get("vip")), None)
    network["pod_cidr"] = dig(talconfig, "clusterPodNets", 0)
    network["service_cidr"] = dig(talconfig, "clusterSvcNets", 0)
    patches = {}
    for pp in sorted((ROOT / "talos/patches").rglob("*.yaml")):
        d = load_docs(pp)
        patches[rel(pp)] = d[0] if d else {}
    network["dns"]["node_resolvers"] = dig(patches.get("talos/patches/global/machine-network.yaml", {}), "machine", "network", "nameservers")
    cluster_patch = patches.get("talos/patches/controller/cluster.yaml", {})
    talos = {
        "config_contract": talenv.get("talosVersion"), "installer_version": talenv.get("talosInstallerVersion"),
        "kubernetes_version": talenv.get("kubernetesVersion"),
        "schematic": (nodes[0]["image"].rsplit("/", 1)[-1] if nodes else None),
        "endpoint": talconfig.get("endpoint"), "cni": dig(talconfig, "cniConfig", "name"),
        "kube_proxy_disabled": dig(cluster_patch, "cluster", "proxy", "disabled"),
        "coredns_disabled": dig(cluster_patch, "cluster", "coreDNS", "disabled"),
        "scheduling_on_control_planes": dig(cluster_patch, "cluster", "allowSchedulingOnControlPlanes"),
        "kernel_modules": [m.get("name") for m in (dig(patches.get("talos/patches/global/machine-kernel.yaml", {}), "machine", "kernel", "modules") or [])],
        "ntp": dig(patches.get("talos/patches/global/machine-time.yaml", {}), "machine", "time", "servers"),
        "sysctls": dig(patches.get("talos/patches/global/machine-sysctls.yaml", {}), "machine", "sysctls"),
        "pod_security": dig(patches.get("talos/patches/controller/admission-controller-patch.yaml", {}), "cluster", "apiServer", "admissionControl", 0, "configuration", "defaults", "enforce"),
    }

    # Storage ------------------------------------------------------------------------
    rook_cluster = hr_values("rook-ceph", "rook-ceph-cluster")
    rook_hr = next((o for p, o in by_kind.get("HelmRelease", []) if dig(o, "metadata", "name") == "rook-ceph-cluster"), {})
    rv = dig(rook_hr, "spec", "values") or {}
    topolvm_hr = next((o for p, o in by_kind.get("HelmRelease", []) if dig(o, "metadata", "name") == "topolvm"), {})
    tv = dig(topolvm_hr, "spec", "values") or {}
    nfs_hr = next((o for p, o in by_kind.get("HelmRelease", []) if dig(o, "metadata", "name") == "csi-driver-nfs"), {})
    nv = dig(nfs_hr, "spec", "values") or {}
    classes = []
    for bp in rv.get("cephBlockPools") or []:
        sc = bp.get("storageClass") or {}
        classes.append({"name": sc.get("name"), "provisioner": "rook-ceph.rbd.csi.ceph.com", "pool": bp.get("name"),
                        "default": sc.get("isDefault"), "reclaim": sc.get("reclaimPolicy"),
                        "replication": dig(bp, "spec", "replicated", "size"), "failure_domain": dig(bp, "spec", "failureDomain"),
                        "compression": dig(sc, "parameters", "compression_mode"), "fstype": dig(sc, "parameters", "csi.storage.k8s.io/fstype")})
    for os_ in rv.get("cephObjectStores") or []:
        sc = os_.get("storageClass") or {}
        classes.append({"name": sc.get("name"), "provisioner": "rook-ceph.ceph.rook.io/bucket", "pool": os_.get("name"),
                        "default": False, "reclaim": sc.get("reclaimPolicy"), "replication": dig(os_, "spec", "dataPool", "replicated", "size"),
                        "failure_domain": dig(os_, "spec", "dataPool", "failureDomain"), "kind": "object"})
    for sc in tv.get("storageClasses") or []:
        s = sc.get("storageClass") or {}
        classes.append({"name": sc.get("name"), "provisioner": "topolvm.io", "default": s.get("isDefaultClass"), "fstype": s.get("fsType"),
                        "binding": s.get("volumeBindingMode"), "expansion": s.get("allowVolumeExpansion"), "node_local": True})
    if dig(nv, "storageClass", "create"):
        classes.append({"name": dig(nv, "storageClass", "name"), "provisioner": "nfs.csi.k8s.io", "default": False,
                        "reclaim": dig(nv, "storageClass", "reclaimPolicy"), "mount_options": dig(nv, "storageClass", "mountOptions"),
                        "server": None, "note": "server/share parameters are commented out in the HelmRelease"})
    pv_usage = {}
    for (ns, nm), pv in pvcs.items():
        pv_usage.setdefault(pv["storage_class"], []).append({"namespace": ns, "claim": nm, "size": pv["size"], "app": pv["app"]})
    storage = {
        "classes": classes,
        "ceph": {"image": dig(rv, "cephImage", "repository"), "version": dig(rv, "cephImage", "tag"),
                 "osd_device_filter": dig(rv, "cephClusterSpec", "storage", "devicePathFilter"),
                 "network": dig(rv, "cephClusterSpec", "network", "provider"), "dashboard_host": subst(dig(rv, "route", "dashboard", "host", "name")),
                 "mgr_modules": [m.get("name") for m in (dig(rv, "cephClusterSpec", "mgr", "modules") or []) if m.get("enabled")],
                 "rgw_instances": dig(rv, "cephObjectStores", 0, "spec", "gateway", "instances")},
        "topolvm": {"volume_group": dig(tv, "lvmd", "deviceClasses", 0, "volume-group"), "thin_pool": dig(tv, "lvmd", "deviceClasses", 0, "thin-pool", "name"),
                    "overprovision": dig(tv, "lvmd", "deviceClasses", 0, "thin-pool", "overprovision-ratio")},
        "nfs_server_var": "${SECRET_NFS_IP}",
        "nfs_exports": sorted({m["path"] for a in apps for m in a["nfs"] if m.get("path")}),
        "pvcs_by_class": pv_usage,
    }

    # Platform versions ----------------------------------------------------------------
    def app_version(ns, name):
        a = hr_values(ns, name)
        return a.get("chart", {}).get("version") if a else None

    def image_tag(ns, name):
        a = hr_values(ns, name)
        return (a.get("images") or [{}])[0].get("tag") if a else None

    platform = {
        "talos": talos["installer_version"], "talos_config_contract": talos["config_contract"], "kubernetes": talos["kubernetes_version"],
        "cilium": app_version("kube-system", "cilium"), "coredns_chart": app_version("kube-system", "coredns"),
        "spegel": app_version("kube-system", "spegel"), "metrics_server": app_version("kube-system", "metrics-server"),
        "reloader": app_version("kube-system", "reloader"), "csi_driver_nfs": app_version("kube-system", "csi-driver-nfs"),
        "flux": image_tag("flux-system", "flux-instance"), "flux_operator": app_version("flux-system", "flux-operator"),
        "cert_manager": app_version("cert-manager", "cert-manager"), "external_secrets": app_version("external-secrets", "external-secrets"),
        "envoy_gateway": app_version("network", "envoy-gateway"), "external_dns": app_version("network", "external-dns"),
        "unifi_webhook": image_tag("network", "unifi-dns"), "k8s_gateway": app_version("network", "k8s-gateway"),
        "cloudflared": image_tag("network", "cloudflared"), "tailscale_operator": app_version("network", "tailscale-operator"),
        "rook": app_version("rook-ceph", "rook-ceph-operator"), "ceph": storage["ceph"]["version"], "ceph_csi_drivers": app_version("rook-ceph", "ceph-csi-drivers"),
        "topolvm": app_version("topolvm", "topolvm"), "cnpg_operator_chart": app_version("database", "cloudnative-pg"),
        "postgres": databases["postgres"]["image"], "dragonfly_operator": app_version("database", "dragonfly-operator"),
        "dragonfly": databases["dragonfly"]["image"], "app_template": (oci_index.get(("*", "app-template")) or {}).get("tag"),
    }
    barman = ROOT / "kubernetes/apps/database/cloudnative-pg/plugin-barman/manifest.yaml"
    if barman.exists():
        m = re.search(r"image: (ghcr.io/cloudnative-pg/plugin-barman-cloud:(\S+))", barman.read_text())
        platform["barman_cloud_plugin"] = m.group(2) if m else None

    # Tools / Renovate / bootstrap ---------------------------------------------------------
    tools = {}
    for line in (ROOT / ".mise.toml").read_text().splitlines():
        m = re.match(r'^"?([^"=\s]+)"?\s*=\s*"([^"]+)"', line.strip())
        if m and ("/" in m.group(1) or m.group(1) in ("python",)):
            tools[m.group(1)] = m.group(2)
    renovate = {"automerge_apps": [], "holds": [], "notes": []}
    try:
        import json5  # type: ignore
        rcfg = json5.loads((ROOT / ".renovaterc.json5").read_text())
    except Exception:
        try:
            rcfg = json.loads(json5_to_json((ROOT / ".renovaterc.json5").read_text()))
        except Exception as exc:
            rcfg = {}
            ERRORS.append({"file": ".renovaterc.json5", "error": f"could not parse: {exc}"})
    for rule in rcfg.get("packageRules", []):
        if rule.get("automerge") and rule.get("matchFileNames"):
            renovate["automerge_apps"] = [f.replace("kubernetes/apps/", "").rstrip("/*") for f in rule["matchFileNames"]]
            renovate["automerge_schedule"] = rule.get("schedule")
        if rule.get("allowedVersions"):
            renovate["holds"].append({"description": rule.get("description"), "packages": rule.get("matchPackageNames"), "allowed": rule["allowedVersions"]})
        if rule.get("automerge") and rule.get("matchPackageNames") and not rule.get("matchFileNames"):
            renovate["notes"].append(f"{rule.get('description')}: {', '.join(rule['matchPackageNames'])}")
    renovate["schedule"] = rcfg.get("schedule")
    bootstrap = []
    for hf in (ROOT / "bootstrap/helmfile.yaml", ROOT / "bootstrap/helmfile.d/00-crds.yaml"):
        for r in dig((load_docs(hf) or [{}])[0], "releases") or []:
            bootstrap.append({"name": r.get("name"), "namespace": r.get("namespace"), "chart": r.get("chart"), "version": r.get("version"), "file": rel(hf)})

    # Hostname catalogue -----------------------------------------------------------------------
    hostnames = []
    tunnel_hosts = set(network["tunnel"]["hostnames"])

    def public_dns(host: str, exposure: str | None) -> str | None:
        if exposure != "external":
            return None
        if host.endswith("." + PRIMARY_DOMAIN):
            return "cloudflare external-dns: proxied CNAME -> external." + PRIMARY_DOMAIN + " -> tunnel"
        return "manual proxied CNAME in the secondary zone -> tunnel"

    def tunnel_covered(host: str) -> bool:
        if host in tunnel_hosts:
            return True
        return any(h.startswith("*.") and host.endswith(h[1:]) for h in tunnel_hosts)

    for a in apps:
        for r in a["routes"]:
            for h in r["hostnames"]:
                ex = r.get("exposure")
                hostnames.append({
                    "host": h, "app": a["name"], "namespace": a["namespace"], "gateway": r.get("gateway"), "exposure": ex,
                    "lan_dns": ("A -> " + (next((g["lb_ip"] for g in gateways if g["name"] == r.get("gateway")), "?")) if ex == "internal"
                                else "CNAME -> external." + PRIMARY_DOMAIN + " (manual UniFi A -> " + next((g["lb_ip"] for g in gateways if g["name"] == "envoy-external"), "?") + ")"),
                    "public_dns": public_dns(h, ex),
                    "tunnel_rule": tunnel_covered(h) if ex == "external" else False,
                    "vpn_dns": ("k8s-gateway -> " + next((g["lb_ip"] for g in gateways if g["name"] == r.get("gateway")), "?")) if h.endswith("." + PRIMARY_DOMAIN) else "not served by k8s-gateway (secondary zone)",
                    "suspended": a["suspended"],
                })
        for s in a["services"]:
            if s.get("hostname"):
                hostnames.append({"host": s["hostname"], "app": a["name"], "namespace": a["namespace"], "gateway": None, "exposure": "lb",
                                  "lan_dns": None, "public_dns": None, "tunnel_rule": False, "vpn_dns": f"k8s-gateway -> {s.get('lb_ip')}", "suspended": a["suspended"]})
    for g in gateways:
        if g.get("k8s_gateway_hostname"):
            hostnames.append({"host": g["k8s_gateway_hostname"], "app": g["name"], "namespace": g["namespace"], "gateway": g["name"], "exposure": "gateway",
                              "lan_dns": None, "public_dns": None, "tunnel_rule": False, "vpn_dns": f"k8s-gateway -> {g['lb_ip']}", "suspended": False})
    hostnames.sort(key=lambda h: (h["host"].split(".", 1)[1], h["host"]))

    head = git("rev-parse", "HEAD")
    state = {
        "schema": 1,
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "generator": "scripts/atlas/build-state.py",
        "source": {"repo": "tturnerdev/home-ops", "branch": git("rev-parse", "--abbrev-ref", "HEAD"), "commit": head,
                   "commit_short": head[:7], "commit_date": git("log", "-1", "--format=%cI"), "subject": git("log", "-1", "--format=%s")},
        "domains": {"primary": PRIMARY_DOMAIN, "secondary": SECONDARY_TOKEN},
        "platform": platform,
        "talos": talos,
        "nodes": nodes,
        "network": network,
        "storage": storage,
        "databases": databases,
        "apps": sorted(apps, key=lambda a: (a["namespace"], a["name"])),
        "hostnames": hostnames,
        "flux": {"kustomizations": kustomizations, "sources": sources,
                 "git": dig(next((o for p, o in by_kind.get("HelmRelease", []) if dig(o, "metadata", "name") == "flux-instance"), {}), "spec", "values", "instance", "sync")},
        "renovate": renovate,
        "tools": tools,
        "bootstrap": bootstrap,
        "errors": ERRORS,
    }
    return state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "kubernetes/apps/infra/atlas/app/site/state.json"))
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("--var", action="append", default=[], metavar="NAME=VALUE", help="resolve a cluster-secrets substitution")
    args = ap.parse_args()
    for item in args.var:
        k, _, v = item.partition("=")
        KNOWN_VARS[k] = v
    state = build()
    text = json.dumps(state, indent=2, sort_keys=False, default=str) + "\n"
    if args.stdout:
        sys.stdout.write(text)
    else:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        print(f"wrote {out} ({len(text)//1024} KiB, {len(state['apps'])} apps, {len(state['hostnames'])} hostnames, {len(ERRORS)} parse errors)")


if __name__ == "__main__":
    main()

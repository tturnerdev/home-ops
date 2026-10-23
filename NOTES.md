### CRD Installation

The CRDs that Flux does not install (or never upgrades, as Helm skips a chart's
`crds/` on upgrade) come from the charts listed in
`bootstrap/helmfile.d/00-crds.yaml`. When deploying a new app that needs new
CRDs, or bumping one of those charts, update the list and apply the CRDs:
```
helmfile --file bootstrap/helmfile.d/00-crds.yaml template | yq ea 'select(.kind == "CustomResourceDefinition")' | kubectl apply --server-side --force-conflicts --filename -
```
`task bootstrap:apps` runs the same pipeline before installing the apps.

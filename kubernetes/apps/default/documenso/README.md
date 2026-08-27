# Documenso

Self-hosted document signing, exposed on the secondary domain
(`sign.${SECRET_DOMAIN_2}`) via the cloudflared tunnel (public) and the
external ingress LB published to UniFi DNS (LAN split-horizon).

## Customization: US English UI (init-container catalog patch)

Upstream Documenso ships **UK English only** ("Organisation", "Cancelled",
"colour", …) and compiles its Lingui translation catalogs **into the image
at build time** — there is no runtime translations folder to drop an
`en-US` locale into, and the locale picker list is baked into the code.
Rather than maintain a fork that rebuilds the image every release, an init
container re-translates the compiled catalogs in place at every pod start.

### How it works

1. `resources/patch-us-english.mjs` is shipped as ConfigMap
   `documenso-us-english` (see `app/kustomization.yaml`).
2. The `us-english` initContainer (same image as the app, so it always
   matches the running version) runs the script, which:
   - copies `/app/apps/remix/build/server/hono/packages/lib/translations/en`
     (server catalog `web.mjs`, also used for outgoing email templates) and
     `/app/apps/remix/build/client/assets` (client locale chunks
     `web-*.js`) into two `emptyDir` volumes;
   - rewrites UK→US spellings in the copies using a word-boundary
     dictionary. The regex `(?<![{\w])word(?!\w)` cannot touch ICU
     placeholder variable names (`organisationName` etc.) or code
     identifiers — compiled catalogs use hashed message keys, so only
     message prose matches.
3. The app container mounts those `emptyDir`s **over** the original
   paths, so the server and every browser get US English.

### Upgrade behavior

Nothing to do on image bumps: the patch re-runs against whatever the new
image ships (matching by words and stable paths, not content hashes).
Failure modes are deliberate:

- **Catalog format changes** → zero replacements → logs a warning and the
  app boots with UK spellings (fails open).
- **Source paths disappear** → the copy fails → the init exits non-zero
  and the pod does not start (an empty overlay would break the app, so
  this fails loud instead).

Check after a version bump:

```sh
kubectl logs -n default deploy/documenso -c us-english
# expect: [us-english] patched <N> files, <M> replacements
```

If a new release introduces UK words missing from the dictionary (e.g.
"labelled"), add the case/tense variants to `DICT` in the script — the
ConfigMap change alone rolls the deployment via Reloader.

### Gotchas learned the hard way

- The ConfigMap carries
  `kustomize.toolkit.fluxcd.io/substitute: disabled` — Flux's postBuild
  envsubst otherwise tries to parse the script's JS `${...}` template
  literals and fails the entire Kustomization build
  ("missing closing brace").
- Client chunk filenames are content-hashed per release but patched
  in place, so Cloudflare's edge may serve pre-patch (UK) cached copies
  to remote visitors until a cache purge or the next release; LAN
  traffic bypasses the edge and always gets patched files.
- The language picker still shows plain "English" — this patches the
  `en` locale in place; a true `en-US` picker entry would require a fork
  and image rebuild.

## Email templates / branding

Recipient-facing emails are React Email templates compiled into the image;
there is no template directory to override. The supported customization
surface is **organisation branding**, stored in
`OrganisationGlobalSettings` (row for the business org): `brandingEnabled`,
`brandingUrl`, `brandingCompanyDetails` (newline-split, replaces the
hardcoded "Documenso, Inc." address in every email footer), and
`brandingLogo` (URL; swaps the email header + signing-page logo when set).
These were enabled via direct SQL (the settings are not in the public API;
the UI equivalent is Organisation settings → Preferences → Branding).
Teams inherit while their own branding columns are NULL.

Notes:
- The "This document was sent using Documenso" line has no off-switch in
  this build (no `hidePoweredBy` column yet); its text lives in the
  translation catalogs, so the init-container patcher could rewrite it if
  ever desired.
- Emails render through the patched catalogs (verified: upstream says
  "cancelled", delivered mail says "canceled"), so email copy is also
  US English.
- Email From identity: `NEXT_PRIVATE_SMTP_FROM_ADDRESS` in
  `externalsecret.yaml`; the address must be a verified "Send mail as"
  alias of the authenticated Gmail account or Google rewrites the header.

## Other notable choices

- **Secrets**: machine-generated values (encryption keys, DB password,
  signing passphrase) and the self-signed 10-year PDF signing certificate
  (`-legacy` PKCS12; subject = the business name) live in sops files
  here; SMTP reuses the n8n relay creds and the CNPG superuser password
  comes from 1Password via `externalsecret.yaml`.
- **`documenso-api-token`** (ExternalSecret) materializes the Documenso
  REST API token from 1Password for tooling/automation; it is *not*
  mounted into the app pod.
- **`strategy: RollingUpdate`** because the app ignores SIGTERM and the
  default Recreate strategy caused ~5 min outages per rollout.
- **TLS**: the ingress serves a real Let's Encrypt cert (HTTP-01 via the
  tunnel — the Cloudflare token is scoped to the primary domain only, so
  DNS-01 can't issue for this zone) so direct LAN HTTPS validates;
  the tunnel path validates against the primary domain's wildcard via
  cloudflared's `originServerName`.
- **ECH is disabled** on the secondary domain's Cloudflare zone —
  split-horizon DNS plus Cloudflare's Encrypted ClientHello config breaks
  Chrome with `ERR_ECH_FALLBACK_CERTIFICATE_INVALID` (the browser fetches
  the HTTPS/type-65 record from public DNS even when the A record is
  overridden locally).

// Rewrites Documenso's compiled Lingui catalogs from UK to US English.
// Runs as an initContainer using the app image: copies the server 'en'
// catalog dir and the client assets dir into emptyDir overlays, then
// patches message text in place. Upstream ships no en-US locale and the
// catalogs are compiled into the image at build time, so this is the
// upgrade-safe seam: word-based, path-stable, and if the layout ever
// changes it logs loudly and the app simply boots with UK spellings.
import { cpSync, readFileSync, writeFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

const SERVER_SRC = '/app/apps/remix/build/server/hono/packages/lib/translations/en';
const CLIENT_SRC = '/app/apps/remix/build/client/assets';
const SERVER_OUT = '/out-server';
const CLIENT_OUT = '/out-client';

const DICT = [
  ['organisations', 'organizations'], ['Organisations', 'Organizations'],
  ['organisation', 'organization'], ['Organisation', 'Organization'],
  ['organised', 'organized'], ['Organised', 'Organized'],
  ['organising', 'organizing'], ['Organising', 'Organizing'],
  ['organise', 'organize'], ['Organise', 'Organize'],
  ['reauthorise', 'reauthorize'], ['Reauthorise', 'Reauthorize'],
  ['authorisation', 'authorization'], ['Authorisation', 'Authorization'],
  ['authorised', 'authorized'], ['Authorised', 'Authorized'],
  ['authorise', 'authorize'], ['Authorise', 'Authorize'],
  ['cancelled', 'canceled'], ['Cancelled', 'Canceled'],
  ['cancelling', 'canceling'], ['Cancelling', 'Canceling'],
  ['colours', 'colors'], ['Colours', 'Colors'],
  ['colour', 'color'], ['Colour', 'Color'],
  ['customised', 'customized'], ['Customised', 'Customized'],
  ['customising', 'customizing'], ['Customising', 'Customizing'],
  ['customise', 'customize'], ['Customise', 'Customize'],
  ['finalising', 'finalizing'], ['Finalising', 'Finalizing'],
  ['finalise', 'finalize'], ['Finalise', 'Finalize'],
];

// Word-boundary match that also refuses a preceding '{' so bare ICU
// placeholder names ({organisation}) can never be rewritten; camelCase
// names like {organisationName} are already safe via the \w boundary.
const patchText = (text) => {
  let n = 0;
  for (const [uk, us] of DICT) {
    text = text.replace(new RegExp(`(?<![{\\w])${uk}(?!\\w)`, 'g'), () => (n++, us));
  }
  return [text, n];
};

try {
  cpSync(SERVER_SRC, SERVER_OUT, { recursive: true });
  cpSync(CLIENT_SRC, CLIENT_OUT, { recursive: true });
} catch (err) {
  // Empty overlays would break the app outright — fail the pod loudly.
  console.error('[us-english] FATAL: source copy failed — image layout changed?', err);
  process.exit(1);
}

let files = 0;
let total = 0;
const patchFile = (p) => {
  try {
    const [out, n] = patchText(readFileSync(p, 'utf8'));
    if (n > 0) {
      writeFileSync(p, out);
      files++;
      total += n;
    }
  } catch (err) {
    console.error(`[us-english] skipping ${p}: ${err.message}`);
  }
};

// Server catalog (also used for emails); web.po is copied untouched — it
// is build residue and its msgid lines double as lookup keys.
patchFile(join(SERVER_OUT, 'web.mjs'));
// Client locale catalog chunks (all locales — patches English fallbacks too).
for (const f of readdirSync(CLIENT_OUT)) {
  if (/^web-.*\.js$/.test(f)) patchFile(join(CLIENT_OUT, f));
}

console.log(`[us-english] patched ${files} files, ${total} replacements`);
if (total === 0) {
  console.error('[us-english] WARNING: zero replacements — upstream catalog format changed? Booting with UK spellings.');
}

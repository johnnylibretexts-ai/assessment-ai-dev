# Assessment AI math rendering runbook

Assessment AI renders reviewer-facing mathematics with self-hosted MathJax 4.1.0, the matching
New Computer Modern 4.1.0 font, and the matching mhchem 4.1.0 font extension. The browser loads
no CDN assets. The Node build stage verifies the
exact npm lock, copies a bounded runtime into the Python image, and writes SHA-256 hashes for every
runtime file to `/app/app/static/vendor/mathjax/integrity.json`.

## Authoring contract

- Inline math: `\(...\)`
- Display math: `\[...\]`
- Dollar delimiters are unsupported.
- Human-facing prose must not contain naked TeX, ASCII pseudo-math, segment markers, HTML, links,
  images, dynamic package loading, or macro definitions.
- Scoring values, computation identities, parameter placeholders, and WeBWorK/IMathAS machine
  templates are not rewritten.

Question and hint textareas intentionally retain canonical TeX. Their previews are populated
through `textContent` and re-typeset with `MathJax.typesetPromise()`; no reviewer text reaches
`innerHTML`. Source equation references are resolved from the stored full source HTML for
presentation. An unresolved reference appears as readable diagnostic text.

## Qualification

```bash
npm ci --ignore-scripts
npm run vendor:mathjax
docker build --target test -t assessment-ai-math-test .
docker build --target runtime -t assessment-ai:math-candidate .
```

Verify the runtime contains `tex-chtml.js`, the safe component, approved TeX extensions, the font
bundle, both Apache-2.0 licenses, and the integrity manifest. Browser qualification must cover a
source excerpt, every question interaction, explanation, critique, hint preview, editor update,
horizontal overflow, CSP/console output, malicious TeX, and an axe A/AA scan. Network logs must
show only same-origin MathJax/font requests.

Before repairing existing data, back up the database and export the exact `current_json`,
`raw_json`, `critique_json`, hint data, status, edit count, and computation/publication bindings for
the bounded draft IDs. Repairs use the normal audited edit/revalidation boundary, retain
`ready_for_review`, reset review confirmations, and never modify raw provider provenance.

## Deployment and rollback

Deploy the image and CSP together. The CSP keeps `script-src 'self'`, `font-src 'self'`, and
`connect-src 'self'`; `style-src 'unsafe-inline'` is limited to MathJax CommonHTML's generated
styles. The sealed corpus receives the same image independently, with its database checksum,
credential absence, reset script, Basic Auth, and mutating-method blocks unchanged.

Rollback restores the previous normal and corpus images plus their prior Caddy files. If a bounded
draft repair must also be reversed, restore only those drafts from the pre-repair manifest through
the audited edit boundary; never replace the whole live database or unrelated review/publication
records.

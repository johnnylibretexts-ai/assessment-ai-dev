# Third-party browser assets

Assessment AI builds the following pinned open-source browser dependency into
its container image. The files are served only from Assessment AI's own
`/static/vendor/` path; no CDN is used at runtime.

- MathJax 4.1.0 — Apache License 2.0
- MathJax New Computer Modern font 4.1.0 — Apache License 2.0
- MathJax mhchem font extension 4.1.0 — Apache License 2.0

The exact npm package integrities are recorded in `package-lock.json`. The
runtime asset build also writes `app/static/vendor/mathjax/integrity.json` with
the SHA-256 digest and byte length of every copied file. The upstream MathJax
license is included with the runtime assets.

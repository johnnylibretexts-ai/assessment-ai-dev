# Third-party components

No upstream source is vendored in this repository. Everything is pulled during
`docker build`, pinned by digest, version, or lockfile.

**Running this raises no licensing question.** The obligations attach when you
redistribute a **built image**.

The dependency surface is unusually clean: **48 Python packages, all
permissive, none GPL or AGPL.** The things that actually need a decision are not
licences at all — they are the **commercial AI APIs** this service calls. Those
are covered at the bottom.

## What the runtime image contains

Built from `Dockerfile`, in two stages.

| Component | Pinned | License |
|---|---|---|
| [node:22.23.0-alpine3.23](https://hub.docker.com/_/node) — build stage only, discarded | digest `sha256:35e2f965…` | MIT (Node) over Alpine, mixed |
| [python:3.12-slim](https://hub.docker.com/_/python) — runtime base | `3.12-slim` | PSF-2.0 + Debian base, mixed |
| MathJax | `4.1.0` | Apache-2.0 |
| MathJax New Computer Modern font | `4.1.0` | Apache-2.0 |
| MathJax mhchem font extension | `4.1.0` | Apache-2.0 |
| Python runtime packages | see below | see below |

The Node stage exists only to vendor MathJax and does not survive into the
runtime image. **No CDN is used at runtime** — MathJax is served from Assessment
AI's own `/static/vendor/` path.

The asset build writes `app/static/vendor/mathjax/integrity.json` with the
SHA-256 digest and byte length of every copied file, and the upstream MathJax
licence ships alongside the assets. Exact npm integrities are in
`package-lock.json`.

### Python runtime packages

Read from installed `dist-info` metadata, not from memory.

**Weak copyleft — one package**

- `certifi` — **MPL-2.0**. It is Mozilla's CA certificate bundle. MPL is
  file-level copyleft, so depending on it unmodified places no obligation on
  your code.

**Permissive — everything else**

`fastapi` (MIT) · `starlette` (BSD-3-Clause) · `uvicorn` (BSD-3-Clause) ·
`uvloop` (MIT) · `httptools` (MIT) · `watchfiles` (MIT) · `websockets`
(BSD-3-Clause) · `click` (BSD-3-Clause) · `pyyaml` (MIT) · `pydantic` (MIT) ·
`pydantic_core` (MIT) · `pydantic_settings` (MIT) · `annotated_types` (MIT) ·
`annotated_doc` (MIT) · `typing_inspection` (MIT) · `typing_extensions`
(PSF-2.0) · `python_dotenv` (BSD-3-Clause) · `httpx` (BSD-3-Clause) ·
`httpcore` (BSD-3-Clause) · `h11` (MIT) · `anyio` (MIT) · `idna` (BSD-3-Clause)
· `sqlalchemy` (MIT) · `greenlet` (MIT AND PSF-2.0) · `jinja2` (BSD-3-Clause) ·
`markupsafe` (BSD-3-Clause) · `beautifulsoup4` (MIT) · `soupsieve` (MIT) ·
`lxml` (BSD-3-Clause) · `pillow` (MIT-CMU/HPND) · `python_multipart`
(Apache-2.0)

### The `compute` extra — computation sidecar only

Installed into `Dockerfile.compute`, not into the main runtime image.

`sympy` (BSD-3-Clause) · `mpmath` (BSD-3-Clause) · `pint` (BSD-3-Clause) ·
`flexcache` (BSD) · `flexparser` (BSD-3-Clause) · `platformdirs` (MIT) ·
`ucumvert` (MIT) · `lark` (MIT)

### Development only — never in a runtime image

`pytest` (MIT) · `pytest_asyncio` (Apache-2.0) · `pluggy` (MIT) · `iniconfig`
(MIT) · `packaging` (Apache-2.0 OR BSD-2-Clause) · `ruff` (MIT) · `playwright`
(Apache-2.0) · `pyee` (MIT) · `pygments` (BSD-2-Clause)

## The authentication sidecar

`docker-compose.yml` runs **oauth2-proxy**, pulled by immutable digest
(`quay.io/oauth2-proxy/oauth2-proxy@sha256:10a11657…`), licensed **MIT**. It is
never rebuilt here, only pinned.

---

## The part that actually needs a decision: commercial AI APIs

Assessment AI generates drafts by calling **third-party hosted models**. These
are paid services under their own terms, not open-source dependencies, and no
licence in this repository grants access to any of them.

| Service | Default | What it is |
|---|---|---|
| **Ollama Cloud** | `https://ollama.com`, model `gpt-oss:120b` | A commercial hosted inference API. The `gpt-oss` model weights are Apache-2.0, but you are buying **hosted inference**, not self-hosting the model — the API terms are Ollama's. |
| **Google Gemini API** | `https://generativelanguage.googleapis.com/v1beta`, model `gemini-3.5-flash` | Proprietary, paid, subject to Google's Generative AI terms. |

`ASSESSMENT_AI_LLM_PROVIDER_ORDER` decides which is tried first. **Both require
your own API key**, and neither key exists anywhere in this repository.

Two consequences worth stating plainly, because they are the kind of thing that
surprises people after deployment rather than before:

- **Page text is sent to a third party.** Generation transmits source content
  from the page being drafted from to whichever provider is configured. Whether
  that is acceptable is a decision about your content and your agreement with
  that provider.
- **Model output is not licensed by us.** Rights in generated assessment items
  are governed by the provider's terms, not by this repository's MIT grant.

## Content this software touches

- **Source pages** are read from LibreTexts and carry their own licences —
  typically Creative Commons, varying per book.
- **Generated drafts** are AI-generated and, by design, unreviewed until a human
  approves them. Nothing here should be described as reviewed or clinically
  approved on the strength of having been generated.
- **Published items** are written into ADAPT, whose own terms and content
  licensing apply once they land there.

None of the above is covered by this repository's MIT grant, which covers the
software only.

## Why this repository is MIT

Everything under `app/`, `evaluation/`, `scripts/` and `deploy/` is original
work. Nothing here contains or modifies copyleft source, and the single MPL-2.0
package is a CA bundle consumed unmodified.

## Refreshing this file

`uv.lock` and `package-lock.json` are both committed and are the authoritative
records. To regenerate the licence list from what is actually installed:

```bash
find .venv/lib/*/site-packages -maxdepth 1 -name '*.dist-info' | while read -r d; do
  n=$(basename "$d" | sed 's/-[0-9].*//')
  l=$(grep -m1 -E '^License(-Expression)?:' "$d/METADATA" | cut -d: -f2- | sed 's/^ *//')
  printf '%-26s %s\n' "$n" "${l:-UNKNOWN}"
done | sort
```

This supersedes the earlier `THIRD_PARTY_NOTICES.md`, which covered only the
browser assets.

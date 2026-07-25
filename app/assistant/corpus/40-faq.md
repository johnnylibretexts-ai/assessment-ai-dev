# Questions testers actually ask

## "Why is the Publish button missing / greyed out?"

Several separate things must all be true, and the control hides rather than erroring:

1. `ADAPT_PUBLISHING_ENABLED` is on, **and** the service password, owned folder id, folder name, and
   author are all configured. Any one missing reports as misconfigured, not enabled.
2. The draft is approved — which itself requires that both Bloom level and difficulty were
   confirmed.
3. The draft has a confirmed curated topic.
4. The source license check passes.

Check the runtime facts for this request to see which of these is actually the blocker on this
deployment.

## "Why did my generation fail?"

Most common causes, in rough order of frequency:

- The URL isn't an accepted public LibreTexts library page. It must be a full HTTPS URL on a
  `*.libretexts.org` library host. Book or chapter landing pages often don't carry enough prose.
- Public sources are disabled on this deployment.
- The page is very short. Concept extraction needs actual explanatory text; a page that's mostly a
  figure or a table of contents gives the model nothing to work with.
- The provider returned output that failed schema validation on every attempt. The service retries
  with the validation errors fed back, but it gives up rather than storing something malformed.
- A requested item type needs an engine that isn't configured — WeBWorK or IMathAS in particular.

Generation runs as a background job, so a failure is recorded on the job with an error code rather
than thrown at the form.

## "Why can't I approve this draft?"

Approval requires both the Bloom level and the difficulty confirmation checkboxes. Confirming only
one leaves the approve action refused. This is deliberate friction, not a bug.

If hints are present, the hint ladder is reviewed separately — an approved question can still have
an unreviewed ladder.

## "Can I edit the question text?"

Yes. Editing creates a new immutable revision rather than overwriting; you'll see it become v2, v3,
and so on. Editing an already-approved draft returns it to review, and a later publication of an
edited revision creates a *new* ADAPT question rather than patching the existing one.

## "Does approving publish it?"

No. Approval never contacts ADAPT. Publication is a separate deliberate action on a separate panel.

## "Can it write to the LibreTexts textbook?"

No. Source pages are read-only through a fixed read proxy. There is no write path to any library
page. "Publishing" here means creating a question in the ADAPT dev shared question bank.

## "What model wrote this?"

Every model call stores its provider, model id, prompt version, attempt number, and raw response.
The draft page shows the model that produced the current revision. The runtime facts for this
request name the model currently configured.

## "Why does the same page give me different questions each time?"

Generation isn't pinned to a fixed seed, and the critique-and-revision pass can take a draft in a
different direction. Two runs over the same page legitimately produce different items.

## "The math looks like raw LaTeX in the editor."

That's intended. Editors show raw canonical TeX with a rendered preview beside it, so you can see
exactly what will be stored. Only `\(...\)` for inline and `\[...\]` for display math are accepted;
dollar-sign delimiters and bare TeX commands are rejected at the schema boundary. Rendering is
self-hosted MathJax — the browser makes no CDN requests.

## "What are the 380 drafts I've heard about?"

A qualification run generated 380 drafts to exercise the pipeline at volume. Those are
**AI-generated, unreviewed dev/demo material**. They were not human-reviewed, are not
student-facing, were never published, and carry no pedagogical endorsement. They exist to prove the
pipeline handles volume, nothing more.

## "Is anything here connected to real students?"

No. This is a dev/demo stack end to end. No real course, no real gradebook, no real student data.

## Known rough edges

- Generation can take 15–60 seconds depending on page length and provider load. The progress
  messages are indicative, not a real percentage of work done.
- Very long source pages are truncated to a character budget before being sent to the model, so an
  item may cite only from the earlier part of a long page.
- Image hotspot items need an eligible image on the source page; pages without one silently produce
  other item types instead.

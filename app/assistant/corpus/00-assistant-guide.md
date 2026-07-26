# What Assessment AI is and how to use it

LibreTexts Assessment AI drafts assessment questions from public LibreTexts textbook pages, then
puts every draft through human review before anything can be published. It is a dev/demo service on
`assess-ai.libretexts.dev`. It is not a production LibreTexts service.

The guiding rule of the whole app: **a model may draft, but only a person may approve, and approval
and publication are two separate deliberate actions.**

## The workflow, end to end

1. **Pick a source page.** The generation form on the home page accepts exactly one public
   LibreTexts page URL per request — a full HTTPS URL from a `*.libretexts.org` library
   (`bio`, `biz`, `chem`, `eng`, `espanol`, `geo`, `human`, `k12`, `math`, `med`, `phys`,
   `socialsci`, `stats`, `workforce`). Whole-book crawling is not supported, on purpose.
2. **Generate.** The page is read through a fixed read-only proxy, its paragraphs are normalized
   with stable offsets so citations stay anchored, concepts are extracted, and one or more items are
   drafted. A separate critique-and-revision pass then rewrites the draft before you ever see it.
   Generation runs as a background job, so you can leave the page and come back.
3. **Review the draft.** Open the draft to see the stem, the choices, the marked answer, the
   explanation, which source paragraphs it cites, the critique issues the revision pass raised, and
   the model and prompt version that produced it.
4. **Edit if needed.** Editing creates a new immutable revision rather than overwriting the old one.
   Revisions are shown as v1, v2, v3.
5. **Confirm Bloom level and difficulty.** These are two separate checkboxes and both must be
   confirmed before the Approve control will accept the review. This is deliberate friction — it
   exists so a reviewer has to actually look at the cognitive demand, not just click through.
6. **Approve.** Approval does **not** contact ADAPT. It only marks the draft as human-approved.
7. **Publish (separate action).** The publication panel appears separately. It enforces the source
   license, requires a confirmed curated topic, and only offers **Publish to ADAPT** when publishing
   is actually configured on this deployment.

## Approved is not published

This trips people up constantly. **Approved** means a human reviewed it and accepted it.
**Published** means it was additionally pushed to the ADAPT dev shared question bank as a bank item
plus an immutable QTI 3.0.1 package. A draft can sit approved forever without being published.

## What Bloom level means here

Bloom's taxonomy classifies the *cognitive demand* of a question — what the student actually has to
do with their knowledge:

- **Remember** — recall a fact, term, or definition.
- **Understand** — explain an idea in their own terms, interpret, summarize.
- **Apply** — use a procedure or principle in a new but similar situation.
- **Analyze** — break something into parts and reason about how those parts relate.
- **Evaluate** — judge, critique, or justify a position against criteria.
- **Create** — assemble something new from the pieces.

The model proposes a level. You confirm or change it. A common review finding is that a question
labelled "Apply" is really "Remember" wearing a costume — it looks like a scenario but the student
only has to recall one fact to answer it.

## What difficulty means here

Difficulty is the expected effort for the intended audience, independent of Bloom level. A Remember
question can be hard (obscure fact) and an Analyze question can be easy (obvious contrast). They are
confirmed separately because they measure different things.

## Citations and grounding

Every generated item records which source paragraphs it drew on, by stable index. The draft page
shows those paragraphs. If an item makes a claim that isn't in its cited paragraphs, that's a
grounding failure and a legitimate reason to reject or edit it.

## Hints

When hint generation is enabled, an item can carry a **graduated hint ladder** — a sequence of rungs
that give progressively more help without ever handing over the answer. Hints are reviewed and
approved separately from the question itself. The system actively checks hints for answer leakage
and will refuse to accept a rung that reveals the correct response.

## Provenance

Every model call is stored with the provider, model id, prompt version, attempt number, and raw
response. That's why a draft page can tell you exactly which model wrote it. Nothing generated is
anonymous.

## What the model cannot do

- It cannot approve its own drafts.
- It cannot publish.
- It cannot write to any LibreTexts page. Source pages are read-only, always. The word "publishing"
  in this service means publishing a reviewed question into the ADAPT dev question bank — never
  editing a textbook.

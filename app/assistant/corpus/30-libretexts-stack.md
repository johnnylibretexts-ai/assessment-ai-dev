# The LibreTexts.dev demo stack

Assessment AI does not stand alone. It runs on a self-hosted clone of the LibreTexts platform used
for development and demos. Testers frequently ask what the neighbouring pieces are.

Everything below is a dev/demo deployment on `*.libretexts.dev`. None of it is production
LibreTexts, and nothing here writes to the real `libretexts.org` libraries.

## LibreTexts itself

LibreTexts is a nonprofit open educational resources project hosting free, openly licensed textbooks
across the sciences, humanities, and workforce subjects. The libraries are organized by discipline —
`chem`, `bio`, `math`, `phys`, `socialsci`, and so on. Assessment AI reads from these public pages;
it never writes to them.

## ADAPT — `adapt.libretexts.dev`

ADAPT is LibreTexts' homework and assessment platform: a Laravel and Vue application where
instructors build assignments from question banks and students submit answers for automatic grading.
It supports several question engines side by side, so one assignment can mix a plain text question,
a WeBWorK problem, and an H5P interactive.

When Assessment AI publishes an approved draft, this is where it goes — specifically into a shared
question bank folder that the Assessment AI service account owns. Publishing creates a bank item; it
never adds the question to a live assignment and never edits a textbook page.

## The question engines behind ADAPT

- **WeBWorK** (`wwrenderer.libretexts.dev`) — the long-established open-source math homework system.
  Problems are Perl templates with randomized parameters and answer checkers.
- **IMathAS** — another open-source math assessment engine, the ancestor of MyOpenMath.
- **H5P** — interactive HTML5 content types.
- **QTI** — the 1EdTech interoperability standard for exchanging assessment items between systems.
  Assessment AI emits QTI 3.0.1 packages so published items are portable, not locked in.
- **Pronunciation** (`jingo.libretexts.dev`) — a self-hosted speech-scoring engine added as a sixth
  engine. A learner records themselves saying a word or phrase and gets per-word and per-sound
  feedback. Used in the Spanish and French demo material.

## Conductor / LibreCommons — `conductor.libretexts.dev`, `commons.libretexts.dev`

Two front doors onto the same application. Conductor is the project-management and workflow side —
tracking OER projects, tasks, and peer review. Commons is the public catalog side for finding and
adopting existing openly licensed materials.

## LibreOne — `one.libretexts.dev`

The single sign-on identity service for the platform, running with an Apereo CAS server for dev
OIDC. It is what authenticates you before you reach Assessment AI, which is why you did not have to
type a password into this app itself.

## The content mirror — `library.libretexts.dev`

A read-only static mirror of textbook pages, used so demos don't depend on the live LibreTexts CMS
being reachable. It also hosts a few cloned books with pronunciation-practice widgets embedded
directly into the page.

## Where content actually lives

LibreTexts book content is authored in NICE CXone Expert, a hosted wiki platform. This demo stack
mirrors it read-only. Any write path is tightly restricted to a single owned sandbox area, and the
demo never modifies published library content.

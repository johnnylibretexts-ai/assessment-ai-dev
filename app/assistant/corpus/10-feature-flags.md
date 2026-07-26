# Feature flags and what each one changes

Assessment AI ships almost every capability behind an off-by-default flag. This is why two
deployments of the same image can behave very differently, and why a control a tester expects may
simply not be on the page.

The live state of every flag is supplied separately in the runtime facts for the current request —
prefer that over anything written here.

| Flag | Off (default) | On |
|---|---|---|
| `PUBLIC_SOURCES_ENABLED` | The generation form refuses public LibreTexts URLs | Public `*.libretexts.org` pages can be used as sources |
| `ADVANCED_ITEMS_ENABLED` | Only the basic item types are offered | The richer interaction types become selectable |
| `PARAMETERIZED_ITEMS_ENABLED` | WeBWorK and IMathAS item generation is refused | Parameterized items may be generated, if their engine is also configured |
| `HINT_GENERATION_ENABLED` | No hint ladders are generated or reviewed | Items can carry a graduated hint ladder |
| `WEBWORK_ENABLED` | WeBWorK items cannot be generated | The self-hosted WeBWorK renderer is available |
| `IMATHAS_ENABLED` | IMathAS items cannot be generated | IMathAS is available, provided its bridge token is set |
| `ADAPT_PUBLISHING_ENABLED` | The **Publish to ADAPT** control never appears | Publishing is offered once the folder, credentials, author, and license all check out |
| `ASSISTANT_ENABLED` | This chat panel does not exist | This chat panel exists |

## Publishing has more than a flag

`ADAPT_PUBLISHING_ENABLED=true` alone is not enough. Publishing reports as configured only when the
service password, the owned folder id, the folder name, and the author are all present. Missing any
of them reports as misconfigured rather than enabled, and the publish control stays hidden. On top
of that, the draft itself must be approved, must have a confirmed curated topic, and must satisfy
the source license check.

## Assessment Computation

There is a separate computation subsystem with three modes — `off`, `assist`, and `enforce`. Its
default is `off`. It is a LibreTexts-owned, assessment-specific validation layer for checking the
mathematical assertions in an item; it is not a general calculator, query service, or code runner.

In `assist` it can produce validation reports but adds no new approval gate. In `enforce` it can
block approval and publication on missing or failed evidence. Its qualification registry ships
empty on purpose, which means `enforce` fails closed even for an otherwise valid configuration.

Importantly, a `validated` computation result only means the structured computational assertions
passed. Source fidelity, wording, accessibility, and pedagogy remain mandatory human review, and
human approval plus the separate publication action are never bypassed.

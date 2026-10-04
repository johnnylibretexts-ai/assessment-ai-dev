# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the actual label strings used in this repo's issue tracker.

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table.

Edit the right-hand column to match whatever vocabulary you actually use.

## Creating the labels

They do not exist in `johnnylibretexts-ai/assessment-ai-dev` until something creates them. `gh issue edit --add-label` fails on a label that isn't defined, so create them once, up front:

```bash
REPO=johnnylibretexts-ai/assessment-ai-dev
gh label create needs-triage    --repo "$REPO" --color FBCA04 --description "Maintainer needs to evaluate this issue"
gh label create needs-info      --repo "$REPO" --color D4C5F9 --description "Waiting on reporter for more information"
gh label create ready-for-agent --repo "$REPO" --color 0E8A16 --description "Fully specified, ready for an AFK agent"
gh label create ready-for-human --repo "$REPO" --color 1D76DB --description "Requires human implementation"
gh label create wontfix         --repo "$REPO" --color FFFFFF --description "Will not be actioned"
```

`wontfix` ships with new GitHub repos by default, so its `create` may fail as already-existing — that is fine, leave it.

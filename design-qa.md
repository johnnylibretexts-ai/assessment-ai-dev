# Design QA — generation progress state

- Source visual truth: `/Users/johnnyrobot/Desktop/Screenshot 2026-07-11 at 3.39.12 PM.png`
- Desktop implementation: `/tmp/assessment-ai-generation-normal.png`
- Loading implementation: `/tmp/assessment-ai-generation-loading.png`
- Mobile loading implementation: `/tmp/assessment-ai-generation-mobile-loading.png`
- Side-by-side source comparison: `/tmp/assessment-ai-generation-reference-comparison.png`
- Desktop viewport: 1439 × 1008
- Mobile viewport: 390 × 844
- States: populated queue at rest; generation request in progress

## Full-view comparison evidence

The source screenshot and desktop implementation were combined into
`/tmp/assessment-ai-generation-reference-comparison.png`
at the same 1439 × 1008 viewport. Header height, maximum content width, hero hierarchy, form-card
position and proportions, button placement, queue spacing, draft card, typography, border colors,
radii, shadows, and semantic colors remain visually aligned. The implementation input has a focus
ring because browser automation filled it immediately before capture; this is an expected interaction
state rather than design drift.

## Focused loading-state evidence

`/tmp/assessment-ai-generation-loading.png` shows the new desktop state directly beneath the
existing URL row. It uses the
existing brand blue, muted text, border radius, and panel spacing. The submit button retains its
width while changing to “Generating…” and becomes disabled. The status block displays one rotating
plain-language message, animated sequential trailing dots, and a keep-open duration note without
obscuring the queue.

`/tmp/assessment-ai-generation-mobile-loading.png` confirms the form stacks at 390 px, the loading
block stays within the card,
copy remains readable, the primary button remains visible, and there is no horizontal overflow.
The focused region was necessary because the requested animation is not visible in the source's
resting state.

## Required fidelity surfaces

- Fonts and typography: existing system/Inter stack, weights, hierarchy, line height, wrapping, and
  small-label treatment are preserved. Loading copy follows the same hierarchy and optical weight.
- Spacing and layout rhythm: the resting page remains aligned to the source. Loading adds one compact
  14 px-separated block inside the form card and stacks cleanly at the mobile breakpoint.
- Colors and visual tokens: loading uses the existing brand, brand-dark, muted, and blue-border
  family; contrast remains clear in normal and disabled states.
- Image quality and asset fidelity: the screen contains no raster illustrations, logos, or custom
  icon assets to reproduce. No replacement image assets or handcrafted icons were introduced.
- Copy and content: status language is student-friendly, concise, and sets a realistic expectation
  to keep the page open. Rotating messages describe the generation workflow without exposing model
  or infrastructure jargon.
- Accessibility and interaction: the status uses `role="status"`, polite live announcements, an
  atomic update, `aria-busy` on the form, a disabled submit control to prevent duplicates, a
  back-forward-cache reset, and a static-dot reduced-motion mode.

## Findings

No actionable P0, P1, or P2 visual or interaction issues remain. The filled desktop input shows its
normal focus ring in the automated capture; this is acceptable P3 state variance.

## Comparison history

- Initial comparison: desktop resting layout matched the supplied screenshot; no P0/P1/P2 drift.
- Loading comparison: progress panel, button state, rotating message, trailing dots, and duration
  guidance were visible and contained.
- Responsive comparison: mobile viewport showed a readable stacked form and contained loading panel
  with no horizontal overflow.
- Console check: no browser console errors.

## Implementation checklist

- [x] Preserve existing visual system and resting layout.
- [x] Disable duplicate submissions while the request is active.
- [x] Show animated trailing dots and rotating plain-language progress text.
- [x] Provide live-region and reduced-motion behavior.
- [x] Verify desktop and mobile loading states in the in-app browser.
- [x] Confirm zero browser console errors.

final result: passed

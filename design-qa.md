# Design QA — public source link containment

- Source visual truth: `<home>/Desktop/Screenshot 2026-07-11 at 12.23.52 PM.png`
- Rendered implementation: `/tmp/assessment-ui-after.png`
- Responsive implementation: `/tmp/assessment-ui-mobile-viewport.png`
- Desktop viewport: 1206 × 900 CSS pixels, full-page capture at 1191 × 1522 pixels
- Mobile viewport: 390 × 844 CSS pixels, viewport capture at 375 × 844 pixels
- State: public Chemistry draft `#2`, ready for review, edit panel collapsed

## Full-view comparison evidence

The reference establishes the two-column review composition: sticky source card on the left and the
review form on the right. The revised implementation preserves that hierarchy, spacing, card
treatment, typography, colors, review controls, and cited-paragraph presentation. The public source
URL is now represented by a compact action inside the source card instead of exposing the full URL
as visible text.

The source screenshot predates public-only mode, so its sandbox title and content intentionally
differ from the rendered public Chemistry draft. Those content differences are not design drift.

## Focused-region comparison evidence

The source/provenance region was checked directly because it contains the reported defect. At the
desktop viewport:

- Source card bounds: x=16–528.55, client width 511px.
- Source link bounds: x=41–245.92, width 204.92px.
- Review card begins at x=548.55.
- The link is fully contained, the columns remain separated, and document scroll width equals the
  viewport width.

At 390px responsive width, the source card and review card stack into one 343px column. The link is
fully contained and the document has no horizontal overflow.

## Comparison history

1. Earlier finding — P1: the visible canonical URL could extend beyond the source card and overlap
   the review column.
2. Fix — retained the canonical URL in `href` and `title`, changed visible copy to “Open original
   LibreTexts page,” added `min-width: 0` to grid children, and added defensive `overflow-wrap` and
   width constraints to the source card/link.
3. Post-fix evidence — desktop and mobile captures show the action contained within the card;
   measured bounds confirm no overlap or horizontal overflow.

## Required fidelity surfaces

- Fonts and typography: existing system font stack, weights, hierarchy, line heights, and wrapping
  remain unchanged; the compact source action uses the existing link styling with a clear 700
  weight.
- Spacing and layout rhythm: two-column grid, 20px gap, card padding/radii, sticky positioning, and
  responsive stacking are preserved.
- Colors and visual tokens: existing brand, muted text, border, surface, warning, and success tokens
  are unchanged.
- Image quality and asset fidelity: this screen contains no image assets; no assets were introduced
  or replaced.
- Copy and content: the full raw URL was replaced only as visible copy. The canonical URL remains
  available as the actual link destination and tooltip, while library and page ID remain visible.

## Findings

No actionable P0, P1, or P2 findings remain for the reported overflow defect.

## Interaction and console checks

- The source link is exposed as one accessible link with the expected canonical LibreTexts URL.
- Review controls remain present in the DOM.
- Browser console warnings/errors: none.

## Follow-up polish

No P3 follow-up is needed for this scoped fix.

final result: passed

(() => {
  "use strict";

  const hintReview = document.querySelector("[data-hint-review-form]");
  const hintApprove = hintReview?.querySelector("[data-hint-approve]");
  const hintChecks = hintReview
    ? Array.from(hintReview.querySelectorAll('input[type="checkbox"]'))
    : [];
  const hintEdit = document.querySelector("[data-hint-edit-form]");
  const hintDirtyMessage = document.querySelector("[data-hint-dirty-message]");
  let hintEditsDirty = false;

  const refreshHintApproval = () => {
    if (!hintApprove) return;
    const allChecked =
      hintChecks.length === 3 && hintChecks.every((input) => input.checked);
    hintApprove.disabled = hintEditsDirty || !allChecked;
    hintApprove.title = hintEditsDirty
      ? "Save the current hint edits before approving this version."
      : "";
    if (hintDirtyMessage) {
      hintDirtyMessage.hidden = !hintEditsDirty;
    }
  };

  hintChecks.forEach((input) => {
    input.addEventListener("change", refreshHintApproval);
  });
  hintEdit?.addEventListener("input", () => {
    hintEditsDirty = true;
    refreshHintApproval();
  });
  refreshHintApproval();

  const firstInvalid = document.querySelector('[aria-invalid="true"]');
  if (firstInvalid instanceof HTMLElement) {
    firstInvalid.focus();
  }
})();

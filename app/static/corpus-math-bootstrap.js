(() => {
  "use strict";

  const SEGMENT_MARKER = /\[\/?SEG\d+\]/gi;
  const DISPLAY_SELECTORS = [
    ".source-paragraph p",
    ".review-pane > .stimulus p",
    ".review-pane > h2",
    ".review-pane > .choices li",
    ".review-pane > .interaction-preview",
    ".review-pane > .interaction-table td",
    ".review-pane > .explanation p",
    ".review-pane > .explanation + details li",
  ];

  function stripSegmentMarkers(root) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      node.textContent = node.textContent.replace(SEGMENT_MARKER, "");
    }
  }

  function addPreview(field, label) {
    if (!field?.id || field.closest("pre, code") || field.dataset.mathPreviewAttached) {
      return;
    }
    field.dataset.mathPreviewAttached = "true";
    field.classList.add("math-raw");
    const preview = document.createElement("div");
    preview.className = "math-preview math-content";
    preview.dataset.mathPreviewFor = field.id;
    preview.setAttribute("aria-label", label);
    field.insertAdjacentElement("afterend", preview);
  }

  document.addEventListener("DOMContentLoaded", async () => {
    const targets = [];
    for (const element of document.querySelectorAll(DISPLAY_SELECTORS.join(","))) {
      if (element.matches("pre, code, select, textarea") || element.querySelector("pre, code")) {
        continue;
      }
      element.classList.add("math-content");
      stripSegmentMarkers(element);
      targets.push(element);
    }

    addPreview(document.querySelector("#stem"), "Rendered question preview");
    addPreview(document.querySelector("#explanation"), "Rendered explanation preview");
    for (const field of document.querySelectorAll(".choice-edit input[type='text']")) {
      addPreview(field, `Rendered choice ${field.id.replace("choice_", "")} preview`);
    }
    for (const field of document.querySelectorAll(".hint-rung textarea")) {
      if (!field.id) {
        field.id = `corpus-hint-${field.name}`;
      }
      addPreview(field, "Rendered hint preview");
    }

    if (targets.length && window.MathJax?.typesetPromise) {
      try {
        await window.MathJax.typesetPromise(targets);
      } catch {
        const warning = document.querySelector("#math-rendering-warning");
        if (warning) {
          warning.hidden = false;
        }
      }
    }
  });
})();

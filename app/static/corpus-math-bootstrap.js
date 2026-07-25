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

  function canonicalizeServerPreview(value) {
    let text = value.replace(SEGMENT_MARKER, "");
    const protectedMath = [];
    const protect = (rendered) => {
      const token = `\u0000MATH${protectedMath.length}\u0000`;
      protectedMath.push([token, rendered]);
      return token;
    };

    text = text.replace(
      /\\\((?:\\.|[^\\])*?\\\)|\\\[(?:\\.|[^\\])*?\\\]/gs,
      (match) => protect(match),
    );
    text = text.replace(
      /\by\(x\)\s*=\s*\(a\s*\+\s*b\s*\*\s*x\)\s*\*\s*e\s*\^\s*\(\s*alpha\s*\*\s*x\s*\)/gi,
      () => protect(String.raw`\(y(x) = (a + bx)e^{\alpha x}\)`),
    );
    text = text.replace(
      /\bk1(?:\*\*|\^)\s*2\s*-\s*4\s*\*\s*k2\s*(<=|>=|=|<|>)\s*0/gi,
      (_, operator) => protect(String.raw`\(k_1^2 - 4k_2 ${operator} 0\)`),
    );
    text = text.replace(
      /\balpha\s*\+\/-\s*i\s*\*\s*beta\b/gi,
      () => protect(String.raw`\(\alpha \pm i\beta\)`),
    );
    text = text.replace(
      /\balpha\s*=\s*-k1\s*\/\s*2\b/gi,
      () => protect(String.raw`\(\alpha = -\frac{k_1}{2}\)`),
    );
    text = text.replace(
      /\balpha\s*=\s*(-?\d+(?:\.\d+)?)\s*\/\s*(\d+(?:\.\d+)?)\b/gi,
      (_, numerator, denominator) =>
        protect(String.raw`\(\alpha = \frac{${numerator}}{${denominator}}\)`),
    );
    text = text.replace(
      /\bk([12])\s*=\s*(-?\d+(?:\.\d+)?)\b/gi,
      (_, suffix, number) => protect(String.raw`\(k_${suffix} = ${number}\)`),
    );
    text = text.replace(
      /\be\s*\^\s*\(\s*alpha\s*\*\s*x\s*\)/gi,
      () => protect(String.raw`\(e^{\alpha x}\)`),
    );
    for (const [pattern, rendered] of [
      [/\balpha\b/gi, String.raw`\(\alpha\)`],
      [/\bbeta\b/gi, String.raw`\(\beta\)`],
      [/\bk1\b/gi, String.raw`\(k_1\)`],
      [/\bk2\b/gi, String.raw`\(k_2\)`],
    ]) {
      text = text.replace(pattern, () => protect(rendered));
    }
    if (/^\s*-?\d+(?:\.\d+)?\s*$/.test(text)) {
      text = protect(String.raw`\(${text.trim()}\)`);
    }
    for (const [token, rendered] of protectedMath) {
      text = text.replaceAll(token, rendered);
    }
    return text;
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
    for (const cell of document.querySelectorAll(
      ".parameter-preview-table tbody td:not(:first-child)",
    )) {
      cell.textContent = canonicalizeServerPreview(cell.textContent ?? "");
      cell.classList.add("math-content");
      targets.push(cell);
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

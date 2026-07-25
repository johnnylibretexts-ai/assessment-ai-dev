"use strict";

(() => {
  const warning = document.getElementById("math-rendering-warning");
  const pending = new Map();
  let renderQueue = Promise.resolve();

  function showWarning() {
    if (warning) {
      warning.hidden = false;
    }
  }

  function mathJaxReady() {
    return (
      window.MathJax && typeof window.MathJax.typesetPromise === "function"
    );
  }

  function typeset(elements, updateContent) {
    renderQueue = renderQueue
      .catch(() => undefined)
      .then(async () => {
        if (!mathJaxReady()) {
          showWarning();
          return;
        }
        if (typeof MathJax.typesetClear === "function") {
          MathJax.typesetClear(elements);
        }
        if (updateContent) {
          updateContent();
        }
        await MathJax.typesetPromise(elements);
      })
      .catch(() => {
        showWarning();
      });
    return renderQueue;
  }

  function updatePreview(preview) {
    const sourceId = preview.dataset.mathPreviewFor;
    const source = sourceId ? document.getElementById(sourceId) : null;
    if (!source) {
      return;
    }
    preview.dataset.mathPreviewState = "queued";
    void typeset([preview], () => {
      preview.dataset.mathPreviewState = "rendering";
      preview.textContent = source.value;
    }).then(() => {
      preview.dataset.mathPreviewState = "ready";
    });
  }

  function schedulePreview(preview) {
    const existing = pending.get(preview);
    if (existing) {
      window.clearTimeout(existing);
    }
    pending.set(
      preview,
      window.setTimeout(() => {
        pending.delete(preview);
        updatePreview(preview);
      }, 180),
    );
  }

  function initializePreviews() {
    const previews = Array.from(
      document.querySelectorAll("[data-math-preview-for]"),
    );
    previews.forEach((preview) => {
      const source = document.getElementById(preview.dataset.mathPreviewFor);
      if (!source) {
        return;
      }
      preview.textContent = source.value;
      preview.dataset.mathPreviewState = "initialized";
      source.addEventListener("input", () => {
        preview.dataset.mathPreviewState = "scheduled";
        schedulePreview(preview);
      });
    });
  }

  document.addEventListener("assessment-math-error", showWarning);
  window.addEventListener("error", (event) => {
    if (String(event.filename || "").includes("/static/vendor/mathjax/")) {
      showWarning();
    }
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initializePreviews);
  } else {
    initializePreviews();
  }
})();

(() => {
  const form = document.querySelector(".generate-form");
  const submit = document.querySelector("#generate-submit");
  const status = document.querySelector("#generation-status");
  const message = document.querySelector("#generation-status-message");
  const sourceInput = document.querySelector("#source_locator");

  if (!form || !submit || !status || !message || !sourceInput) return;

  const submitLabel = submit.querySelector("[data-submit-label]");
  const submitBusy = submit.querySelector("[data-submit-busy]");
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const messages = [
    "Reading the LibreTexts page",
    "Finding the clearest teachable concepts",
    "Drafting a cited multiple-choice question",
    "Checking the answer and distractors",
    "Revising the draft for human review",
  ];
  let messageTimer;

  const reset = () => {
    window.clearInterval(messageTimer);
    delete form.dataset.submitting;
    form.classList.remove("is-generating");
    form.removeAttribute("aria-busy");
    submit.disabled = false;
    sourceInput.readOnly = false;
    submitLabel.hidden = false;
    submitBusy.hidden = true;
    status.hidden = true;
    message.textContent = messages[0];
  };

  form.addEventListener("submit", (event) => {
    if (form.dataset.submitting === "true") {
      event.preventDefault();
      return;
    }

    form.dataset.submitting = "true";
    form.classList.add("is-generating");
    form.setAttribute("aria-busy", "true");
    submit.disabled = true;
    sourceInput.readOnly = true;
    submitLabel.hidden = true;
    submitBusy.hidden = false;
    status.hidden = false;
    message.textContent = messages[0];

    if (!reduceMotion.matches) {
      let messageIndex = 0;
      messageTimer = window.setInterval(() => {
        messageIndex = (messageIndex + 1) % messages.length;
        message.textContent = messages[messageIndex];
      }, 4000);
    }
  });

  window.addEventListener("pageshow", reset);
})();

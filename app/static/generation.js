(() => {
  const form = document.querySelector(".generate-form");
  const submit = document.querySelector("#generate-submit");
  const status = document.querySelector("#generation-status");
  const message = document.querySelector("#generation-status-message");
  const sourceInput = document.querySelector("#source_locator");
  const generationMode = document.querySelector("#generation_mode");
  const itemCount = document.querySelector("#item-count");
  const itemTypes = Array.from(
    document.querySelectorAll('input[name="item_types"]')
  );
  const itemTypeOptions = document.querySelector("#item-type-options");
  const itemTypeHelp = document.querySelector("#item-type-help");
  const defaultItemTypeHelp = itemTypeHelp?.textContent || "";
  const maxSelectedTypes = 8;
  // Auto mix discards item_types server-side (see jobs.py::_validated_item_types),
  // so leaving the boxes clickable invites people to pick formats that are then
  // silently ignored. Locking them is the UI telling the truth about the request
  // it is going to send.
  const autoModeHelp =
    "Auto mix chooses the formats for you, working through the available " +
    "interaction types in order. Switch to Choose types to pick your own.";

  if (!form || !submit || !status || !message || !sourceInput) return;

  const submitLabel = submit.querySelector("[data-submit-label]");
  const submitBusy = submit.querySelector("[data-submit-busy]");
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const messages = [
    "Reading the LibreTexts page",
    "Finding the clearest teachable concepts",
    "Drafting cited assessment items",
    "Checking the answer and distractors",
    "Revising the draft for human review",
  ];
  let messageTimer;

  const selectedTypeCount = () =>
    itemTypes.filter((checkbox) => checkbox.checked).length;

  const validateSelectedTypes = ({ raiseTotal = false } = {}) => {
    if (!generationMode || !itemCount || generationMode.value !== "selected") {
      itemCount?.setCustomValidity("");
      // Only lock when we can actually read the mode. If the control is missing
      // the safe fallback is a usable form, not an unusable one.
      const locked = Boolean(generationMode) && Boolean(itemCount);
      itemTypes.forEach((checkbox) => {
        checkbox.disabled = locked;
      });
      itemTypeOptions?.classList.toggle("is-locked", locked);
      if (itemTypeHelp) {
        itemTypeHelp.textContent = locked ? autoModeHelp : defaultItemTypeHelp;
      }
      return;
    }
    const selected = selectedTypeCount();
    itemTypeOptions?.classList.remove("is-locked");
    itemTypes.forEach((checkbox) => {
      checkbox.disabled = selected >= maxSelectedTypes && !checkbox.checked;
    });
    const total = Number.parseInt(itemCount.value, 10);
    if (raiseTotal && selected > total && selected <= maxSelectedTypes) {
      itemCount.value = String(selected);
    }
    const resolvedTotal = Number.parseInt(itemCount.value, 10);
    const invalid = selected > 0 && resolvedTotal < selected;
    itemCount.setCustomValidity(
      invalid
        ? `Choose at least ${selected} total items to generate each selected type.`
        : ""
    );
    if (itemTypeHelp && selected > 0) {
      itemTypeHelp.textContent =
        `${selected} type${selected === 1 ? "" : "s"} selected. ` +
        `The ${itemCount.value} total item${itemCount.value === "1" ? "" : "s"} ` +
        "will include each selected type once before any type repeats." +
        (selected === maxSelectedTypes
          ? " The maximum of 8 selected types has been reached."
          : "");
    } else if (itemTypeHelp) {
      itemTypeHelp.textContent = defaultItemTypeHelp;
    }
  };

  generationMode?.addEventListener("change", () => validateSelectedTypes());
  itemCount?.addEventListener("input", () => validateSelectedTypes());
  itemTypes.forEach((checkbox) => {
    checkbox.addEventListener("change", () =>
      validateSelectedTypes({ raiseTotal: true })
    );
  });
  validateSelectedTypes();

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

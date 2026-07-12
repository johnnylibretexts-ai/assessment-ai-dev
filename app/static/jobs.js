(() => {
  const panel = document.querySelector("[data-generation-job]");
  if (!panel) return;

  const jobId = panel.dataset.generationJob;
  const stage = document.querySelector("#job-stage");
  const heading = document.querySelector("#job-heading");
  const progress = document.querySelector("[role='progressbar']");
  const bar = document.querySelector(".progress-bar");
  const error = document.querySelector("#job-error");
  const result = document.querySelector("#job-result");
  const labels = {
    queued: "Waiting for the generation worker",
    queued_after_restart: "Resuming after a service restart",
    fetching_source: "Reading the LibreTexts source page",
    generating_and_revising: "Drafting, checking, and revising the items",
    concept_extraction: "Identifying source-grounded concepts",
    initial_draft: "Drafting a type-appropriate assessment item",
    critique: "Checking citations, distractors, and answer quality",
    revision: "Revising the draft from the quality check",
    hint_ladder: "Building the conceptual, strategic, and specific hints",
    complete: "Generation complete",
    failed: "Generation could not be completed",
  };

  const poll = async () => {
    try {
      const response = await fetch(`/jobs/${encodeURIComponent(jobId)}`, {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error("The generation status is unavailable.");
      const job = await response.json();
      stage.textContent = labels[job.stage] || job.stage.replaceAll("_", " ");
      progress.setAttribute("aria-valuenow", String(job.progress));
      bar.style.width = `${job.progress}%`;
      if (job.status === "succeeded") {
        heading.textContent = "Assessment items ready for review";
        result.href = job.draft_url || job.queue_url;
        result.hidden = false;
        return;
      }
      if (job.status === "failed") {
        heading.textContent = "Generation stopped";
        error.textContent = job.error || "Generation failed safely.";
        error.hidden = false;
        return;
      }
      window.setTimeout(poll, 1000);
    } catch (caught) {
      error.textContent = caught.message;
      error.hidden = false;
      window.setTimeout(poll, 3000);
    }
  };

  poll();
})();

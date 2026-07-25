(() => {
  const licenseCode = document.querySelector("#license-code");
  const licenseVersion = document.querySelector("#license-version");
  const licenseLabel = document.querySelector("#license-label");

  if (!licenseCode || !licenseVersion || !licenseLabel) return;

  const updateLabel = () => {
    const option = licenseCode.selectedOptions[0];
    const baseLabel = option?.dataset.licenseLabel || "";
    const version = licenseVersion.value.trim();
    licenseLabel.value = baseLabel && version ? `${baseLabel} ${version}` : baseLabel;
  };

  licenseCode.addEventListener("change", updateLabel);
  licenseVersion.addEventListener("input", updateLabel);
})();

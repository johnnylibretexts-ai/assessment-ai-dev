import { createHash } from "node:crypto";
import {
  cpSync,
  existsSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const outputRoot = resolve(
  process.env.MATHJAX_VENDOR_OUTPUT ||
    join(projectRoot, "app", "static", "vendor", "mathjax"),
);
const mathjaxRoot = join(projectRoot, "node_modules", "mathjax");
const fontRoot = join(
  projectRoot,
  "node_modules",
  "@mathjax",
  "mathjax-newcm-font",
);
const mhchemFontRoot = join(
  projectRoot,
  "node_modules",
  "@mathjax",
  "mathjax-mhchem-font-extension",
);

const mathjaxFiles = [
  "LICENSE",
  "tex-chtml.js",
  "a11y/assistive-mml.js",
  "ui/safe.js",
  "input/tex/extensions/boldsymbol.js",
  "input/tex/extensions/cancel.js",
  "input/tex/extensions/cases.js",
  "input/tex/extensions/color.js",
  "input/tex/extensions/empheq.js",
  "input/tex/extensions/enclose.js",
  "input/tex/extensions/mathtools.js",
  "input/tex/extensions/mhchem.js",
  "input/tex/extensions/physics.js",
  "input/tex/extensions/textcomp.js",
  "input/tex/extensions/units.js",
  "input/tex/extensions/upgreek.js",
];

if (
  !existsSync(mathjaxRoot) ||
  !existsSync(fontRoot) ||
  !existsSync(mhchemFontRoot)
) {
  throw new Error("Run npm ci before vendoring MathJax.");
}

rmSync(outputRoot, { recursive: true, force: true });
mkdirSync(outputRoot, { recursive: true });

for (const relativePath of mathjaxFiles) {
  const source = join(mathjaxRoot, relativePath);
  const destination = join(outputRoot, "runtime", relativePath);
  mkdirSync(dirname(destination), { recursive: true });
  cpSync(source, destination);
}

for (const relativePath of ["package.json", "chtml.js", "chtml"]) {
  const source = join(fontRoot, relativePath);
  const destination = join(outputRoot, "mathjax-newcm-font", relativePath);
  mkdirSync(dirname(destination), { recursive: true });
  cpSync(source, destination, { recursive: true });
}

for (const relativePath of ["package.json", "chtml.js", "chtml"]) {
  const source = join(mhchemFontRoot, relativePath);
  const destination = join(
    outputRoot,
    "mathjax-mhchem-font-extension",
    relativePath,
  );
  mkdirSync(dirname(destination), { recursive: true });
  cpSync(source, destination, { recursive: true });
}

const manifestFiles = [];
const collect = async (directory) => {
  const { readdir } = await import("node:fs/promises");
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const absolutePath = join(directory, entry.name);
    if (entry.isDirectory()) {
      await collect(absolutePath);
      continue;
    }
    const relativePath = absolutePath
      .slice(outputRoot.length + 1)
      .replaceAll("\\", "/");
    const body = readFileSync(absolutePath);
    manifestFiles.push({
      path: relativePath,
      bytes: body.length,
      sha256: createHash("sha256").update(body).digest("hex"),
    });
  }
};

await collect(outputRoot);
manifestFiles.sort((left, right) => left.path.localeCompare(right.path));

writeFileSync(
  join(outputRoot, "integrity.json"),
  `${JSON.stringify(
    {
      generatedBy: "scripts/vendor-mathjax.mjs",
      mathjax_version: "4.1.0",
      font_version: "4.1.0",
      mhchem_font_version: "4.1.0",
      license: "Apache-2.0",
      files: manifestFiles,
    },
    null,
    2,
  )}\n`,
);

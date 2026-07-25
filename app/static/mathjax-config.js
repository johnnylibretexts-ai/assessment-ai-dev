"use strict";

window.MathJax = {
  loader: {
    paths: {
      mathjax: "/static/vendor/mathjax/runtime",
      tex: "[mathjax]/input/tex/extensions",
      "mathjax-mhchem-extension":
        "/static/vendor/mathjax/mathjax-mhchem-font-extension",
    },
    load: [
      "a11y/assistive-mml",
      "ui/safe",
      "[tex]/boldsymbol",
      "[tex]/cancel",
      "[tex]/cases",
      "[tex]/color",
      "[tex]/mathtools",
      "[tex]/mhchem",
      "[tex]/physics",
      "[tex]/textcomp",
      "[tex]/units",
      "[tex]/upgreek",
    ],
  },
  tex: {
    inlineMath: [["\\(", "\\)"]],
    displayMath: [["\\[", "\\]"]],
    processEscapes: true,
    processEnvironments: false,
    packages: {
      "[+]": [
        "boldsymbol",
        "cancel",
        "cases",
        "color",
        "mathtools",
        "mhchem",
        "physics",
        "textcomp",
        "units",
        "upgreek",
      ],
      "[-]": ["autoload", "configmacros", "newcommand", "require"],
    },
  },
  output: {
    font: "mathjax-newcm",
    fontPath: "/static/vendor/mathjax/mathjax-newcm-font",
  },
  options: {
    processHtmlClass: "math-content",
    ignoreHtmlClass: "math-raw",
    skipHtmlTags: ["script", "noscript", "style", "textarea", "pre", "code", "select", "option"],
    enableMenu: false,
    enableSpeech: false,
    enableBraille: false,
    enableAssistiveMml: true,
    menuOptions: {
      settings: {
        enrich: false,
        speech: false,
        braille: false,
        assistiveMml: true,
      },
    },
    safeOptions: {
      allow: {
        URLs: "none",
        classes: "none",
        cssIDs: "none",
        styles: "none",
      },
    },
  },
  startup: {
    elements: [".math-content"],
  },
};

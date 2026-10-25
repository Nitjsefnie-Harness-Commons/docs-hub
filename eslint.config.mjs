// ESLint flat config: public/ and tests/.
//
// This does NOT introduce a build step -- the invariant in CONTRIBUTING.md
// still holds. public/*.js is served to the browser byte-for-byte; eslint is
// a CI gate that reads those files and emits nothing. package.json pins
// only CI tooling and lets Dependabot bump it.
//
// Rules are the recommended set (real defects: undeclared identifiers, unused
// bindings, unreachable code, duplicate keys) plus four that catch the
// mistakes this particular file is exposed to: it is one 680-line script with
// no modules, so a stray `var` or an accidental `==` is genuinely hard to see.
// Style opinions -- quotes, semicolons, indentation -- are deliberately absent.

import js from "@eslint/js";
import globals from "globals";

export default [
  {
    // The store holds published documents, which are arbitrary third-party
    // HTML with inline scripts. Linting them would be linting our users.
    ignores: ["store/**", "node_modules/**", ".venv/**"],
  },
  js.configs.recommended,
  {
    files: ["public/**/*.js"],
    languageOptions: {
      ecmaVersion: 2024,
      // Plain <script>, not a module: index.html loads app.js directly.
      sourceType: "script",
      globals: { ...globals.browser },
    },
    rules: {
      "no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
      // `== null` stays allowed ("smart"): it is the idiomatic null-or-undefined
      // test, and this file uses it against optional API fields.
      eqeqeq: ["error", "smart"],
      "no-var": "error",
      "prefer-const": "error",
    },
  },
  {
    files: ["tests/**/*.mjs"],
    languageOptions: {
      sourceType: "module",
      globals: { ...globals.node },
    },
  },
];

// ESLint (flat config) cho script trình duyệt trong static/. Chạy:  npx eslint static
const js = require("@eslint/js");
const globals = require("globals");

module.exports = [
  js.configs.recommended,
  {
    files: ["static/**/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: { ...globals.browser, google: "readonly" },
    },
    rules: {
      // Các file trang là script cổ điển dùng chung phạm vi toàn cục (app.js định nghĩa, theme-toggle.js dùng).
      "no-unused-vars": ["warn", { vars: "local", args: "none", caughtErrors: "none" }],
      "no-empty": ["error", { allowEmptyCatch: true }],
      // "when" is in eslint's builtin list but is not a browser global; page scripts may use the name.
      "no-redeclare": ["error", { builtinGlobals: false }],
    },
  },
  {
    files: ["static/app/sw.js"],
    languageOptions: { globals: { ...globals.serviceworker } },
  },
];

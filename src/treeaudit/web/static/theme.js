// TreeAudit — applies the chosen theme before the page is drawn (loaded without
// "defer", in <head>), so that it never flashes in the wrong colours. The
// button that changes it is in app.js. Without a choice, the system decides.
"use strict";
(() => {
  try {
    const theme = window.localStorage.getItem("treeaudit-theme");
    if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  } catch (error) {
    // Storage unavailable (private window, blocked site data): follow the system.
  }
})();

// Follows the device's light or dark setting. Loaded before the page is drawn.
(function () {
  "use strict";

  var query = window.matchMedia("(prefers-color-scheme: dark)");

  function apply() {
    document.documentElement.setAttribute("data-bs-theme", query.matches ? "dark" : "light");
  }

  apply();
  query.addEventListener("change", apply);
})();

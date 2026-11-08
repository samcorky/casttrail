// Collapses the main navigation behind a menu button on narrow screens.
// Without this script the button stays hidden and the links are always shown.
(function () {
  "use strict";

  var button = document.querySelector(".nav-toggle");
  var menu = document.getElementById("nav-menu");
  if (!button || !menu) {
    return;
  }

  document.documentElement.classList.add("js");
  button.hidden = false;

  function setOpen(open) {
    button.setAttribute("aria-expanded", open ? "true" : "false");
    menu.toggleAttribute("data-open", open);
  }

  button.addEventListener("click", function () {
    setOpen(button.getAttribute("aria-expanded") !== "true");
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && button.getAttribute("aria-expanded") === "true") {
      setOpen(false);
      button.focus();
    }
  });
})();

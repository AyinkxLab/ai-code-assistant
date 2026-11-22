// Organization admin console: show a busy state while a member action posts.
// Confirmation is enforced server-side (a required checkbox) and by the
// browser's form validation, so this script only handles the loading state.
(function () {
  "use strict";

  document.querySelectorAll("form[data-org-admin]").forEach(function (form) {
    form.addEventListener("submit", function () {
      if (!form.checkValidity()) {
        return;
      }
      form.setAttribute("aria-busy", "true");
      var button = form.querySelector("button[type='submit']");
      if (button) {
        button.disabled = true;
        button.textContent = button.getAttribute("data-loading-label") || "Working...";
      }
    });
  });
})();

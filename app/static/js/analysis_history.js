(function () {
  "use strict";
  var meta = document.querySelector('meta[name="csrf-token"]');
  var csrf = meta ? meta.content : "";
  document.addEventListener("click", function (event) {
    var button = event.target.closest("button[data-analysis-id]");
    if (!button) return;
    if (!window.confirm("Delete this saved analysis?")) return;
    fetch("/tools/api/analyses/" + encodeURIComponent(button.dataset.analysisId), {
      method: "DELETE",
      headers: { "X-CSRFToken": csrf },
    }).then(function (response) {
      if (!response.ok) throw new Error("Could not delete this analysis.");
      button.closest("article").remove();
    }).catch(function (error) { window.alert(error.message); });
  });
})();

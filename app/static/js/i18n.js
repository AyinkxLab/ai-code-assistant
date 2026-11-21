/* Client-side translation helper (#714).
 *
 * The server embeds the active catalog in <script id="i18n-catalog">, so
 * templates and JavaScript read the same source of truth instead of keeping
 * duplicate string tables.
 */
(function () {
  const node = document.getElementById("i18n-catalog");
  let messages = {};
  if (node) {
    try {
      messages = JSON.parse(node.textContent);
    } catch (error) {
      messages = {};
    }
  }

  const locale = document.documentElement.lang || "en";
  const dir = document.documentElement.dir || "ltr";

  window.AICA_I18N = {
    locale: locale,
    dir: dir,
    messages: messages,
    t: function (key, fallback) {
      return messages[key] || fallback || key;
    },
  };
})();

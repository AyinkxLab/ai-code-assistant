// AI Code Assistant — read-only Stellar/Soroban developer tools page.
// Network status/selection, account inspection, contract inspection, and
// ledger-entry lookup. All requests are read-only and bound to the configured
// Stellar network. Presentation helpers live in stellar_tools.js.

(function () {
  "use strict";

  var tools = window.StellarTools;

  function getCsrf() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) return meta.content;
    var input = document.querySelector('input[name="csrf_token"]');
    return input ? input.value : "";
  }

  function api(url, options) {
    options = options || {};
    options.headers = Object.assign({}, options.headers || {}, {
      "X-CSRFToken": getCsrf(),
    });
    return fetch(url, options).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok) {
          var error = new Error(
            data && data.error ? data.error : "Request failed (" + response.status + ")."
          );
          throw error;
        }
        return data;
      });
    });
  }

  function renderError(container, error) {
    container.innerHTML = tools.errorMessage(error);
  }

  function renderSelection(selection) {
    if (!selection) return "";
    var options = (selection.selectable || [])
      .map(function (item) {
        var selected = item.value === selection.effective_network ? " selected" : "";
        return (
          '<option value="' +
          tools.esc(item.value) +
          '"' +
          selected +
          ">" +
          tools.esc(item.label) +
          "</option>"
        );
      })
      .join("");
    var note = selection.stored_network
      ? "Using your saved selection (" + tools.esc(selection.stored_network) + ")."
      : "Using the configured default (" + tools.esc(selection.default_network) + ").";
    var html = '<div class="repo-toolbar">';
    html +=
      '<select id="stellar-network-select" class="sidebar-search" aria-label="Active Stellar network">' +
      options +
      "</select>";
    html +=
      '<button id="stellar-network-apply" class="btn btn-primary btn-sm" type="button">Switch network</button>';
    html += "</div>";
    html +=
      '<p class="field-hint">' +
      note +
      " Mainnet is never used automatically; select it explicitly.</p>";
    return html;
  }

  function saveNetwork() {
    var output = document.getElementById("stellar-network-output");
    var select = document.getElementById("stellar-network-select");
    if (!select) return;
    var value = select.value;
    output.innerHTML = tools.loading();
    api("/stellar/api/network", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ network: value }),
    })
      .then(loadNetwork)
      .catch(function (error) {
        renderError(output, error);
      });
  }

  function loadNetwork() {
    var output = document.getElementById("stellar-network-output");
    output.innerHTML = tools.loading();
    tools
      .apiGet("/stellar/api/network")
      .then(function (data) {
        var html = tools.renderNetworkStatus(data);
        html += renderSelection(data.selection || null);
        output.innerHTML = html;
        var apply = document.getElementById("stellar-network-apply");
        if (apply) apply.addEventListener("click", saveNetwork);
      })
      .catch(function (error) {
        renderError(output, error);
      });
  }

  function inspectAccount() {
    var output = document.getElementById("stellar-account-output");
    var address = document.getElementById("stellar-account-input").value.trim();
    if (!address) {
      renderError(output, { message: "Enter a G… account address." });
      return;
    }
    output.innerHTML = tools.loading();
    tools
      .apiGet("/stellar/api/account?address=" + encodeURIComponent(address))
      .then(function (data) {
        output.innerHTML = tools.renderAccount(data);
      })
      .catch(function (error) {
        renderError(output, error);
      });
  }

  function inspectContract() {
    var output = document.getElementById("stellar-contract-output");
    var contractId = document.getElementById("stellar-contract-input").value.trim();
    var wasmHash = document.getElementById("stellar-wasm-input").value.trim();
    if (!contractId) {
      renderError(output, { message: "Enter a C… contract id." });
      return;
    }
    output.innerHTML = tools.loading();
    var url = "/stellar/api/contract?address=" + encodeURIComponent(contractId);
    if (wasmHash) url += "&wasm_hash=" + encodeURIComponent(wasmHash);
    tools
      .apiGet(url)
      .then(function (data) {
        output.innerHTML = tools.renderContract(data);
      })
      .catch(function (error) {
        renderError(output, error);
      });
  }

  function inspectLedgerEntry() {
    var output = document.getElementById("stellar-ledger-output");
    var key = document.getElementById("stellar-ledger-input").value.trim();
    if (!key) {
      renderError(output, { message: "Enter a base64 ledger key." });
      return;
    }
    output.innerHTML = tools.loading();
    tools
      .apiGet("/stellar/api/ledger-entry?key=" + encodeURIComponent(key))
      .then(function (data) {
        output.innerHTML = tools.renderLedgerEntry(data);
      })
      .catch(function (error) {
        renderError(output, error);
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    loadNetwork();
    document.getElementById("stellar-network-refresh").addEventListener("click", loadNetwork);
    document.getElementById("stellar-account-btn").addEventListener("click", inspectAccount);
    document.getElementById("stellar-account-input").addEventListener("keydown", function (event) {
      if (event.key === "Enter") inspectAccount();
    });
    document.getElementById("stellar-contract-btn").addEventListener("click", inspectContract);
    document.getElementById("stellar-contract-input").addEventListener("keydown", function (event) {
      if (event.key === "Enter") inspectContract();
    });
    document.getElementById("stellar-wasm-input").addEventListener("keydown", function (event) {
      if (event.key === "Enter") inspectContract();
    });
    document.getElementById("stellar-ledger-btn").addEventListener("click", inspectLedgerEntry);
    document.getElementById("stellar-ledger-input").addEventListener("keydown", function (event) {
      if (event.key === "Enter") inspectLedgerEntry();
    });
  });
})();

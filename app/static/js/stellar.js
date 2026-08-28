// AI Code Assistant — read-only Stellar/Soroban developer tools.
// Network status, account inspection, and contract inspection. All requests
// are read-only and bound to the configured Stellar network.

(function () {
  "use strict";

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
          var error = new Error(data && data.error ? data.error : "Request failed (" + response.status + ").");
          throw error;
        }
        return data;
      });
    });
  }

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text == null ? "" : String(text);
    return div.innerHTML;
  }

  function renderError(container, error) {
    container.innerHTML =
      '<p class="sidebar-empty">' + escapeHtml(error && error.message ? error.message : "An error occurred.") + "</p>";
  }

  function renderNetwork(data) {
    var html = '<ul class="metric-list">';
    html += "<li><code>Network</code> — " + escapeHtml(data.network.network) + "</li>";
    html += "<li><code>Passphrase</code> — " + escapeHtml(data.network.network_passphrase) + "</li>";
    html += "<li><code>Horizon</code> — " + escapeHtml(data.network.horizon_url) + "</li>";
    html += "<li><code>RPC</code> — " + escapeHtml(data.network.rpc_url || "(none)") + "</li>";
    html += "<li><code>Public</code> — " + (data.network.is_public ? "yes" : "no") + "</li>";
    if (data.rpc_available) {
      html += "<li><code>RPC status</code> — " + escapeHtml(data.health.status) + "</li>";
      html += "<li><code>Latest ledger</code> — " + escapeHtml(String(data.latest_ledger.sequence)) + "</li>";
      html += "<li><code>Retention</code> — " + escapeHtml(String(data.health.ledgerRetentionWindow)) + " ledgers</li>";
    } else {
      html += "<li><code>RPC status</code> — unavailable (" + escapeHtml(data.rpc_error || "no response") + ")</li>";
    }
    html += "</ul>";
    return html;
  }

  function renderAccount(data) {
    var account = data.account;
    var html = "<ul class=\"metric-list\">";
    html += "<li><code>Address</code> — " + escapeHtml(data.address) + "</li>";
    html += "<li><code>Network</code> — " + escapeHtml(data.network.network) + "</li>";
    html += "<li><code>Sequence</code> — " + escapeHtml(String(account.sequence)) + "</li>";
    html += "<li><code>Subentry count</code> — " + escapeHtml(String(account.subentry_count)) + "</li>";
    html += "<li><code>Ledger freshness</code> — " +
      (data.ledger_freshness.available
        ? "ledger " + escapeHtml(String(data.ledger_freshness.sequence))
        : "unavailable") + "</li>";
    html += "</ul>";
    if (account.balances && account.balances.length) {
      html += '<h3 class="metric-title">Balances</h3><ul class="metric-list">';
      account.balances.forEach(function (balance) {
        var asset = balance.asset_code
          ? balance.asset_code + ":" + (balance.asset_issuer || "")
          : balance.asset_type;
        html += "<li><code>" + escapeHtml(asset) + "</code> — " + escapeHtml(balance.balance) + "</li>";
      });
      html += "</ul>";
    }
    return html;
  }

  function renderContract(data) {
    var html = "<ul class=\"metric-list\">";
    html += "<li><code>Contract</code> — " + escapeHtml(data.contract_id) + "</li>";
    html += "<li><code>Network</code> — " + escapeHtml(data.network.network) + "</li>";
    html += "<li><code>Instance entry</code> — " + (data.found ? "found" : "not found") + "</li>";
    html += "<li><code>Latest ledger</code> — " + escapeHtml(String(data.latest_ledger)) + "</li>";
    if (data.instance_entry) {
      html += "<li><code>Instance modified ledger</code> — " +
        escapeHtml(String(data.instance_entry.lastModifiedLedgerSeq)) + "</li>";
      html += "<li><code>Instance XDR</code> — retrieved (bounded, not decoded)</li>";
    }
    if ("wasm_hash" in data) {
      html += "<li><code>Wasm code</code> — " + (data.code_found ? "found" : "not found") + "</li>";
    }
    html += "</ul>";
    html += '<p class="field-hint">Ledger entries are returned as opaque XDR. ' +
      "Decoding SCVal values is tracked as contributor work; nothing here pretends to decode what it does not.</p>";
    return html;
  }

  function loadNetwork() {
    var output = document.getElementById("stellar-network-output");
    api("/stellar/api/network")
      .then(function (data) {
        output.innerHTML = renderNetwork(data);
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
    output.innerHTML = '<p class="sidebar-empty">Inspecting account…</p>';
    api("/stellar/api/account?address=" + encodeURIComponent(address))
      .then(function (data) {
        output.innerHTML = renderAccount(data);
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
    output.innerHTML = '<p class="sidebar-empty">Inspecting contract…</p>';
    var url = "/stellar/api/contract?address=" + encodeURIComponent(contractId);
    if (wasmHash) url += "&wasm_hash=" + encodeURIComponent(wasmHash);
    api(url)
      .then(function (data) {
        output.innerHTML = renderContract(data);
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
  });
})();

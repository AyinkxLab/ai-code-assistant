// Shared, read-only Stellar/Soroban presentation helpers.
// Used by both the standalone /stellar page (stellar.js) and the project
// explorer "Stellar" tab (project.js). Everything stays bounded: raw XDR is
// shown truncated in a monospace block labelled "raw XDR", and structured
// decoded views are rendered from the bounded values the backend already
// produced. No signing, no submission, no secrets.

(function () {
  "use strict";

  var TIMEOUT_MS = 25000;

  function csrf() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) return meta.content;
    var input = document.querySelector('input[name="csrf_token"]');
    return input ? input.value : "";
  }

  function esc(value) {
    var div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function apiGet(url) {
    var controller = new AbortController();
    var timer = setTimeout(function () {
      controller.abort();
    }, TIMEOUT_MS);
    function clearTimer() {
      clearTimeout(timer);
    }
    return fetch(url, {
      headers: { "X-CSRFToken": csrf() },
      signal: controller.signal,
    })
      .then(function (response) {
        return response.json().then(function (data) {
          if (!response.ok) {
            var error = new Error(
              data && data.error ? data.error : "Request failed (" + response.status + ")."
            );
            error.status = response.status;
            throw error;
          }
          return data;
        });
      })
      .then(
        function (data) {
          clearTimer();
          return data;
        },
        function (error) {
          clearTimer();
          if (error && error.name === "AbortError") {
            var timeout = new Error("The request timed out. Try again shortly.");
            timeout.timedOut = true;
            throw timeout;
          }
          throw error;
        }
      );
  }

  function loading() {
    return '<p class="sidebar-empty">Loading…</p>';
  }

  function empty(message) {
    return '<p class="sidebar-empty">' + esc(message) + "</p>";
  }

  function errorMessage(error) {
    var message =
      error && error.message ? error.message : "An unexpected error occurred.";
    var note = error && error.status === 502
      ? " (the RPC/Horizon service could not complete the read-only request)"
      : "";
    return '<p class="sidebar-empty">' + esc(message + note) + "</p>";
  }

  function kvList(pairs) {
    var html = '<ul class="metric-list">';
    pairs.forEach(function (pair) {
      html += "<li><code>" + esc(pair[0]) + "</code> — " + esc(pair[1]) + "</li>";
    });
    html += "</ul>";
    return html;
  }

  function jsonView(label, obj) {
    return (
      '<h4 class="metric-title">' +
      esc(label) +
      "</h4>" +
      '<pre class="code-view stellar-json">' +
      esc(JSON.stringify(obj, null, 2)) +
      "</pre>"
    );
  }

  function xdrView(label, xdr) {
    if (!xdr) return "";
    return (
      '<details class="stellar-xdr">' +
      "<summary>" +
      esc(label) +
      " (raw XDR, bounded)</summary>" +
      '<pre class="code-view stellar-raw-xdr">' +
      esc(xdr) +
      "</pre>" +
      "</details>"
    );
  }

  function renderNetworkStatus(data) {
    var net = data && data.network;
    if (!net) return empty("Network status is unavailable.");
    var rows = [["Network", net.network]];
    if (net.network_passphrase) rows.push(["Passphrase", net.network_passphrase]);
    if (net.horizon_url) rows.push(["Horizon", net.horizon_url]);
    rows.push(["RPC", net.rpc_url || "(none)"]);
    if (net.is_public !== undefined) {
      rows.push(["Public network", net.is_public ? "yes" : "no"]);
    }
    if (data.rpc_available) {
      if (data.health && data.health.status) rows.push(["RPC status", data.health.status]);
      if (data.latest_ledger && data.latest_ledger.sequence) {
        rows.push(["Latest ledger", String(data.latest_ledger.sequence)]);
      }
    } else {
      rows.push(["RPC status", "unavailable (" + (data.rpc_error || "no response") + ")"]);
    }
    return kvList(rows);
  }

  function renderAccount(data) {
    var html = "";
    if (!data || !data.account) return empty("No account data returned.");
    var account = data.account;
    var balances = account.balances || [];
    var nativeBalances = balances.filter(function (balance) {
      return balance.asset_type === "native";
    });
    var trustlines = balances.filter(function (balance) {
      return balance.asset_type !== "native";
    });
    var flags = account.flags || {};
    var signers = account.signers || [];
    var dataEntries = Array.isArray(data.manage_data)
      ? data.manage_data
      : Object.keys(account.data || {}).map(function (key) {
          return { key: key, value: account.data[key], decoded_text: undefined };
        });
    var transactions = (data.transactions && data.transactions.records) || [];
    var networkName = (data.network && data.network.network) || "";

    if (networkName) {
      html +=
        '<p class="stellar-account-network"><span class="tag tag-confirmed">' +
        esc(networkName) +
        "</span></p>";
    }

    html += kvList([
      ["Address", data.address || ""],
      ["Network", (data.network && data.network.network) || ""],
      ["Sequence", String(account.sequence != null ? account.sequence : "")],
      [
        "Subentry count",
        String(account.subentry_count != null ? account.subentry_count : ""),
      ],
      [
        "Ledger freshness",
        data.ledger_freshness && data.ledger_freshness.available
          ? "ledger " + String(data.ledger_freshness.sequence)
          : "unavailable",
      ],
    ]);

    html += '<h4 class="metric-title">Balances</h4>';
    if (nativeBalances.length) {
      html += '<ul class="metric-list">';
      nativeBalances.forEach(function (balance) {
        var asset = balance.asset_code
          ? balance.asset_code + ":" + (balance.asset_issuer || "")
          : balance.asset_type;
        html +=
          "<li><code>" + esc(asset) + "</code> — " + esc(balance.balance) + "</li>";
      });
      html += "</ul>";
    } else {
      html += empty("No native balance returned.");
    }

    html += '<h4 class="metric-title">Trustlines</h4>';
    if (trustlines.length) {
      html += '<div class="stellar-table-wrap"><table class="stellar-table"><thead><tr>';
      html += "<th>Asset</th><th>Balance</th><th>Limit</th><th>Authorization</th>";
      html += "</tr></thead><tbody>";
      trustlines.forEach(function (balance) {
        var asset = balance.asset_code || balance.asset_type || "Unknown asset";
        var authorization;
        if (balance.is_authorized === undefined) {
          authorization = "not reported";
        } else {
          authorization = balance.is_authorized ? "authorized" : "unauthorized";
          if (balance.is_authorized_to_maintain_liabilities !== undefined) {
            authorization +=
              " · " +
              (balance.is_authorized_to_maintain_liabilities
                ? "maintains liabilities"
                : "no liabilities");
          }
        }
        html +=
          "<tr><td><code>" + esc(asset) + "</code><br><small>" +
          esc(balance.asset_issuer || "") + "</small></td><td>" +
          esc(balance.balance || "") + "</td><td>" + esc(balance.limit || "") +
          "</td><td>" + esc(authorization) + "</td></tr>";
      });
      html += "</tbody></table></div>";
    } else {
      html += empty("No trustlines found.");
    }

    html += '<h4 class="metric-title">Account flags</h4>';
    if (Object.keys(flags).length) {
      html += kvList(Object.keys(flags).map(function (key) {
        return [key, flags[key] ? "enabled" : "disabled"];
      }));
    } else {
      html += empty("No account flags returned.");
    }

    html += '<h4 class="metric-title">Signers</h4>';
    if (signers.length) {
      html += '<ul class="metric-list">';
      signers.forEach(function (signer) {
        html += "<li><code>" + esc(signer.key || "") + "</code> — " +
          esc(signer.type || "signer") + ", weight " + esc(signer.weight) + "</li>";
      });
      html += "</ul>";
    } else {
      html += empty("No additional signers returned.");
    }

    html += '<h4 class="metric-title">Manage-data entries</h4>';
    if (dataEntries.length) {
      html += '<ul class="metric-list">';
      dataEntries.forEach(function (entry) {
        var label = entry.key || "";
        if (entry.decoded_text) {
          html +=
            "<li><code>" + esc(label) + "</code> — " + esc(entry.decoded_text) +
            " <small>(base64: " + esc(entry.value) + ")</small></li>";
        } else {
          html += "<li><code>" + esc(label) + "</code> — " + esc(entry.value) + "</li>";
        }
      });
      html += "</ul>";
    } else {
      html += empty("No manage-data entries returned.");
    }

    html += '<h4 class="metric-title">Recent transactions</h4>';
    if (transactions.length) {
      html += '<div class="stellar-table-wrap"><table class="stellar-table"><thead><tr>';
      html += "<th>Ledger</th><th>Created</th><th>Status</th><th>Hash</th><th>Memo</th>";
      html += "</tr></thead><tbody>";
      transactions.forEach(function (transaction) {
        html += "<tr><td>" + esc(transaction.ledger) + "</td><td>" +
          esc(transaction.created_at || "") + "</td><td>" +
          esc(transaction.successful === false ? "failed" : "successful") +
          "</td><td><code>" + esc(transaction.hash || "") + "</code></td><td>" +
          esc(transaction.memo || "") + "</td></tr>";
      });
      html += "</tbody></table></div>";
    } else {
      html += empty("No recent transactions found.");
    }

    html += '<p class="field-hint">Account and transaction details are read-only Horizon data on the selected network.</p>';
    return html;
  }

  function renderEntryDecoded(entry, labels) {
    var html = "";
    if (!entry) return html;
    var decoded = entry.decoded;
    if (decoded && decoded.decoded) {
      html += jsonView(labels.decoded, decoded.detail || decoded);
    } else if (decoded) {
      html +=
        '<p class="field-hint">' +
        esc(labels.notDecoded + (decoded.reason ? " (" + decoded.reason + ")" : "")) +
        "</p>";
    }
    html += xdrView(labels.rawXdr, entry.xdr);
    return html;
  }

  function renderContract(data) {
    if (!data) return empty("No contract data returned.");
    var rows = [
      ["Contract", data.contract_id || ""],
      ["Network", (data.network && data.network.network) || ""],
      ["Latest ledger", data.latest_ledger != null ? String(data.latest_ledger) : ""],
      ["Instance entry", data.found ? "found" : "not found"],
    ];
    if (data.instance_entry) {
      if (data.instance_entry.lastModifiedLedgerSeq != null) {
        rows.push(["Instance modified ledger", String(data.instance_entry.lastModifiedLedgerSeq)]);
      }
      if (data.instance_entry.liveUntilLedgerSeq != null) {
        rows.push(["Instance live until ledger", String(data.instance_entry.liveUntilLedgerSeq)]);
      }
    }
    if ("wasm_hash" in data) {
      rows.push(["Wasm code", data.code_found ? "found" : "not found"]);
    }
    var html = kvList(rows);
    if (data.instance_entry) {
      html +=
        '<h4 class="metric-title">Contract instance entry</h4>' +
        renderEntryDecoded(data.instance_entry, {
          decoded: "Decoded instance entry",
          notDecoded: "The instance entry could not be decoded into a structured view.",
          rawXdr: "Instance entry",
        });
    }
    if (data.code_entry) {
      html +=
        '<h4 class="metric-title">Deployed wasm metadata</h4>' +
        renderEntryDecoded(data.code_entry, {
          decoded: "Decoded code entry",
          notDecoded: "The code entry could not be decoded into a structured view.",
          rawXdr: "Code entry",
        });
    }
    html +=
      '<p class="field-hint">All values are read-only network data bound to the ' +
      "configured network. Raw XDR is shown bounded and is never re-encoded into " +
      "a guessed value.</p>";
    return html;
  }

  function renderLedgerEntry(data) {
    if (!data) return empty("No ledger entry data returned.");
    var rows = [
      ["Network", (data.network && data.network.network) || ""],
      ["Found", data.found ? "yes" : "no"],
    ];
    if (data.latest_ledger != null) rows.push(["Latest ledger", String(data.latest_ledger)]);
    var html = kvList(rows);
    if (data.entry) {
      if (data.entry.lastModifiedLedgerSeq != null) {
        html += kvList([["Last modified ledger", String(data.entry.lastModifiedLedgerSeq)]]);
      }
      html += renderEntryDecoded(data.entry, {
        decoded: "Decoded ledger entry",
        notDecoded: "This ledger entry type is not decoded into a structured view.",
        rawXdr: "Ledger entry",
      });
    } else {
      html +=
        '<p class="field-hint">No entry exists for this ledger key on the configured ' +
        "network.</p>";
    }
    return html;
  }

  function renderDetection(data) {
    var isStellar = !!(data && data.is_stellar);
    var badge = isStellar
      ? '<span class="tag tag-confirmed">Stellar project</span>'
      : '<span class="tag">Not a Stellar project</span>';
    var html = '<div class="stellar-panel">';
    html += "<div><h3>Stellar / Soroban detection</h3>" + badge + "</div>";
    html += kvList([
      ["Confidence", data.confidence || "none"],
      ["Stellar", isStellar ? "yes" : "no"],
      ["Soroban (smart contracts)", data && data.is_soroban ? "yes" : "no"],
    ]);
    var network = data && data.network && data.network.network;
    if (network) {
      html += kvList([["Network hint", network]]);
    }
    if (data && data.contract_entry_point) {
      html +=
        '<h4 class="metric-title">Contract entry point</h4>' +
        '<p><a class="stellar-file-link" href="#" data-stellar-path="' +
        esc(data.contract_entry_point) +
        '">' +
        esc(data.contract_entry_point) +
        "</a></p>";
    }
    if (data && data.evidence && data.evidence.length) {
      html += '<h4 class="metric-title">Evidence</h4><ul class="metric-list">';
      data.evidence.forEach(function (line) {
        html += "<li>" + esc(line) + "</li>";
      });
      html += "</ul>";
    }
    var relevant = data && data.relevant_files ? data.relevant_files : [];
    if (relevant.length) {
      html += '<h4 class="metric-title">Relevant Stellar files</h4><ul class="metric-list">';
      relevant.forEach(function (path) {
        html +=
          '<li><a class="stellar-file-link" href="#" data-stellar-path="' +
          esc(path) +
          '">' +
          esc(path) +
          "</a></li>";
      });
      html += "</ul>";
    }
    if (!isStellar) {
      html +=
        '<p class="field-hint">This project shows no Stellar/Soroban signals. Plain ' +
        "Rust, Python, JavaScript, and other projects are never classified as Stellar " +
        "without concrete evidence.</p>";
    }
    html += "</div>";
    return html;
  }

  function bindFileLinks(container) {
    if (!container) return;
    container.addEventListener("click", function (event) {
      var link = event.target && event.target.closest
        ? event.target.closest("[data-stellar-path]")
        : null;
      if (!link) return;
      event.preventDefault();
      var path = link.getAttribute("data-stellar-path");
      var fn = window.StellarTools && window.StellarTools.onOpenFile;
      if (fn) fn(path);
    });
  }

  window.StellarTools = {
    TIMEOUT_MS: TIMEOUT_MS,
    esc: esc,
    apiGet: apiGet,
    loading: loading,
    empty: empty,
    errorMessage: errorMessage,
    kvList: kvList,
    jsonView: jsonView,
    xdrView: xdrView,
    renderNetworkStatus: renderNetworkStatus,
    renderAccount: renderAccount,
    renderContract: renderContract,
    renderLedgerEntry: renderLedgerEntry,
    renderDetection: renderDetection,
    bindFileLinks: bindFileLinks,
  };
})();

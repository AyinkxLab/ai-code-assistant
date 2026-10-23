// AI Code Assistant — workspace team page
// Member management (owner), invitations (owner), collaboration settings
// (owner), ownership transfer (owner), and self-leave (members).

(function () {
  "use strict";

  var WORKSPACE_ID = parseInt(window.COLLAB_WORKSPACE_ID, 10) || 0;
  var MY_ROLE = window.COLLAB_MY_ROLE || "viewer";

  function getCsrf() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) return meta.content;
    var input = document.querySelector('input[name="csrf_token"]');
    return input ? input.value : "";
  }

  function flash(message, category) {
    var stack = document.querySelector(".flash-stack");
    if (!stack) {
      stack = document.createElement("div");
      stack.className = "flash-stack";
      var main = document.querySelector(".main-content");
      (main || document.body).prepend(stack);
    }
    var el = document.createElement("div");
    el.className = "flash flash-" + (category || "info");
    el.textContent = message;
    stack.appendChild(el);
    setTimeout(function () { el.remove(); }, 6000);
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

  function escapeHtml(value) {
    var div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function rolePill(role) {
    return '<span class="role-pill role-' + escapeHtml(role) + '">' + escapeHtml(role) + "</span>";
  }

  function statusPill(status) {
    return '<span class="status-pill status-' + escapeHtml(status) + '">' + escapeHtml(status) + "</span>";
  }

  function avatar(username) {
    var initial = (username || "?").charAt(0).toUpperCase();
    return '<span class="member-avatar">' + escapeHtml(initial) + "</span>";
  }

  function formatTime(iso) {
    if (!iso) return "";
    return new Date(iso).toLocaleString();
  }

  function loadMembers() {
    api("/workspaces/api/workspaces/" + WORKSPACE_ID + "/members")
      .then(function (members) {
        var list = document.getElementById("member-list");
        var transfer = document.getElementById("transfer-select");
        if (!list) return;
        if (!members.length) {
          list.innerHTML = '<p class="empty-note">No members yet.</p>';
          return;
        }
        var html = "";
        members.forEach(function (member) {
          var canManage = MY_ROLE === "owner" && member.role !== "owner";
          var actions = "";
          if (canManage) {
            actions =
              '<select class="field-input member-role" data-user-id="' + member.user_id + '" style="flex:0 0 140px;">' +
              '<option value="viewer"' + (member.role === "viewer" ? " selected" : "") + ">Viewer</option>" +
              '<option value="contributor"' + (member.role === "contributor" ? " selected" : "") + ">Contributor</option>" +
              "</select>" +
              '<button class="btn btn-ghost btn-sm btn-danger member-remove" data-user-id="' + member.user_id + '" data-username="' + escapeHtml(member.username || "") + '" type="button">Remove</button>';
          } else if (member.role === "owner") {
            actions = rolePill("owner");
          }
          html +=
            '<div class="member-row">' +
            '<div class="member-info">' +
            avatar(member.username) +
            '<div>' +
            '<div class="member-name">' + escapeHtml(member.username) + "</div>" +
            '<div class="member-meta">Joined ' + formatTime(member.joined_at) + (member.last_active_at ? " &middot; active " + formatTime(member.last_active_at) : "") + "</div>" +
            "</div>" +
            "</div>" +
            '<div class="member-actions">' + actions + "</div>" +
            "</div>";
        });
        list.innerHTML = html;

        if (transfer) {
          transfer.innerHTML = '<option value="">Choose a member...</option>';
          members.forEach(function (member) {
            if (member.role === "owner") return;
            transfer.innerHTML +=
              '<option value="' + member.user_id + '">' + escapeHtml(member.username) + "</option>";
          });
        }

        list.querySelectorAll(".member-role").forEach(function (select) {
          select.addEventListener("change", function () {
            api("/workspaces/api/workspaces/" + WORKSPACE_ID + "/members/" + select.dataset.userId, {
              method: "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ role: select.value }),
            })
              .then(function () { flash("Role updated.", "success"); loadMembers(); })
              .catch(function (error) { flash(error.message, "error"); });
          });
        });

        list.querySelectorAll(".member-remove").forEach(function (btn) {
          btn.addEventListener("click", function () {
            if (!confirm("Remove " + btn.dataset.username + " from this workspace?")) return;
            api("/workspaces/api/workspaces/" + WORKSPACE_ID + "/members/" + btn.dataset.userId, {
              method: "DELETE",
            })
              .then(function () { flash("Member removed.", "success"); loadMembers(); })
              .catch(function (error) { flash(error.message, "error"); });
          });
        });
      })
      .catch(function (error) {
        flash(error.message, "error");
      });
  }

  function loadInvitations() {
    var list = document.getElementById("invite-list");
    if (!list) return;
    api("/workspaces/api/workspaces/" + WORKSPACE_ID + "/invitations")
      .then(function (data) {
        if (!data.items.length) {
          list.innerHTML = '<p class="empty-note">No invitations yet.</p>';
          return;
        }
        var html = "";
        data.items.forEach(function (inv) {
          html +=
            '<div class="invite-row">' +
            '<div class="invite-info">' +
            "<div>" +
            '<div class="invite-email">' + escapeHtml(inv.email) + "</div>" +
            '<div class="invite-meta">Invited by ' + escapeHtml(inv.inviter_username || "deleted user") +
            (inv.expires_at ? " &middot; expires " + formatTime(inv.expires_at) : "") + "</div>" +
            "</div>" +
            "</div>" +
            '<div class="invite-actions">' + rolePill(inv.role) + statusPill(inv.status) +
            (inv.status === "pending"
              ? '<button class="btn btn-ghost btn-sm invite-cancel" data-id="' + inv.id + '" type="button">Cancel</button>'
              : "") +
            "</div>" +
            "</div>";
        });
        list.innerHTML = html;
        list.querySelectorAll(".invite-cancel").forEach(function (btn) {
          btn.addEventListener("click", function () {
            api("/workspaces/api/workspaces/" + WORKSPACE_ID + "/invitations/" + btn.dataset.id, {
              method: "DELETE",
            })
              .then(function () { flash("Invitation cancelled.", "success"); loadInvitations(); })
              .catch(function (error) { flash(error.message, "error"); });
          });
        });
      })
      .catch(function (error) {
        flash(error.message, "error");
      });
  }

  function loadSettings() {
    var toggle = document.getElementById("invitations-enabled");
    var select = document.getElementById("default-role");
    if (!toggle) return;
    api("/workspaces/api/workspaces/" + WORKSPACE_ID + "/settings")
      .then(function (settings) {
        toggle.checked = !!settings.invitations_enabled;
        select.value = settings.default_member_role || "viewer";
      })
      .catch(function (error) {
        flash(error.message, "error");
      });
  }

  function loadWorkspaceUsage() {
    var panel = document.getElementById("workspace-usage-panel");
    if (!panel) return;
    if (!window.AICAWorkspaceUsage) {
      var note = document.getElementById("workspace-usage-note");
      if (note) note.textContent = "Usage unavailable: the usage panel did not load.";
      return;
    }
    window.AICAWorkspaceUsage.load({
      document: document,
      fetchUsage: function () {
        return api("/workspaces/api/workspaces/" + WORKSPACE_ID + "/usage");
      },
    });
  }

  function bindControls() {
    var inviteBtn = document.getElementById("invite-btn");
    if (inviteBtn) {
      inviteBtn.addEventListener("click", function () {
        var email = document.getElementById("invite-email").value.trim();
        var role = document.getElementById("invite-role").value;
        if (!email) {
          flash("Enter an email address.", "warning");
          return;
        }
        inviteBtn.disabled = true;
        api("/workspaces/api/workspaces/" + WORKSPACE_ID + "/invitations", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email: email, role: role }),
        })
          .then(function () {
            flash("Invitation sent by email.", "success");
            document.getElementById("invite-email").value = "";
            loadInvitations();
          })
          .catch(function (error) { flash(error.message, "error"); })
          .then(function () { inviteBtn.disabled = false; });
      });
    }

    var settingsSave = document.getElementById("settings-save");
    if (settingsSave) {
      settingsSave.addEventListener("click", function () {
        api("/workspaces/api/workspaces/" + WORKSPACE_ID + "/settings", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            invitations_enabled: document.getElementById("invitations-enabled").checked,
            default_member_role: document.getElementById("default-role").value,
          }),
        })
          .then(function () { flash("Settings saved.", "success"); })
          .catch(function (error) { flash(error.message, "error"); });
      });
    }

    var transferBtn = document.getElementById("transfer-btn");
    if (transferBtn) {
      transferBtn.addEventListener("click", function () {
        var select = document.getElementById("transfer-select");
        if (!select.value) {
          flash("Choose a member to transfer ownership to.", "warning");
          return;
        }
        if (!confirm("Transfer ownership of this workspace to the selected member? This cannot be undone.")) return;
        api("/workspaces/api/workspaces/" + WORKSPACE_ID + "/transfer", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ user_id: parseInt(select.value, 10) }),
        })
          .then(function () {
            flash("Ownership transferred.", "success");
            window.location.reload();
          })
          .catch(function (error) { flash(error.message, "error"); });
      });
    }

    var leaveBtn = document.getElementById("leave-btn");
    if (leaveBtn) {
      leaveBtn.addEventListener("click", function () {
        if (!confirm("Leave this workspace? You will lose access to its projects.")) return;
        api("/workspaces/api/workspaces/" + WORKSPACE_ID + "/membership", { method: "DELETE" })
          .then(function () {
            flash("You left the workspace.", "success");
            window.location.href = "/workspaces/";
          })
          .catch(function (error) { flash(error.message, "error"); });
      });
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    loadMembers();
    loadInvitations();
    loadSettings();
    loadWorkspaceUsage();
    bindControls();
  });
})();

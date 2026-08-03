(function () {
  "use strict";

  function normalizeBasePath(rootUrl) {
    try {
      var pathname = new URL(rootUrl, window.location.href).pathname.replace(/\/+$/, "");
      return pathname === "/" ? "" : pathname;
    } catch (_error) {
      return "";
    }
  }

  function configureMaintenance(root) {
    var params = new URLSearchParams(window.location.search);
    var changed = false;
    var pageScope = root.dataset.maintenanceScope || "index";
    var pageStorageKey = "maintenanceBypass:" + pageScope;
    var pageBypass = params.get("maintenance_bypass");
    var chatbotStorageKey = "maintenanceBypass:chatbot";
    var chatbotBypass = params.get("chatbot_maintenance_bypass");

    if (pageBypass === "1") localStorage.setItem(pageStorageKey, "1");
    if (pageBypass === "0") localStorage.removeItem(pageStorageKey);
    if (pageBypass !== null) {
      params.delete("maintenance_bypass");
      changed = true;
    }

    if (chatbotBypass === "1") localStorage.setItem(chatbotStorageKey, "1");
    if (chatbotBypass === "0") localStorage.removeItem(chatbotStorageKey);
    if (chatbotBypass !== null) {
      params.delete("chatbot_maintenance_bypass");
      changed = true;
    }

    if (changed) {
      var query = params.toString();
      window.history.replaceState(null, document.title, window.location.pathname + (query ? "?" + query : "") + window.location.hash);
    }

    if (root.dataset.maintenanceEnabled === "true" && localStorage.getItem(pageStorageKey) !== "1") {
      document.documentElement.classList.add("app-maintenance-active");
      document.body.classList.add("app-maintenance-active");
    }

    var chatbotBypassed = localStorage.getItem(chatbotStorageKey) === "1";
    window.CHATBOT_DISABLED_BY_MAINTENANCE = root.dataset.chatbotMaintenance === "true" && !chatbotBypassed;
    var cookiePath = normalizeBasePath(root.dataset.homeRootUrl) || "/";
    document.cookie = "chatbotMaintenanceBypass=" + (chatbotBypassed ? "1" : "0") + "; path=" + cookiePath + "; SameSite=Lax";
    if (window.CHATBOT_DISABLED_BY_MAINTENANCE) {
      var shell = document.getElementById("chatbotAgentShell");
      if (shell) shell.remove();
    }
  }

  function showRefreshError(message) {
    if (window.OplotUI && typeof window.OplotUI.showToast === "function") {
      window.OplotUI.showToast(message, "danger");
    }
  }

  function initDashboardRefresh(root) {
    var link = root.querySelector("[data-home-dashboard-link]");
    if (!link || link.dataset.homeRefreshInitialized === "true") return;
    link.dataset.homeRefreshInitialized = "true";

    link.addEventListener("click", function (event) {
      if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      if (link.target || link.dataset.refreshFailed === "true") return;

      var refreshUrl = link.dataset.refreshUrl;
      if (!refreshUrl) return;
      event.preventDefault();
      if (link.getAttribute("aria-busy") === "true") return;

      link.setAttribute("aria-busy", "true");
      var loader = document.querySelector("[data-home-dashboard-loader]");
      if (loader) loader.hidden = false;
      var controller = new AbortController();
      var timeout = window.setTimeout(function () { controller.abort(); }, 20000);

      fetch(refreshUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        credentials: "same-origin",
        signal: controller.signal
      }).then(function (response) {
        if (!response.ok) throw new Error("refresh_http_error");
        return response.json();
      }).then(function (payload) {
        if (!payload || payload.success !== true) throw new Error("refresh_failed");
        window.location.assign(link.href);
      }).catch(function () {
        link.dataset.refreshFailed = "true";
        link.removeAttribute("aria-busy");
        if (loader) loader.hidden = true;
        showRefreshError("Не удалось обновить данные. Повторный клик откроет рабочий стол без предварительного обновления.");
      }).finally(function () {
        window.clearTimeout(timeout);
      });
    });
  }

  function initHome() {
    var root = document.querySelector(".oplot-home-workspace");
    if (!root || root.dataset.homeInitialized === "true") return;
    root.dataset.homeInitialized = "true";
    initDashboardRefresh(root);
  }

  var homeRoot = document.querySelector(".oplot-home-workspace");
  if (homeRoot) {
    window.CHATBOT_BASE_PATH = normalizeBasePath(homeRoot.dataset.homeRootUrl);
    window.dashboardData = { page_context: "home" };
    configureMaintenance(homeRoot);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initHome, { once: true });
  else initHome();
}());

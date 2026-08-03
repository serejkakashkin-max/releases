(function () {
  "use strict";

  window.OplotComponentInitializers = window.OplotComponentInitializers || [];
  var operationCount = 0;
  var lastModalTrigger = null;
  var htmxLifecycleInitialized = false;

  function updateOperationIndicator() {
    var indicator = document.getElementById("oplot-operation-indicator");
    if (indicator) {
      indicator.hidden = operationCount === 0;
    }
  }

  function beginOperation() {
    operationCount += 1;
    updateOperationIndicator();
  }

  function endOperation() {
    operationCount = Math.max(0, operationCount - 1);
    updateOperationIndicator();
  }

  function syncThemeControls() {
    var dark = window.OplotTheme && window.OplotTheme.current() === "dark";
    document.querySelectorAll("[data-oplot-theme-toggle]").forEach(function (button) {
      button.setAttribute("aria-pressed", dark ? "true" : "false");
      var label = button.querySelector(".theme-label");
      if (label) {
        label.textContent = dark ? "Светлая тема" : "Тёмная тема";
      }
    });
  }

  function initThemeControls(container) {
    (container || document).querySelectorAll("[data-oplot-theme-toggle]").forEach(function (button) {
      if (button.dataset.oplotInitialized === "true") {
        return;
      }
      button.dataset.oplotInitialized = "true";
      button.addEventListener("click", function () {
        if (window.OplotTheme) {
          window.OplotTheme.toggle();
          syncThemeControls();
        }
      });
    });
    syncThemeControls();
  }

  function showToast(message, kind) {
    var region = document.getElementById("oplot-toast-region");
    if (!region || !message) {
      return null;
    }
    var toast = document.createElement("div");
    toast.className = "oplot-toast" + (kind === "danger" ? " is-danger" : "");
    toast.setAttribute("role", kind === "danger" ? "alert" : "status");
    var text = document.createElement("div");
    text.className = "oplot-toast__message";
    text.textContent = String(message);
    var close = document.createElement("button");
    close.type = "button";
    close.className = "oplot-toast__close";
    close.setAttribute("aria-label", "Закрыть уведомление");
    close.textContent = "×";
    close.addEventListener("click", function () { toast.remove(); });
    toast.appendChild(text);
    toast.appendChild(close);
    region.appendChild(toast);
    window.setTimeout(function () { toast.remove(); }, 6000);
    return toast;
  }

  function initModalFocus(container) {
    (container || document).querySelectorAll(".modal").forEach(function (modal) {
      if (modal.dataset.oplotFocusInitialized === "true") {
        return;
      }
      modal.dataset.oplotFocusInitialized = "true";
      modal.addEventListener("show.bs.modal", function (event) {
        lastModalTrigger = event.relatedTarget || document.activeElement;
      });
      modal.addEventListener("hidden.bs.modal", function () {
        if (lastModalTrigger && typeof lastModalTrigger.focus === "function" && document.contains(lastModalTrigger)) {
          lastModalTrigger.focus();
        }
        lastModalTrigger = null;
      });
      var confirm = modal.querySelector("[data-oplot-confirm]");
      if (confirm) {
        confirm.addEventListener("click", function () {
          modal.dispatchEvent(new CustomEvent("oplot:confirm", { bubbles: true }));
          var Modal = window.tabler && (window.tabler.Modal || (window.tabler.bootstrap && window.tabler.bootstrap.Modal));
          if (Modal) {
            Modal.getOrCreateInstance(modal).hide();
          }
        });
      }
    });
  }

  function initOplotComponents(container) {
    var target = container || document;
    initThemeControls(target);
    initModalFocus(target);
    window.OplotComponentInitializers.forEach(function (initializer) {
      initializer(target);
    });
  }

  function initHtmxLifecycle() {
    if (!window.htmx || htmxLifecycleInitialized) {
      return;
    }
    htmxLifecycleInitialized = true;
    document.addEventListener("htmx:beforeRequest", beginOperation);
    document.addEventListener("htmx:configRequest", function (event) {
      var meta = document.querySelector('meta[name="oplot-csrf-token"]');
      if (meta && event.detail && event.detail.headers) {
        event.detail.headers["X-CSRF-Token"] = meta.content;
      }
    });
    document.addEventListener("htmx:afterRequest", endOperation);
    document.addEventListener("htmx:sendError", endOperation);
    document.addEventListener("htmx:afterSwap", function (event) {
      initOplotComponents(event.detail && event.detail.target ? event.detail.target : document);
    });
  }

  window.initOplotComponents = initOplotComponents;
  window.OplotUI = {
    beginOperation: beginOperation,
    endOperation: endOperation,
    showToast: showToast
  };

  document.addEventListener("DOMContentLoaded", function () {
    initHtmxLifecycle();
    initOplotComponents(document);
  });
  document.addEventListener("oplot:themechange", syncThemeControls);
})();

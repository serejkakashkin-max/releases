(function () {
  "use strict";

  window.OplotComponentInitializers = window.OplotComponentInitializers || [];
  var operationCount = 0;

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

  function syncThemeButton(button) {
    if (!button || !window.OplotTheme) {
      return;
    }
    var dark = window.OplotTheme.current() === "dark";
    button.setAttribute("aria-pressed", dark ? "true" : "false");
    var label = button.querySelector(".theme-label");
    if (label) {
      label.textContent = dark ? "Светлая тема" : "Тёмная тема";
    }
  }

  function initThemeToggle(container) {
    var button = (container || document).querySelector("#oplot-theme-toggle");
    if (!button) {
      return;
    }
    syncThemeButton(button);
    if (button.dataset.oplotInitialized === "true") {
      return;
    }
    button.dataset.oplotInitialized = "true";
    button.addEventListener("click", function () {
      window.OplotTheme.toggle();
      syncThemeButton(button);
    });
  }

  function showToast(message, kind) {
    var region = document.getElementById("oplot-toast-region");
    if (!region) {
      return;
    }
    var toast = document.createElement("div");
    toast.className = "toast show text-bg-" + (kind === "danger" ? "danger" : "secondary");
    toast.setAttribute("role", "status");
    var body = document.createElement("div");
    body.className = "d-flex";
    var text = document.createElement("div");
    text.className = "toast-body";
    text.textContent = message;
    var close = document.createElement("button");
    close.type = "button";
    close.className = "btn-close btn-close-white me-2 m-auto";
    close.setAttribute("aria-label", "Закрыть");
    close.addEventListener("click", function () { toast.remove(); });
    body.appendChild(text);
    body.appendChild(close);
    toast.appendChild(body);
    region.appendChild(toast);
    window.setTimeout(function () { toast.remove(); }, 6000);
  }

  function initOplotComponents(container) {
    var target = container || document;
    initThemeToggle(document);
    window.OplotComponentInitializers.forEach(function (initializer) {
      initializer(target);
    });
  }

  window.initOplotComponents = initOplotComponents;
  window.OplotUI = {
    beginOperation: beginOperation,
    endOperation: endOperation,
    showToast: showToast
  };

  document.addEventListener("DOMContentLoaded", function () {
    initOplotComponents(document);
  });
  document.addEventListener("htmx:beforeRequest", beginOperation);
  document.addEventListener("htmx:afterRequest", function (event) {
    endOperation();
    if (event.detail && event.detail.failed) {
      showToast("Не удалось обновить каталог.", "danger");
    }
  });
  document.addEventListener("htmx:beforeSwap", function (event) {
    var detail = event.detail || {};
    var status = detail.xhr ? detail.xhr.status : 0;
    if (status === 503 && detail.target && detail.target.id === "template-catalog") {
      detail.shouldSwap = true;
      detail.isError = false;
    }
  });
  document.addEventListener("htmx:sendError", function () {
    showToast("Сетевая ошибка при обновлении каталога.", "danger");
  });
  document.addEventListener("htmx:afterSwap", function (event) {
    initOplotComponents(event.detail && event.detail.target ? event.detail.target : document);
  });
})();

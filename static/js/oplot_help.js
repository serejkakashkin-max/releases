(function () {
  "use strict";

  function setCopyStatus(root, message, isError) {
    var status = root.querySelector("[data-help-copy-status]");
    if (status) {
      status.textContent = message;
      status.classList.toggle("is-error", Boolean(isError));
    }
    if (window.OplotUI && typeof window.OplotUI.showToast === "function") {
      window.OplotUI.showToast(message, isError ? "danger" : "success");
    }
  }

  function fallbackCopy(text) {
    var textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.select();
    var copied = document.execCommand("copy");
    textarea.remove();
    if (!copied) throw new Error("copy_failed");
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return Promise.resolve().then(function () { fallbackCopy(text); });
  }

  function initClipboard(root) {
    root.querySelectorAll("[data-help-copy]").forEach(function (button) {
      if (button.dataset.helpCopyInitialized === "true") return;
      button.dataset.helpCopyInitialized = "true";
      button.addEventListener("click", function () {
        var text = button.dataset.helpCopy || "";
        if (!text) return;
        var originalLabel = button.textContent;
        button.disabled = true;
        copyText(text).then(function () {
          button.textContent = "Скопировано";
          setCopyStatus(root, "Команда скопирована", false);
        }).catch(function () {
          setCopyStatus(root, "Не удалось скопировать команду. Выделите текст вручную.", true);
        }).finally(function () {
          window.setTimeout(function () {
            button.textContent = originalLabel;
            button.disabled = false;
          }, 1400);
        });
      });
    });
  }

  function initSectionTracking(root) {
    if (!("IntersectionObserver" in window) || root._oplotHelpObserver) return;
    var links = Array.from(root.querySelectorAll('.oplot-help-toc a[href^="#"]'));
    var byId = new Map(links.map(function (link) { return [link.getAttribute("href").slice(1), link]; }));
    var sections = Array.from(root.querySelectorAll(".oplot-help-section[id]")).filter(function (section) { return byId.has(section.id); });
    if (!sections.length) return;

    root._oplotHelpObserver = new IntersectionObserver(function (entries) {
      var visible = entries.filter(function (entry) { return entry.isIntersecting; }).sort(function (a, b) { return a.boundingClientRect.top - b.boundingClientRect.top; });
      if (!visible.length) return;
      links.forEach(function (link) { link.removeAttribute("aria-current"); });
      byId.get(visible[0].target.id).setAttribute("aria-current", "location");
    }, { rootMargin: "-96px 0px -65% 0px", threshold: [0, 0.1] });
    sections.forEach(function (section) { root._oplotHelpObserver.observe(section); });
  }

  function initHelp(container) {
    var root = (container || document).querySelector ? (container || document).querySelector(".oplot-help-layout") : null;
    if (!root) return;
    initClipboard(root);
    initSectionTracking(root);
  }

  window.initOplotHelp = initHelp;
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", function () { initHelp(document); }, { once: true });
  else initHelp(document);
}());

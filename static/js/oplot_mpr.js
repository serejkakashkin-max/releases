(function () {
  "use strict";

  var CONFIG_ID = "oplot-mpr-config";
  var ROOT_ID = "oplotMprRoot";
  var modalInstances = new WeakMap();

  function parseConfig() {
    var element = document.getElementById(CONFIG_ID);
    if (!element) {
      throw new Error("mpr_configuration_error");
    }
    var config;
    try {
      config = JSON.parse(element.textContent || "{}");
    } catch (_error) {
      throw new Error("mpr_configuration_error");
    }
    if (!config || !config.urls || !isLocalUrl(config.urls.preview) || !isLocalUrl(config.urls.generate)) {
      throw new Error("mpr_configuration_error");
    }
    return config;
  }

  function isLocalUrl(value) {
    if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//")) {
      return false;
    }
    try {
      return new URL(value, window.location.origin).origin === window.location.origin;
    } catch (_error) {
      return false;
    }
  }

  function getMprModal(element) {
    var ModalApi = window.tabler && window.tabler.Modal;
    if (typeof ModalApi !== "function") {
      throw new Error("mpr_modal_unavailable");
    }
    if (typeof ModalApi.getOrCreateInstance === "function") {
      var managed = ModalApi.getOrCreateInstance(element);
      modalInstances.set(element, managed);
      return managed;
    }
    if (typeof ModalApi.getInstance === "function") {
      var existing = ModalApi.getInstance(element);
      if (existing) {
        modalInstances.set(element, existing);
        return existing;
      }
    }
    if (modalInstances.has(element)) {
      return modalInstances.get(element);
    }
    var instance = new ModalApi(element);
    modalInstances.set(element, instance);
    return instance;
  }

  function initOplotMprPage() {
    var root = document.getElementById(ROOT_ID);
    if (!root || root.dataset.oplotMprInitialized === "true") {
      return;
    }
    root.dataset.oplotMprInitialized = "true";

    var fileInput = document.getElementById("mprFiles");
    var fileList = document.getElementById("mprFileList");
    var templateCodeInput = document.getElementById("mprTemplateCode");
    var alertBox = document.getElementById("mprAlert");
    var statusBox = document.getElementById("mprStatus");
    var generateBtn = document.getElementById("mprGenerateBtn");
    var packageModalElement = document.getElementById("mprPackageModal");
    var packageSummary = document.getElementById("mprPackageSummary");
    var packageOptions = document.getElementById("mprPackageOptions");
    var unmappedBox = document.getElementById("mprUnmapped");
    var confirmGenerateBtn = document.getElementById("mprConfirmGenerateBtn");
    var confirmGenerateLabel = document.getElementById("mprConfirmGenerateLabel");
    var resultRoot = document.getElementById("mprResult");
    var downloadAgainBtn = document.getElementById("mprDownloadAgainBtn");
    var newGenerationBtn = document.getElementById("mprNewGenerationBtn");
    var config;
    var selectedTemplateCode = "";
    var selectedTemplateName = "";
    var mprPreview = null;
    var resultObjectUrl = "";
    var resultFilename = "";
    var modalReturnFocus = null;

    function setAlert(message, details) {
      alertBox.replaceChildren();
      if (!message) {
        alertBox.hidden = true;
        return;
      }
      var title = document.createElement("strong");
      title.textContent = String(message);
      alertBox.appendChild(title);
      if (Array.isArray(details) && details.length) {
        var list = document.createElement("ul");
        details.forEach(function (item) {
          var row = document.createElement("li");
          row.textContent = String(item);
          list.appendChild(row);
        });
        alertBox.appendChild(list);
      }
      alertBox.hidden = false;
    }

    function setLoading(loading, message) {
      root.setAttribute("aria-busy", loading ? "true" : "false");
      generateBtn.disabled = loading || !selectedTemplateCode;
      statusBox.classList.toggle("is-loading", loading);
      statusBox.textContent = message || "";
    }

    function formatBytes(size) {
      var bytes = Number(size || 0);
      if (bytes < 1024) return bytes + " Б";
      if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " КиБ";
      return (bytes / (1024 * 1024)).toFixed(1) + " МиБ";
    }

    function renderFileList() {
      fileList.replaceChildren();
      var files = Array.from(fileInput.files || []);
      if (!files.length) {
        var empty = document.createElement("div");
        empty.className = "oplot-mpr__empty-files";
        empty.textContent = "Файлы пока не выбраны.";
        fileList.appendChild(empty);
        return;
      }
      files.forEach(function (file) {
        var row = document.createElement("div");
        row.className = "oplot-mpr__file-row";
        var name = document.createElement("strong");
        name.textContent = file.name;
        var size = document.createElement("span");
        size.textContent = formatBytes(file.size);
        row.appendChild(name);
        row.appendChild(size);
        fileList.appendChild(row);
      });
    }

    function selectTemplate(code, name) {
      selectedTemplateCode = String(code || "");
      selectedTemplateName = String(name || selectedTemplateCode);
      templateCodeInput.value = selectedTemplateCode;
      document.querySelectorAll("[data-template-code]").forEach(function (item) {
        var active = item.dataset.templateCode === selectedTemplateCode;
        item.classList.toggle("is-active", active);
        if (item.tagName === "BUTTON") item.setAttribute("aria-pressed", active ? "true" : "false");
      });
      mprPreview = null;
      setAlert("");
    }

    function buildMprFormData(files) {
      var formData = new FormData();
      formData.append("template_code", selectedTemplateCode);
      files.forEach(function (file) { formData.append("files", file, file.name); });
      return formData;
    }

    function getSelectedPackageCodes() {
      return Array.from(packageOptions.querySelectorAll('.oplot-mpr__package-checkbox:checked')).map(function (item) { return item.value; });
    }

    function packageLabel(code) {
      var match = (mprPreview && mprPreview.packages || []).find(function (item) { return item.code === code; });
      return match ? match.label : code;
    }

    function updateConfirmGenerateState() {
      var count = getSelectedPackageCodes().length;
      var hasUnmapped = Boolean((mprPreview && mprPreview.unmapped || []).length);
      confirmGenerateBtn.disabled = count === 0 || hasUnmapped;
      confirmGenerateLabel.textContent = count > 1 ? "Сформировать пакет (" + count + ")" : count === 1 ? "Сформировать DOCX (1)" : "Выберите документ";
    }

    function renderPackageSelection(preview, filesCount) {
      packageSummary.textContent = "Файлов: " + filesCount + " · хостов после обработки: " + (preview.rows_count || 0);
      packageOptions.replaceChildren();
      (preview.packages || []).forEach(function (item) {
        var disabled = !item.available;
        var label = document.createElement("label");
        label.className = "oplot-mpr__package-option " + (disabled ? "is-disabled" : "is-selected");
        var checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.name = "packages";
        checkbox.className = "oplot-mpr__package-checkbox";
        checkbox.value = String(item.code || "");
        checkbox.disabled = disabled;
        checkbox.checked = !disabled;
        var copy = document.createElement("span");
        var name = document.createElement("strong");
        name.textContent = String(item.label || item.code || "");
        var datacenters = document.createElement("small");
        datacenters.textContent = (item.datacenters || []).join(" · ");
        copy.appendChild(name);
        copy.appendChild(datacenters);
        var count = document.createElement("span");
        count.className = "oplot-mpr__package-count";
        count.textContent = (item.rows_count || 0) + " хостов";
        label.appendChild(checkbox);
        label.appendChild(copy);
        label.appendChild(count);
        checkbox.addEventListener("change", function () {
          label.classList.toggle("is-selected", checkbox.checked);
          updateConfirmGenerateState();
        });
        packageOptions.appendChild(label);
      });
      unmappedBox.replaceChildren();
      var unmapped = preview.unmapped || [];
      if (unmapped.length) {
        var warning = document.createElement("strong");
        warning.textContent = "Есть хосты с нераспределённым значением ЦОД:";
        var list = document.createElement("ul");
        unmapped.forEach(function (item) {
          var row = document.createElement("li");
          row.textContent = String(item.datacenter) + ": " + item.rows_count + " строк";
          list.appendChild(row);
        });
        unmappedBox.appendChild(warning);
        unmappedBox.appendChild(list);
        unmappedBox.hidden = false;
      } else {
        unmappedBox.hidden = true;
      }
      updateConfirmGenerateState();
    }

    async function readErrorPayload(response) {
      var contentType = response.headers.get("content-type") || "";
      if (contentType.includes("application/json")) return await response.json();
      return { error: "Сервис вернул непредвиденный ответ" };
    }

    function getDownloadFilename(disposition) {
      var utfMatch = String(disposition || "").match(/filename\*=UTF-8''([^;]+)/i);
      if (utfMatch) {
        try { return decodeURIComponent(utfMatch[1]); } catch (_error) { return ""; }
      }
      var match = String(disposition || "").match(/filename="?([^";]+)"?/i);
      return match ? match[1] : "";
    }

    function revokeResultUrl() {
      if (resultObjectUrl) {
        URL.revokeObjectURL(resultObjectUrl);
        resultObjectUrl = "";
      }
    }

    function triggerDownload(url, filename) {
      var link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
    }

    async function downloadResponse(response, selectedPackages) {
      var blob = await response.blob();
      var disposition = response.headers.get("content-disposition") || "";
      var contentType = response.headers.get("content-type") || "";
      var isZip = contentType.includes("application/zip");
      var fallback = isZip ? "mpr.zip" : "mpr.docx";
      resultFilename = getDownloadFilename(disposition) || fallback;
      revokeResultUrl();
      resultObjectUrl = URL.createObjectURL(blob);
      triggerDownload(resultObjectUrl, resultFilename);
      document.getElementById("mprResultFilename").textContent = resultFilename;
      document.getElementById("mprResultType").textContent = isZip ? "ZIP" : "DOCX";
      document.getElementById("mprResultTemplate").textContent = selectedTemplateName || selectedTemplateCode;
      document.getElementById("mprResultSources").textContent = Array.from(fileInput.files || []).map(function (file) { return file.name; }).join(", ");
      document.getElementById("mprResultRows").textContent = String(mprPreview && mprPreview.rows_count || 0);
      document.getElementById("mprResultPackages").textContent = selectedPackages.map(packageLabel).join(", ");
      resultRoot.hidden = false;
      resultRoot.scrollIntoView({ block: "nearest", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    }

    function resetGeneration() {
      revokeResultUrl();
      resultFilename = "";
      resultRoot.hidden = true;
      fileInput.value = "";
      mprPreview = null;
      packageOptions.replaceChildren();
      unmappedBox.hidden = true;
      setAlert("");
      setLoading(false, "");
      selectTemplate(config.initial_template_code, document.querySelector('[data-template-code="' + CSS.escape(config.initial_template_code) + '"]')?.dataset.templateName || config.initial_template_code);
      renderFileList();
      fileInput.focus();
    }

    try {
      config = parseConfig();
      selectedTemplateCode = String(config.initial_template_code || "");
    } catch (_error) {
      config = null;
      generateBtn.disabled = true;
      confirmGenerateBtn.disabled = true;
      setAlert("Страница МПР настроена некорректно. Обновите страницу или обратитесь к администратору.");
      renderFileList();
      return;
    }

    document.querySelectorAll("button[data-template-code]").forEach(function (button) {
      button.addEventListener("click", function () { selectTemplate(button.dataset.templateCode, button.dataset.templateName); });
    });
    var initialTemplate = document.querySelector('[data-template-code="' + CSS.escape(selectedTemplateCode) + '"]');
    selectTemplate(selectedTemplateCode, initialTemplate && initialTemplate.dataset.templateName);

    fileInput.addEventListener("change", function () {
      renderFileList();
      mprPreview = null;
      resultRoot.hidden = true;
      revokeResultUrl();
      setAlert("");
    });

    generateBtn.addEventListener("click", async function () {
      setAlert("");
      var files = Array.from(fileInput.files || []);
      if (!selectedTemplateCode) return setAlert("Не выбран шаблон");
      if (!files.length) return setAlert("Не загружены файлы");
      setLoading(true, "Проверяем состав комплектов…");
      try {
        var response = await fetch(config.urls.preview, { method: "POST", body: buildMprFormData(files) });
        if (!response.ok) throw await readErrorPayload(response);
        mprPreview = await response.json();
        renderPackageSelection(mprPreview, files.length);
        setLoading(false, "Данные проверены. Выберите комплекты для формирования.");
        modalReturnFocus = generateBtn;
        getMprModal(packageModalElement).show();
      } catch (error) {
        setLoading(false, "");
        setAlert(error && error.error || (error && error.message === "mpr_modal_unavailable" ? "Не удалось открыть выбор комплектов. Обновите страницу." : "Не удалось проверить данные МПР"), error && error.details || []);
      }
    });

    confirmGenerateBtn.addEventListener("click", async function () {
      var selectedPackages = getSelectedPackageCodes();
      if (!selectedPackages.length || Boolean((mprPreview && mprPreview.unmapped || []).length)) return updateConfirmGenerateState();
      var files = Array.from(fileInput.files || []);
      var formData = buildMprFormData(files);
      selectedPackages.forEach(function (code) { formData.append("packages", code); });
      confirmGenerateBtn.disabled = true;
      confirmGenerateLabel.textContent = "Формируем…";
      root.setAttribute("aria-busy", "true");
      try {
        var response = await fetch(config.urls.generate, { method: "POST", body: formData });
        if (!response.ok) throw await readErrorPayload(response);
        await downloadResponse(response, selectedPackages);
        getMprModal(packageModalElement).hide();
        statusBox.textContent = selectedPackages.length > 1 ? "Пакет МПР сформирован." : "DOCX МПР сформирован.";
        setAlert("");
      } catch (error) {
        try { getMprModal(packageModalElement).hide(); } catch (_modalError) { /* controlled below */ }
        statusBox.textContent = "";
        setAlert(error && error.error || "Не удалось сформировать документы МПР", error && error.details || []);
      } finally {
        root.setAttribute("aria-busy", "false");
        updateConfirmGenerateState();
      }
    });

    packageModalElement.addEventListener("hidden.bs.modal", function () {
      if (modalReturnFocus && document.contains(modalReturnFocus)) modalReturnFocus.focus();
      modalReturnFocus = null;
    });
    downloadAgainBtn.addEventListener("click", function () { if (resultObjectUrl) triggerDownload(resultObjectUrl, resultFilename); });
    newGenerationBtn.addEventListener("click", resetGeneration);
    window.addEventListener("pagehide", revokeResultUrl, { once: true });
    renderFileList();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initOplotMprPage, { once: true });
  } else {
    initOplotMprPage();
  }
})();

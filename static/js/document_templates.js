(function () {
  "use strict";

  var activeController = null;
  var modalInstance = null;
  var initialized = false;
  var previewGeneration = 0;
  var PLACEHOLDER_PATH = "word/media/oplot-external-image-placeholder.png";
  var PLACEHOLDER_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL1WQAAAABJRU5ErkJggg==";
  var OFFICE_RELATIONSHIP_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";

  function base64Bytes(value) {
    var binary = window.atob(value);
    var bytes = new Uint8Array(binary.length);
    for (var index = 0; index < binary.length; index += 1) {
      bytes[index] = binary.charCodeAt(index);
    }
    return bytes;
  }

  function relativeTarget(fromDirectory, targetPath) {
    var from = fromDirectory.split("/").filter(Boolean);
    var to = targetPath.split("/").filter(Boolean);
    while (from.length && to.length && from[0] === to[0]) {
      from.shift();
      to.shift();
    }
    return from.map(function () { return ".."; }).concat(to).join("/");
  }

  function relationshipOwnerPath(relsPath) {
    var match = relsPath.match(/^(.*?)(?:\/)?_rels\/([^/]+)\.rels$/);
    if (!match) {
      return "";
    }
    return (match[1] ? match[1] + "/" : "") + match[2];
  }

  function relationshipOwnerDirectory(relsPath) {
    var ownerPath = relationshipOwnerPath(relsPath);
    var separator = ownerPath.lastIndexOf("/");
    return separator === -1 ? "" : ownerPath.slice(0, separator);
  }

  function convertLinkedImagesToEmbedded(zip, ownerPath, relationshipIds) {
    var ownerEntry = zip.file(ownerPath);
    if (!ownerEntry || !relationshipIds.length) {
      return Promise.resolve();
    }
    return ownerEntry.async("string").then(function (xml) {
      var ownerXml = new DOMParser().parseFromString(xml, "application/xml");
      if (ownerXml.querySelector("parsererror")) {
        throw new Error("Некорректная DOCX XML-часть");
      }
      var changed = false;
      Array.prototype.slice.call(ownerXml.getElementsByTagName("*")).forEach(function (node) {
        var relationshipId = node.getAttributeNS(OFFICE_RELATIONSHIP_NS, "link");
        if (relationshipIds.indexOf(relationshipId) === -1) {
          return;
        }
        node.setAttributeNS(OFFICE_RELATIONSHIP_NS, "r:embed", relationshipId);
        node.removeAttributeNS(OFFICE_RELATIONSHIP_NS, "link");
        changed = true;
      });
      if (changed) {
        zip.file(ownerPath, new XMLSerializer().serializeToString(ownerXml));
      }
    });
  }

  function ensurePngContentType(zip) {
    var entry = zip.file("[Content_Types].xml");
    if (!entry) {
      return Promise.resolve();
    }
    return entry.async("string").then(function (xml) {
      var documentXml = new DOMParser().parseFromString(xml, "application/xml");
      if (documentXml.querySelector("parsererror")) {
        throw new Error("Некорректный DOCX content types");
      }
      var defaults = Array.prototype.slice.call(documentXml.getElementsByTagNameNS("*", "Default"));
      var hasPng = defaults.some(function (node) {
        return String(node.getAttribute("Extension") || "").toLowerCase() === "png";
      });
      if (!hasPng) {
        var root = documentXml.documentElement;
        var node = documentXml.createElementNS(root.namespaceURI, "Default");
        node.setAttribute("Extension", "png");
        node.setAttribute("ContentType", "image/png");
        root.appendChild(node);
        zip.file("[Content_Types].xml", new XMLSerializer().serializeToString(documentXml));
      }
    });
  }

  function neutralizeRelationshipFile(zip, relsPath) {
    return zip.file(relsPath).async("string").then(function (xml) {
      var relationshipsXml = new DOMParser().parseFromString(xml, "application/xml");
      if (relationshipsXml.querySelector("parsererror")) {
        throw new Error("Некорректные DOCX relationships");
      }
      var changed = false;
      var ownerDirectory = relationshipOwnerDirectory(relsPath);
      var embeddedImageIds = [];
      var relationships = Array.prototype.slice.call(relationshipsXml.getElementsByTagNameNS("*", "Relationship"));
      relationships.forEach(function (relationship) {
        if (String(relationship.getAttribute("TargetMode") || "").toLowerCase() !== "external") {
          return;
        }
        var type = String(relationship.getAttribute("Type") || "").toLowerCase();
        if (type.endsWith("/image")) {
          relationship.setAttribute("Target", relativeTarget(ownerDirectory, PLACEHOLDER_PATH));
          relationship.removeAttribute("TargetMode");
          embeddedImageIds.push(String(relationship.getAttribute("Id") || ""));
        } else {
          relationship.setAttribute("Target", "about:blank");
        }
        changed = true;
      });
      if (changed) {
        zip.file(relsPath, new XMLSerializer().serializeToString(relationshipsXml));
      }
      return convertLinkedImagesToEmbedded(
        zip,
        relationshipOwnerPath(relsPath),
        embeddedImageIds.filter(Boolean)
      );
    });
  }

  function buildSafePreviewBuffer(arrayBuffer) {
    if (!window.JSZip) {
      return Promise.reject(new Error("Локальная библиотека JSZip не загружена"));
    }
    return window.JSZip.loadAsync(arrayBuffer).then(function (zip) {
      zip.file(PLACEHOLDER_PATH, base64Bytes(PLACEHOLDER_BASE64));
      var tasks = [];
      zip.forEach(function (path, entry) {
        if (!entry.dir && path.toLowerCase().endsWith(".rels")) {
          tasks.push(neutralizeRelationshipFile(zip, path));
        }
      });
      tasks.push(ensurePngContentType(zip));
      return Promise.all(tasks).then(function () {
        return zip.generateAsync({ type: "arraybuffer", compression: "DEFLATE" });
      });
    });
  }

  function elements() {
    return {
      modal: document.getElementById("document-preview-modal"),
      title: document.getElementById("document-preview-title"),
      source: document.getElementById("document-preview-source"),
      loader: document.getElementById("document-preview-loader"),
      error: document.getElementById("document-preview-error"),
      container: document.getElementById("document-preview-container")
    };
  }

  function cleanupPreview() {
    previewGeneration += 1;
    if (activeController) {
      activeController.abort();
      activeController = null;
    }
    var view = elements();
    if (view.container) {
      view.container.replaceChildren();
    }
    if (view.error) {
      view.error.hidden = true;
      view.error.textContent = "";
    }
    if (view.loader) {
      view.loader.hidden = true;
    }
  }

  function showError(message) {
    var view = elements();
    if (view.loader) {
      view.loader.hidden = true;
    }
    if (view.error) {
      view.error.textContent = message;
      view.error.hidden = false;
    }
    if (window.OplotUI) {
      window.OplotUI.showToast(message, "danger");
    }
  }

  function openPreview(button) {
    var view = elements();
    var Modal = window.tabler && (window.tabler.Modal || (window.tabler.bootstrap && window.tabler.bootstrap.Modal));
    if (!view.modal || !view.container || !Modal) {
      showError("Компонент предпросмотра не загружен.");
      return;
    }
    cleanupPreview();
    var generation = previewGeneration;
    activeController = new AbortController();
    view.title.textContent = button.dataset.documentName || "Просмотр документа";
    if (view.source) {
      view.source.textContent = button.dataset.previewSource || "Документ";
    }
    view.loader.hidden = false;
    var renderTarget = document.createElement("div");
    view.container.appendChild(renderTarget);
    modalInstance = Modal.getOrCreateInstance(view.modal);
    modalInstance.show();
    if (window.OplotUI) {
      window.OplotUI.beginOperation();
    }

    fetch(button.dataset.previewUrl, {
      method: "GET",
      credentials: "same-origin",
      headers: { "Accept": "application/vnd.openxmlformats-officedocument.wordprocessingml.document" },
      signal: activeController.signal
    }).then(function (response) {
      if (!response.ok) {
        throw new Error(response.status === 404 ? "Документ больше не доступен." : "Не удалось загрузить документ.");
      }
      return response.arrayBuffer();
    }).then(buildSafePreviewBuffer).then(function (safeBuffer) {
      if (generation !== previewGeneration) {
        throw new DOMException("Preview superseded", "AbortError");
      }
      if (!window.docx || typeof window.docx.renderAsync !== "function") {
        throw new Error("Локальная библиотека docx-preview не загружена.");
      }
      return window.docx.renderAsync(safeBuffer, renderTarget, null, {
        experimental: false,
        renderAltChunks: false,
        ignoreFonts: true,
        useBase64URL: true,
        renderHeaders: true,
        renderFooters: true,
        renderFootnotes: true,
        renderEndnotes: true,
        renderComments: false
      });
    }).then(function () {
      if (generation === previewGeneration) {
        view.loader.hidden = true;
      }
    }).catch(function (error) {
      if (error && error.name === "AbortError") {
        return;
      }
      if (generation === previewGeneration) {
        showError(error && error.message ? error.message : "Не удалось отобразить документ.");
      }
    }).finally(function () {
      if (window.OplotUI) {
        window.OplotUI.endOperation();
      }
    });
  }

  function readVariantsMap() {
    var source = document.getElementById("oplot-dtc-variants-map");
    if (!source) {
      return {};
    }
    try {
      var value = JSON.parse(source.textContent || "{}");
      return value && typeof value === "object" && !Array.isArray(value) ? value : {};
    } catch (_error) {
      return {};
    }
  }

  function variantsForCategory(map, category) {
    if (category && Array.isArray(map[category])) {
      return map[category].slice();
    }
    var seen = Object.create(null);
    var result = [];
    Object.keys(map).forEach(function (key) {
      if (!Array.isArray(map[key])) {
        return;
      }
      map[key].forEach(function (value) {
        var text = String(value || "");
        if (text && !seen[text]) {
          seen[text] = true;
          result.push(text);
        }
      });
    });
    return result.sort(function (left, right) {
      return left.localeCompare(right, "ru", { sensitivity: "base" });
    });
  }

  function syncVariantFilter() {
    var category = document.getElementById("category-filter");
    var variant = document.getElementById("variant-filter");
    if (!category || !variant) {
      return;
    }
    var selected = variant.value;
    var values = variantsForCategory(readVariantsMap(), category.value);
    while (variant.options.length) {
      variant.remove(0);
    }
    variant.add(new Option("Все", ""));
    values.forEach(function (value) {
      variant.add(new Option(value, value));
    });
    variant.value = values.indexOf(selected) !== -1 ? selected : "";
  }

  function initialize() {
    if (initialized) {
      return;
    }
    initialized = true;
    syncVariantFilter();
    document.addEventListener("change", function (event) {
      if (event.target && event.target.id === "category-filter") {
        syncVariantFilter();
      }
    }, true);
    document.addEventListener("click", function (event) {
      var button = event.target.closest(".js-document-preview");
      if (button) {
        event.preventDefault();
        openPreview(button);
      }
      var uploadButton = event.target.closest(".js-candidate-upload");
      if (uploadButton) {
        event.preventDefault();
        var uploadModal = document.getElementById("candidate-upload-modal");
        var form = document.getElementById("candidate-upload-form");
        var Modal = window.tabler && (window.tabler.Modal || (window.tabler.bootstrap && window.tabler.bootstrap.Modal));
        if (uploadModal && form && Modal) {
          form.action = uploadButton.dataset.uploadUrl;
          document.getElementById("upload-active-name").textContent = uploadButton.dataset.documentName || "";
          document.getElementById("upload-active-sha").textContent = uploadButton.dataset.documentSha || "";
          Modal.getOrCreateInstance(uploadModal).show();
        }
      }
      var rollbackButton = event.target.closest(".js-rollback-open");
      if (rollbackButton) {
        var rollbackForm = document.getElementById("rollback-confirm-form");
        if (rollbackForm) {
          rollbackForm.action = rollbackButton.dataset.rollbackUrl;
          document.getElementById("rollback-version-sha").textContent = rollbackButton.dataset.versionSha || "";
          document.getElementById("rollback-version-actor").textContent = rollbackButton.dataset.versionActor || "";
          document.getElementById("rollback-version-date").textContent = rollbackButton.dataset.versionDate || "";
        }
      }
    });
    document.addEventListener("submit", function (event) {
      var form = event.target;
      if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
        event.preventDefault();
        return;
      }
      if (!event.defaultPrevented) {
        Array.prototype.forEach.call(form.querySelectorAll('button[type="submit"]'), function (button) {
          button.disabled = true;
          button.classList.add("is-loading");
        });
      }
    });
    var candidateFile = document.getElementById("candidate-file");
    if (candidateFile) {
      candidateFile.addEventListener("change", function () {
        var caption = document.getElementById("candidate-file-caption");
        var file = candidateFile.files && candidateFile.files[0];
        if (caption) {
          caption.textContent = file ? file.name + " · " + (file.size / 1024 / 1024).toFixed(2) + " МиБ" : "Файл ещё не выбран";
        }
        var submit = document.getElementById("candidate-upload-submit");
        if (submit) {
          submit.disabled = Boolean(file && file.size > 10 * 1024 * 1024);
        }
      });
    }
    var view = elements();
    if (view.modal) {
      view.modal.addEventListener("hidden.bs.modal", cleanupPreview);
    }
    document.addEventListener("htmx:afterRequest", function (event) {
      var status = event.detail && event.detail.xhr ? event.detail.xhr.status : 0;
      var target = event.detail && event.detail.target;
      if (event.detail && event.detail.failed && !(status === 422 && target && target.id === "candidate-panel")) {
        if (window.OplotUI) {
          window.OplotUI.showToast("Не удалось обновить данные Центра шаблонов.", "danger");
        }
      }
    });
    document.addEventListener("htmx:afterSwap", function (event) {
      if (event.detail && event.detail.target && event.detail.target.id === "template-catalog") {
        syncVariantFilter();
      }
    });
    document.addEventListener("htmx:beforeSwap", function (event) {
      var detail = event.detail || {};
      var status = detail.xhr ? detail.xhr.status : 0;
      if ((status === 503 && detail.target && detail.target.id === "template-catalog") ||
          (status === 422 && detail.target && detail.target.id === "candidate-panel")) {
        detail.shouldSwap = true;
        detail.isError = false;
      }
    });
    document.addEventListener("htmx:sendError", function () {
      if (window.OplotUI) {
        window.OplotUI.showToast("Сетевая ошибка при обновлении Центра шаблонов.", "danger");
      }
    });
  }

  window.OplotComponentInitializers = window.OplotComponentInitializers || [];
  window.OplotComponentInitializers.push(initialize);
})();

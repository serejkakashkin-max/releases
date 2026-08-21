(function () {
  "use strict";

  function initOplotSupAdminPage() {
    const root = document.getElementById("oplotSupAdminRoot");
    if (!root || root.dataset.oplotSupAdminInitialized === "true") return;
    root.dataset.oplotSupAdminInitialized = "true";

    function showConfigFailure() {
      const error = document.getElementById("supAdminConfigError");
      if (error) {
        error.textContent = "Не удалось безопасно инициализировать административный интерфейс.";
        error.classList.remove("hidden");
      }
      root.inert = true;
    }

    function localUrl(value) {
      if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//")) return "";
      try {
        const parsed = new URL(value, window.location.origin);
        if (parsed.origin !== window.location.origin) return "";
        return `${parsed.pathname}${parsed.search}${parsed.hash}`;
      } catch (_error) {
        return "";
      }
    }

    function parseSupUiConfig() {
      try {
        const node = document.getElementById("oplot-sup-admin-config");
        const parsed = JSON.parse(node?.textContent || "null");
        const required = [
          "data", "save", "admin_session_login", "admin_session_status", "release_refresh_status",
          "release_refresh_start", "employee_directory", "employee_directory_save",
          "va_admin", "va_competencies", "release_monitor"
        ];
        if (!parsed || typeof parsed !== "object" || !parsed.urls || !parsed.url_templates) return null;
        const urls = {};
        for (const key of required) {
          urls[key] = localUrl(parsed.urls[key]);
          if (!urls[key]) return null;
        }
        const templates = {};
        for (const key of ["va_employee_settings", "va_competency"]) {
          const spec = parsed.url_templates[key];
          if (!spec || typeof spec.placeholder !== "string") return null;
          const value = localUrl(spec.value);
          if (!value || value.split(spec.placeholder).length !== 2) return null;
          templates[key] = { value, placeholder: spec.placeholder };
        }
        const scheduleManager = parsed.schedule_manager || {};
        const scheduleUrl = scheduleManager.url ? localUrl(scheduleManager.url) : "";
        return {
          urls,
          url_templates: templates,
          default_tab: typeof parsed.default_tab === "string" ? parsed.default_tab : "employees",
          default_view: typeof parsed.default_view === "string" ? parsed.default_view : "employees",
          tab_aliases: parsed.tab_aliases && typeof parsed.tab_aliases === "object" ? parsed.tab_aliases : {},
          schedule_manager: { ...scheduleManager, url: scheduleUrl }
        };
      } catch (_error) {
        return null;
      }
    }

    const supUiConfig = parseSupUiConfig();
    if (!supUiConfig) {
      showConfigFailure();
      return;
    }

    function getSupUrl(key) {
      const value = supUiConfig.urls[key];
      if (!value) throw new Error("Недоступен безопасный адрес административной операции.");
      return value;
    }

    function getSupUrlTemplate(key, rawValue) {
      const spec = supUiConfig.url_templates[key];
      if (!spec || spec.value.split(spec.placeholder).length !== 2) {
        throw new Error("Недоступен безопасный шаблон адреса административной операции.");
      }
      return spec.value.replace(spec.placeholder, encodeURIComponent(String(rawValue)));
    }

      const TOKEN_KEY = "supAdminToken";
      const state = {
        revision: "",
        config: null,
        loadedConfig: null,
        metadata: {},
        path: "",
        readError: "",
        rawJson: "",
        dirty: false,
        activeTab: supUiConfig.default_tab,
        employeeFilter: "all",
        prefixFilter: "all",
        directory: {
          status: "missing",
          revision: 0,
          etag: "missing",
          employees: [],
          selectedIndex: 0,
          filter: "all",
          consumerHealth: {}
        },
        directoryView: supUiConfig.default_view,
        va: {
          directory: { status: "missing", revision: null, etag: "missing" },
          settings: {
            status: "missing",
            revision: 0,
            etag: "missing",
            migration_status: "required",
            ready: false,
            defaults: {},
            employees: {}
          },
          competencies: { status: "missing", etag: "missing", items: [] },
          newcomerAlerts: { status: "unavailable", items: [] }
        },
        vaDrafts: {},
        competencyModal: { code: "", mode: "add" },
        modal: { type: "", index: -1 },
        releaseRefresh: {
          controller: null,
          confirmationMode: "",
          lastPayload: null
        }
      };

      const maintenanceLabels = {
        index: "Главная страница",
        release_monitor: "Блок релизов",
        duty_dashboard: "Рабочий стол дежурного",
        chatbot: "Чат-бот"
      };
      const maintenanceDescriptions = {
        index: "Закрывает главную страницу проекта.",
        release_monitor: "Закрывает Блок релизов, Центр назначений и связанные экраны.",
        duty_dashboard: "Закрывает рабочий стол дежурного.",
        chatbot: "Отключает обращения к чат-боту для пользователей."
      };
      const maintenanceCodes = {
        index: "MAIN",
        release_monitor: "RM",
        duty_dashboard: "DUTY",
        chatbot: "BOT"
      };

      const $ = (id) => document.getElementById(id);
      const clone = (value) => JSON.parse(JSON.stringify(value || {}));
      const buttonFeedbackTimers = new WeakMap();

      function setStatus(message, kind = "") {
        const box = $("statusBox");
        box.textContent = message;
        box.className = `status ${kind || ""}`.trim();
      }

      function beginButtonAction(button, label = "Сохранение...") {
        if (!button) return;
        const timer = buttonFeedbackTimers.get(button);
        if (timer) clearTimeout(timer);
        if (!button.dataset.actionIdleLabel) {
          button.dataset.actionIdleLabel = button.textContent.trim();
        }
        button.classList.remove("action-success", "action-error");
        button.classList.add("action-busy");
        button.textContent = label;
        button.disabled = true;
        button.setAttribute("aria-busy", "true");
      }

      function finishButtonAction(button, {
        success = true,
        label = success ? "Сохранено" : "Не сохранено",
        disabled = false,
        duration = 1500
      } = {}) {
        if (!button) return;
        const timer = buttonFeedbackTimers.get(button);
        if (timer) clearTimeout(timer);
        const idleLabel = button.dataset.actionIdleLabel || button.textContent.trim();
        button.classList.remove("action-busy", "action-success", "action-error");
        button.classList.add(success ? "action-success" : "action-error");
        button.textContent = label;
        button.disabled = true;
        button.removeAttribute("aria-busy");
        const restoreTimer = window.setTimeout(() => {
          if (!button.isConnected) return;
          button.classList.remove("action-success", "action-error");
          button.textContent = idleLabel;
          button.disabled = disabled;
          buttonFeedbackTimers.delete(button);
        }, duration);
        buttonFeedbackTimers.set(button, restoreTimer);
      }

      function getToken() {
        return sessionStorage.getItem(TOKEN_KEY) || "";
      }

      function splitLines(value) {
        return String(value || "")
          .split(/\r?\n|,/)
          .map((item) => item.trim())
          .filter(Boolean);
      }

      function lines(values) {
        return (values || []).join("\n");
      }

      function collectSbertrackRoutesFromDom() {
        const rows = [];
        document.querySelectorAll("[data-sbertrack-route-row]").forEach((row) => {
          rows.push({
            enabled: row.querySelector("[data-route-enabled]")?.checked || false,
            name: row.querySelector("[data-route-name]")?.value.trim() || "",
            target_system: row.querySelector("[data-route-target]")?.value || "sbertrack",
            subject_triggers: splitLines(row.querySelector("[data-route-triggers]")?.value || ""),
            spaces: splitLines(row.querySelector("[data-route-spaces]")?.value || ""),
            jira_projects: splitLines(row.querySelector("[data-route-projects]")?.value || ""),
            jira_domain: row.querySelector("[data-route-domain]")?.value || "sberbank",
            jira_issue_type: row.querySelector("[data-route-type]")?.value.trim() || "Task",
            jira_issue_type_id: row.querySelector("[data-route-type-id]")?.value.trim() || "",
            jira_epic_name_field: row.querySelector("[data-route-epic-name-field]")?.value.trim() || "",
            jira_epic_link: {
              field_id: row.querySelector("[data-route-epic-link-field]")?.value.trim() || "",
              key: row.querySelector("[data-route-epic-link-key]")?.value.trim() || ""
            },
            jira_priority: row.querySelector("[data-route-jira-priority]")?.value.trim() || "Minor",
            jira_labels: splitLines(row.querySelector("[data-route-labels]")?.value || ""),
            jira_team: {
              field_id: row.querySelector("[data-route-team-field]")?.value.trim() || "",
              value_id: row.querySelector("[data-route-team-value]")?.value.trim() || "",
              name: row.querySelector("[data-route-team-name]")?.value.trim() || ""
            },
            suit: row.querySelector("[data-route-suit]")?.value.trim() || "task",
            priority: row.querySelector("[data-route-priority]")?.value.trim() || "low",
            summary_template: row.querySelector("[data-route-summary]")?.value.trim() || "{subject}"
          });
        });
        return rows;
      }

      function collectSbertrackUsersFromDom() {
        const rows = [];
        document.querySelectorAll("[data-sbertrack-user-row]").forEach((row) => {
          rows.push({
            enabled: row.querySelector("[data-user-enabled]")?.checked || false,
            email: (row.querySelector("[data-user-email]")?.value || "").trim().toLowerCase(),
            name: row.querySelector("[data-user-name]")?.value.trim() || "",
            sbertrack_user_id: row.querySelector("[data-user-id]")?.value.trim() || ""
          });
        });
        return rows;
      }

      function escapeHtml(value) {
        return String(value || "")
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;");
      }

      function escapeAttr(value) {
        return escapeHtml(value).replace(/'/g, "&#39;");
      }

      function initTokenFromUrl() {
        const params = new URLSearchParams(window.location.search);
        const legacyTabs = supUiConfig.tab_aliases;
        const requestedTab = legacyTabs[params.get("tab")] || params.get("tab");
        const requestedView = params.get("view");
        if (document.querySelector(`[data-tab="${requestedTab}"]`)) {
          state.activeTab = requestedTab;
        }
        if (["employees", "competencies"].includes(requestedView)) {
          state.directoryView = requestedView;
        }
        const token = params.get("token");
        if (token) {
          sessionStorage.setItem(TOKEN_KEY, token);
          params.delete("token");
          const nextUrl = `${window.location.pathname}${params.toString() ? "?" + params.toString() : ""}`;
          window.history.replaceState({}, document.title, nextUrl);
        }
      }

      async function api(path, options = {}) {
        const headers = new Headers(options.headers || {});
        headers.set("X-SUP-Admin-Token", getToken());
        if (options.body && !headers.has("Content-Type")) {
          headers.set("Content-Type", "application/json");
        }
        const response = await fetch(path, { ...options, headers });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || payload.success === false) {
          const error = new Error(payload.error || `HTTP ${response.status}`);
          error.payload = payload;
          error.status = response.status;
          throw error;
        }
        return payload;
      }

      function initConfigShape(config) {
        config.maintenance = config.maintenance || {};
        config.automation = config.automation || {};
        config.automation.release_monitor_unassigned_email = config.automation.release_monitor_unassigned_email || { enabled: false, recipients: [] };
        config.automation.release_monitor_responsible_email = config.automation.release_monitor_responsible_email || {};
        config.automation.email_to_sbertrack = config.automation.email_to_sbertrack || {};
        const unassigned = config.automation.release_monitor_unassigned_email;
        unassigned.recipients = Array.isArray(unassigned.recipients) ? unassigned.recipients : [];
        unassigned.weekly_reminder_recipients = Array.isArray(unassigned.weekly_reminder_recipients) ? unassigned.weekly_reminder_recipients : [];
        const responsible = config.automation.release_monitor_responsible_email;
        responsible.employee_recipients = Array.isArray(responsible.employee_recipients) ? responsible.employee_recipients : [];
        responsible.weekly_digest_recipients = Array.isArray(responsible.weekly_digest_recipients) ? responsible.weekly_digest_recipients : [];
        const sbertrack = config.automation.email_to_sbertrack;
        sbertrack.technical_mailboxes = Array.isArray(sbertrack.technical_mailboxes) ? sbertrack.technical_mailboxes : [];
        sbertrack.routes = Array.isArray(sbertrack.routes) ? sbertrack.routes : [];
        config.release_monitor = config.release_monitor || {};
        config.release_monitor.prefixes = Array.isArray(config.release_monitor.prefixes) ? config.release_monitor.prefixes : [];
        config.modules = config.modules || {};
        config.modules.va_schedule_manager = config.modules.va_schedule_manager || { enabled: false };
        config.modules.va_schedule_manager.enabled = Boolean(config.modules.va_schedule_manager.enabled);
        config.document_template_center = config.document_template_center || {};
        config.document_template_center.history_retention_limit = Math.max(
          1,
          Math.min(30, Number(config.document_template_center.history_retention_limit || 2))
        );
        config.integrations = config.integrations || {};
        config.integrations.gigachat = config.integrations.gigachat || { enabled: true };
        config.integrations.gigachat.enabled = config.integrations.gigachat.enabled !== false;
        config.sbertrack_users = Array.isArray(config.sbertrack_users) ? config.sbertrack_users : [];
        return config;
      }

      function currentFormConfig() {
        const config = initConfigShape(clone(state.config));
        document.querySelectorAll("[data-maintenance]").forEach((input) => {
          config.maintenance[input.dataset.maintenance] = input.checked;
        });
        const unassigned = config.automation.release_monitor_unassigned_email;
        const responsible = config.automation.release_monitor_responsible_email;
        unassigned.enabled = $("unassignedEnabled").checked;
        unassigned.recipients = splitLines($("unassignedRecipientsInput").value);
        unassigned.weekly_reminder_enabled = $("weeklyReminderEnabled").checked;
        unassigned.weekly_reminder_time = $("weeklyReminderTimeInput").value || "09:00";
        unassigned.weekly_reminder_recipients = splitLines($("weeklyReminderRecipientsInput").value);
        responsible.enabled = $("responsibleEnabled").checked;
        responsible.weekly_digest_enabled = $("weeklyEnabled").checked;
        responsible.weekly_digest_time = $("weeklyTimeInput").value || "16:00";
        responsible.weekly_digest_recipients = splitLines($("weeklyRecipientsInput").value);
        responsible.assignment_email_delay_minutes = Number($("assignmentDelayInput").value || 0);
        responsible.personal_email_send_interval_seconds = Number($("sendIntervalInput").value || 0);
        const sbertrack = config.automation.email_to_sbertrack;
        sbertrack.enabled = $("sbertrackEnabled").checked;
        sbertrack.dry_run = $("sbertrackDryRun").checked;
        sbertrack.poll_interval_seconds = Number($("sbertrackPollIntervalInput").value || 0);
        sbertrack.lookback_limit = Number($("sbertrackLookbackInput").value || 0);
        sbertrack.max_pending_per_cycle = Number($("sbertrackPendingInput").value || 0);
        sbertrack.body_max_chars = Number($("sbertrackBodyLimitInput").value || 0);
        sbertrack.technical_mailboxes = splitLines($("sbertrackTechnicalMailboxesInput").value);
        sbertrack.routes = collectSbertrackRoutesFromDom();
        config.sbertrack_users = collectSbertrackUsersFromDom();
        config.modules = config.modules || {};
        config.modules.va_schedule_manager = config.modules.va_schedule_manager || {};
        config.modules.va_schedule_manager.enabled = Boolean($("vaScheduleManagerEnabled")?.checked);
        config.document_template_center = config.document_template_center || {};
        config.document_template_center.history_retention_limit = Math.max(
          1,
          Math.min(30, Number($("dtcHistoryRetentionLimit")?.value || 2))
        );
        config.integrations = config.integrations || {};
        config.integrations.gigachat = config.integrations.gigachat || {};
        config.integrations.gigachat.enabled = Boolean($("gigachatEnabled")?.checked);
        return config;
      }

      function markDirty() {
        state.dirty = true;
        $("saveBtn").disabled = false;
        $("resetBtn").disabled = false;
        $("fileStateBadge").textContent = "есть изменения";
        $("fileStateBadge").className = "badge yellow";
        refreshDerived();
      }

      function markClean() {
        state.dirty = false;
        $("saveBtn").disabled = true;
        $("resetBtn").disabled = true;
      }

      function renderMetadata(payload) {
        state.metadata = payload.metadata || {};
        state.path = payload.path || "";
        state.readError = payload.read_error || "";
        const fileName = state.metadata.file_name || "feature_flags.json";
        const mtime = state.metadata.file_mtime_display || "-";
        const hash = (payload.revision || "").slice(0, 12) || "-";
        $("fileName").textContent = fileName;
        $("fileMtime").textContent = mtime;
        $("fileHash").textContent = hash;
        $("diagPath").textContent = state.path || "-";
        $("diagFileName").textContent = fileName;
        $("diagMtime").textContent = mtime;
        $("diagHash").textContent = hash;
        $("diagBackupCount").textContent = String(state.metadata.backup_count ?? 0);
        $("diagReadError").textContent = state.readError || "-";
        renderSberTrackRuntimeStatus(state.metadata.email_to_sbertrack_status || {});
        renderVaScheduleManager(state.metadata.va_schedule_manager || {});
        $("fileStateBadge").textContent = state.readError ? "ошибка чтения" : "файл корректен";
        $("fileStateBadge").className = state.readError ? "badge red" : "badge green";
      }

      function renderMaintenance(values) {
        $("maintenanceToggles").innerHTML = Object.entries(maintenanceLabels).map(([key, label]) => {
          const checked = Boolean(values[key]);
          return `
            <label class="toggle-card ${checked ? "closed" : ""}" style="text-transform:none;margin:0;">
              <span class="mini-icon">${maintenanceCodes[key] || "SUP"}</span>
              <span>
                <strong>${label}</strong>
                <span class="muted" style="display:block;margin-top:3px;">${maintenanceDescriptions[key] || ""}</span>
              </span>
              <span style="display:flex;align-items:center;gap:10px;">
                <span class="badge ${checked ? "yellow" : "green"}">${checked ? "Закрыт" : "Открыт"}</span>
                <span class="switch"><input type="checkbox" data-maintenance="${key}" ${checked ? "checked" : ""}><span></span></span>
              </span>
            </label>
          `;
        }).join("");
      }

      function renderMail(config) {
        const automation = config.automation || {};
        const unassigned = automation.release_monitor_unassigned_email || {};
        const responsible = automation.release_monitor_responsible_email || {};
        $("unassignedEnabled").checked = Boolean(unassigned.enabled);
        $("unassignedRecipientsInput").value = lines(unassigned.recipients || []);
        $("weeklyReminderEnabled").checked = Boolean(unassigned.weekly_reminder_enabled);
        $("weeklyReminderTimeInput").value = unassigned.weekly_reminder_time || "09:00";
        const reminderRecipients = unassigned.weekly_reminder_recipients || [];
        $("weeklyReminderRecipientsInput").value = lines(reminderRecipients.length ? reminderRecipients : (unassigned.recipients || []));
        $("weeklyEnabled").checked = Boolean(responsible.weekly_digest_enabled);
        $("weeklyTimeInput").value = responsible.weekly_digest_time || "16:00";
        const weeklyRecipients = responsible.weekly_digest_recipients || [];
        $("weeklyRecipientsInput").value = lines(weeklyRecipients.length ? weeklyRecipients : (unassigned.recipients || []));
        $("responsibleEnabled").checked = Boolean(responsible.enabled);
        $("assignmentDelayInput").value = responsible.assignment_email_delay_minutes ?? 6;
        $("sendIntervalInput").value = responsible.personal_email_send_interval_seconds ?? 5;
        $("weeklyFallbackHint").textContent = weeklyRecipients.length
          ? "Используется отдельный список получателей недельной сводки."
          : "Сейчас показан список писем “без ответственного”; его можно отредактировать отдельно для недельной сводки.";
        $("weeklyReminderFallbackHint").textContent = reminderRecipients.length
          ? "Используется отдельный список получателей понедельничного письма."
          : "Сейчас показан общий список писем “без ответственного”; его можно отредактировать отдельно для понедельника.";
      }

      function renderSberTrack(config) {
        const automation = config.automation || {};
        const sbertrack = automation.email_to_sbertrack || {};
        $("sbertrackEnabled").checked = Boolean(sbertrack.enabled);
        $("sbertrackDryRun").checked = sbertrack.dry_run !== false;
        $("sbertrackPollIntervalInput").value = sbertrack.poll_interval_seconds ?? 300;
        $("sbertrackLookbackInput").value = sbertrack.lookback_limit ?? 20;
        $("sbertrackPendingInput").value = sbertrack.max_pending_per_cycle ?? 10;
        $("sbertrackBodyLimitInput").value = sbertrack.body_max_chars ?? 6000;
        $("sbertrackTechnicalMailboxesInput").value = lines(sbertrack.technical_mailboxes || []);
        renderSberTrackRoutes(sbertrack.routes || []);
        renderSberTrackUsers(config.sbertrack_users || []);
      }

      function normalizeEmailRouteForUi(route) {
        const next = { ...(route || {}) };
        const target = String(next.target_system || "sbertrack").toLowerCase();
        const projects = Array.isArray(next.jira_projects) ? next.jira_projects : [];
        const issueType = String(next.jira_issue_type || "").toLowerCase();
        const labels = Array.isArray(next.jira_labels) ? next.jira_labels : [];
        const team = next.jira_team && typeof next.jira_team === "object" ? next.jira_team : {};
        const isLegacyEmrm = target === "jira"
          && projects.some((item) => String(item || "").trim().toUpperCase() === "EMRM")
          && (issueType === "story" || !issueType)
          && String(team.value_id || "").trim() === "4681";

        if (isLegacyEmrm) {
          next.name = "EMRM";
          next.subject_triggers = ["EMRM"];
          next.summary_template = "{subject}";
          next.jira_issue_type = "Task";
          next.jira_issue_type_id = "3";
          next.jira_epic_name_field = "";
          next.jira_epic_link = {
            field_id: "customfield_10006",
            key: "EMRM-40162"
          };
          next.jira_labels = labels.length === 1 && labels[0] === "MPR" ? ["FromChannel"] : labels;
          next.jira_team = {
            field_id: "customfield_11902",
            value_id: "6651",
            name: "[\u0424\u043e\u043a\u0443\u0441] ForREST"
          };
        }

        if (target === "jira" && String(next.jira_issue_type || "").toLowerCase() === "epic") {
          next.jira_issue_type_id = next.jira_issue_type_id || "10000";
          next.jira_epic_name_field = next.jira_epic_name_field || "customfield_10007";
        }
        if (target === "jira" && projects.some((item) => String(item || "").trim().toUpperCase() === "EMRM")) {
          const epicLink = next.jira_epic_link && typeof next.jira_epic_link === "object" ? next.jira_epic_link : {};
          next.jira_epic_link = {
            field_id: epicLink.field_id || "customfield_10006",
            key: epicLink.key || "EMRM-40162"
          };
        }
        return next;
      }

      function renderSberTrackRoutes(routes) {
        $("sbertrackRouteRows").innerHTML = (routes || []).map((rawRoute, index) => {
          const route = normalizeEmailRouteForUi(rawRoute);
          return `
          <tr data-sbertrack-route-row="${index}">
            <td><input type="checkbox" data-route-enabled data-dirty-field ${route.enabled === false ? "" : "checked"}></td>
            <td>
              <input data-route-name data-dirty-field value="${escapeAttr(route.name || "")}" placeholder="EMRM">
              <input data-route-summary data-dirty-field value="${escapeAttr(route.summary_template || "{subject}")}" placeholder="{subject}" style="margin-top:6px;">
            </td>
            <td><textarea data-route-triggers data-dirty-field placeholder="EMRM">${escapeHtml(lines(route.subject_triggers || []))}</textarea></td>
            <td>
              <select data-route-target data-dirty-field>
                <option value="sbertrack" ${route.target_system === "jira" ? "" : "selected"}>SberTrack</option>
                <option value="jira" ${route.target_system === "jira" ? "selected" : ""}>Jira</option>
              </select>
              <select data-route-domain data-dirty-field style="margin-top:6px;">
                <option value="sberbank" ${route.jira_domain === "delta" ? "" : "selected"}>sberbank</option>
                <option value="delta" ${route.jira_domain === "delta" ? "selected" : ""}>delta</option>
              </select>
               <small class="muted">Spaces SberTrack</small>
               <textarea data-route-spaces data-dirty-field placeholder="TSTM&#10;OPLOT" style="margin-top:6px;">${escapeHtml(lines(route.spaces || []))}</textarea>
               <small class="muted" data-route-projects-label style="display:block;margin-top:6px;">Jira projects</small>
               <textarea data-route-projects data-dirty-field placeholder="EMRM" style="margin-top:6px;">${escapeHtml(lines(route.jira_projects || []))}</textarea>
            </td>
            <td>
               <small class="muted" data-route-sbertrack-fields>Suit / priority</small>
               <input data-route-suit data-dirty-field value="${escapeAttr(route.suit || "task")}" placeholder="task">
               <input data-route-priority data-dirty-field value="${escapeAttr(route.priority || "low")}" placeholder="low" style="margin-top:6px;">
               <small class="muted" data-route-jira-fields style="display:block;margin-top:6px;">Jira issue / priority / labels / team</small>
              <input data-route-type data-dirty-field value="${escapeAttr(route.jira_issue_type || "Task")}" placeholder="Task" style="margin-top:6px;">
              <input data-route-type-id data-dirty-field value="${escapeAttr(route.jira_issue_type_id || "3")}" placeholder="3" style="margin-top:6px;">
              <input data-route-epic-name-field data-dirty-field value="${escapeAttr(route.jira_epic_name_field || "")}" placeholder="Epic Name field, only for Epic" style="margin-top:6px;">
              <input data-route-jira-priority data-dirty-field value="${escapeAttr(route.jira_priority || "Minor")}" placeholder="Minor" style="margin-top:6px;">
              <input data-route-labels data-dirty-field value="${escapeAttr((route.jira_labels || ["FromChannel"]).join("\n"))}" placeholder="FromChannel" style="margin-top:6px;">
              <input data-route-epic-link-field data-dirty-field value="${escapeAttr(route.jira_epic_link?.field_id || "")}" placeholder="Epic Link field: customfield_10006" style="margin-top:6px;">
              <input data-route-epic-link-key data-dirty-field value="${escapeAttr(route.jira_epic_link?.key || "")}" placeholder="Epic Link key: EMRM-40162" style="margin-top:6px;">
              <input data-route-team-field data-dirty-field value="${escapeAttr(route.jira_team?.field_id || "")}" placeholder="customfield_11902" style="margin-top:6px;">
              <input data-route-team-value data-dirty-field value="${escapeAttr(route.jira_team?.value_id || "")}" placeholder="6651" style="margin-top:6px;">
              <input data-route-team-name data-dirty-field value="${escapeAttr(route.jira_team?.name || "")}" placeholder="ForREST" style="margin-top:6px;">
            </td>
            <td class="action-cell">
              <button type="button" class="icon-btn danger" data-remove-sbertrack-route="${index}">Удал.</button>
            </td>
          </tr>
        `;
        }).join("");
        $("sbertrackRouteEmpty").classList.toggle("hidden", Boolean((routes || []).length));
        refreshEmailRouteFields();
      }

      function refreshEmailRouteFields() {
        document.querySelectorAll("[data-sbertrack-route-row]").forEach((row) => {
          const isJira = row.querySelector("[data-route-target]")?.value === "jira";
          const setVisible = (selector, visible) => {
            const element = row.querySelector(selector);
            if (element) element.style.display = visible ? "" : "none";
          };
          setVisible("[data-route-domain]", isJira);
          setVisible("[data-route-projects-label]", isJira);
          setVisible("[data-route-projects]", isJira);
          setVisible("[data-route-sbertrack-fields]", !isJira);
          setVisible("[data-route-suit]", !isJira);
          setVisible("[data-route-priority]", !isJira);
          setVisible("[data-route-jira-fields]", isJira);
          ["[data-route-type]", "[data-route-jira-priority]", "[data-route-labels]", "[data-route-team-field]", "[data-route-team-value]", "[data-route-team-name]"]
            .forEach((selector) => setVisible(selector, isJira));
          setVisible("[data-route-spaces]", !isJira);
          const spacesLabel = row.querySelector("[data-route-spaces]")?.previousElementSibling;
          if (spacesLabel) spacesLabel.style.display = isJira ? "none" : "";
        });
      }

      function renderSberTrackUsers(users) {
        $("sbertrackUserRows").innerHTML = (users || []).map((user, index) => `
          <tr data-sbertrack-user-row="${index}">
            <td><input type="checkbox" data-user-enabled data-dirty-field ${user.enabled === false ? "" : "checked"}></td>
            <td><input data-user-email data-dirty-field value="${escapeAttr(user.email || "")}" placeholder="ivanov.ii@sberbank.ru"></td>
            <td><input data-user-name data-dirty-field value="${escapeAttr(user.name || "")}" placeholder="Иванов И.И."></td>
            <td><input data-user-id data-dirty-field value="${escapeAttr(user.sbertrack_user_id || "")}" placeholder="12345678"></td>
            <td class="action-cell">
              <button type="button" class="icon-btn danger" data-remove-sbertrack-user="${index}">Удал.</button>
            </td>
          </tr>
        `).join("");
        $("sbertrackUserEmpty").classList.toggle("hidden", Boolean((users || []).length));
      }


      function renderVaScheduleManager(metadata) {
        const moduleConfig = state.config?.modules?.va_schedule_manager || { enabled: false };
        const enabledInput = $("vaScheduleManagerEnabled");
        if (enabledInput) enabledInput.checked = Boolean(moduleConfig.enabled);
        const status = metadata.status || "unknown";
        const loaded = Boolean(metadata.loaded);
        const packageText = metadata.package_present ? "найден" : "отсутствует";
        const loadedText = loaded ? "загружен" : "не загружен";
        const runtime = metadata.runtime || {};
        const runtimeText = Object.keys(runtime).length
          ? Object.entries(runtime).map(([key, value]) => `${key}: ${value.exists ? "ok" : "нет"}`).join(" · ")
          : "-";
        $("vaScheduleManagerVersion").textContent = metadata.version || "-";
        $("vaScheduleManagerPackage").textContent = packageText;
        $("vaScheduleManagerLoaded").textContent = loadedText;
        $("vaScheduleManagerRuntime").textContent = runtimeText;
        const statusText = metadata.error
          ? metadata.error
          : loaded
            ? "Модуль загружен."
            : status === "disabled"
              ? "Модуль выключен в СУП-параметрах. После включения нужен перезапуск Flask."
              : status === "missing"
                ? "Папка VA/schedule_manager отсутствует. Основное приложение продолжает работать."
                : "Модуль пока не зарегистрирован.";
        $("vaScheduleManagerStatusText").textContent = statusText;
        $("vaScheduleManagerStatusBox").className = `notice compact ${loaded ? "notice-success" : metadata.error ? "notice-error" : "notice-warning"}`;
        const openBtn = $("openVaScheduleManagerBtn");
        const safeTarget = supUiConfig.schedule_manager.url || "";
        openBtn.disabled = !loaded || !safeTarget;
        openBtn.dataset.url = safeTarget;
      }

      function renderGigaChatSettings() {
        const input = $("gigachatEnabled");
        if (input) input.checked = state.config?.integrations?.gigachat?.enabled !== false;
      }

      function renderDocumentTemplateCenterSettings() {
        const input = $("dtcHistoryRetentionLimit");
        if (!input) return;
        input.value = String(state.config?.document_template_center?.history_retention_limit || 2);
      }

      let adminSessionRequest = null;

      async function ensureAdminSession() {
        if (adminSessionRequest) return adminSessionRequest;
        adminSessionRequest = (async () => {
          const statusResponse = await fetch(getSupUrl("admin_session_status"), {
            method: "GET",
            credentials: "same-origin",
            headers: { "Accept": "application/json" }
          });
          const statusPayload = await statusResponse.json().catch(() => ({}));
          if (statusResponse.ok && statusPayload.authenticated && statusPayload.csrf_token) {
            sessionStorage.setItem("sup_admin_csrf_token", statusPayload.csrf_token);
            return statusPayload;
          }

          sessionStorage.removeItem("sup_admin_csrf_token");
          const token = getToken();
          if (!token) throw new Error("Введите SUP token.");
          const response = await fetch(getSupUrl("admin_session_login"), {
            method: "POST",
            credentials: "same-origin",
            headers: { "Accept": "application/json", "Content-Type": "application/json" },
            body: JSON.stringify({ token })
          });
          const payload = await response.json().catch(() => ({}));
          if (!response.ok || payload.success === false || !payload.csrf_token) {
            throw new Error(payload.error || "Административный вход не выполнен.");
          }
          sessionStorage.setItem("sup_admin_csrf_token", payload.csrf_token);
          return payload;
        })();
        try {
          return await adminSessionRequest;
        } finally {
          adminSessionRequest = null;
        }
      }

      async function adminApi(path, options = {}) {
        const headers = new Headers(options.headers || {});
        if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
        const csrfToken = sessionStorage.getItem("sup_admin_csrf_token") || "";
        if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
        const response = await fetch(path, { ...options, headers });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || payload.success === false) {
          const error = new Error(payload.error || `HTTP ${response.status}`);
          error.payload = payload;
          error.status = response.status;
          throw error;
        }
        return payload;
      }

      const releaseRefreshModeLabels = {
        quick: "Быстрое обновление",
        full: "Полное обновление",
        reliable_full: "Надёжное полное",
        auto_incremental: "Автоматическое тихое"
      };

      const releaseRefreshStateLabels = {
        idle: "Ожидание",
        refreshing: "Выполняется",
        completed: "Завершено",
        rejected: "Не применено",
        failed: "Ошибка",
        interrupted: "Прервано",
        skipped: "Пропущено"
      };

      function shortRevision(value) {
        const revision = String(value || "").trim();
        if (!revision) return "-";
        return revision.length > 22 ? `${revision.slice(0, 19)}…` : revision;
      }

      function renderReleaseRefresh(payload = {}) {
        state.releaseRefresh.lastPayload = payload;
        const refresh = payload.refresh || {};
        const snapshot = payload.snapshot || {};
        const refreshState = String(refresh.state || "idle");
        const mode = String(refresh.mode || "");
        const busy = refreshState === "refreshing";
        const stateBadge = $("releaseRefreshStateBadge");
        stateBadge.textContent = releaseRefreshStateLabels[refreshState] || "Неизвестно";
        stateBadge.className = `badge ${
          busy ? "blue" :
          refreshState === "completed" ? "green" :
          ["failed", "rejected", "interrupted"].includes(refreshState) ? "red" :
          refreshState === "skipped" ? "yellow" : ""
        }`.trim();

        const modeBadge = $("releaseRefreshModeBadge");
        modeBadge.textContent = releaseRefreshModeLabels[mode] || mode;
        modeBadge.classList.toggle("hidden", !mode);
        const lifecycleText = busy && refresh.started_at
          ? ` Начало: ${refresh.started_at}.`
          : !busy && refresh.finished_at
            ? ` Завершено: ${refresh.finished_at}.`
            : "";
        $("releaseRefreshStateText").textContent = (refresh.message || (
          busy ? "Обновление таблицы выполняется." : "Можно запустить обновление."
        )) + lifecycleText;
        $("releaseRefreshRows").textContent = Number.isFinite(Number(snapshot.count))
          ? String(Number(snapshot.count))
          : "-";
        $("releaseRefreshRevision").textContent = shortRevision(payload.view_revision || snapshot.data_revision);
        $("releaseRefreshRevision").title = String(payload.view_revision || snapshot.data_revision || "");
        $("releaseRefreshUpdatedAt").textContent = snapshot.last_updated || payload.updated_at || "-";
        $("releaseRefreshQuickAt").textContent = snapshot.last_quick_sync || "-";
        $("releaseRefreshFullAt").textContent = snapshot.last_full_sync || "-";

        document.querySelectorAll("[data-release-refresh-mode]").forEach((button) => {
          button.disabled = busy;
        });
        if (busy) closeReleaseRefreshConfirmation();
      }

      function stopReleaseRefreshPolling() {
        const controller = state.releaseRefresh.controller;
        if (!controller) return;
        controller.stopped = true;
        if (controller.timer) window.clearTimeout(controller.timer);
        if (controller.abortController) controller.abortController.abort();
        state.releaseRefresh.controller = null;
      }

      function scheduleReleaseRefreshPoll(controller, delayMs) {
        if (
          controller.stopped ||
          controller !== state.releaseRefresh.controller ||
          document.hidden ||
          state.activeTab !== "release-refresh"
        ) return;
        if (controller.timer) window.clearTimeout(controller.timer);
        controller.timer = window.setTimeout(() => runReleaseRefreshPoll(controller), delayMs);
      }

      async function runReleaseRefreshPoll(controller) {
        if (
          controller.stopped ||
          controller !== state.releaseRefresh.controller ||
          document.hidden ||
          state.activeTab !== "release-refresh"
        ) return;
        controller.timer = null;
        controller.abortController = new AbortController();
        let nextDelay = 15000;
        try {
          const payload = await adminApi(
            getSupUrl("release_refresh_status"),
            { signal: controller.abortController.signal }
          );
          renderReleaseRefresh(payload);
          controller.errorCount = 0;
          nextDelay = payload.refresh?.state === "refreshing" ? 2000 : 15000;
        } catch (error) {
          if (error.name !== "AbortError") {
            controller.errorCount += 1;
            $("releaseRefreshStateText").textContent = error.message;
            $("releaseRefreshStateBadge").textContent = "Недоступно";
            $("releaseRefreshStateBadge").className = "badge red";
            nextDelay = Math.min(60000, 2000 * (2 ** Math.min(controller.errorCount, 5)));
          }
        } finally {
          controller.abortController = null;
          scheduleReleaseRefreshPoll(controller, nextDelay);
        }
      }

      function startReleaseRefreshPolling({ immediate = true } = {}) {
        stopReleaseRefreshPolling();
        if (document.hidden || state.activeTab !== "release-refresh") return;
        const controller = {
          stopped: false,
          timer: null,
          abortController: null,
          errorCount: 0
        };
        state.releaseRefresh.controller = controller;
        scheduleReleaseRefreshPoll(controller, immediate ? 0 : 15000);
      }

      function closeReleaseRefreshConfirmation() {
        state.releaseRefresh.confirmationMode = "";
        $("releaseRefreshConfirmation")?.classList.add("hidden");
      }

      function requestReleaseRefresh(mode) {
        if (mode === "quick") {
          startReleaseRefresh(mode);
          return;
        }
        state.releaseRefresh.confirmationMode = mode;
        $("releaseRefreshConfirmationText").textContent = mode === "reliable_full"
          ? "Запустить надёжное полное обновление? Оно может занять заметное время, текущая таблица останется доступной."
          : "Запустить полное обновление? Текущая таблица будет заменена только после успешной проверки нового snapshot.";
        $("releaseRefreshConfirmation").classList.remove("hidden");
        $("releaseRefreshConfirmBtn").focus();
      }

      async function startReleaseRefresh(mode) {
        const sourceButton = document.querySelector(`[data-release-refresh-mode="${mode}"]`);
        closeReleaseRefreshConfirmation();
        beginButtonAction(sourceButton, "Запуск...");
        try {
          await ensureAdminSession();
          const payload = await adminApi(
            getSupUrl("release_refresh_start"),
            {
              method: "POST",
              body: JSON.stringify({ mode })
            }
          );
          renderReleaseRefresh({
            ...(state.releaseRefresh.lastPayload || {}),
            refresh: payload.refresh || {}
          });
          finishButtonAction(sourceButton, { label: "Запущено", disabled: true });
          window.setTimeout(() => {
            if (state.releaseRefresh.lastPayload) {
              renderReleaseRefresh(state.releaseRefresh.lastPayload);
            }
          }, 1600);
          startReleaseRefreshPolling({ immediate: true });
        } catch (error) {
          const refreshStillRunning = error.payload?.refresh?.state === "refreshing";
          if (error.payload?.refresh) {
            renderReleaseRefresh({
              ...(state.releaseRefresh.lastPayload || {}),
              refresh: error.payload.refresh
            });
          }
          $("releaseRefreshStateText").textContent = error.message;
          finishButtonAction(sourceButton, {
            success: false,
            disabled: refreshStillRunning
          });
          if (error.payload?.refresh) {
            window.setTimeout(() => {
              if (state.releaseRefresh.lastPayload) {
                renderReleaseRefresh(state.releaseRefresh.lastPayload);
              }
            }, 1600);
          }
          startReleaseRefreshPolling({ immediate: true });
        }
      }

      function emptyDirectoryEmployee() {
        return {
          employee_id: crypto.randomUUID(),
          enabled: true,
          full_name: "",
          release_name: "",
          jira_names: { delta: "", sberbank: "" },
          aliases: [],
          emails: [],
          phone: "",
          location: "",
          personnel_number: "",
          memberships: {
            release_monitor: { enabled: false, order: null },
            release_zni: { enabled: false },
            duty_dashboard: { enabled: false, role: "none", order: null },
            release_notifications: { enabled: false },
            va_schedule_manager: { enabled: false, order: null }
          },
          source_refs: []
        };
      }

      function aliasesFromText(value) {
        return String(value || "").split(/\r?\n/).map((line) => line.trim()).filter(Boolean).map((line) => {
          const parts = line.split("|");
          return {
            type: (parts.shift() || "").trim(),
            jira_domain: (parts.shift() || "").trim(),
            value: parts.join("|").trim()
          };
        });
      }

      function deduplicateAliases(aliases) {
        const seen = new Set();
        return (aliases || []).filter((alias) => {
          const value = String(alias?.value || "").trim();
          const type = String(alias?.type || "").trim().toLowerCase();
          const jiraDomain = String(alias?.jira_domain || "").trim().toLowerCase();
          const key = `${type}|${jiraDomain}|${value.toLowerCase()}`;
          if (!value || !type || seen.has(key)) return false;
          seen.add(key);
          return true;
        }).map((alias) => ({
          type: String(alias.type || "").trim().toLowerCase(),
          jira_domain: String(alias.jira_domain || "").trim().toLowerCase(),
          value: String(alias.value || "").trim()
        }));
      }

      function directoryAliasPreview(employee, releaseName, vaEnabled, additionalAliases = []) {
        const existing = deduplicateAliases([
          ...(employee.aliases || []),
          ...additionalAliases
        ]);
        const hasStableScheduleIdentity = existing.some((alias) => alias.type === "schedule")
          || (employee.source_refs || []).some((value) => String(value || "").startsWith("va:employees:"));
        const generated = [...existing];
        const normalizedReleaseName = String(releaseName || "").trim();
        if (normalizedReleaseName) {
          generated.push(
            { type: "release", jira_domain: "", value: normalizedReleaseName },
            { type: "va", jira_domain: "", value: normalizedReleaseName }
          );
        }
        if (vaEnabled && !hasStableScheduleIdentity) {
          const previousVaName = existing.find((alias) => alias.type === "va")?.value || "";
          const scheduleName = previousVaName || normalizedReleaseName || String(employee.full_name || "").trim();
          if (scheduleName) {
            generated.push({ type: "schedule", jira_domain: "", value: scheduleName });
          }
        }
        return deduplicateAliases(generated);
      }

      function aliasKindLabel(alias) {
        return alias.type === "jira" && alias.jira_domain
          ? `jira:${alias.jira_domain}`
          : alias.type;
      }

      function renderDirectoryAliasPreview(row) {
        const index = Number(row?.dataset.directoryEmployee);
        const employee = state.directory.employees[index];
        const target = row?.querySelector("[data-directory-alias-preview]");
        if (!employee || !target) return;
        const additional = aliasesFromText(row.querySelector("[data-directory-additional-aliases]")?.value || "");
        const aliases = directoryAliasPreview(
          employee,
          row.querySelector("[data-directory-release-name]")?.value,
          Boolean(row.querySelector("[data-directory-va-enabled]")?.checked),
          additional
        );
        target.innerHTML = aliases.length
          ? aliases.map((alias) => `
            <div class="directory-alias-row">
              <span class="directory-alias-kind">${escapeHtml(aliasKindLabel(alias))}</span>
              <span class="directory-alias-value" title="${escapeAttr(alias.value)}">${escapeHtml(alias.value)}</span>
            </div>
          `).join("")
          : '<div class="directory-alias-empty">Алиасы появятся после заполнения Release name.</div>';
      }

      function optionalInteger(value) {
        const normalized = String(value ?? "").trim();
        return normalized === "" ? null : Number(normalized);
      }

      function vaOrderForDisplay(value) {
        return Number.isInteger(value) && value >= 0 ? value + 1 : null;
      }

      function vaOrderForStorage(value) {
        const displayOrder = optionalInteger(value);
        return Number.isInteger(displayOrder) && displayOrder >= 1
          ? displayOrder - 1
          : displayOrder;
      }

      function nextDirectoryOrder(enabledSelector, orderSelector, role = null, minimum = 0) {
        const orders = Array.from(document.querySelectorAll("[data-directory-employee]"))
          .filter((row) => {
            const enabled = row.querySelector(enabledSelector);
            if (!enabled?.checked) return false;
            if (role === null) return true;
            return row.querySelector("[data-directory-dashboard-role]")?.value === role;
          })
          .map((row) => optionalInteger(row.querySelector(orderSelector)?.value))
          .filter((value) => Number.isInteger(value) && value >= minimum);
        return orders.length ? Math.max(...orders) + 1 : minimum;
      }

      function fillDirectoryOrder(target, orderSelector, enabledSelector, role = null, minimum = 0) {
        const row = target.closest("[data-directory-employee]");
        const orderInput = row?.querySelector(orderSelector);
        if (!orderInput || String(orderInput.value || "").trim() !== "") return;
        orderInput.value = String(nextDirectoryOrder(
          enabledSelector,
          orderSelector,
          role,
          minimum
        ));
        orderInput.dataset.previousValue = orderInput.value;
      }

      function renderEmployeeDirectory(payload) {
        const directory = payload.directory || {};
        const selectedEmployeeId = state.directory.employees?.[state.directory.selectedIndex]?.employee_id || "";
        const nextEmployees = clone(directory.employees || []);
        const selectedIndex = Math.max(0, nextEmployees.findIndex((employee) => employee.employee_id === selectedEmployeeId));
        state.directory = {
          status: payload.status || "missing",
          revision: payload.revision == null ? null : payload.revision,
          etag: payload.etag || "missing",
          employees: nextEmployees,
          selectedIndex,
          filter: state.directory.filter || "all",
          consumerHealth: clone(payload.consumer_health || {})
        };
        const statusBadge = $("directoryStatusBadge");
        statusBadge.textContent = state.directory.status;
        statusBadge.className = `badge ${state.directory.status === "available" ? "green" : state.directory.status === "missing" || state.directory.status === "empty" ? "yellow" : "red"}`;
        $("directoryRevisionText").textContent = `revision: ${state.directory.revision ?? "-"} · etag: ${(state.directory.etag || "-").slice(0, 22)}`;
        const writable = ["missing", "empty", "available"].includes(state.directory.status);
        $("saveDirectoryBtn").disabled = !writable;
        $("addDirectoryEmployeeBtn").disabled = !writable;
        $("directoryStatusText").textContent = writable
          ? ""
          : "Справочник нельзя редактировать до явного восстановления его состояния.";
        $("directoryStatusText").className = `status ${writable ? "" : "error"}`.trim();
        renderDirectoryEmployees();
      }

      function directoryInitials(name) {
        const parts = String(name || "").trim().split(/\s+/).filter(Boolean);
        if (!parts.length) return "+";
        return parts.slice(0, 2).map((part) => part[0]).join("").toUpperCase();
      }

      function directoryEmployeeMatches(employee) {
        const query = String($("directoryEmployeeSearch")?.value || "").trim().toLowerCase();
        const haystack = [
          employee.full_name,
          employee.release_name,
          employee.jira_names?.delta,
          employee.jira_names?.sberbank,
          ...(employee.emails || [])
        ].join(" ").toLowerCase();
        if (query && !haystack.includes(query)) return false;
        if (state.directory.filter === "active") return employee.enabled !== false;
        if (state.directory.filter === "archived") return employee.enabled === false;
        return true;
      }

      function directoryServiceDots(employee) {
        const memberships = employee.memberships || {};
        const services = [
          [memberships.release_monitor?.enabled, "Блок релизов"],
          [memberships.release_zni?.enabled, "Релизные ЗНИ"],
          [memberships.duty_dashboard?.enabled, "Рабочий стол"],
          [memberships.release_notifications?.enabled, "Релизные письма"],
          [memberships.va_schedule_manager?.enabled, "VA Schedule Manager"]
        ];
        return services.map(([enabled, title]) => `<span class="directory-service-dot ${enabled ? "active" : ""}" title="${escapeAttr(title)}"></span>`).join("");
      }

      function renderDirectoryNavigator() {
        const employees = state.directory.employees || [];
        const visible = employees.map((employee, index) => ({ employee, index }))
          .filter(({ employee }) => directoryEmployeeMatches(employee));
        $("directoryVisibleCount").textContent = `${visible.length} из ${employees.length}`;
        $("directoryEmployeeCount").textContent = `${employees.length} сотрудников`;
        $("directoryEmployeeNavigator").innerHTML = visible.map(({ employee, index }) => {
          const detail = employee.release_name || employee.emails?.[0] || "Без дополнительных данных";
          return `<button type="button" class="directory-person ${index === state.directory.selectedIndex ? "active" : ""} ${employee.enabled === false ? "archived" : ""}" data-select-directory-employee="${index}">
            <span class="directory-person-avatar">${escapeHtml(directoryInitials(employee.full_name))}</span>
            <span class="directory-person-copy">
              <span class="directory-person-name">${escapeHtml(employee.full_name || "Новый сотрудник")}</span>
              <span class="directory-person-detail">${escapeHtml(detail)}</span>
            </span>
            <span class="directory-person-services" title="Участие в сервисах">${directoryServiceDots(employee)}</span>
          </button>`;
        }).join("");
        if (!visible.length) {
          $("directoryEmployeeNavigator").innerHTML = `<div class="empty-state">Ничего не найдено.</div>`;
        }
      }

      function applyDirectoryNavigationFilter() {
        const employees = state.directory.employees || [];
        const visibleIndices = employees.map((employee, index) => directoryEmployeeMatches(employee) ? index : -1)
          .filter((index) => index >= 0);
        if (visibleIndices.length && !visibleIndices.includes(state.directory.selectedIndex)) {
          state.directory.selectedIndex = visibleIndices[0];
          renderDirectoryEmployees();
          return;
        }
        renderDirectoryNavigator();
      }

      function vaCompetencyByCode(code) {
        return (state.va.competencies.items || []).find((item) => item.code === code);
      }

      function renderVaSettingsSection(employee, index) {
        const memberships = employee.memberships || {};
        const vaMembership = memberships.va_schedule_manager || {};
        const serverState = state.va.settings.employees?.[employee.employee_id];
        const settings = clone(state.vaDrafts[employee.employee_id] || serverState?.settings || state.va.settings.defaults || {
          status: "active",
          role: "employee",
          competencies: ["support"],
          overtime_ready: true
        });
        const editable = Boolean(
          state.va.directory.status === "available"
          && state.va.settings.ready
          && employee.enabled
          && vaMembership.enabled
        );
        const disabled = editable ? "" : "disabled";
        const stateLabel = state.vaDrafts[employee.employee_id]
          ? "изменено"
          : serverState?.explicit
            ? "явные настройки"
            : "по умолчанию";
        let reason = "";
        if (state.va.directory.status !== "available") reason = "Employee Directory недоступен.";
        else if (!state.va.settings.ready) reason = "Хранилище настроек VA не готово.";
        else if (!employee.enabled) reason = "Архивный сотрудник доступен только для просмотра.";
        else if (!vaMembership.enabled) reason = "Включите VA Schedule Manager, чтобы настроить участие в графике.";
        else if (state.vaDrafts[employee.employee_id]) reason = "Настройки будут сохранены общей кнопкой «Сохранить».";
        const options = (state.va.competencies.items || []).map((competency) => {
          const checked = (settings.competencies || []).includes(competency.code);
          return `<label class="va-multi-select-option ${checked ? "selected" : ""}" data-va-competency-option data-code="${escapeAttr(competency.code)}">
            <input type="checkbox" value="${escapeAttr(competency.code)}" ${checked ? "checked" : ""} ${disabled}>
            <span><strong>${escapeHtml(competency.name)}</strong><small class="muted">${escapeHtml(competency.code)}</small></span>
          </label>`;
        }).join("");
        return `
          <section class="directory-section va-settings-section" data-va-settings-section="${index}" data-va-employee-id="${escapeAttr(employee.employee_id || "")}">
            <div class="directory-section-head">
              <div class="va-settings-head-copy">
                <h3>Настройки графика дежурств</h3>
                <span class="badge ${serverState?.explicit ? "blue" : ""}">${escapeHtml(stateLabel)}</span>
              </div>
              <button type="button" class="ghost" data-open-competencies>Справочник компетенций</button>
            </div>
            <div class="va-settings-grid">
              <div>
                <label>Статус в графике</label>
                <select data-va-status ${disabled}>
                  <option value="active" ${settings.status === "active" ? "selected" : ""}>Активен</option>
                  <option value="long_leave" ${settings.status === "long_leave" ? "selected" : ""}>Длительный отпуск</option>
                  <option value="dismissed" ${settings.status === "dismissed" ? "selected" : ""}>Уволен</option>
                </select>
              </div>
              <div>
                <label>Роль в графике</label>
                <select data-va-role ${disabled}>
                  <option value="employee" ${settings.role === "employee" ? "selected" : ""}>Сотрудник</option>
                  <option value="manager" ${settings.role === "manager" ? "selected" : ""}>Руководитель</option>
                </select>
              </div>
              <div class="wide">
                <label>Компетенции</label>
                <div class="va-multi-select" data-va-multi-select>
                  <button type="button" class="va-multi-select-toggle" data-va-multi-select-toggle aria-expanded="false" ${disabled}>
                    <span class="va-multi-select-value" data-va-multi-select-value></span>
                  </button>
                  <div class="va-multi-select-menu">
                    <div class="va-multi-select-tools">
                      <input type="search" placeholder="Найти компетенцию" data-va-competency-search>
                      <div class="va-multi-select-filters">
                        <button type="button" class="active" data-va-competency-filter="all">Все</button>
                        <button type="button" data-va-competency-filter="selected">Выбранные</button>
                      </div>
                    </div>
                    <div class="va-multi-select-options">
                      ${options}
                      <div class="va-multi-select-empty hidden" data-va-multi-select-empty>Ничего не найдено</div>
                    </div>
                  </div>
                </div>
              </div>
              <label class="va-overtime-toggle wide">
                <span><strong>Готов к сверхурочной работе</strong><small class="muted">Разрешает назначение смены ВХ в выходные и праздники.</small></span>
                <input type="checkbox" data-va-overtime ${settings.overtime_ready ? "checked" : ""} ${disabled}>
              </label>
            </div>
            <div class="va-settings-actions">
              <div class="inline-status ${reason ? "warning" : ""}" data-va-settings-status>${escapeHtml(reason)}</div>
            </div>
          </section>
        `;
      }

      function refreshVaMultiSelects() {
        document.querySelectorAll("[data-va-multi-select]").forEach((container) => {
          const menu = getVaMultiSelectMenu(container);
          const selected = Array.from(menu?.querySelectorAll("[data-va-competency-option] input:checked") || [])
            .map((input) => ({
              code: input.value,
              name: vaCompetencyByCode(input.value)?.name || input.value
            }));
          const value = container.querySelector("[data-va-multi-select-value]");
          if (!value) return;
          if (!selected.length) {
            value.innerHTML = `<span class="muted">Не выбраны</span>`;
            return;
          }
          const visible = selected.slice(0, 2);
          value.innerHTML = visible.map((item) => `<span class="va-competency-chip" title="${escapeAttr(item.name)}">${escapeHtml(item.name)}</span>`).join("")
            + (selected.length > visible.length ? `<span class="va-competency-chip">+${selected.length - visible.length}</span>` : "");
        });
      }

      function captureVaSettingsDraft(control) {
        const owner = getVaMultiSelectOwner(control);
        const section = control.closest("[data-va-settings-section]")
          || owner?.closest("[data-va-settings-section]");
        if (!section) return;
        const employeeId = section.dataset.vaEmployeeId || "";
        if (!employeeId) return;
        const menu = owner ? getVaMultiSelectMenu(owner) : section;
        state.vaDrafts[employeeId] = {
          status: section.querySelector("[data-va-status]")?.value || "active",
          role: section.querySelector("[data-va-role]")?.value || "employee",
          competencies: Array.from(menu?.querySelectorAll("[data-va-competency-option] input:checked") || []).map((input) => input.value),
          overtime_ready: Boolean(section.querySelector("[data-va-overtime]")?.checked)
        };
        const status = section.querySelector("[data-va-settings-status]");
        if (status) {
          status.textContent = "Настройки будут сохранены общей кнопкой «Сохранить».";
          status.className = "inline-status warning";
        }
      }

      function filterVaCompetencies(container) {
        const menu = getVaMultiSelectMenu(container);
        if (!menu) return;
        const query = (menu.querySelector("[data-va-competency-search]")?.value || "").trim().toLowerCase();
        const selectedOnly = menu.querySelector("[data-va-competency-filter].active")?.dataset.vaCompetencyFilter === "selected";
        let visibleCount = 0;
        menu.querySelectorAll("[data-va-competency-option]").forEach((row) => {
          const input = row.querySelector("input");
          const matches = (!query || row.textContent.toLowerCase().includes(query))
            && (!selectedOnly || input?.checked);
          row.classList.toggle("hidden", !matches);
          row.classList.toggle("selected", Boolean(input?.checked));
          if (matches) visibleCount += 1;
        });
        menu.querySelector("[data-va-multi-select-empty]")?.classList.toggle("hidden", visibleCount > 0);
      }

      const vaMultiSelectOverlay = {
        owner: null,
        menu: null,
        placeholder: null,
        frame: 0
      };

      function getVaMultiSelectOwner(element) {
        return element?.closest?.("[data-va-multi-select]")
          || (vaMultiSelectOverlay.menu?.contains(element) ? vaMultiSelectOverlay.owner : null);
      }

      function getVaMultiSelectMenu(container) {
        if (vaMultiSelectOverlay.owner === container) return vaMultiSelectOverlay.menu;
        return container?.querySelector?.(".va-multi-select-menu") || null;
      }

      function positionVaMultiSelect() {
        const { owner, menu } = vaMultiSelectOverlay;
        const toggle = owner?.querySelector("[data-va-multi-select-toggle]");
        if (!owner || !menu || !toggle || !document.body.contains(toggle)) {
          closeVaMultiSelect();
          return;
        }
        const rect = toggle.getBoundingClientRect();
        const viewportWidth = document.documentElement.clientWidth;
        const viewportHeight = document.documentElement.clientHeight;
        const margin = 12;
        const gap = 6;
        const spaceBelow = viewportHeight - rect.bottom - margin;
        const spaceAbove = rect.top - margin;
        const openAbove = spaceBelow < 260 && spaceAbove > spaceBelow;
        const availableHeight = Math.max(150, Math.min(380, (openAbove ? spaceAbove : spaceBelow) - gap));
        const width = Math.min(420, Math.max(rect.width, 300), viewportWidth - margin * 2);
        const left = Math.max(margin, Math.min(rect.left, viewportWidth - width - margin));
        menu.style.width = `${width}px`;
        menu.style.maxHeight = `${availableHeight}px`;
        menu.style.left = `${left}px`;
        menu.style.top = openAbove
          ? `${Math.max(margin, rect.top - gap - Math.min(menu.scrollHeight, availableHeight))}px`
          : `${Math.min(viewportHeight - margin - availableHeight, rect.bottom + gap)}px`;
        const toolsHeight = menu.querySelector(".va-multi-select-tools")?.offsetHeight || 0;
        const options = menu.querySelector(".va-multi-select-options");
        if (options) options.style.maxHeight = `${Math.max(90, availableHeight - toolsHeight - 2)}px`;
      }

      function scheduleVaMultiSelectPosition() {
        if (!vaMultiSelectOverlay.owner || vaMultiSelectOverlay.frame) return;
        vaMultiSelectOverlay.frame = requestAnimationFrame(() => {
          vaMultiSelectOverlay.frame = 0;
          positionVaMultiSelect();
        });
      }

      function openVaMultiSelect(container) {
        if (!container || vaMultiSelectOverlay.owner === container) {
          closeVaMultiSelect();
          return;
        }
        closeVaMultiSelect();
        const menu = container.querySelector(".va-multi-select-menu");
        const toggle = container.querySelector("[data-va-multi-select-toggle]");
        if (!menu || !toggle || toggle.disabled) return;
        const placeholder = document.createComment("va-multi-select-menu");
        menu.parentNode.insertBefore(placeholder, menu);
        $("vaMultiSelectLayer").appendChild(menu);
        vaMultiSelectOverlay.owner = container;
        vaMultiSelectOverlay.menu = menu;
        vaMultiSelectOverlay.placeholder = placeholder;
        container.classList.add("open");
        menu.classList.add("floating");
        toggle.setAttribute("aria-expanded", "true");
        filterVaCompetencies(container);
        positionVaMultiSelect();
        setTimeout(() => menu.querySelector("[data-va-competency-search]")?.focus(), 0);
      }

      function closeVaMultiSelect({ restoreFocus = false } = {}) {
        const { owner, menu, placeholder, frame } = vaMultiSelectOverlay;
        if (frame) cancelAnimationFrame(frame);
        const toggle = owner?.querySelector("[data-va-multi-select-toggle]");
        owner?.classList.remove("open");
        toggle?.setAttribute("aria-expanded", "false");
        if (menu) {
          menu.classList.remove("floating");
          menu.removeAttribute("style");
          menu.querySelector(".va-multi-select-options")?.removeAttribute("style");
          if (placeholder?.parentNode) {
            placeholder.parentNode.insertBefore(menu, placeholder);
            placeholder.remove();
          } else {
            menu.remove();
          }
        }
        vaMultiSelectOverlay.owner = null;
        vaMultiSelectOverlay.menu = null;
        vaMultiSelectOverlay.placeholder = null;
        vaMultiSelectOverlay.frame = 0;
        if (restoreFocus) toggle?.focus();
      }

      function showDirectoryView(view) {
        closeVaMultiSelect();
        state.directoryView = view === "competencies" ? "competencies" : "employees";
        document.querySelectorAll("[data-directory-view]").forEach((button) => {
          button.classList.toggle("active", button.dataset.directoryView === state.directoryView);
        });
        document.querySelectorAll("[data-directory-subview]").forEach((panel) => {
          panel.classList.toggle("active", panel.dataset.directorySubview === state.directoryView);
        });
        if (state.activeTab === "employees") {
          const params = new URLSearchParams(window.location.search);
          params.set("tab", "employees");
          params.set("view", state.directoryView);
          window.history.replaceState({}, document.title, `${window.location.pathname}?${params.toString()}`);
        }
      }

      function renderCompetencies() {
        const catalog = state.va.competencies || { status: "missing", etag: "missing", items: [] };
        const items = catalog.items || [];
        const badge = $("competencyStatusBadge");
        badge.textContent = catalog.status;
        badge.className = `badge ${catalog.status === "available" || catalog.status === "missing" || catalog.status === "empty" ? "green" : "red"}`;
        $("competencyCountBadge").textContent = `${items.length} компетенций`;
        $("competencyEtagText").textContent = `etag: ${catalog.etag || "missing"}`;
        $("competencyStatusText").textContent = catalog.status === "invalid"
          ? "Справочник компетенций повреждён. Изменения заблокированы."
          : "";
        $("competencyStatusText").className = `status ${catalog.status === "invalid" ? "error" : ""}`.trim();
        $("addCompetencyBtn").disabled = catalog.status === "invalid";
        $("competencyRows").innerHTML = items.map((item) => `
          <tr>
            <td><span class="competency-system-code">${escapeHtml(item.code)}</span></td>
            <td><strong>${escapeHtml(item.name)}</strong>${item.is_system ? '<span class="badge blue">системная</span>' : ""}</td>
            <td>${escapeHtml(item.description || "—")}</td>
            <td>${Number(item.usage_count || 0)}</td>
            <td class="action-cell">
              <div class="competency-actions">
                <button type="button" class="ghost" data-edit-competency="${escapeAttr(item.code)}" ${item.is_system ? "disabled" : ""}>Изменить</button>
                <button type="button" class="danger" data-delete-competency="${escapeAttr(item.code)}" ${(item.is_system || Number(item.usage_count || 0) > 0) ? "disabled" : ""}>Удалить</button>
              </div>
            </td>
          </tr>
        `).join("");
        $("competencyEmpty").classList.toggle("hidden", items.length > 0);
      }

      function renderDirectoryEmployees() {
        closeVaMultiSelect();
        const employees = state.directory.employees || [];
        if (state.directory.selectedIndex >= employees.length) {
          state.directory.selectedIndex = Math.max(0, employees.length - 1);
        }
        $("directoryEmployeeList").innerHTML = employees.map((employee, index) => {
          const memberships = employee.memberships || {};
          const release = memberships.release_monitor || { enabled: false, order: null };
          const dashboard = memberships.duty_dashboard || { enabled: false, role: "none", order: null };
          const location = String(employee.location || "").trim().toLowerCase();
          const locationInvalid = Boolean(location && !["moscow", "khabarovsk"].includes(location));
          const vaEnabled = Boolean(memberships.va_schedule_manager?.enabled);
          const vaDisplayOrder = vaOrderForDisplay(memberships.va_schedule_manager?.order);
          const locationRequired = employee.enabled !== false && vaEnabled && !location;
          const locationError = locationInvalid
            ? "Выберите поддерживаемую локацию: Москва или Хабаровск."
            : locationRequired
              ? "Для участия в графике дежурств необходимо выбрать локацию."
              : "";
          const aliases = directoryAliasPreview(
            employee,
            employee.release_name,
            vaEnabled
          );
          return `
            <div class="directory-entry ${index === state.directory.selectedIndex ? "active" : ""}" data-directory-employee="${index}">
              <div class="directory-entry-head">
                <div class="directory-entry-title">
                  <h3>${escapeHtml(employee.full_name || "Новый сотрудник")}</h3>
                  <div class="muted directory-meta directory-entry-id">ID ${escapeHtml(String(employee.employee_id || "").slice(0, 12))}</div>
                </div>
                <div class="directory-actions">
                  <label class="directory-active-control" title="Выключение переводит сотрудника в общий архив всех подключённых сервисов"><input type="checkbox" data-directory-enabled ${employee.enabled === false ? "" : "checked"}> <span data-directory-enabled-label>${employee.enabled === false ? "Уволен / архив" : "Активен"}</span></label>
                  <button type="button" class="danger" data-remove-directory-employee="${index}">Удалить</button>
                </div>
              </div>
              <section class="directory-section">
                <div class="directory-section-head"><h3>Основные данные</h3></div>
                <div class="directory-fields">
                <div><label>Полное ФИО</label><input data-directory-full-name value="${escapeAttr(employee.full_name || "")}"></div>
                <div><label>Release name</label><input data-directory-release-name value="${escapeAttr(employee.release_name || "")}"></div>
                </div>
              </section>
              <section class="directory-section">
                <div class="directory-section-head"><h3>Jira и контакты</h3></div>
                <div class="directory-fields">
                  <div><label>Jira Delta</label><input data-directory-jira-delta value="${escapeAttr(employee.jira_names?.delta || "")}"></div>
                  <div><label>Jira Sberbank</label><input data-directory-jira-sberbank value="${escapeAttr(employee.jira_names?.sberbank || "")}"></div>
                  <div><label>Email, по одному в строке</label><textarea data-directory-emails>${escapeHtml(lines(employee.emails || []))}</textarea></div>
                  <div><label>Телефон</label><input data-directory-phone value="${escapeAttr(employee.phone || "")}"></div>
                  <div data-directory-location-field><label>Локация</label><select data-directory-location class="${locationError ? "invalid" : ""}" aria-invalid="${locationError ? "true" : "false"}">
                    <option value="" ${location ? "" : "selected"}>Не выбрана</option>
                    <option value="moscow" ${location === "moscow" ? "selected" : ""}>Москва</option>
                    <option value="khabarovsk" ${location === "khabarovsk" ? "selected" : ""}>Хабаровск</option>
                    ${locationInvalid ? `<option value="${escapeAttr(location)}" selected>Некорректное значение: ${escapeHtml(location)}</option>` : ""}
                  </select><span class="field-error" data-directory-location-error>${escapeHtml(locationError)}</span></div>
                  <div><label>Табельный номер</label><input data-directory-personnel value="${escapeAttr(employee.personnel_number || "")}"></div>
                </div>
              </section>
              <section class="directory-section">
                <div class="directory-section-head"><h3>Участие в сервисах</h3></div>
                <div class="directory-memberships">
                <div class="directory-membership">
                  <label class="directory-membership-title"><input type="checkbox" data-directory-release-enabled ${release.enabled ? "checked" : ""}> Блок релизов</label>
                  <div class="directory-membership-field"><label>Порядок</label><input type="number" min="0" data-directory-release-order value="${release.order == null ? "" : Number(release.order)}" placeholder="Автоматически"></div>
                </div>
                <div class="directory-membership">
                  <label class="directory-membership-title"><input type="checkbox" data-directory-zni-enabled ${memberships.release_zni?.enabled ? "checked" : ""}> Релизные ЗНИ</label>
                </div>
                <div class="directory-membership">
                  <label class="directory-membership-title"><input type="checkbox" data-directory-dashboard-enabled ${dashboard.enabled ? "checked" : ""}> Рабочий стол</label>
                  <div class="directory-membership-field"><label>Роль</label><select data-directory-dashboard-role>
                    <option value="none" ${dashboard.role === "none" ? "selected" : ""}>Не участвует</option>
                    <option value="primary" ${dashboard.role === "primary" ? "selected" : ""}>Основной</option>
                    <option value="extra" ${dashboard.role === "extra" ? "selected" : ""}>Дополнительный</option>
                  </select></div>
                  <div class="directory-membership-field"><label>Порядок</label><input type="number" min="0" data-directory-dashboard-order value="${dashboard.order == null ? "" : Number(dashboard.order)}" placeholder="Автоматически"></div>
                </div>
                <div class="directory-membership">
                  <label class="directory-membership-title"><input type="checkbox" data-directory-notifications-enabled ${memberships.release_notifications?.enabled ? "checked" : ""}> Релизные письма</label>
                </div>
                <div class="directory-membership">
                  <label class="directory-membership-title"><input type="checkbox" data-directory-va-enabled ${memberships.va_schedule_manager?.enabled ? "checked" : ""}> VA Schedule Manager</label>
                  <div class="directory-membership-field"><label>Позиция в графике</label><input type="number" min="1" step="1" data-directory-va-order data-previous-value="${vaDisplayOrder ?? ""}" value="${vaDisplayOrder ?? ""}" placeholder="Автоматически"><span class="field-error" data-directory-va-order-error></span></div>
                  <div class="directory-membership-requirement ${locationRequired ? "" : "hidden"}" data-directory-va-location-requirement>
                    <span>Для графика нужна локация.</span>
                    <button type="button" class="link" data-focus-directory-location>Выбрать</button>
                  </div>
                </div>
                </div>
              </section>
              ${renderVaSettingsSection(employee, index)}
              <details class="directory-advanced">
                <summary>Алиасы и исторические имена</summary>
                <div class="directory-advanced-body">
                  <label>Автоматические и сохранённые алиасы</label>
                  <div class="directory-alias-list" data-directory-alias-preview>
                    ${aliases.length ? aliases.map((alias) => `
                      <div class="directory-alias-row">
                        <span class="directory-alias-kind">${escapeHtml(aliasKindLabel(alias))}</span>
                        <span class="directory-alias-value" title="${escapeAttr(alias.value)}">${escapeHtml(alias.value)}</span>
                      </div>
                    `).join("") : '<div class="directory-alias-empty">Алиасы появятся после заполнения Release name.</div>'}
                  </div>
                  <label>Добавить дополнительные алиасы</label>
                  <textarea data-directory-additional-aliases placeholder="full||Историческое ФИО&#10;jira|delta|Историческое Jira-имя"></textarea>
                  <span class="directory-alias-help">Формат: type|jira_domain|value. Обязательные release, va и schedule формируются сервером и не удаляются обычным сохранением.</span>
                </div>
              </details>
              </div>
            `;
        }).join("");
        $("directoryEmployeeEmpty").classList.toggle("hidden", employees.length > 0);
        renderDirectoryNavigator();
        refreshVaMultiSelects();
      }

      function collectDirectoryEmployees() {
        return Array.from(document.querySelectorAll("[data-directory-employee]")).map((row, index) => {
          const current = state.directory.employees[index] || emptyDirectoryEmployee();
          const releaseEnabled = row.querySelector("[data-directory-release-enabled]").checked;
          const dashboardEnabled = row.querySelector("[data-directory-dashboard-enabled]").checked;
          return {
            employee_id: current.employee_id,
            enabled: row.querySelector("[data-directory-enabled]").checked,
            full_name: row.querySelector("[data-directory-full-name]").value.trim(),
            release_name: row.querySelector("[data-directory-release-name]").value.trim(),
            jira_names: {
              delta: row.querySelector("[data-directory-jira-delta]").value.trim(),
              sberbank: row.querySelector("[data-directory-jira-sberbank]").value.trim()
            },
            aliases: deduplicateAliases([
              ...(current.aliases || []),
              ...aliasesFromText(row.querySelector("[data-directory-additional-aliases]")?.value || "")
            ]),
            emails: splitLines(row.querySelector("[data-directory-emails]").value).map((value) => value.toLowerCase()),
            phone: row.querySelector("[data-directory-phone]").value.trim(),
            location: row.querySelector("[data-directory-location]").value.trim(),
            personnel_number: row.querySelector("[data-directory-personnel]").value.trim(),
            memberships: {
              release_monitor: {
                enabled: releaseEnabled,
                order: releaseEnabled ? optionalInteger(row.querySelector("[data-directory-release-order]").value) : null
              },
              release_zni: { enabled: row.querySelector("[data-directory-zni-enabled]").checked },
              duty_dashboard: {
                enabled: dashboardEnabled,
                role: dashboardEnabled ? row.querySelector("[data-directory-dashboard-role]").value : "none",
                order: dashboardEnabled ? optionalInteger(row.querySelector("[data-directory-dashboard-order]").value) : null
              },
              release_notifications: { enabled: row.querySelector("[data-directory-notifications-enabled]").checked },
              va_schedule_manager: {
                enabled: row.querySelector("[data-directory-va-enabled]").checked,
                order: row.querySelector("[data-directory-va-enabled]").checked
                  ? vaOrderForStorage(row.querySelector("[data-directory-va-order]").value)
                  : null
              }
            },
            source_refs: Array.isArray(current.source_refs) ? current.source_refs : []
          };
        });
      }

      function syncDirectoryStateFromForm() {
        const rows = document.querySelectorAll("[data-directory-employee]");
        if (!rows.length) return;
        state.directory.employees = collectDirectoryEmployees();
        renderDirectoryNavigator();
      }

      function updateDirectoryLocationValidation(row, { focus = false } = {}) {
        const locationSelect = row?.querySelector("[data-directory-location]");
        if (!locationSelect) return true;
        const value = String(locationSelect.value || "").trim().toLowerCase();
        const employeeEnabled = Boolean(row.querySelector("[data-directory-enabled]")?.checked);
        const vaEnabled = Boolean(row.querySelector("[data-directory-va-enabled]")?.checked);
        const unsupported = Boolean(value && !["moscow", "khabarovsk"].includes(value));
        const required = employeeEnabled && vaEnabled && !value;
        const message = unsupported
          ? "Выберите поддерживаемую локацию: Москва или Хабаровск."
          : required
            ? "Для участия в графике дежурств необходимо выбрать локацию."
            : "";
        locationSelect.classList.toggle("invalid", Boolean(message));
        locationSelect.setAttribute("aria-invalid", message ? "true" : "false");
        const error = row.querySelector("[data-directory-location-error]");
        if (error) error.textContent = message;
        row.querySelector("[data-directory-va-location-requirement]")
          ?.classList.toggle("hidden", !required);
        if (focus && message) {
          locationSelect.scrollIntoView({ block: "center", behavior: "smooth" });
          window.setTimeout(() => locationSelect.focus(), 180);
        }
        return !message;
      }

      function validateDirectoryLocationsBeforeSave(employees) {
        const invalidIndex = employees.findIndex((employee) => {
          const location = String(employee.location || "").trim().toLowerCase();
          const vaEnabled = Boolean(employee.memberships?.va_schedule_manager?.enabled);
          const required = employee.enabled !== false && vaEnabled && !location;
          const unsupported = Boolean(location && !["moscow", "khabarovsk"].includes(location));
          return required || unsupported;
        });
        if (invalidIndex < 0) return true;

        state.directory.employees = employees;
        state.directory.selectedIndex = invalidIndex;
        state.directory.filter = "all";
        renderDirectoryEmployees();
        const row = document.querySelector(`[data-directory-employee="${invalidIndex}"]`);
        updateDirectoryLocationValidation(row, { focus: true });
        setStatus(
          "Не удалось сохранить сотрудника: для участия в графике дежурств выберите локацию «Москва» или «Хабаровск».",
          "error"
        );
        return false;
      }

      function setVaOrderError(input, message = "") {
        if (!input) return;
        input.classList.toggle("invalid", Boolean(message));
        input.setAttribute("aria-invalid", message ? "true" : "false");
        const error = input.closest(".directory-membership-field")
          ?.querySelector("[data-directory-va-order-error]");
        if (error) error.textContent = message;
      }

      function vaEmployeeNameFromRow(row) {
        return String(
          row?.querySelector("[data-directory-full-name]")?.value
          || row?.querySelector("[data-directory-release-name]")?.value
          || "сотрудник без имени"
        ).trim();
      }

      function activeVaOrderRows() {
        return Array.from(document.querySelectorAll("[data-directory-employee]"))
          .filter((row) => row.querySelector("[data-directory-enabled]")?.checked)
          .filter((row) => row.querySelector("[data-directory-va-enabled]")?.checked);
      }

      function resolveVaOrderChange(input) {
        const row = input?.closest("[data-directory-employee]");
        if (!row) return true;
        const desired = optionalInteger(input.value);
        if (!Number.isInteger(desired) || desired < 1) {
          setVaOrderError(input, "Укажите целую позицию, начиная с 1.");
          return false;
        }
        setVaOrderError(input);

        const rows = activeVaOrderRows();
        const occupants = rows.filter((candidate) => (
          candidate !== row
          && optionalInteger(candidate.querySelector("[data-directory-va-order]")?.value) === desired
        ));
        if (!occupants.length) {
          input.dataset.previousValue = String(desired);
          return true;
        }

        const occupiedBy = occupants.map(vaEmployeeNameFromRow).join(", ");
        const confirmed = window.confirm(
          `Позиция ${desired} уже занята: ${occupiedBy}.\n\n`
          + `Поставить «${vaEmployeeNameFromRow(row)}» на позицию ${desired}? `
          + "Сотрудник на этой позиции и все следующие участники графика "
          + "будут сдвинуты на одну позицию вниз."
        );
        if (!confirmed) {
          input.value = input.dataset.previousValue || "";
          setVaOrderError(input);
          return true;
        }

        const orderedRows = rows
          .filter((candidate) => candidate !== row)
          .map((candidate, originalIndex) => ({
            row: candidate,
            order: optionalInteger(candidate.querySelector("[data-directory-va-order]")?.value),
            originalIndex
          }))
          .sort((left, right) => {
            const leftOrder = Number.isInteger(left.order) ? left.order : Number.MAX_SAFE_INTEGER;
            const rightOrder = Number.isInteger(right.order) ? right.order : Number.MAX_SAFE_INTEGER;
            return leftOrder - rightOrder || left.originalIndex - right.originalIndex;
          })
          .map((item) => item.row);
        orderedRows.splice(Math.min(desired - 1, orderedRows.length), 0, row);
        orderedRows.forEach((candidate, index) => {
          const candidateInput = candidate.querySelector("[data-directory-va-order]");
          if (!candidateInput) return;
          candidateInput.value = String(index + 1);
          candidateInput.dataset.previousValue = candidateInput.value;
          setVaOrderError(candidateInput);
        });
        return true;
      }

      function validateVaOrdersBeforeSave() {
        for (const row of activeVaOrderRows()) {
          const input = row.querySelector("[data-directory-va-order]");
          if (!resolveVaOrderChange(input)) {
            state.directory.selectedIndex = Number(row.dataset.directoryEmployee);
            input.scrollIntoView({ block: "center", behavior: "smooth" });
            window.setTimeout(() => input.focus(), 180);
            setStatus("Проверьте позицию сотрудника в графике дежурств.", "error");
            return false;
          }
        }
        return true;
      }

      function formatDirectoryValidationError(item) {
        if (typeof item === "string") return item;
        const path = String(item?.path || "");
        const code = String(item?.code || "");
        const match = path.match(/^employees\[(\d+)]\.location$/);
        if (match && code === "required_for_va_schedule_manager") {
          const employee = state.directory.employees[Number(match[1])] || {};
          return `${employee.full_name || "Сотрудник"}: выберите локацию для участия в графике дежурств.`;
        }
        if (match && code === "unsupported") {
          const employee = state.directory.employees[Number(match[1])] || {};
          return `${employee.full_name || "Сотрудник"}: выбрана неподдерживаемая локация.`;
        }
        const vaOrderMatch = path.match(/^employees\[(\d+)]\.memberships\.va_schedule_manager\.order$/);
        if (vaOrderMatch && code === "duplicate_active") {
          const employee = state.directory.employees[Number(vaOrderMatch[1])] || {};
          const storedOrder = employee.memberships?.va_schedule_manager?.order;
          const occupiedBy = state.directory.employees.find((candidate, index) => (
            index !== Number(vaOrderMatch[1])
            && candidate.enabled !== false
            && candidate.memberships?.va_schedule_manager?.enabled
            && candidate.memberships.va_schedule_manager.order === storedOrder
          ));
          return `Позиция ${vaOrderForDisplay(storedOrder) ?? ""} уже занята`
            + `${occupiedBy?.full_name ? ` сотрудником «${occupiedBy.full_name}»` : ""}.`;
        }
        return `${path || "Справочник"}: ${code || "ошибка проверки"}`;
      }

      async function loadEmployeeDirectory() {
        try {
          await ensureAdminSession();
          const payload = await adminApi(getSupUrl("employee_directory"));
          renderEmployeeDirectory(payload);
          await loadVaScheduleManagerData();
          if (state.activeTab === "release-refresh") {
            startReleaseRefreshPolling({ immediate: true });
          }
        } catch (error) {
          $("directoryStatusText").textContent = error.message;
          $("directoryStatusText").className = "status error";
          $("saveDirectoryBtn").disabled = true;
          $("addDirectoryEmployeeBtn").disabled = true;
        }
      }

      async function saveEmployeeDirectory() {
        const button = $("saveDirectoryBtn");
        setStatus("");
        if (!validateVaOrdersBeforeSave()) {
          finishButtonAction(button, {
            success: false,
            label: "Проверьте порядок",
            disabled: false
          });
          return;
        }
        const employees = collectDirectoryEmployees();
        if (!validateDirectoryLocationsBeforeSave(employees)) {
          finishButtonAction(button, {
            success: false,
            label: "Проверьте данные",
            disabled: false
          });
          return;
        }
        beginButtonAction(button);
        let directorySaved = false;
        try {
          const payload = await adminApi(getSupUrl("employee_directory_save"), {
            method: "POST",
            body: JSON.stringify({
              expected_revision: state.directory.revision,
              expected_etag: state.directory.etag,
              employees
            })
          });
          directorySaved = true;
          renderEmployeeDirectory(payload);
          const vaLoaded = await loadVaScheduleManagerData();
          if (!vaLoaded && Object.keys(state.vaDrafts).length) {
            const error = new Error("Не удалось загрузить актуальное состояние настроек графика.");
            error.vaSettingsSave = true;
            throw error;
          }
          await savePendingVaEmployeeSettings();
          setStatus("");
          finishButtonAction(button, { disabled: button.disabled });
        } catch (error) {
          const details = Array.isArray(error.payload?.errors)
            ? error.payload.errors.map(formatDirectoryValidationError).join("\n")
            : "";
          const message = directorySaved && error.vaSettingsSave
            ? `Справочник сотрудников сохранён, но настройки графика не сохранены: ${error.message}`
            : `${error.message}${details ? "\n" + details : ""}`;
          setStatus(message, "error");
          finishButtonAction(button, {
            success: false,
            label: directorySaved ? "Сохранено частично" : "Не сохранено",
            disabled: false
          });
        }
      }

      function renderVaScheduleManagerData(payload) {
        state.va = {
          directory: clone(payload.directory || {}),
          settings: clone(payload.settings || {}),
          competencies: clone(payload.competencies || {}),
          newcomerAlerts: clone(payload.newcomer_alerts || { status: "unavailable", items: [] })
        };
        renderDirectoryEmployees();
        renderCompetencies();
        renderNewcomerAlerts();
        showDirectoryView(state.directoryView);
      }

      function renderNewcomerAlerts() {
        const banner = $("vaNewcomerAlerts");
        const items = state.va.newcomerAlerts?.items || [];
        if (!banner || state.va.newcomerAlerts?.status !== "available" || !items.length) {
          if (banner) {
            banner.style.display = "none";
            banner.textContent = "";
          }
          return;
        }
        banner.style.display = "block";
        banner.innerHTML = `<strong>Проверьте статус новичка:</strong><ul>${items.map((item) => `
          <li><button type="button" class="link-button" data-newcomer-alert-employee="${escapeAttr(item.employee_id)}">Сотрудник ${escapeHtml(item.employee_name)} числится новичком уже ${escapeHtml(item.months_passed)} мес. — возможно, стоит снять статус</button></li>
        `).join("")}</ul>`;
      }

      async function loadVaScheduleManagerData() {
        try {
          const payload = await adminApi(getSupUrl("va_admin"));
          renderVaScheduleManagerData(payload);
          return true;
        } catch (error) {
          state.va.settings.ready = false;
          $("competencyStatusText").textContent = error.message;
          $("competencyStatusText").className = "status error";
          renderDirectoryEmployees();
          return false;
        }
      }

      async function savePendingVaEmployeeSettings() {
        const drafts = Object.entries(clone(state.vaDrafts));
        for (const [employeeId, settings] of drafts) {
          const employee = state.directory.employees.find((item) => item.employee_id === employeeId);
          if (
            !employee
            || employee.enabled === false
            || !employee.memberships?.va_schedule_manager?.enabled
          ) {
            delete state.vaDrafts[employeeId];
            continue;
          }
          try {
            const payload = await adminApi(
              getSupUrlTemplate("va_employee_settings", employeeId),
              {
                method: "POST",
                body: JSON.stringify({
                  directory_etag: state.va.directory.etag,
                  settings_revision: state.va.settings.revision,
                  settings_etag: state.va.settings.etag,
                  settings
                })
              }
            );
            delete state.vaDrafts[employeeId];
            renderVaScheduleManagerData(payload);
          } catch (error) {
            error.vaSettingsSave = true;
            throw error;
          }
        }
      }

      function openCompetencyModal(code = "") {
        const item = (state.va.competencies.items || []).find((value) => value.code === code);
        state.competencyModal = { code: item?.code || "", mode: item ? "edit" : "add" };
        $("competencyModalTitle").textContent = item ? "Редактировать компетенцию" : "Добавить компетенцию";
        $("competencyModalCode").value = item?.code || "";
        $("competencyModalCode").disabled = Boolean(item);
        $("competencyModalName").value = item?.name || "";
        $("competencyModalDescription").value = item?.description || "";
        $("competencyModalStatus").textContent = item
          ? "Код существующей компетенции не изменяется."
          : "Используйте латинские буквы, цифры и знак подчёркивания.";
        $("competencyModalStatus").className = "status";
        $("competencyModalBackdrop").classList.remove("hidden");
        (item ? $("competencyModalName") : $("competencyModalCode")).focus();
      }

      function closeCompetencyModal() {
        $("competencyModalBackdrop").classList.add("hidden");
        state.competencyModal = { code: "", mode: "add" };
      }

      async function saveCompetencyModal() {
        const competency = {
          code: $("competencyModalCode").value.trim(),
          name: $("competencyModalName").value.trim(),
          description: $("competencyModalDescription").value.trim()
        };
        const editing = state.competencyModal.mode === "edit";
        const path = editing
          ? getSupUrlTemplate("va_competency", state.competencyModal.code)
          : getSupUrl("va_competencies");
        const button = $("competencyModalSaveBtn");
        beginButtonAction(button);
        try {
          const payload = await adminApi(path, {
            method: editing ? "PATCH" : "POST",
            body: JSON.stringify({
              expected_etag: state.va.competencies.etag,
              competency
            })
          });
          state.va.competencies = clone(payload.competencies);
          renderDirectoryEmployees();
          renderCompetencies();
          showDirectoryView("competencies");
          setStatus("");
          finishButtonAction(button, { duration: 700 });
          window.setTimeout(closeCompetencyModal, 700);
        } catch (error) {
          $("competencyModalStatus").textContent = error.message;
          $("competencyModalStatus").className = "status error";
          finishButtonAction(button, { success: false, disabled: false });
        }
      }

      async function deleteCompetency(code) {
        const item = (state.va.competencies.items || []).find((value) => value.code === code);
        if (!item || !confirm(`Удалить компетенцию «${item.name}»?`)) return;
        try {
          const payload = await adminApi(
            getSupUrlTemplate("va_competency", code),
            {
              method: "DELETE",
              body: JSON.stringify({ expected_etag: state.va.competencies.etag })
            }
          );
          state.va.competencies = clone(payload.competencies);
          renderDirectoryEmployees();
          renderCompetencies();
          showDirectoryView("competencies");
          setStatus("");
        } catch (error) {
          setStatus(error.message, "error");
        }
      }

      function renderSberTrackRuntimeStatus(status) {
        const mode = status.mode || "disabled";
        const modeText = mode === "dry_run"
          ? "dry-run, задачи не создаются"
          : mode === "active"
            ? "боевой режим"
            : mode === "error"
              ? "ошибка"
              : "выключено";
        $("diagSbertrackMode").textContent = modeText;
        $("diagSbertrackCheckedAt").textContent = status.last_checked_at || "-";
        $("diagSbertrackUid").textContent = String(status.last_checked_uid || 0);
        $("diagSbertrackPending").textContent = String(status.pending_count || 0);
        $("diagSbertrackDryRun").textContent = String(status.dry_run_match_count || 0);
        $("diagSbertrackCreated").textContent = String(status.created_count || 0);
        $("diagSbertrackResult").textContent = status.last_result || "-";
        $("diagSbertrackError").textContent = status.last_error || "-";
        const statusBox = $("sbertrackRuntimeStatus");
        statusBox.textContent = `Email → SberTrack: ${modeText}. UID: ${status.last_checked_uid || 0}; pending: ${status.pending_count || 0}; dry-run matches: ${status.dry_run_match_count || 0}.`;
        statusBox.className = `status ${status.last_error ? "error" : mode === "active" ? "success" : ""}`.trim();
      }

      function employeeMatches(row) {
        const query = $("employeeSearch").value.trim().toLowerCase();
        const emails = (row.emails || []).join(" ").toLowerCase();
        const matchesQuery = !query || row.name.toLowerCase().includes(query) || emails.includes(query);
        if (!matchesQuery) return false;
        if (state.employeeFilter === "enabled") return row.enabled !== false;
        if (state.employeeFilter === "disabled") return row.enabled === false;
        if (state.employeeFilter === "without_email") return !(row.emails || []).length;
        return true;
      }

      function renderEmployees() {
        const rows = state.config.automation.release_monitor_responsible_email.employee_recipients || [];
        const filtered = rows.map((row, index) => ({ ...row, index })).filter(employeeMatches);
        $("employeeRows").innerHTML = filtered.map(({ index, name, enabled, emails }) => `
          <tr class="${enabled === false ? "disabled-row" : ""}">
            <td><input type="checkbox" data-toggle-employee="${index}" ${enabled === false ? "" : "checked"}></td>
            <td><strong>${escapeHtml(name)}</strong></td>
            <td><span class="badge ${emails && emails.length ? "blue" : "red"}">${(emails || []).length}</span></td>
            <td><div class="chips">${emailChips(emails || [])}</div></td>
            <td class="action-cell">
              <button type="button" class="icon-btn ghost" data-edit-employee="${index}" title="Редактировать">Изм.</button>
              <button type="button" class="icon-btn danger" data-remove-employee="${index}" title="Удалить">Удал.</button>
            </td>
          </tr>
        `).join("");
        $("employeeEmpty").classList.toggle("hidden", filtered.length > 0);
        refreshDerived();
      }

      function emailChips(emails) {
        if (!emails.length) return `<span class="chip">email не указан</span>`;
        const visible = emails.slice(0, 2).map((email) => `<span class="chip">${escapeHtml(email)}</span>`);
        if (emails.length > 2) visible.push(`<span class="chip">+${emails.length - 2}</span>`);
        return visible.join("");
      }

      function prefixMatches(row) {
        const query = $("prefixSearch").value.trim().toLowerCase();
        const haystack = `${row.prefix || ""} ${row.jira_domain || ""} ${row.system || ""}`.toLowerCase();
        const matchesQuery = !query || haystack.includes(query);
        if (!matchesQuery) return false;
        if (state.prefixFilter === "enabled") return row.enabled !== false;
        if (state.prefixFilter === "disabled") return row.enabled === false;
        return true;
      }

      function renderPrefixes() {
        const rows = state.config.release_monitor.prefixes || [];
        const filtered = rows.map((row, index) => ({ ...row, index })).filter(prefixMatches);
        $("prefixRows").innerHTML = filtered.map(({ index, prefix, enabled, jira_domain, system }) => `
          <tr class="${enabled === false ? "disabled-row" : ""}">
            <td><input type="checkbox" data-toggle-prefix="${index}" ${enabled === false ? "" : "checked"}></td>
            <td><strong>${escapeHtml(prefix || "")}</strong></td>
            <td><span class="badge blue">${escapeHtml(jira_domain || "-")}</span></td>
            <td>${escapeHtml(system || "-")}</td>
            <td class="action-cell">
              <button type="button" class="icon-btn ghost" data-edit-prefix="${index}" title="Редактировать">Изм.</button>
              <button type="button" class="icon-btn danger" data-remove-prefix="${index}" title="Удалить">Удал.</button>
            </td>
          </tr>
        `).join("");
        $("prefixEmpty").classList.toggle("hidden", filtered.length > 0);
        refreshDerived();
      }

      function refreshDerived() {
        if (!state.config) return;
        const config = currentFormConfig();
        const responsible = config.automation.release_monitor_responsible_email;
        const employees = responsible.employee_recipients || [];
        const activeEmployees = employees.filter((row) => row.enabled !== false).length;
        const totalEmployeeEmails = employees.reduce((total, row) => total + (row.emails || []).length, 0);
        $("personalActiveEmployees").textContent = String(activeEmployees);
        $("personalEmailCount").textContent = String(totalEmployeeEmails);
      }

      function renderSystemOptions() {
        const systems = state.metadata.standard_systems || ["CLM", "EMRM", "АИСТ", "AI-Агенты", "Фокус"];
        $("systemOptions").innerHTML = systems.map((system) => `<option value="${escapeAttr(system)}"></option>`).join("");
      }

      function renderDomainOptions(selected) {
        const domains = state.metadata.jira_domains || ["sberbank", "delta"];
        $("modalPrefixDomain").innerHTML = domains.map((domain) => `<option value="${escapeAttr(domain)}" ${domain === selected ? "selected" : ""}>${escapeHtml(domain)}</option>`).join("");
      }

      function render(payload) {
        state.revision = payload.revision || "";
        state.config = initConfigShape(clone(payload.config || {}));
        state.loadedConfig = clone(state.config);
        state.rawJson = payload.raw_json_preview || "";
        renderMetadata(payload);
        renderSystemOptions();
        renderMaintenance(state.config.maintenance || {});
        renderMail(state.config);
        renderSberTrack(state.config);
        renderDocumentTemplateCenterSettings();
        renderGigaChatSettings();
        renderEmployees();
        renderPrefixes();
        renderVaScheduleManager(state.metadata.va_schedule_manager || {});
        $("rawJsonPreview").textContent = state.rawJson;
        $("tokenBox").classList.add("hidden");
        $("appBox").classList.remove("hidden");
        markClean();
        refreshDerived();
        showTab(state.activeTab || "employees");
        if (payload.read_error) {
          setStatus(`Файл feature_flags.json сейчас поврежден: ${payload.read_error}. После сохранения будет создана валидная структура, старое содержимое уйдет в backup.`, "error");
        } else {
          setStatus("");
        }
      }

      function showTab(name, focusCardId = "") {
        closeVaMultiSelect();
        if (!document.querySelector(`[data-tab="${name}"]`)) name = "employees";
        state.activeTab = name;
        if (name === "release-refresh") {
          ensureAdminSession()
            .then(() => startReleaseRefreshPolling({ immediate: true }))
            .catch((error) => setStatus(error.message, "error"));
        } else stopReleaseRefreshPolling();
        document.querySelectorAll("[data-panel]").forEach((panel) => panel.classList.toggle("active", panel.dataset.panel === name));
        document.querySelectorAll("[data-tab]").forEach((button) => {
          const active = button.dataset.tab === name;
          button.classList.toggle("active", active);
          if (active) button.setAttribute("aria-current", "page");
          else button.removeAttribute("aria-current");
        });
        const params = new URLSearchParams(window.location.search);
        params.set("tab", name);
        if (name === "employees" && !params.get("view")) params.set("view", state.directoryView);
        if (name !== "employees") params.delete("view");
        const nextUrl = `${window.location.pathname}?${params.toString()}`;
        window.history.replaceState({}, document.title, nextUrl);
        document.querySelector(`[data-tab="${name}"]`)?.scrollIntoView({ block: "nearest", inline: "nearest" });
        if (focusCardId) {
          setTimeout(() => $(focusCardId)?.scrollIntoView({ block: "center", behavior: "smooth" }), 50);
        }
      }

      function collectConfig() {
        return currentFormConfig();
      }

      async function loadData({ force = false } = {}) {
        if (state.dirty && !force && !confirm("Есть несохраненные изменения. Обновить данные без сохранения?")) {
          return;
        }
        if (!getToken()) {
          $("tokenBox").classList.remove("hidden");
          $("appBox").classList.add("hidden");
          $("saveBtn").disabled = true;
          $("resetBtn").disabled = true;
          return;
        }
        setStatus("Загружаю СУП-параметры...");
        try {
          const payload = await api(getSupUrl("data"));
          render(payload);
          await loadEmployeeDirectory();
        } catch (error) {
          $("tokenBox").classList.remove("hidden");
          $("appBox").classList.add("hidden");
          $("saveBtn").disabled = true;
          $("resetBtn").disabled = true;
          setStatus(error.message, "error");
        }
      }

      async function saveData() {
        const button = $("saveBtn");
        setStatus("");
        beginButtonAction(button);
        clearTabErrors();
        try {
          const payload = await api(getSupUrl("save"), {
            method: "POST",
            body: JSON.stringify({ revision: state.revision, config: collectConfig() })
          });
          render(payload);
          setStatus("");
          finishButtonAction(button, { disabled: true });
        } catch (error) {
          const errors = error.payload && Array.isArray(error.payload.errors)
            ? error.payload.errors
            : [];
          markErrorTabs(errors.join("\n") || error.message);
          const details = errors.length ? "\n" + errors.join("\n") : "";
          setStatus(`${error.message}${details}`, "error");
          finishButtonAction(button, { success: false, disabled: false });
        }
      }

      function resetChanges() {
        if (!state.loadedConfig) return;
        const button = $("resetBtn");
        state.config = clone(state.loadedConfig);
        renderMaintenance(state.config.maintenance || {});
        renderMail(state.config);
        renderSberTrack(state.config);
        renderDocumentTemplateCenterSettings();
        renderGigaChatSettings();
        renderEmployees();
        renderPrefixes();
        renderVaScheduleManager(state.metadata.va_schedule_manager || {});
        markClean();
        renderMetadata({ metadata: state.metadata, revision: state.revision, read_error: state.readError, path: state.path });
        setStatus("");
        finishButtonAction(button, { label: "Сброшено", disabled: true });
      }

      function clearTabErrors() {
        document.querySelectorAll("[data-tab]").forEach((button) => button.classList.remove("has-error"));
      }

      function markErrorTabs(message) {
        clearTabErrors();
        const rules = [
          ["maintenance", /Режим обслуживания|maintenance/i],
          ["mail", /Письма|Weekly|digest|Получатели|персональн|email/i],
          ["employees", /Сотрудник|ФИО/i],
          ["prefixes", /Prefix|Release prefixes|jira_domain|system/i],
          ["sbertrack", /SberTrack|Jira|Email → SberTrack|route|technical mailboxes/i],
        ];
        rules.forEach(([tab, pattern]) => {
          if (pattern.test(message)) {
            document.querySelector(`[data-tab="${tab}"]`)?.classList.add("has-error");
          }
        });
      }

      function openEmployeeModal(index = -1) {
        const employees = state.config.automation.release_monitor_responsible_email.employee_recipients || [];
        const row = index >= 0 ? employees[index] : { enabled: true, name: "", emails: [] };
        state.modal = { type: "employee", index };
        $("modalTitle").textContent = index >= 0 ? "Редактировать сотрудника" : "Добавить сотрудника";
        $("employeeForm").classList.remove("hidden");
        $("prefixForm").classList.add("hidden");
        $("modalEmployeeEnabled").checked = row.enabled !== false;
        $("modalEmployeeName").value = row.name || "";
        $("modalEmployeeEmails").value = lines(row.emails || []);
        $("modalBackdrop").classList.remove("hidden");
        $("modalEmployeeName").focus();
      }

      function openPrefixModal(index = -1) {
        const prefixes = state.config.release_monitor.prefixes || [];
        const row = index >= 0 ? prefixes[index] : { enabled: true, prefix: "", jira_domain: "sberbank", system: "" };
        state.modal = { type: "prefix", index };
        $("modalTitle").textContent = index >= 0 ? "Редактировать prefix" : "Добавить prefix";
        $("employeeForm").classList.add("hidden");
        $("prefixForm").classList.remove("hidden");
        $("modalPrefixEnabled").checked = row.enabled !== false;
        $("modalPrefixCode").value = row.prefix || "";
        renderDomainOptions(row.jira_domain || "sberbank");
        $("modalPrefixSystem").value = row.system || "";
        $("modalBackdrop").classList.remove("hidden");
        $("modalPrefixCode").focus();
      }

      function closeModal() {
        $("modalBackdrop").classList.add("hidden");
        state.modal = { type: "", index: -1 };
      }

      function saveModal() {
        if (state.modal.type === "employee") {
          const rows = state.config.automation.release_monitor_responsible_email.employee_recipients;
          const nextRow = {
            enabled: $("modalEmployeeEnabled").checked,
            name: $("modalEmployeeName").value.trim(),
            emails: splitLines($("modalEmployeeEmails").value)
          };
          if (state.modal.index >= 0) rows[state.modal.index] = nextRow;
          else rows.push(nextRow);
          renderEmployees();
        }
        if (state.modal.type === "prefix") {
          const rows = state.config.release_monitor.prefixes;
          const nextRow = {
            enabled: $("modalPrefixEnabled").checked,
            prefix: $("modalPrefixCode").value.trim().toUpperCase(),
            jira_domain: $("modalPrefixDomain").value,
            system: $("modalPrefixSystem").value.trim()
          };
          if (state.modal.index >= 0) rows[state.modal.index] = nextRow;
          else rows.push(nextRow);
          renderPrefixes();
        }
        markDirty();
        closeModal();
      }

      document.addEventListener("click", (event) => {
        const directoryView = event.target.closest("[data-directory-view]");
        if (directoryView) showDirectoryView(directoryView.dataset.directoryView);

        const openCompetencies = event.target.closest("[data-open-competencies]");
        if (openCompetencies) showDirectoryView("competencies");

        const vaMultiSelectToggle = event.target.closest("[data-va-multi-select-toggle]");
        if (vaMultiSelectToggle) {
          const container = vaMultiSelectToggle.closest("[data-va-multi-select]");
          openVaMultiSelect(container);
        } else if (
          vaMultiSelectOverlay.owner
          && !vaMultiSelectOverlay.owner.contains(event.target)
          && !vaMultiSelectOverlay.menu?.contains(event.target)
        ) {
          closeVaMultiSelect();
        }

        const vaCompetencyFilter = event.target.closest("[data-va-competency-filter]");
        if (vaCompetencyFilter) {
          const container = getVaMultiSelectOwner(vaCompetencyFilter);
          const menu = getVaMultiSelectMenu(container);
          menu?.querySelectorAll("[data-va-competency-filter]").forEach((button) => {
            button.classList.toggle("active", button === vaCompetencyFilter);
          });
          filterVaCompetencies(container);
        }

        const editCompetency = event.target.closest("[data-edit-competency]");
        if (editCompetency && !editCompetency.disabled) {
          openCompetencyModal(editCompetency.dataset.editCompetency);
        }

        const deleteCompetencyButton = event.target.closest("[data-delete-competency]");
        if (deleteCompetencyButton && !deleteCompetencyButton.disabled) {
          deleteCompetency(deleteCompetencyButton.dataset.deleteCompetency);
        }

        const tabButton = event.target.closest("[data-tab]");
        if (tabButton) showTab(tabButton.dataset.tab);

        const openTabButton = event.target.closest("[data-open-tab]");
        if (openTabButton) showTab(openTabButton.dataset.openTab, openTabButton.dataset.focusCard || "");

        const employeeFilter = event.target.closest("[data-employee-filter]");
        if (employeeFilter) {
          state.employeeFilter = employeeFilter.dataset.employeeFilter;
          document.querySelectorAll("[data-employee-filter]").forEach((button) => button.classList.toggle("active", button === employeeFilter));
          renderEmployees();
        }

        const directoryFilter = event.target.closest("[data-directory-filter]");
        if (directoryFilter) {
          syncDirectoryStateFromForm();
          state.directory.filter = directoryFilter.dataset.directoryFilter;
          document.querySelectorAll("[data-directory-filter]").forEach((button) => button.classList.toggle("active", button === directoryFilter));
          applyDirectoryNavigationFilter();
        }

        const newcomerAlert = event.target.closest("[data-newcomer-alert-employee]");
        if (newcomerAlert) {
          const employeeId = newcomerAlert.dataset.newcomerAlertEmployee;
          const index = (state.directory.employees || []).findIndex(
            (employee) => String(employee.employee_id || "") === String(employeeId)
          );
          if (index < 0) {
            setStatus("Сотрудник не найден в текущем справочнике.", "error");
          } else {
            showDirectoryView("employees");
            state.directory.selectedIndex = index;
            renderDirectoryEmployees();
            document.querySelector(`[data-directory-employee="${index}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" });
          }
          return;
        }

        const directoryEmployee = event.target.closest("[data-select-directory-employee]");
        if (directoryEmployee) {
          state.directory.employees = collectDirectoryEmployees();
          state.directory.selectedIndex = Number(directoryEmployee.dataset.selectDirectoryEmployee);
          renderDirectoryEmployees();
        }

        const prefixFilter = event.target.closest("[data-prefix-filter]");
        if (prefixFilter) {
          state.prefixFilter = prefixFilter.dataset.prefixFilter;
          document.querySelectorAll("[data-prefix-filter]").forEach((button) => button.classList.toggle("active", button === prefixFilter));
          renderPrefixes();
        }

        const editEmployee = event.target.closest("[data-edit-employee]");
        if (editEmployee) openEmployeeModal(Number(editEmployee.dataset.editEmployee));

        const removeEmployee = event.target.closest("[data-remove-employee]");
        if (removeEmployee) {
          state.config.automation.release_monitor_responsible_email.employee_recipients.splice(Number(removeEmployee.dataset.removeEmployee), 1);
          renderEmployees();
          markDirty();
        }

        const removeDirectoryEmployee = event.target.closest("[data-remove-directory-employee]");
        if (removeDirectoryEmployee) {
          state.directory.employees = collectDirectoryEmployees();
          state.directory.employees.splice(Number(removeDirectoryEmployee.dataset.removeDirectoryEmployee), 1);
          state.directory.selectedIndex = Math.min(state.directory.selectedIndex, Math.max(0, state.directory.employees.length - 1));
          renderDirectoryEmployees();
          return;
        }

        const focusDirectoryLocation = event.target.closest("[data-focus-directory-location]");
        if (focusDirectoryLocation) {
          const row = focusDirectoryLocation.closest("[data-directory-employee]");
          updateDirectoryLocationValidation(row, { focus: true });
          return;
        }

        const editPrefix = event.target.closest("[data-edit-prefix]");
        if (editPrefix) openPrefixModal(Number(editPrefix.dataset.editPrefix));

        const removePrefix = event.target.closest("[data-remove-prefix]");
        if (removePrefix) {
          state.config.release_monitor.prefixes.splice(Number(removePrefix.dataset.removePrefix), 1);
          renderPrefixes();
          markDirty();
        }

        const removeSbertrackRoute = event.target.closest("[data-remove-sbertrack-route]");
        if (removeSbertrackRoute) {
          state.config = currentFormConfig();
          state.config.automation.email_to_sbertrack.routes.splice(Number(removeSbertrackRoute.dataset.removeSbertrackRoute), 1);
          renderSberTrack(state.config);
          markDirty();
        }

        const removeSbertrackUser = event.target.closest("[data-remove-sbertrack-user]");
        if (removeSbertrackUser) {
          state.config = currentFormConfig();
          state.config.sbertrack_users.splice(Number(removeSbertrackUser.dataset.removeSbertrackUser), 1);
          renderSberTrack(state.config);
          markDirty();
        }
      });

      document.addEventListener("change", (event) => {
        if (event.target.matches("[data-va-competency-option] input")) {
          const container = getVaMultiSelectOwner(event.target);
          captureVaSettingsDraft(event.target);
          refreshVaMultiSelects();
          filterVaCompetencies(container);
          return;
        }
        if (event.target.matches("[data-va-status], [data-va-role], [data-va-overtime]")) {
          captureVaSettingsDraft(event.target);
          return;
        }
        if (event.target.matches("[data-directory-enabled]")) {
          const label = event.target.closest("label")?.querySelector("[data-directory-enabled-label]");
          if (label) label.textContent = event.target.checked ? "Активен" : "Уволен / архив";
          const row = event.target.closest("[data-directory-employee]");
          const employeeId = row?.querySelector("[data-va-settings-section]")?.dataset.vaEmployeeId;
          if (!event.target.checked && employeeId) delete state.vaDrafts[employeeId];
          updateDirectoryLocationValidation(event.target.closest("[data-directory-employee]"));
          syncDirectoryStateFromForm();
          renderDirectoryEmployees();
          return;
        }
        if (event.target.matches("[data-directory-release-enabled]")) {
          const orderInput = event.target.closest("[data-directory-employee]")?.querySelector("[data-directory-release-order]");
          if (event.target.checked) {
            fillDirectoryOrder(
              event.target,
              "[data-directory-release-order]",
              "[data-directory-release-enabled]"
            );
          } else if (orderInput) {
            orderInput.value = "";
          }
          syncDirectoryStateFromForm();
          return;
        }
        if (event.target.matches("[data-directory-va-enabled]")) {
          const row = event.target.closest("[data-directory-employee]");
          const orderInput = row?.querySelector("[data-directory-va-order]");
          if (event.target.checked) {
            fillDirectoryOrder(
              event.target,
              "[data-directory-va-order]",
              "[data-directory-va-enabled]",
              null,
              1
            );
          } else if (orderInput) {
            orderInput.value = "";
            const employeeId = row?.querySelector("[data-va-settings-section]")?.dataset.vaEmployeeId;
            if (employeeId) delete state.vaDrafts[employeeId];
          }
          updateDirectoryLocationValidation(row);
          syncDirectoryStateFromForm();
          renderDirectoryEmployees();
          return;
        }
        if (event.target.matches("[data-directory-va-order]")) {
          resolveVaOrderChange(event.target);
          syncDirectoryStateFromForm();
          return;
        }
        if (event.target.matches("[data-directory-location]")) {
          updateDirectoryLocationValidation(event.target.closest("[data-directory-employee]"));
          syncDirectoryStateFromForm();
          return;
        }
        if (event.target.matches("[data-directory-dashboard-enabled], [data-directory-dashboard-role]")) {
          const row = event.target.closest("[data-directory-employee]");
          const enabled = row?.querySelector("[data-directory-dashboard-enabled]")?.checked;
          const role = row?.querySelector("[data-directory-dashboard-role]")?.value;
          const orderInput = row?.querySelector("[data-directory-dashboard-order]");
          if (!enabled && orderInput) orderInput.value = "";
          if (event.target.matches("[data-directory-dashboard-role]") && orderInput) {
            orderInput.value = "";
          }
          if (enabled && ["primary", "extra"].includes(role)) {
            fillDirectoryOrder(
              event.target,
              "[data-directory-dashboard-order]",
              "[data-directory-dashboard-enabled]",
              role
            );
          }
          syncDirectoryStateFromForm();
          return;
        }
        if (event.target.closest("[data-directory-employee]")) {
          syncDirectoryStateFromForm();
          return;
        }
        const employeeToggle = event.target.closest("[data-toggle-employee]");
        if (employeeToggle) {
          const row = state.config.automation.release_monitor_responsible_email.employee_recipients[Number(employeeToggle.dataset.toggleEmployee)];
          row.enabled = employeeToggle.checked;
          renderEmployees();
          markDirty();
          return;
        }
        const prefixToggle = event.target.closest("[data-toggle-prefix]");
        if (prefixToggle) {
          const row = state.config.release_monitor.prefixes[Number(prefixToggle.dataset.togglePrefix)];
          row.enabled = prefixToggle.checked;
          renderPrefixes();
          markDirty();
          return;
        }
        if (event.target.matches("[data-maintenance]")) {
          state.config.maintenance = currentFormConfig().maintenance;
          renderMaintenance(state.config.maintenance);
          markDirty();
          return;
        }
        if (event.target.matches("[data-route-target]")) {
          refreshEmailRouteFields();
          markDirty();
          return;
        }
        if (event.target.matches("[data-dirty-field]")) {
          markDirty();
        }
      });

      document.addEventListener("input", (event) => {
        if (event.target.matches("[data-va-competency-search]")) {
          filterVaCompetencies(getVaMultiSelectOwner(event.target));
          return;
        }
        if (event.target.matches("[data-directory-release-name], [data-directory-full-name], [data-directory-additional-aliases]")) {
          const row = event.target.closest("[data-directory-employee]");
          syncDirectoryStateFromForm();
          renderDirectoryAliasPreview(row);
          return;
        }
        if (event.target.matches("[data-dirty-field]")) markDirty();
        if (event.target.closest("[data-directory-employee]")) syncDirectoryStateFromForm();
      });

      $("employeeSearch").addEventListener("input", renderEmployees);
      $("prefixSearch").addEventListener("input", renderPrefixes);
      $("directoryEmployeeSearch").addEventListener("input", () => {
        syncDirectoryStateFromForm();
        applyDirectoryNavigationFilter();
      });
      $("addEmployeeBtn").addEventListener("click", () => openEmployeeModal());
      $("addDirectoryEmployeeBtn").addEventListener("click", () => {
        state.directory.employees = collectDirectoryEmployees();
        state.directory.employees.push(emptyDirectoryEmployee());
        state.directory.selectedIndex = state.directory.employees.length - 1;
        state.directory.filter = "all";
        $("directoryEmployeeSearch").value = "";
        document.querySelectorAll("[data-directory-filter]").forEach((button) => button.classList.toggle("active", button.dataset.directoryFilter === "all"));
        renderDirectoryEmployees();
      });
      $("reloadDirectoryBtn").addEventListener("click", loadEmployeeDirectory);
      $("saveDirectoryBtn").addEventListener("click", saveEmployeeDirectory);
      $("addCompetencyBtn").addEventListener("click", () => openCompetencyModal());
      $("addPrefixBtn").addEventListener("click", () => openPrefixModal());
      $("addSbertrackRouteBtn").addEventListener("click", () => {
        state.config = currentFormConfig();
        state.config.automation.email_to_sbertrack.routes.push({
          enabled: true,
          name: "EMRM",
          target_system: "jira",
          subject_triggers: ["EMRM"],
          spaces: [],
          jira_projects: ["EMRM"],
          jira_domain: "sberbank",
          jira_issue_type: "Task",
          jira_issue_type_id: "3",
          jira_epic_name_field: "",
          jira_epic_link: { field_id: "customfield_10006", key: "EMRM-40162" },
          jira_priority: "Minor",
          jira_labels: ["FromChannel"],
          jira_team: { field_id: "customfield_11902", value_id: "6651", name: "[\u0424\u043e\u043a\u0443\u0441] ForREST" },
          suit: "task",
          priority: "low",
          summary_template: "{subject}"
        });
        renderSberTrack(state.config);
        markDirty();
      });
      $("addSbertrackUserBtn").addEventListener("click", () => {
        state.config = currentFormConfig();
        state.config.sbertrack_users.push({
          enabled: true,
          email: "",
          name: "",
          sbertrack_user_id: ""
        });
        renderSberTrack(state.config);
        markDirty();
      });
      $("modalCloseBtn").addEventListener("click", closeModal);
      $("modalCancelBtn").addEventListener("click", closeModal);
      $("modalSaveBtn").addEventListener("click", saveModal);
      $("modalBackdrop").addEventListener("click", (event) => {
        if (event.target === $("modalBackdrop")) closeModal();
      });
      $("competencyModalCloseBtn").addEventListener("click", closeCompetencyModal);
      $("competencyModalCancelBtn").addEventListener("click", closeCompetencyModal);
      $("competencyModalSaveBtn").addEventListener("click", saveCompetencyModal);
      $("competencyModalBackdrop").addEventListener("click", (event) => {
        if (event.target === $("competencyModalBackdrop")) closeCompetencyModal();
      });
      document.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") return;
        if (!$("competencyModalBackdrop").classList.contains("hidden")) {
          closeCompetencyModal();
          return;
        }
        closeVaMultiSelect({ restoreFocus: true });
      });
      window.addEventListener("resize", scheduleVaMultiSelectPosition);
      window.addEventListener("scroll", scheduleVaMultiSelectPosition, true);
      $("saveBtn").addEventListener("click", saveData);
      $("reloadBtn").addEventListener("click", () => loadData());
      $("resetBtn").addEventListener("click", resetChanges);
      $("openReleaseMonitorBtn").addEventListener("click", () => {
        window.location.assign(getSupUrl("release_monitor"));
      });
      document.querySelectorAll("[data-release-refresh-mode]").forEach((button) => {
        button.addEventListener("click", () => requestReleaseRefresh(button.dataset.releaseRefreshMode));
      });
      $("releaseRefreshCancelBtn").addEventListener("click", closeReleaseRefreshConfirmation);
      $("releaseRefreshConfirmBtn").addEventListener("click", () => {
        const mode = state.releaseRefresh.confirmationMode;
        if (mode) startReleaseRefresh(mode);
      });
      $("vaScheduleManagerEnabled").addEventListener("change", markDirty);
      $("loginAdminSessionBtn").addEventListener("click", async () => {
        const button = $("loginAdminSessionBtn");
        beginButtonAction(button, "Проверка...");
        try {
          await ensureAdminSession();
          setStatus("");
          finishButtonAction(button, { label: "Доступ открыт", disabled: false });
          if (state.activeTab === "release-refresh") {
            startReleaseRefreshPolling({ immediate: true });
          }
        } catch (error) {
          setStatus(error.message, "error");
          finishButtonAction(button, { success: false, disabled: false });
        }
      });
      $("openVaScheduleManagerBtn").addEventListener("click", async () => {
        const targetUrl = $("openVaScheduleManagerBtn").dataset.url;
        if (!targetUrl) return;
        try {
          await ensureAdminSession();
          window.location.assign(targetUrl);
        } catch (error) {
          setStatus(error.message, "error");
        }
      });

      const unlockBtn = $("unlockBtn");
      if (unlockBtn) {
        unlockBtn.addEventListener("click", () => {
          const token = $("tokenInput").value.trim();
          if (token) {
            sessionStorage.setItem(TOKEN_KEY, token);
            $("tokenInput").value = "";
            loadData({ force: true });
          }
        });
      }
      const clearTokenBtn = $("clearTokenBtn");
      if (clearTokenBtn) {
        clearTokenBtn.addEventListener("click", () => {
          sessionStorage.removeItem(TOKEN_KEY);
          location.reload();
        });
      }
      window.addEventListener("beforeunload", (event) => {
        stopReleaseRefreshPolling();
        if (!state.dirty) return;
        event.preventDefault();
        event.returnValue = "";
      });
      document.addEventListener("visibilitychange", () => {
        if (document.hidden) stopReleaseRefreshPolling();
        else if (
          state.activeTab === "release-refresh" &&
          sessionStorage.getItem("sup_admin_csrf_token")
        ) startReleaseRefreshPolling({ immediate: true });
      });
      window.addEventListener("pagehide", stopReleaseRefreshPolling);
      window.addEventListener("pageshow", () => {
        if (
          !document.hidden &&
          state.activeTab === "release-refresh" &&
          sessionStorage.getItem("sup_admin_csrf_token")
        ) {
          startReleaseRefreshPolling({ immediate: true });
        }
      });

      initTokenFromUrl();
      loadData({ force: true });

  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initOplotSupAdminPage, { once: true });
  } else {
    initOplotSupAdminPage();
  }
})();

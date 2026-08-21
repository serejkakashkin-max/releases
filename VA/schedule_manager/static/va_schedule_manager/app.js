(function () {
  const focusableSelector = [
    'a[href]',
    'button:not([disabled])',
    'input:not([disabled]):not([type="hidden"])',
    'select:not([disabled])',
    'textarea:not([disabled])',
    '[tabindex]:not([tabindex="-1"])'
  ].join(',');

  function focusableElements(container) {
    return Array.from(container.querySelectorAll(focusableSelector))
      .filter((element) => element.offsetParent !== null || element === document.activeElement);
  }

  function rememberModalTrigger(event) {
    const trigger = event.target.closest('[data-modal-trigger][data-focus-key]');
    if (!trigger) {
      return;
    }
    window.sessionStorage.setItem('scheduleManager.returnFocus', trigger.dataset.focusKey);
  }

  function restoreFocusAfterModalClose() {
    const focusKey = window.sessionStorage.getItem('scheduleManager.returnFocus');
    if (!focusKey || document.querySelector('[data-modal]')) {
      return;
    }
    window.sessionStorage.removeItem('scheduleManager.returnFocus');
    const trigger = document.querySelector(`[data-focus-key="${focusKey}"]`);
    if (trigger) {
      trigger.focus({ preventScroll: true });
    }
  }

  function closeModal(modal) {
    const backdrop = modal.closest('[data-modal-backdrop]');
    const closeHref = backdrop ? backdrop.dataset.closeHref : '';
    if (closeHref) {
      window.location.assign(closeHref);
    }
  }

  function trapTab(event, modal) {
    const focusable = focusableElements(modal);
    if (!focusable.length) {
      event.preventDefault();
      modal.focus();
      return;
    }

    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function setupModal(modal) {
    const focusable = focusableElements(modal);
    const initialFocus = modal.querySelector('[data-modal-close]') || focusable[0] || modal;
    initialFocus.focus({ preventScroll: true });

    document.addEventListener('keydown', (event) => {
      if (!document.body.contains(modal)) {
        return;
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        closeModal(modal);
      }
      if (event.key === 'Tab') {
        trapTab(event, modal);
      }
    });
  }

  function setupBackdropClose() {
    document.querySelectorAll('[data-modal-backdrop]').forEach((backdrop) => {
      backdrop.addEventListener('click', (event) => {
        if (event.target !== backdrop) {
          return;
        }
        const modal = backdrop.querySelector('[data-modal]');
        if (modal) {
          closeModal(modal);
        }
      });
    });
  }

  function setupAutoSubmitForms() {
    document.querySelectorAll('[data-auto-submit-form]').forEach((form) => {
      let lastSignature = new URLSearchParams(new FormData(form)).toString();
      form.querySelectorAll('[data-auto-submit-control]').forEach((control) => {
        control.addEventListener('change', () => {
          const signature = new URLSearchParams(new FormData(form)).toString();
          if (signature === lastSignature) {
            return;
          }
          lastSignature = signature;
          form.requestSubmit();
        });
      });
    });
  }

  function setupUserMessages() {
    document.querySelectorAll('[data-message-layer]').forEach((layer) => {
      function closeLayer() {
        document.removeEventListener('click', closeOnOutsideClick);
        layer.remove();
      }

      function removeFrame(frame) {
        if (!frame || !frame.parentElement) {
          return;
        }
        frame.remove();
        if (!layer.querySelector('[data-message-frame]')) {
          closeLayer();
        }
      }

      function closeOnOutsideClick(event) {
        if (!layer.parentElement || event.target.closest('[data-message-frame]')) {
          return;
        }
        closeLayer();
      }

      layer.addEventListener('click', (event) => {
        if (event.target === layer) {
          closeLayer();
          return;
        }
        const close = event.target.closest('[data-message-close]');
        if (close) {
          removeFrame(close.closest('[data-message-frame]'));
        }
      });
      document.addEventListener('click', closeOnOutsideClick);

      layer.querySelectorAll('[data-message-frame]').forEach((frame) => {
        if (!['success', 'info'].includes(frame.dataset.messageKind || '')) {
          return;
        }
        window.setTimeout(() => removeFrame(frame), 10000);
      });
    });
  }

  function appendAutoplanLog(log, step) {
    const item = document.createElement('li');
    item.className = step.status === 'error' ? 'is-error' : 'is-done';
    const time = document.createElement('span');
    time.className = 'autoplan-progress-time';
    time.textContent = step.created_at || '';
    const message = document.createElement('span');
    message.textContent = step.message || '';
    item.append(time, message);
    log.append(item);
    log.scrollTop = log.scrollHeight;
  }

  function setAutoplanFormBusy(form, busy) {
    form.querySelectorAll('button, input, select, textarea').forEach((control) => {
      control.disabled = busy;
    });
    form.querySelectorAll('a').forEach((link) => {
      link.classList.toggle('is-disabled', busy);
      link.setAttribute('aria-disabled', busy ? 'true' : 'false');
    });
  }

  function setupAutoplanProgressForms() {
    document.querySelectorAll('[data-autoplan-form]').forEach((form) => {
      const progress = form.querySelector('[data-autoplan-progress]');
      const status = form.querySelector('[data-autoplan-progress-status]');
      const log = form.querySelector('[data-autoplan-progress-log]');
      if (!progress || !status || !log || !form.dataset.autoplanApiUrl || !form.dataset.autoplanStatusUrlTemplate) {
        return;
      }

      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const data = Object.fromEntries(new FormData(form).entries());
        progress.hidden = false;
        log.replaceChildren();
        status.textContent = 'Запускаю задачу...';
        setAutoplanFormBusy(form, true);
        appendAutoplanLog(log, { created_at: '', message: 'Отправляю запрос на автопланирование.' });

        try {
          const response = await fetch(form.dataset.autoplanApiUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
          });
          const payload = await response.json();
          if (!response.ok || !payload.ok) {
            throw new Error(payload.error && payload.error.message ? payload.error.message : 'Не удалось запустить автопланирование.');
          }

          const jobId = payload.data.job_id;
          const statusUrl = form.dataset.autoplanStatusUrlTemplate.replace('__JOB_ID__', encodeURIComponent(jobId));
          let renderedSteps = 0;
          const poll = async () => {
            const statusResponse = await fetch(statusUrl, { cache: 'no-store' });
            const statusPayload = await statusResponse.json();
            if (!statusResponse.ok || !statusPayload.ok) {
              throw new Error(statusPayload.error && statusPayload.error.message ? statusPayload.error.message : 'Не удалось получить статус автоплана.');
            }
            const job = statusPayload.data;
            const steps = Array.isArray(job.steps) ? job.steps : [];
            steps.slice(renderedSteps).forEach((step) => appendAutoplanLog(log, step));
            renderedSteps = steps.length;
            if (job.status === 'done') {
              status.textContent = 'Готово. Обновляю график...';
              window.setTimeout(() => {
                window.location.assign(job.redirect_url || window.location.href);
              }, 700);
              return;
            }
            if (job.status === 'error') {
              status.textContent = 'Автопланирование остановлено.';
              setAutoplanFormBusy(form, false);
              return;
            }
            status.textContent = 'Выполняю расчет...';
            window.setTimeout(poll, 900);
          };
          window.setTimeout(poll, 400);
        } catch (error) {
          status.textContent = 'Ошибка запуска.';
          appendAutoplanLog(log, { created_at: '', message: error.message || 'Не удалось выполнить автопланирование.', status: 'error' });
          setAutoplanFormBusy(form, false);
        }
      });
    });
  }

  function parseJsonScript(container, selector) {
    const node = container.querySelector(selector);
    if (!node) {
      return [];
    }
    try {
      return JSON.parse(node.textContent || '[]');
    } catch (_error) {
      return [];
    }
  }

  function setScheduleStatus(editor, message, kind) {
    const status = editor.querySelector('[data-schedule-edit-status]');
    if (!status) {
      return;
    }
    status.textContent = message || '';
    status.dataset.kind = kind || '';
  }

  function formatHours(value) {
    const number = Number(value || 0);
    if (Number.isInteger(number)) {
      return String(number);
    }
    return String(Math.round(number * 10) / 10).replace('.', ',');
  }

  function cssEscape(value) {
    if (window.CSS && typeof window.CSS.escape === 'function') {
      return window.CSS.escape(value);
    }
    return String(value).replace(/"/g, '\\"');
  }

  function updateViolationView(editor, schedule) {
    const badge = editor.querySelector('[data-violation-badge]');
    const list = editor.querySelector('[data-violations-list]');
    const count = Number(schedule.violation_count || 0);
    if (badge) {
      const badgeNode = document.createElement('span');
      badgeNode.className = count ? 'va-badge va-badge-warning' : 'va-badge va-badge-success';
      badgeNode.textContent = count ? `Нарушений: ${count}` : 'Проверено';
      badge.replaceChildren(badgeNode);
    }
    if (!list) {
      return;
    }
    list.replaceChildren();
    if (!count) {
      list.hidden = true;
      return;
    }
    list.hidden = false;
    (schedule.violations || []).forEach((violation) => {
      const item = document.createElement('li');
      const employee = violation.employee_name ? `${violation.employee_name} — ` : '';
      item.textContent = `${violation.day} число, ${violation.shift}: ${employee}${violation.message}`;
      list.appendChild(item);
    });
  }

  function updateValidationStopCells(editor, schedule) {
    editor.querySelectorAll('[data-validation-stop-cell]').forEach((cell) => {
      cell.classList.remove('autoplan-stop-cell');
      delete cell.dataset.validationStopCell;
      delete cell.dataset.autoplanStop;
      cell.removeAttribute('title');
    });
    (schedule.violations || []).forEach((violation) => {
      if (!violation.employee_name || !String(violation.message || '').includes('Переходящая смена')) {
        return;
      }
      const selector = `[data-schedule-cell][data-employee-name="${cssEscape(violation.employee_name)}"][data-day="${cssEscape(violation.day)}"]`;
      const cell = editor.querySelector(selector);
      if (!cell) {
        return;
      }
      cell.classList.add('autoplan-stop-cell');
      cell.dataset.validationStopCell = 'true';
      cell.dataset.autoplanStop = violation.message;
      cell.title = violation.message;
    });
  }

  function clearAutoplanArtifact(editor, data) {
    if (!data || !data.autoplan_artifact_cleared) {
      return;
    }
    const artifact = editor.querySelector('[data-autoplan-artifact]');
    if (artifact) {
      artifact.remove();
    }
    editor.querySelectorAll('[data-autoplan-stop]').forEach((cell) => {
      if (cell.dataset.validationStopCell === 'true') {
        return;
      }
      cell.classList.remove('autoplan-stop-cell');
      delete cell.dataset.autoplanStop;
      cell.removeAttribute('title');
    });
  }

  function applyCellPayload(cell, payload) {
    const code = payload.shift_code || '';
    const displayCode = payload.display_code || code;
    const color = payload.color || '';
    const textColor = payload.text_color || '';
    cell.dataset.shiftCode = code;
    cell.textContent = displayCode;
    Array.from(cell.classList)
      .filter((name) => name.indexOf('shift-') === 0 && name !== 'shift-cell')
      .forEach((name) => cell.classList.remove(name));
    if (code) {
      cell.classList.add(`shift-${code.toLowerCase()}`);
      cell.style.backgroundColor = color;
      cell.style.color = textColor;
      cell.title = payload.shift_name || code;
    } else {
      cell.style.backgroundColor = '';
      cell.style.color = '';
      cell.title = '';
    }
  }

  function applyCellResult(editor, cell, payload) {
    applyCellPayload(cell, payload.cell);

    const hours = editor.querySelector(`[data-hours-cell][data-employee-name="${cssEscape(payload.row.employee_name)}"]`);
    if (hours) {
      hours.textContent = formatHours(payload.row.hours);
    }
    updateViolationView(editor, payload.schedule || {});
    clearAutoplanArtifact(editor, payload);
    updateValidationStopCells(editor, payload.schedule || {});
  }

  function findScheduleCell(editor, employeeName, day) {
    return editor.querySelector(
      `[data-schedule-cell][data-employee-name="${cssEscape(employeeName)}"][data-day="${Number(day)}"]`
    );
  }

  function applyBulkFillResult(editor, data) {
    (data.cells || []).forEach((cellPayload) => {
      const cell = findScheduleCell(editor, cellPayload.employee_name, cellPayload.day);
      if (cell) {
        applyCellPayload(cell, cellPayload);
      }
    });
    (data.rows || []).forEach((rowPayload) => {
      const hours = editor.querySelector(`[data-hours-cell][data-employee-name="${cssEscape(rowPayload.employee_name)}"]`);
      if (hours) {
        hours.textContent = formatHours(rowPayload.hours);
      }
    });
    updateViolationView(editor, data.schedule || {});
    clearAutoplanArtifact(editor, data);
    updateValidationStopCells(editor, data.schedule || {});
  }

  async function readJsonResponse(response) {
    const text = await response.text();
    if (!text) {
      return null;
    }
    try {
      return JSON.parse(text);
    } catch (_error) {
      return null;
    }
  }

  function restoreScheduleCell(cell, original) {
    cell.dataset.shiftCode = original.code;
    cell.textContent = original.text;
    cell.className = original.className;
    cell.style.cssText = original.style;
    cell.title = original.title;
  }

  function closeActiveCellEditor(editor, focusCell) {
    const active = editor.activeCellEditor;
    if (!active || active.cell.dataset.saving === 'true') {
      return;
    }
    restoreScheduleCell(active.cell, active.original);
    delete editor.activeCellEditor;
    if (focusCell) {
      active.cell.focus({ preventScroll: true });
    }
  }

  function isHolidayShift(options, code) {
    const selected = options.find((shift) => shift.code === code);
    if (!selected) {
      return false;
    }
    const values = [selected.code, selected.display_code, selected.name]
      .filter(Boolean)
      .map((value) => String(value).trim().toLowerCase());
    return values.includes('п') || values.includes('праздник');
  }

  function productionCalendarWarningText(targetCells, days) {
    const byDay = new Map((days || []).map((day) => [Number(day.day), day]));
    const selectedDays = Array.from(new Set(targetCells.map((cell) => Number(cell.day))))
      .filter(Boolean)
      .sort((left, right) => left - right);
    if (!selectedDays.length) {
      return 'Производственный календарь: дату не удалось определить.';
    }
    return selectedDays.map((dayNumber) => {
      const day = byDay.get(dayNumber);
      if (!day) {
        return `${dayNumber} число: нет данных производственного календаря.`;
      }
      if (day.calendar_warning) {
        return `${dayNumber} число: производственный календарь не проверен (${day.calendar_warning}).`;
      }
      const source = day.calendar_source ? ` (${day.calendar_source})` : '';
      if (day.is_production_holiday) {
        return `${dayNumber} число: праздничный день по производственному календарю${source}.`;
      }
      return `${dayNumber} число: не праздничный день по производственному календарю${source}.`;
    }).join('\n');
  }

  function confirmHolidayFill(targetCells, days) {
    const selectedDayNumbers = Array.from(new Set(targetCells.map((cell) => Number(cell.day))))
      .filter(Boolean)
      .sort((left, right) => left - right);
    const dayText = selectedDayNumbers.length === 1
      ? `${selectedDayNumbers[0]} число`
      : `${selectedDayNumbers.join(', ')} числа`;
    const calendarText = productionCalendarWarningText(targetCells, days);
    const firstConfirmed = window.confirm(
      `Внимание: "Праздник" применяется не к одной ячейке, а ко всей дате (${dayText}) для всех сотрудников.\n\n${calendarText}\n\nПродолжить?`
    );
    if (!firstConfirmed) {
      return false;
    }
    return window.confirm(
      'Повторное подтверждение: все смены в выбранной дате будут заменены на "Праздник". Это может очистить уже заполненный график. Точно применить?'
    );
  }

  async function saveScheduleCell(editor, cell, select, original, options, days) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 10000);
    const selectedCode = select.value;
    const targetCells = cell.dataset.selected === 'true'
      ? selectedCellPayload(editor)
      : [{ employee_name: cell.dataset.employeeName, day: Number(cell.dataset.day) }];
    const holidayConfirmed = isHolidayShift(options, selectedCode);
    if (holidayConfirmed && !confirmHolidayFill(targetCells, days)) {
      window.clearTimeout(timeout);
      restoreScheduleCell(cell, original);
      if (editor.activeCellEditor && editor.activeCellEditor.cell === cell) {
        delete editor.activeCellEditor;
      }
      setScheduleStatus(editor, 'Праздник не применен.', 'warning');
      cell.focus({ preventScroll: true });
      return;
    }
    cell.dataset.saving = 'true';
    select.disabled = true;
    setScheduleStatus(editor, targetCells.length > 1 ? 'Сохраняю выделенные ячейки...' : 'Сохраняю изменение...', 'loading');

    try {
      const response = await fetch(editor.dataset.bulkFillUrl || editor.dataset.updateUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: editor.dataset.bulkFillUrl
          ? JSON.stringify({
              sheet_name: editor.dataset.sheetName,
              cells: targetCells,
              shift_code: selectedCode,
              holiday_confirmed: holidayConfirmed
            })
          : JSON.stringify({
              sheet_name: editor.dataset.sheetName,
              employee_name: cell.dataset.employeeName,
              day: Number(cell.dataset.day),
              shift_code: selectedCode,
              holiday_confirmed: holidayConfirmed
            }),
        signal: controller.signal
      });
      const payload = await readJsonResponse(response);
      if (!response.ok || !payload || !payload.ok) {
        const message = payload && payload.error && payload.error.message
          ? payload.error.message
          : 'Не удалось сохранить изменение.';
        throw new Error(message);
      }
      if (editor.dataset.bulkFillUrl) {
        applyBulkFillResult(editor, payload.data);
        clearScheduleSelection(editor);
        const suffix = payload.data.applied_to_full_days
          ? ' Праздник применен ко всей дате.'
          : '';
        setScheduleStatus(editor, targetCells.length > 1 ? `Выделенные ячейки сохранены.${suffix}` : `Изменение сохранено.${suffix}`, 'success');
      } else {
        applyCellResult(editor, cell, payload.data);
        setScheduleStatus(editor, 'Изменение сохранено.', 'success');
      }
    } catch (error) {
      restoreScheduleCell(cell, original);
      const message = error.name === 'AbortError'
        ? 'Сервер долго не отвечает. Изменение не сохранено.'
        : error.message;
      setScheduleStatus(editor, message, 'error');
    } finally {
      window.clearTimeout(timeout);
      delete cell.dataset.saving;
      if (editor.activeCellEditor && editor.activeCellEditor.cell === cell) {
        delete editor.activeCellEditor;
      }
    }
  }

  function makeShiftSelect(options, currentCode) {
    const select = document.createElement('select');
    select.className = 'shift-cell-select';
    const empty = document.createElement('option');
    empty.value = '';
    empty.textContent = 'Пусто';
    select.appendChild(empty);
    options.forEach((shift) => {
      const option = document.createElement('option');
      option.value = shift.code;
      option.textContent = `${shift.display_code || shift.code} · ${shift.name}`;
      option.selected = shift.code === currentCode;
      select.appendChild(option);
    });
    if (!currentCode) {
      empty.selected = true;
    }
    return select;
  }

  function shiftMap(options) {
    return options.reduce((result, shift) => {
      result[shift.code] = shift;
      return result;
    }, {});
  }

  function renderScheduleCell(editor, optionsByCode, employeeName, day, code) {
    const shift = optionsByCode[code] || null;
    const cell = document.createElement('td');
    cell.className = `shift-cell ${code ? `shift-${code.toLowerCase()} ` : ''}editable-shift-cell`;
    cell.tabIndex = 0;
    cell.setAttribute('role', 'button');
    cell.setAttribute('aria-label', `Изменить смену ${employeeName} на ${day} число`);
    cell.dataset.scheduleCell = '';
    cell.dataset.employeeName = employeeName;
    cell.dataset.day = String(day);
    cell.dataset.shiftCode = code || '';
    if (shift) {
      cell.style.backgroundColor = shift.color;
      cell.style.color = shift.text_color;
      cell.title = shift.name;
      cell.textContent = shift.display_code || shift.code;
    }
    return cell;
  }

  function appendScheduleRow(editor, options, days, row) {
    const body = editor.querySelector('[data-schedule-body]');
    if (!body) {
      return;
    }
    const byCode = shiftMap(options);
    const tr = document.createElement('tr');
    const name = document.createElement('th');
    name.className = 'sticky-col employee-col';
    name.textContent = row.employee_name;
    tr.appendChild(name);

    const hours = document.createElement('td');
    hours.className = 'hours-col';
    hours.dataset.hoursCell = '';
    hours.dataset.employeeName = row.employee_name;
    hours.textContent = formatHours(row.hours);
    tr.appendChild(hours);

    const actions = document.createElement('td');
    actions.className = 'row-action-col';
    const deleteButton = document.createElement('button');
    deleteButton.className = 'button button-danger-soft button-compact';
    deleteButton.type = 'button';
    deleteButton.dataset.deleteScheduleEmployee = '';
    deleteButton.dataset.employeeName = row.employee_name;
    deleteButton.textContent = 'Удалить';
    actions.appendChild(deleteButton);
    tr.appendChild(actions);

    days.forEach((day) => {
      const code = row.assignments[String(day.day)] || '';
      tr.appendChild(renderScheduleCell(editor, byCode, row.employee_name, day.day, code));
    });
    body.appendChild(tr);
  }

  function setupAddEmployeeModal(editor, options, days) {
    const trigger = editor.querySelector('[data-add-employee-trigger]');
    const modal = editor.querySelector('[data-add-employee-modal]');
    const form = editor.querySelector('[data-add-employee-form]');
    if (!trigger || !modal || !form || !editor.dataset.addEmployeeUrl) {
      return;
    }
    const dialog = modal.querySelector('[role="dialog"]');
    const employeeSelect = form.querySelector('select[name="employee_name"]');
    const submit = form.querySelector('button[type="submit"]');

    function close() {
      modal.hidden = true;
      trigger.focus({ preventScroll: true });
    }

    trigger.addEventListener('click', () => {
      if (employeeSelect && !employeeSelect.options.length) {
        setScheduleStatus(editor, 'Все активные сотрудники уже есть в текущем графике.', 'error');
        return;
      }
      modal.hidden = false;
      if (dialog) {
        dialog.focus({ preventScroll: true });
      }
    });
    modal.querySelectorAll('[data-add-employee-close]').forEach((button) => {
      button.addEventListener('click', close);
    });
    modal.addEventListener('click', (event) => {
      if (event.target.matches('[data-add-employee-backdrop]')) {
        close();
      }
    });
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const formData = new FormData(form);
      if (submit) {
        submit.disabled = true;
      }
      setScheduleStatus(editor, 'Добавляю сотрудника...', 'loading');
      try {
        const response = await fetch(editor.dataset.addEmployeeUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            sheet_name: editor.dataset.sheetName,
            employee_name: formData.get('employee_name'),
            fill_mode: formData.get('fill_mode') || 'empty'
          })
        });
        const payload = await readJsonResponse(response);
        if (!response.ok || !payload || !payload.ok) {
          const message = payload && payload.error && payload.error.message
            ? payload.error.message
            : 'Не удалось добавить сотрудника.';
          throw new Error(message);
        }
        appendScheduleRow(editor, options, days, payload.data.row);
        updateViolationView(editor, payload.data.schedule || {});
        clearAutoplanArtifact(editor, payload.data);
        updateValidationStopCells(editor, payload.data.schedule || {});
        if (employeeSelect && employeeSelect.selectedIndex >= 0) {
          employeeSelect.remove(employeeSelect.selectedIndex);
        }
        setScheduleStatus(editor, 'Сотрудник добавлен в график.', 'success');
        close();
      } catch (error) {
        setScheduleStatus(editor, error.message, 'error');
      } finally {
        if (submit) {
          submit.disabled = false;
        }
      }
    });
  }

  async function deleteScheduleEmployee(editor, button) {
    const employeeName = button.dataset.employeeName || '';
    if (!employeeName) {
      return;
    }
    const confirmed = window.confirm(`Удалить сотрудника ${employeeName} из текущего месяца?`);
    if (!confirmed) {
      return;
    }

    button.disabled = true;
    setScheduleStatus(editor, 'Удаляю сотрудника...', 'loading');
    try {
      const response = await fetch(editor.dataset.deleteEmployeeUrl, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          sheet_name: editor.dataset.sheetName,
          employee_name: employeeName
        })
      });
      const payload = await readJsonResponse(response);
      if (!response.ok || !payload || !payload.ok) {
        const message = payload && payload.error && payload.error.message
          ? payload.error.message
          : 'Не удалось удалить сотрудника.';
        throw new Error(message);
      }
      const row = button.closest('tr');
      if (row) {
        row.remove();
      }
      updateViolationView(editor, payload.data.schedule || {});
      clearAutoplanArtifact(editor, payload.data);
      updateValidationStopCells(editor, payload.data.schedule || {});
      setScheduleStatus(editor, 'Сотрудник удален из текущего месяца.', 'success');
    } catch (error) {
      button.disabled = false;
      setScheduleStatus(editor, error.message, 'error');
    }
  }

  function setupInlineToggle(triggerSelector, modalSelector, closeSelector, backdropSelector) {
    const trigger = document.querySelector(triggerSelector);
    const modal = document.querySelector(modalSelector);
    if (!trigger || !modal) {
      return;
    }
    const dialog = modal.querySelector('[role="dialog"]');

    function close() {
      modal.hidden = true;
      trigger.focus({ preventScroll: true });
    }

    trigger.addEventListener('click', () => {
      modal.hidden = false;
      if (dialog) {
        dialog.focus({ preventScroll: true });
      }
    });
    modal.querySelectorAll(closeSelector).forEach((button) => {
      button.addEventListener('click', close);
    });
    modal.addEventListener('click', (event) => {
      if (event.target.matches(backdropSelector)) {
        close();
      }
    });
  }

  function selectedCells(editor) {
    return Array.from(editor.querySelectorAll('[data-schedule-cell][data-selected="true"]'));
  }

  function selectedCellPayload(editor) {
    return selectedCells(editor).map((cell) => ({
      employee_name: cell.dataset.employeeName,
      day: Number(cell.dataset.day)
    }));
  }

  function scheduleCells(editor) {
    return Array.from(editor.querySelectorAll('[data-schedule-cell]'));
  }

  function setSelectionAnchor(editor, cell) {
    editor.dataset.selectionAnchorEmployee = cell.dataset.employeeName || '';
    editor.dataset.selectionAnchorDay = cell.dataset.day || '';
  }

  function selectionAnchor(editor) {
    const employeeName = editor.dataset.selectionAnchorEmployee || '';
    const day = Number(editor.dataset.selectionAnchorDay || 0);
    if (!employeeName || !day) {
      return null;
    }
    return findScheduleCell(editor, employeeName, day);
  }

  function cellPosition(cell) {
    const row = cell.closest('tr');
    if (!row || !row.parentElement) {
      return null;
    }
    const rowIndex = Array.from(row.parentElement.children).indexOf(row);
    const columnIndex = Array.from(row.querySelectorAll('[data-schedule-cell]')).indexOf(cell);
    if (rowIndex < 0 || columnIndex < 0) {
      return null;
    }
    return { rowIndex, columnIndex };
  }

  function cellsInRange(editor, startCell, endCell) {
    const start = cellPosition(startCell);
    const end = cellPosition(endCell);
    const body = editor.querySelector('[data-schedule-body]');
    if (!start || !end || !body) {
      return [];
    }
    const minRow = Math.min(start.rowIndex, end.rowIndex);
    const maxRow = Math.max(start.rowIndex, end.rowIndex);
    const minColumn = Math.min(start.columnIndex, end.columnIndex);
    const maxColumn = Math.max(start.columnIndex, end.columnIndex);
    return Array.from(body.querySelectorAll('tr')).slice(minRow, maxRow + 1).flatMap((row) => {
      return Array.from(row.querySelectorAll('[data-schedule-cell]')).slice(minColumn, maxColumn + 1);
    });
  }

  function setCellSelected(cell, selected) {
    if (selected) {
      cell.dataset.selected = 'true';
      cell.classList.add('selected-shift-cell');
      cell.setAttribute('aria-pressed', 'true');
    } else {
      delete cell.dataset.selected;
      cell.classList.remove('selected-shift-cell');
      cell.setAttribute('aria-pressed', 'false');
    }
  }

  function updateSelectionToolbar(editor) {
    const toolbar = editor.querySelector('[data-bulk-fill-toolbar]');
    const counter = editor.querySelector('[data-selection-count]');
    const count = selectedCells(editor).length;
    if (toolbar) {
      toolbar.hidden = count === 0;
    }
    if (counter) {
      counter.textContent = `Выбрано: ${count}`;
    }
  }

  function clearScheduleSelection(editor) {
    selectedCells(editor).forEach((cell) => setCellSelected(cell, false));
    delete editor.dataset.selectionAnchorEmployee;
    delete editor.dataset.selectionAnchorDay;
    updateSelectionToolbar(editor);
  }

  function selectScheduleRange(editor, startCell, endCell, additive) {
    if (!additive) {
      scheduleCells(editor).forEach((cell) => setCellSelected(cell, false));
    }
    cellsInRange(editor, startCell, endCell).forEach((cell) => setCellSelected(cell, true));
    setSelectionAnchor(editor, startCell);
    updateSelectionToolbar(editor);
  }

  function toggleScheduleCellSelection(editor, cell) {
    const selected = cell.dataset.selected === 'true';
    setCellSelected(cell, !selected);
    setSelectionAnchor(editor, cell);
    updateSelectionToolbar(editor);
  }

  function setupBulkFill(editor) {
    const clearButton = editor.querySelector('[data-clear-selection]');
    if (clearButton) {
      clearButton.addEventListener('click', () => clearScheduleSelection(editor));
    }
  }

  function setupExcelLikeSelection(editor) {
    let dragState = null;

    function applyDragSelection(state, endCell) {
      const range = new Set(cellsInRange(editor, state.startCell, endCell));
      scheduleCells(editor).forEach((cell) => {
        setCellSelected(cell, state.baseSelection.has(cell) || range.has(cell));
      });
      setSelectionAnchor(editor, state.startCell);
      updateSelectionToolbar(editor);
    }

    editor.addEventListener('pointerdown', (event) => {
      const cell = event.target.closest('[data-schedule-cell]');
      if (!cell || !editor.contains(cell) || event.button !== 0 || event.target.tagName === 'SELECT') {
        return;
      }
      if (event.shiftKey) {
        return;
      }
      const additive = event.ctrlKey || event.metaKey;
      dragState = {
        startCell: cell,
        lastCell: cell,
        moved: false,
        baseSelection: additive ? new Set(selectedCells(editor)) : new Set()
      };
    });

    editor.addEventListener('pointerover', (event) => {
      if (!dragState) {
        return;
      }
      const cell = event.target.closest('[data-schedule-cell]');
      if (!cell || !editor.contains(cell) || cell === dragState.lastCell) {
        return;
      }
      dragState.lastCell = cell;
      dragState.moved = true;
      applyDragSelection(dragState, cell);
    });

    window.addEventListener('pointerup', () => {
      if (!dragState) {
        return;
      }
      if (dragState.moved) {
        editor.dataset.suppressNextCellClick = 'true';
      }
      dragState = null;
    });

    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape' || !selectedCells(editor).length) {
        return;
      }
      event.preventDefault();
      clearScheduleSelection(editor);
      setScheduleStatus(editor, 'Выделение снято.', 'success');
    });
  }

  function openScheduleCellEditor(editor, cell, options, days) {
    if (cell.dataset.saving === 'true' || cell.querySelector('select')) {
      return;
    }
    closeActiveCellEditor(editor, false);
    const original = {
      code: cell.dataset.shiftCode || '',
      text: cell.textContent,
      className: cell.className,
      style: cell.style.cssText,
      title: cell.title
    };
    const select = makeShiftSelect(options, original.code);
    editor.activeCellEditor = { cell, original, select };
    cell.replaceChildren(select);
    select.focus();

    select.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') {
        return;
      }
      event.preventDefault();
      restoreScheduleCell(cell, original);
      if (editor.activeCellEditor && editor.activeCellEditor.cell === cell) {
        delete editor.activeCellEditor;
      }
      cell.focus({ preventScroll: true });
    });
    select.addEventListener('blur', () => {
      window.setTimeout(() => {
        if (cell.dataset.saving === 'true') {
          return;
        }
        if (editor.activeCellEditor && editor.activeCellEditor.cell === cell) {
          closeActiveCellEditor(editor, false);
        }
      }, 0);
    });
    select.addEventListener('change', () => saveScheduleCell(editor, cell, select, original, options, days));
  }

  function setupScheduleEditor(editor) {
    const options = parseJsonScript(editor, '[data-shift-options-json]');
    const days = parseJsonScript(editor, '[data-schedule-days-json]');
    if (!options.length || !editor.dataset.updateUrl || !editor.dataset.sheetName) {
      return;
    }
    editor.addEventListener('pointerdown', (event) => {
      const cell = event.target.closest('[data-schedule-cell]');
      if (cell && editor.contains(cell) && editor.activeCellEditor && editor.activeCellEditor.cell !== cell) {
        closeActiveCellEditor(editor, false);
      }
    }, true);
    editor.addEventListener('click', (event) => {
      const deleteButton = event.target.closest('[data-delete-schedule-employee]');
      if (deleteButton && editor.contains(deleteButton)) {
        deleteScheduleEmployee(editor, deleteButton);
        return;
      }
      const cell = event.target.closest('[data-schedule-cell]');
      if (!cell || !editor.contains(cell) || event.target.tagName === 'SELECT') {
        return;
      }
      if (editor.dataset.suppressNextCellClick === 'true') {
        delete editor.dataset.suppressNextCellClick;
        event.preventDefault();
        return;
      }
      if (event.shiftKey) {
        event.preventDefault();
        selectScheduleRange(editor, selectionAnchor(editor) || cell, cell, event.ctrlKey || event.metaKey);
        return;
      }
      if (event.ctrlKey || event.metaKey) {
        event.preventDefault();
        toggleScheduleCellSelection(editor, cell);
        return;
      }
      if (selectedCells(editor).length && cell.dataset.selected !== 'true') {
        clearScheduleSelection(editor);
      }
      openScheduleCellEditor(editor, cell, options, days);
    });
    editor.addEventListener('keydown', (event) => {
      const cell = event.target.closest('[data-schedule-cell]');
      if (!cell || !['Enter', ' '].includes(event.key)) {
        return;
      }
      event.preventDefault();
      openScheduleCellEditor(editor, cell, options, days);
    });
    setupAddEmployeeModal(editor, options, days);
    setupBulkFill(editor);
    setupExcelLikeSelection(editor);
    document.addEventListener('pointerdown', (event) => {
      if (editor.contains(event.target)) {
        return;
      }
      closeActiveCellEditor(editor, false);
    });
  }

  document.addEventListener('click', rememberModalTrigger);
  document.addEventListener('DOMContentLoaded', () => {
    restoreFocusAfterModalClose();
    setupBackdropClose();
    setupAutoSubmitForms();
    setupUserMessages();
    setupAutoplanProgressForms();
    document.querySelectorAll('[data-modal]').forEach(setupModal);
    document.querySelectorAll('[data-schedule-editor]').forEach(setupScheduleEditor);
    setupInlineToggle(
      '[data-file-panel-trigger]',
      '[data-file-panel-modal]',
      '[data-file-panel-close]',
      '[data-file-panel-backdrop]'
    );
    setupInlineToggle(
      '[data-create-month-trigger]',
      '[data-create-month-modal]',
      '[data-create-month-close]',
      '[data-create-month-backdrop]'
    );
    setupInlineToggle(
      '[data-copy-month-trigger]',
      '[data-copy-month-modal]',
      '[data-copy-month-close]',
      '[data-copy-month-backdrop]'
    );
  });
})();

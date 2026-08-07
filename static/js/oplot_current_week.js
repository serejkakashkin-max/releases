(() => {
  "use strict";

  const root = document.querySelector(".oplot-current-week__root");
  if (!root || root.dataset.oplotCurrentWeekInitialized === "1") return;
  root.dataset.oplotCurrentWeekInitialized = "1";

  const filterButtons = Array.from(root.querySelectorAll(".counter-filter"));
  const summaryButtons = Array.from(root.querySelectorAll(".summary-card-button"));
  const clearButton = root.querySelector("#clearReportFilter");
  const labelNode = root.querySelector("#activeFilterLabel");
  const showFinalRows = root.querySelector("#showFinalRows");
  const refreshButton = root.querySelector("#currentWeekRefresh");
  let currentType = "";
  let currentValue = "";

  function normalize(value) {
    return String(value || "").trim().toLowerCase();
  }

  function renumberVisibleRows(rows) {
    let visibleIndex = 1;
    rows.forEach((row) => {
      if (row.hidden) return;
      const numberCell = row.querySelector(".week-row-number");
      if (numberCell) numberCell.textContent = String(visibleIndex);
      visibleIndex += 1;
    });
  }

  function applyFilter() {
    const rows = Array.from(root.querySelectorAll("tbody tr[data-system], tbody tr[data-status]"));
    rows.forEach((row) => {
      const isFinal = row.dataset.final === "1";
      const finalAllowed = Boolean(showFinalRows && showFinalRows.checked)
        || (currentType === "summary" && currentValue === "installed")
        || (currentType === "summary" && currentValue === "all_with_final");

      if (isFinal && !finalAllowed) {
        row.hidden = true;
        return;
      }

      if (!currentType || !currentValue) {
        row.hidden = false;
        return;
      }

      const system = normalize(row.dataset.system);
      const status = normalize(row.dataset.status);
      const isReroll = row.dataset.reroll === "1";
      const isHotfix = row.dataset.hotfix === "1";
      let matched = false;

      if (currentType === "summary") {
        if (currentValue === "all") matched = row.dataset.final !== "1";
        else if (currentValue === "all_with_final") matched = true;
        else if (currentValue === "installed") matched = row.dataset.installed === "1";
        else if (currentValue === "reroll") matched = isReroll;
        else if (currentValue === "hotfix") matched = isHotfix;
      } else if (currentType === "system") {
        matched = system === normalize(currentValue);
      } else if (currentType === "status") {
        matched = status === normalize(currentValue);
      }

      row.hidden = !matched;
    });

    renumberVisibleRows(rows);

    if (!labelNode || !clearButton) return;
    if (!currentType || !currentValue) {
      labelNode.textContent = "не выбран";
      clearButton.hidden = true;
      return;
    }

    let suffix = "";
    if (currentType === "summary") {
      const summaryLabels = {
        all: " (предстоящие релизы)",
        all_with_final: " (включая финальные)",
        installed: " (установлен на ПРОМ)",
        reroll: " (перераскатки)",
        hotfix: " (хотфиксы)",
      };
      suffix = summaryLabels[currentValue] || "";
    } else if (currentType === "system") {
      suffix = " (система)";
    } else if (currentType === "status") {
      suffix = " (статус)";
    }
    labelNode.textContent = currentValue + suffix;
    clearButton.hidden = false;
  }

  filterButtons.forEach((button) => {
    button.addEventListener("click", () => {
      currentType = button.dataset.filterType || "";
      currentValue = button.dataset.filterValue || "";
      applyFilter();
    });
  });

  summaryButtons.forEach((button) => {
    button.addEventListener("click", () => {
      currentType = button.dataset.filterType || "";
      currentValue = button.dataset.filterValue || "";
      applyFilter();
    });
  });

  if (clearButton) {
    clearButton.addEventListener("click", () => {
      currentType = "";
      currentValue = "";
      applyFilter();
    });
  }

  if (showFinalRows) showFinalRows.addEventListener("change", applyFilter);
  if (refreshButton) refreshButton.addEventListener("click", () => window.location.reload());

  applyFilter();

  function millisecondsUntilNextMorningRefresh() {
    const now = new Date();
    const next = new Date(now);
    next.setHours(6, 30, 0, 0);
    if (next <= now) next.setDate(next.getDate() + 1);
    return next.getTime() - now.getTime();
  }

  window.setTimeout(() => window.location.reload(), millisecondsUntilNextMorningRefresh());
})();

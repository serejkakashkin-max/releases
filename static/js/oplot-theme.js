(function () {
  "use strict";

  var STORAGE_KEY = "theme";

  function normalizeTheme(value) {
    return value === "dark" ? "dark" : "light";
  }

  function readTheme() {
    try {
      return normalizeTheme(window.localStorage.getItem(STORAGE_KEY));
    } catch (error) {
      return "light";
    }
  }

  function applyTheme(theme, persist) {
    var value = normalizeTheme(theme);
    document.documentElement.setAttribute("data-theme", value);
    document.documentElement.setAttribute("data-bs-theme", value);
    if (persist) {
      try {
        window.localStorage.setItem(STORAGE_KEY, value);
      } catch (error) {
        // Storage can be unavailable in hardened or private browser contexts.
      }
    }
    return value;
  }

  window.OplotTheme = {
    apply: applyTheme,
    current: function () {
      return normalizeTheme(document.documentElement.getAttribute("data-theme"));
    },
    toggle: function () {
      return applyTheme(this.current() === "dark" ? "light" : "dark", true);
    }
  };

  applyTheme(readTheme(), false);
})();

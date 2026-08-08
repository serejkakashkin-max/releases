(function () {
  "use strict";

  var STORAGE_KEY = "theme";
  var transitionGeneration = 0;

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

  function suspendThemeTransitions() {
    var root = document.documentElement;
    var generation = ++transitionGeneration;
    root.classList.add("oplot-theme-switching");

    function release() {
      if (generation === transitionGeneration) {
        root.classList.remove("oplot-theme-switching");
      }
    }

    if (typeof window.requestAnimationFrame === "function") {
      window.requestAnimationFrame(function () {
        window.requestAnimationFrame(release);
      });
    } else {
      window.setTimeout(release, 0);
    }
  }

  function applyTheme(theme, persist) {
    var value = normalizeTheme(theme);
    var changed = document.documentElement.getAttribute("data-theme") !== value ||
      document.documentElement.getAttribute("data-bs-theme") !== value;
    if (persist && changed) {
      suspendThemeTransitions();
    }
    document.documentElement.setAttribute("data-theme", value);
    document.documentElement.setAttribute("data-bs-theme", value);
    if (persist) {
      try {
        window.localStorage.setItem(STORAGE_KEY, value);
      } catch (error) {
        // Storage can be unavailable in hardened or private browser contexts.
      }
    }
    try {
      document.dispatchEvent(new CustomEvent("oplot:themechange", { detail: { theme: value } }));
    } catch (error) {
      // CustomEvent can be unavailable in unusually restricted web views.
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

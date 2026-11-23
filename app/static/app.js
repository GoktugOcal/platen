// Theme (light / dark / system) and sidebar (expanded / collapsed / mobile open).
(function () {
  "use strict";
  const root = document.documentElement;
  const mobile = () => window.matchMedia("(max-width: 860px)").matches;

  function setTheme(theme) {
    if (theme === "system") {
      root.removeAttribute("data-theme");
      localStorage.removeItem("platen-theme");
    } else {
      root.setAttribute("data-theme", theme);
      localStorage.setItem("platen-theme", theme);
    }
    document.querySelectorAll("[data-theme-label]").forEach((el) => {
      el.textContent = theme === "system" ? "System theme" : theme === "dark" ? "Dark theme" : "Light theme";
    });
  }

  function currentTheme() {
    return localStorage.getItem("platen-theme") || "system";
  }

  document.addEventListener("click", (e) => {
    const themeBtn = e.target.closest("[data-action='theme']");
    if (themeBtn) {
      const order = ["system", "light", "dark"];
      setTheme(order[(order.indexOf(currentTheme()) + 1) % order.length]);
      return;
    }
    const sideBtn = e.target.closest("[data-action='sidebar']");
    if (sideBtn) {
      if (mobile()) {
        root.setAttribute("data-sidebar", root.getAttribute("data-sidebar") === "open" ? "closed" : "open");
      } else {
        const collapsed = root.getAttribute("data-sidebar") === "collapsed";
        root.setAttribute("data-sidebar", collapsed ? "expanded" : "collapsed");
        localStorage.setItem("platen-sidebar", collapsed ? "expanded" : "collapsed");
      }
      return;
    }
    if (e.target.closest(".scrim")) root.setAttribute("data-sidebar", "closed");
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "b" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      document.querySelector("[data-action='sidebar']")?.click();
    }
    if (e.key === "Escape" && root.getAttribute("data-sidebar") === "open") root.setAttribute("data-sidebar", "closed");
  });

  setTheme(currentTheme());

  // Small behaviours declared in the templates with data attributes, so the
  // Content-Security-Policy can forbid inline scripts and event handlers.
  document.addEventListener("change", (e) => {
    const el = e.target;
    if (el.matches("select[data-autosubmit]")) el.form.submit();
    else if (el.matches("select[data-navigate]")) {
      window.location = el.dataset.navigate + encodeURIComponent(el.value);
    } else if (el.matches("input[data-reveals]")) {
      const target = document.getElementById(el.dataset.reveals);
      if (target) target.hidden = !el.checked;
    }
  });
  document.addEventListener("submit", (e) => {
    const form = e.target;
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      e.preventDefault();
      return;
    }
    form.querySelectorAll("[data-busy]").forEach((b) => b.setAttribute("aria-busy", "true"));
  });
  document.addEventListener("click", (e) => {
    if (e.target.closest("a[data-back]") && window.history.length > 1) {
      e.preventDefault();
      window.history.back();
    }
  });
})();

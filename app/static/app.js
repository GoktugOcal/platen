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
})();

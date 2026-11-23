// Apply the saved theme and sidebar state before first paint (no flash).
// Loaded synchronously in <head>; everything else lives in app.js.
(function () {
  var root = document.documentElement;
  var t = localStorage.getItem("platen-theme");
  if (t === "light" || t === "dark") root.setAttribute("data-theme", t);
  if (localStorage.getItem("platen-sidebar") === "collapsed" && !matchMedia("(max-width: 860px)").matches)
    root.setAttribute("data-sidebar", "collapsed");
})();

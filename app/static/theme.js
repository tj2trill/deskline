/* Runs before first paint so a saved light/dark choice never flashes the other theme. */
(function () {
  try {
    var t = localStorage.getItem("dl-theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  } catch (e) { /* storage blocked: follow the OS setting */ }
})();

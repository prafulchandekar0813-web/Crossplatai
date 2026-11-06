// Runs before first paint so the saved theme never flashes. Kept as a separate file because the CSP forbids inline scripts.
try {
  const t = localStorage.getItem("workbench.theme");
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
} catch (e) { /* storage unavailable: follow the system theme */ }

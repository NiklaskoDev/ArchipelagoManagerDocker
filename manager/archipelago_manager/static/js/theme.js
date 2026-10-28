// The color theme chosen in the user menu (app.js), set before the page is drawn so it does not flash in the other one.
// Without a choice, the system's (style.css). A plain script in the <head>: modules only run after the page is drawn.
try {
  const theme = localStorage.getItem("apm-theme");
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
} catch { /* storage is blocked: the system's theme */ }

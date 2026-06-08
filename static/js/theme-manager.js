const ThemeManager = (() => {
  const STORAGE_KEY = "fuzzy_theme_settings";
  const defaults = {
    mode: "light",
    accent: "#2563eb",
    customAccent: "",
    cardStyle: "elevated",
    density: "comfortable",
  };

  let settings = { ...defaults };
  let listenersAttached = false;

  const body = document.body;
  const panel = document.getElementById("theme-panel");
  const btnOpen = document.getElementById("theme-customizer-btn");
  const btnReset = document.getElementById("theme-reset-btn");
  const modeButtons = Array.from(document.querySelectorAll("[data-theme-mode]"));
  const accentButtons = Array.from(document.querySelectorAll("[data-theme-accent]"));
  const cardButtons = Array.from(document.querySelectorAll("[data-theme-card]"));
  const densityButtons = Array.from(document.querySelectorAll("[data-theme-density]"));
  const customAccentInput = document.getElementById("theme-custom-accent");

  function parseSettings(raw) {
    if (!raw) return { ...defaults }; 
    try {
      const parsed = JSON.parse(raw);
      return {
        ...defaults,
        ...parsed,
      };
    } catch {
      return { ...defaults };
    }
  }

  function loadSettings() {
    const stored = localStorage.getItem(STORAGE_KEY);
    return parseSettings(stored);
  }

  function saveSettings() {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
  }

  function toHex(value) {
    if (!value) return defaults.accent;
    const hex = String(value).trim();
    if (/^#[0-9A-Fa-f]{3,6}$/.test(hex)) return hex;
    return defaults.accent;
  }

  function hexToRgba(hex, alpha) {
    const normalized = hex.replace("#", "");
    const short = normalized.length === 3;
    const r = parseInt(short ? normalized[0] + normalized[0] : normalized.slice(0, 2), 16);
    const g = parseInt(short ? normalized[1] + normalized[1] : normalized.slice(2, 4), 16);
    const b = parseInt(short ? normalized[2] + normalized[2] : normalized.slice(4, 6), 16);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }

  function applyMode(mode) {
    body.classList.remove("theme-light", "theme-dark", "theme-system");
    body.classList.add(`theme-${mode}`);
  }

  function applyCardStyle(cardStyle) {
    body.classList.remove("card-flat", "card-elevated", "card-glass");
    body.classList.add(`card-${cardStyle}`);
  }

  function applyDensity(density) {
    body.classList.remove("density-compact", "density-comfortable", "density-spacious");
    body.classList.add(`density-${density}`);
  }

  function applyAccent(accent) {
    const safeAccent = toHex(accent || settings.accent);
    body.style.setProperty("--accent", safeAccent);
    body.style.setProperty("--accent-strong", hexToRgba(safeAccent, 0.88));
    body.style.setProperty("--accent-soft", hexToRgba(safeAccent, 0.14));
    body.style.setProperty("--accent-muted", hexToRgba(safeAccent, 0.18));
  }

  function updateActiveControls() {
    modeButtons.forEach(button => {
      button.classList.toggle("active", button.dataset.themeMode === settings.mode);
    });
    accentButtons.forEach(button => {
      button.classList.toggle("active", button.dataset.themeAccent === settings.accent && !settings.customAccent);
    });
    cardButtons.forEach(button => {
      button.classList.toggle("active", button.dataset.themeCard === settings.cardStyle);
    });
    densityButtons.forEach(button => {
      button.classList.toggle("active", button.dataset.themeDensity === settings.density);
    });
    if (customAccentInput) {
      customAccentInput.value = settings.customAccent || settings.accent;
    }
  }

  function applySettings(newSettings = {}) {
    settings = { ...settings, ...newSettings };
    if (!settings.mode) settings.mode = defaults.mode;
    if (!settings.accent) settings.accent = defaults.accent;
    if (!settings.cardStyle) settings.cardStyle = defaults.cardStyle;
    if (!settings.density) settings.density = defaults.density;
    applyMode(settings.mode);
    applyCardStyle(settings.cardStyle);
    applyDensity(settings.density);
    applyAccent(settings.customAccent || settings.accent);
    updateActiveControls();
    saveSettings();
  }

  function togglePanel() {
    if (!panel) return;
    panel.classList.toggle("open");
    panel.setAttribute("aria-hidden", String(!panel.classList.contains("open")));
  }

  function openPanel() {
    if (!panel) return;
    panel.classList.add("open");
    panel.setAttribute("aria-hidden", "false");
  }

  function closePanel() {
    if (!panel) return;
    panel.classList.remove("open");
    panel.setAttribute("aria-hidden", "true");
  }

  function resetDefaults() {
    settings = { ...defaults };
    applySettings(settings);
  }

  function handleButtonClick(e) {
    const target = e.currentTarget;
    if (target.dataset.themeMode) {
      applySettings({ mode: target.dataset.themeMode });
      return;
    }
    if (target.dataset.themeAccent) {
      applySettings({ accent: target.dataset.themeAccent, customAccent: "" });
      return;
    }
    if (target.dataset.themeCard) {
      applySettings({ cardStyle: target.dataset.themeCard });
      return;
    }
    if (target.dataset.themeDensity) {
      applySettings({ density: target.dataset.themeDensity });
    }
  }

  function attachListeners() {
    if (listenersAttached) return;
    listenersAttached = true;

    if (btnOpen) btnOpen.addEventListener("click", openPanel);
    if (btnReset) btnReset.addEventListener("click", resetDefaults);
    if (modeButtons.length) modeButtons.forEach(button => {
      button.addEventListener("click", handleButtonClick);
    });
    if (accentButtons.length) accentButtons.forEach(button => {
      button.addEventListener("click", handleButtonClick);
    });
    if (cardButtons.length) cardButtons.forEach(button => {
      button.addEventListener("click", handleButtonClick);
    });
    if (densityButtons.length) densityButtons.forEach(button => {
      button.addEventListener("click", handleButtonClick);
    });
    if (customAccentInput) {
      customAccentInput.addEventListener("input", event => {
        applySettings({ customAccent: event.target.value });
      });
    }
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && panel?.classList.contains("open")) {
        closePanel();
      }
    });
  }

  function init() {
    settings = loadSettings();
    applySettings(settings);
    attachListeners();
  }

  window.ThemeManager = {
    init,
    openPanel,
    closePanel,
    togglePanel,
    resetDefaults,
    applySettings,
  };

  return window.ThemeManager;
})();

document.addEventListener("DOMContentLoaded", () => {
  if (window.ThemeManager) {
    window.ThemeManager.init();
  }
});

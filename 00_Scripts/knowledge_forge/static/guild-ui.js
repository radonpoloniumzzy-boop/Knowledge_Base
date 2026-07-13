(() => {
  "use strict";

  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  function initSharedUi() {
    document.documentElement.dataset.motion = reducedMotion.matches ? "reduced" : "full";

    const activeNavItem = document.querySelector(".nav a.active");
    const mainNav = document.querySelector(".nav");
    if (activeNavItem && mainNav && window.matchMedia("(max-width: 760px)").matches) {
      mainNav.scrollLeft = Math.max(
        0,
        activeNavItem.offsetLeft - (mainNav.clientWidth - activeNavItem.offsetWidth) / 2,
      );
    }

    document.addEventListener("submit", (event) => {
      if (event.defaultPrevented) return;
      const button = event.target.querySelector("button[type='submit']");
      if (!button || button.disabled) return;
      window.setTimeout(() => {
        button.disabled = true;
        button.setAttribute("aria-busy", "true");
      }, 0);
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        document.dispatchEvent(new CustomEvent("guild:collapse"));
      }
    });
  }

  window.KnowledgeForgeUi = {
    reducedMotion,
    icon(name, className = "guild-icon") {
      return `<svg class="${className}" aria-hidden="true"><use href="/static/guild-icons.svg#${name}"></use></svg>`;
    },
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initSharedUi, { once: true });
  } else {
    initSharedUi();
  }
})();

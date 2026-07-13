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

    if (document.body.dataset.page === "dashboard") initDashboard();
    if (document.body.dataset.page === "ingest") initIngest();
    if (document.body.dataset.page === "packs") initPacks();
  }

  function initPacks() {
    document.querySelectorAll("[data-tag-picker]").forEach((picker) => {
      const tabs = [...picker.querySelectorAll(".tag-tab")];
      const panels = [...picker.querySelectorAll(".tag-panel")];
      const checks = [...picker.querySelectorAll("input[name='selected_tags']")];
      const summary = picker.querySelector("[data-selected-summary]");
      const activate = (top) => {
        tabs.forEach((tab) => tab.classList.toggle("active", tab.dataset.top === top));
        panels.forEach((panel) => panel.classList.toggle("active", panel.dataset.panel === top));
      };
      const updateSummary = () => {
        const selected = checks.filter((item) => item.checked).map((item) => item.dataset.label || item.value);
        if (!selected.length) summary.textContent = "未选择标签";
        else {
          const preview = selected.slice(0, 4).join("、");
          summary.textContent = selected.length > 4 ? `${preview} 等 ${selected.length} 个标签` : preview;
        }
      };
      tabs.forEach((tab) => tab.addEventListener("click", () => activate(tab.dataset.top)));
      checks.forEach((check) => check.addEventListener("change", updateSummary));
      picker.querySelector("[data-action='clear-tags']")?.addEventListener("click", () => {
        checks.forEach((check) => { check.checked = false; });
        updateSummary();
      });
      updateSummary();
    });
  }

  function initIngest() {
    const forge = document.querySelector("[data-alchemy-forge]");
    const queueHost = document.getElementById("job-queue");
    const refreshStatus = document.getElementById("job-refresh-status");
    const form = document.querySelector("[data-upload-form]");
    const input = form?.querySelector("[data-file-input]");
    const dropzone = form?.querySelector("[data-dropzone]");
    const selection = form?.querySelector("[data-file-selection]");
    if (!forge || !queueHost || !form || !input || !dropzone) return;

    const labels = {
      idle: "炉火待命",
      drag: "炉火已增强，松手即可接收",
      waiting: "预热中，等待炼制",
      processing: "知识正在炼制",
      paused: "炉火收束为余烬",
      needs_attention: "炉火已停止，请处理异常",
      completed: "炼制完成，正在冷却",
    };
    let lastServerState = null;
    let coolingTimer = null;

    function setForgeState(state) {
      forge.dataset.forgeState = state;
      const label = forge.querySelector("[data-forge-label]");
      if (label) label.textContent = labels[state] || labels.idle;
    }

    function syncForgeState() {
      const fragment = queueHost.querySelector("[data-job-queue]");
      const serverState = fragment?.dataset.queueState || "idle";
      window.clearTimeout(coolingTimer);
      if (serverState === "completed") {
        if (lastServerState !== "completed") {
          setForgeState("completed");
          coolingTimer = window.setTimeout(() => setForgeState("idle"), 1600);
        } else {
          setForgeState("idle");
        }
      } else {
        setForgeState(serverState);
      }
      lastServerState = serverState;
    }

    function showSelection() {
      const count = input.files?.length || 0;
      if (selection) selection.textContent = count ? `已选择 ${count} 个文件` : "尚未选择文件";
      if (count) setForgeState("waiting");
    }

    ["dragenter", "dragover"].forEach((name) => dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      setForgeState("drag");
      dropzone.classList.add("is-dragging");
    }));
    dropzone.addEventListener("dragleave", () => {
      dropzone.classList.remove("is-dragging");
      window.setTimeout(() => input.files?.length ? setForgeState("waiting") : syncForgeState(), 0);
    });
    dropzone.addEventListener("drop", (event) => {
      event.preventDefault();
      dropzone.classList.remove("is-dragging");
      if (event.dataTransfer?.files?.length) input.files = event.dataTransfer.files;
      showSelection();
    });
    input.addEventListener("change", showSelection);

    async function refreshJobs() {
      try {
        const response = await fetch("/ingest/jobs", { cache: "no-store" });
        if (!response.ok) throw new Error("refresh failed");
        queueHost.innerHTML = await response.text();
        refreshStatus.textContent = "刚刚更新";
        syncForgeState();
      } catch (_error) {
        refreshStatus.textContent = "更新失败，正在重试";
      }
    }

    syncForgeState();
    window.setInterval(refreshJobs, 2000);
  }

  function initDashboard() {
    const panel = document.querySelector("[data-constellation]");
    const map = panel?.querySelector("[data-constellation-map]");
    const lineLayer = panel?.querySelector("[data-constellation-lines]");
    const roots = [...(panel?.querySelectorAll("[data-root]") || [])];
    if (!panel || !map || !lineLayer || !roots.length) return;

    let lockedRoot = null;
    let activeRoot = null;
    const activeLink = panel.querySelector("[data-constellation-link]");

    function drawLines(root) {
      lineLayer.replaceChildren();
      if (!root) return;
      const mapBox = map.getBoundingClientRect();
      const rootBox = root.getBoundingClientRect();
      const rootX = rootBox.left + rootBox.width / 2 - mapBox.left;
      const rootY = rootBox.top + rootBox.height / 2 - mapBox.top;
      root.closest("[data-root-wrap]").querySelectorAll("[data-child]").forEach((child) => {
        const childBox = child.getBoundingClientRect();
        const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
        line.setAttribute("x1", rootX);
        line.setAttribute("y1", rootY);
        line.setAttribute("x2", childBox.left + childBox.width / 2 - mapBox.left);
        line.setAttribute("y2", childBox.top + childBox.height / 2 - mapBox.top);
        lineLayer.append(line);
      });
    }

    function placeChildren(root) {
      const mapBox = map.getBoundingClientRect();
      const rootBox = root.getBoundingClientRect();
      const rootX = rootBox.left + rootBox.width / 2 - mapBox.left;
      const rootY = rootBox.top + rootBox.height / 2 - mapBox.top;
      const towardCenter = Math.atan2(mapBox.height / 2 - rootY, mapBox.width / 2 - rootX);
      const children = [...root.closest("[data-root-wrap]").querySelectorAll("[data-child]")];
      children.forEach((child, index) => {
        const spread = children.length === 1 ? 0 : (index / (children.length - 1) - 0.5) * 1.45;
        const radius = Math.min(150, Math.max(104, mapBox.width * 0.18));
        const x = rootX + Math.cos(towardCenter + spread) * radius;
        const y = rootY + Math.sin(towardCenter + spread) * radius;
        child.style.left = `${Math.max(54, Math.min(mapBox.width - 54, x)) - rootX}px`;
        child.style.top = `${Math.max(38, Math.min(mapBox.height - 38, y)) - rootY}px`;
      });
    }

    function activate(root, locked = false) {
      activeRoot = root;
      panel.classList.add("is-expanded");
      roots.forEach((item) => {
        const selected = item === root;
        item.setAttribute("aria-expanded", String(selected));
        item.closest("[data-root-wrap]").classList.toggle("is-active", selected);
      });
      activeLink.textContent = `查看${root.dataset.label}知识`;
      activeLink.href = root.dataset.url;
      if (locked) lockedRoot = root;
      placeChildren(root);
      requestAnimationFrame(() => drawLines(root));
    }

    function collapse(force = false) {
      if (lockedRoot && !force) return;
      lockedRoot = null;
      activeRoot = null;
      panel.classList.remove("is-expanded");
      roots.forEach((root) => {
        root.setAttribute("aria-expanded", "false");
        root.closest("[data-root-wrap]").classList.remove("is-active");
      });
      activeLink.textContent = "查看全部知识";
      activeLink.href = "/library";
      lineLayer.replaceChildren();
    }

    roots.forEach((root) => {
      root.addEventListener("pointerenter", () => { if (!lockedRoot) activate(root); });
      root.addEventListener("focus", () => { if (!lockedRoot) activate(root); });
      root.addEventListener("click", () => {
        if (lockedRoot === root) collapse(true);
        else activate(root, true);
      });
    });
    map.addEventListener("pointerleave", () => collapse());
    map.addEventListener("click", (event) => {
      if (event.target === map || event.target.classList.contains("star-field")) collapse(true);
    });
    panel.querySelector("[data-constellation-collapse]")?.addEventListener("click", () => collapse(true));
    document.addEventListener("guild:collapse", () => collapse(true));
    window.addEventListener("resize", () => { if (activeRoot) { placeChildren(activeRoot); drawLines(activeRoot); } });
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

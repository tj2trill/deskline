/* Deskline client: soft navigation + small enhancements. Everything works without JS;
   this only makes it smoother (no full reloads, kept scroll, busy states, toasts). */
(() => {
  "use strict";
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const STANDALONE = /^\/(s|p|u|widget|static|webhooks|api|proposals)\//; // pages with their own layout or non-HTML
  const prefetchCache = new Map(); // url -> {at, promise}
  let inflight = null;
  let timers = [];

  /* ---------- progress bar ---------- */
  const progress = {
    t: null,
    start() {
      const bar = document.querySelector(".progress");
      if (!bar) return;
      clearTimeout(this.t);
      this.t = setTimeout(() => { bar.classList.add("on"); bar.style.width = "35%";
        this.t = setTimeout(() => (bar.style.width = "75%"), 400); }, 120);
    },
    done() {
      clearTimeout(this.t);
      const bar = document.querySelector(".progress");
      if (!bar || !bar.classList.contains("on")) return;
      bar.style.width = "100%";
      setTimeout(() => { bar.classList.remove("on"); bar.style.width = "0"; }, 220);
    },
  };

  /* ---------- navigation ---------- */
  const eligible = (url) => url.origin === location.origin && !STANDALONE.test(url.pathname) && !/\.(csv|js|css|png|jpg|svg|pdf)$/i.test(url.pathname);

  async function fetchPage(url, init = {}) {
    const res = await fetch(url, { credentials: "same-origin", redirect: "follow", ...init,
      headers: { "X-Requested-With": "deskline", ...(init.headers || {}) } });
    const type = res.headers.get("content-type") || "";
    const body = type.includes("text/html") ? await res.text() : null;
    let detail = null;
    if (!body && type.includes("json")) { try { detail = (await res.json()).detail; } catch (_) { /* ignore */ } }
    return { res, body, detail, url: new URL(res.url) };
  }

  function prefetch(href) {
    const url = new URL(href, location.href);
    if (!eligible(url) || url.href === location.href) return;
    const hit = prefetchCache.get(url.href);
    if (hit && Date.now() - hit.at < 10000) return;
    prefetchCache.set(url.href, { at: Date.now(), promise: fetchPage(url).catch(() => null) });
  }

  async function visit(href, { method = "GET", body = null, keepScroll = false, push = true, restoreY = null } = {}) {
    const url = new URL(href, location.href);
    const fromPath = location.pathname + location.search;
    const y = scrollY;
    inflight = url.href;
    progress.start();
    let page;
    try {
      const cached = method === "GET" && prefetchCache.get(url.href);
      page = cached && Date.now() - cached.at < 10000 ? await cached.promise : null;
      if (!page) page = await fetchPage(url, { method, body });
    } catch (err) {
      progress.done();
      location.href = url.href; // network trouble: let the browser handle it
      return;
    } finally {
      prefetchCache.clear();
    }
    if (inflight !== url.href) return; // a newer navigation won
    progress.done();
    if (!page || !page.body) {
      if (page && page.detail) return toast(String(page.detail), "bad");
      location.href = (page && page.url.href) || url.href;
      return;
    }
    const doc = new DOMParser().parseFromString(page.body, "text/html");
    const landed = page.url.pathname + page.url.search;
    const samePage = landed === fromPath || (method !== "GET" && page.url.pathname === location.pathname);
    const swap = () => {
      clearTimers();
      document.title = doc.title;
      const next = document.importNode(doc.body, true);
      document.body.replaceWith(next);
      hideTip();
      if (push) {
        if (samePage) history.replaceState({ y: 0 }, "", page.url.href);
        else { history.replaceState({ y }, ""); history.pushState({ y: 0 }, "", page.url.href); }
      }
      shownPath = location.pathname + location.search;
      runScripts(document.body);
      hydrate(document);
      if (restoreY !== null) scrollTo(0, restoreY);
      else if ((keepScroll || method !== "GET") && samePage) scrollTo(0, y);
      else if (page.url.hash) document.querySelector(page.url.hash)?.scrollIntoView();
      else { scrollTo(0, 0); document.getElementById("main")?.focus({ preventScroll: true }); }
    };
    if (document.startViewTransition && !reduceMotion) document.startViewTransition(swap);
    else swap();
  }

  function runScripts(root) {
    root.querySelectorAll("script").forEach((old) => {
      if (old.src && document.head.querySelector(`script[src="${old.getAttribute("src")}"]`)) return;
      const s = document.createElement("script");
      for (const a of old.attributes) s.setAttribute(a.name, a.value);
      s.textContent = old.textContent;
      old.replaceWith(s);
    });
  }

  function clearTimers() { timers.forEach(clearInterval); timers = []; }

  /* ---------- link + form interception ---------- */
  document.addEventListener("click", (e) => {
    const toggle = e.target.closest("[data-nav-toggle]");
    if (toggle) { setNav(!document.body.classList.contains("nav-open")); return; }
    if (e.target.closest("[data-theme-toggle]")) { toggleTheme(); return; }
    if (e.target.closest("[data-cmdk-open]")) { cmdk.open(); return; }
    if (e.target.closest("[data-print]")) { window.print(); return; }
    const copy = e.target.closest("[data-copy]");
    if (copy) { copyText(copy); return; }
    const close = e.target.closest(".toast-close");
    if (close) { dismiss(close.closest(".toast")); return; }
    const row = e.target.closest("tr[data-href]");
    if (row && !e.target.closest("a,button,input,select,textarea,form")) { visit(row.dataset.href); return; }
    const a = e.target.closest("a[href]");
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    if ((a.target && a.target !== "_self") || a.hasAttribute("download") || a.closest("[data-hard]")) return;
    const url = new URL(a.href, location.href);
    if (!eligible(url)) return;
    if (url.hash && url.pathname === location.pathname && url.search === location.search) return;
    e.preventDefault();
    visit(url.href);
  });

  document.addEventListener("pointerover", (e) => {
    const a = e.target.closest("a[data-prefetch], .tabs a");
    if (a) prefetch(a.href);
  }, { passive: true });

  document.addEventListener("submit", async (e) => {
    const form = e.target;
    if (e.defaultPrevented || form.method === "dialog") return;
    if (form.dataset.confirm && !form.dataset.confirmed) {
      e.preventDefault();
      if (await confirmDialog(form.dataset.confirm, form.dataset.confirmTitle, form.dataset.confirmOk)) {
        form.dataset.confirmed = "1";
        form.requestSubmit(e.submitter || undefined);
      }
      return;
    }
    delete form.dataset.confirmed;
    const action = new URL((e.submitter && e.submitter.getAttribute("formaction")) || form.getAttribute("action") || location.href, location.href);
    if (form.closest("[data-hard]") || form.target === "_blank" || !eligible(action)) return;
    e.preventDefault();
    const data = new FormData(form, e.submitter || undefined);
    const busy = e.submitter || form.querySelector("button[type=submit],button:not([type])");
    if (busy) { busy.setAttribute("aria-busy", "true"); busy.disabled = true; }
    if (form.method.toLowerCase() === "get") {
      action.search = new URLSearchParams(data).toString();
      await visit(action.href);
    } else {
      await visit(action.href, { method: "POST", body: data });
    }
    if (busy && busy.isConnected) { busy.removeAttribute("aria-busy"); busy.disabled = false; }
  });

  /* ---------- mobile drawer ---------- */
  function setNav(open) {
    document.body.classList.toggle("nav-open", open);
    document.querySelectorAll("button[data-nav-toggle]").forEach((b) => b.setAttribute("aria-expanded", String(open)));
    if (open) document.querySelector(".rail a")?.focus({ preventScroll: true });
  }

  /* ---------- theme ---------- */
  const isDark = () => {
    const forced = document.documentElement.dataset.theme;
    return forced ? forced === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  };
  function paintThemeToggle(root = document) {
    root.querySelectorAll("[data-theme-toggle] use").forEach((u) => u.setAttribute("href", isDark() ? "#i-sun" : "#i-moon"));
    root.querySelectorAll("[data-theme-toggle]").forEach((b) => b.setAttribute("aria-pressed", String(isDark())));
  }
  function toggleTheme() {
    const next = isDark() ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("dl-theme", next); } catch (_) { /* private mode: lasts for this tab */ }
    paintThemeToggle();
  }

  /* ---------- command palette (Ctrl/Cmd+K) ---------- */
  const cmdk = {
    items: [], active: 0, seq: 0, timer: null,
    el() { return document.getElementById("cmdk"); },
    open() {
      const dlg = this.el();
      if (!dlg || !dlg.showModal || dlg.open) return;
      setNav(false);
      dlg.showModal();
      const input = dlg.querySelector("input");
      input.value = "";
      this.render("");
      input.focus();
    },
    close() { const dlg = this.el(); if (dlg && dlg.open) dlg.close(); },
    pages() {
      return [...document.querySelectorAll(".rail .nav a")].map((a) => ({
        label: a.querySelector("span")?.textContent.trim() || a.textContent.trim(), href: a.getAttribute("href"),
        icon: a.dataset.icon || "list", group: "Pages" }));
    },
    actions() {
      if (!this.el()?.dataset.search) return [];
      return [{ label: "New client", href: "/clients/new", icon: "plus", group: "Actions" },
              { label: "Find leads", href: "/leads", icon: "search", group: "Actions" }];
    },
    render(q, remote = []) {
      const dlg = this.el(); if (!dlg) return;
      const list = dlg.querySelector(".cmdk-list");
      const needle = q.trim().toLowerCase();
      const local = [...this.actions(), ...this.pages()].filter((i) => !needle || i.label.toLowerCase().includes(needle));
      this.items = [...remote, ...local];
      this.active = 0;
      list.textContent = "";
      let group = null;
      this.items.forEach((item, i) => {
        if (item.group !== group) {
          group = item.group;
          const g = document.createElement("li"); g.className = "group"; g.setAttribute("role", "presentation"); g.textContent = group;
          list.appendChild(g);
        }
        const li = document.createElement("li");
        li.setAttribute("role", "option"); li.id = "cmdk-o-" + i; li.dataset.i = i;
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"); svg.setAttribute("class", "icon"); svg.setAttribute("aria-hidden", "true");
        const use = document.createElementNS("http://www.w3.org/2000/svg", "use"); use.setAttribute("href", "#i-" + (item.icon || "list")); svg.appendChild(use);
        const label = document.createElement("span"); label.className = "ellipsis"; label.textContent = item.label;
        li.append(svg, label);
        if (item.hint) { const sm = document.createElement("small"); sm.textContent = item.hint; li.appendChild(sm); }
        list.appendChild(li);
      });
      if (!this.items.length) {
        const none = document.createElement("li"); none.className = "none"; none.setAttribute("role", "presentation");
        none.textContent = needle ? `Nothing matches "${q.trim()}".` : "Type to search.";
        list.appendChild(none);
      }
      this.select(0);
    },
    select(i) {
      const dlg = this.el(); if (!dlg || !this.items.length) { dlg?.querySelector("input")?.removeAttribute("aria-activedescendant"); return; }
      this.active = (i + this.items.length) % this.items.length;
      dlg.querySelectorAll("[role=option]").forEach((o) => o.setAttribute("aria-selected", String(Number(o.dataset.i) === this.active)));
      const cur = dlg.querySelector(`#cmdk-o-${this.active}`);
      cur?.scrollIntoView({ block: "nearest" });
      dlg.querySelector("input").setAttribute("aria-activedescendant", "cmdk-o-" + this.active);
    },
    go(i = this.active) {
      const item = this.items[i]; if (!item) return;
      this.close();
      visit(item.href);
    },
    search(q) {
      const url = this.el()?.dataset.search;
      this.render(q);
      clearTimeout(this.timer);
      if (!url || q.trim().length < 2) return;
      const seq = ++this.seq;
      this.timer = setTimeout(async () => {
        try {
          const r = await fetch(url + "?q=" + encodeURIComponent(q.trim()), { credentials: "same-origin", headers: { Accept: "application/json" } });
          if (!r.ok || seq !== this.seq) return;
          const data = await r.json();
          if (seq === this.seq && this.el()?.open) this.render(q, data.results || []);
        } catch (_) { /* offline: page links still work */ }
      }, 140);
    },
  };
  document.addEventListener("input", (e) => { if (e.target.closest("#cmdk")) cmdk.search(e.target.value); });
  document.addEventListener("click", (e) => {
    const opt = e.target.closest("#cmdk [role=option]");
    if (opt) cmdk.go(Number(opt.dataset.i));
    else if (e.target.id === "cmdk") cmdk.close(); // click on the backdrop
  });
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "k") { e.preventDefault(); cmdk.open(); return; }
    if (e.key === "Escape" && document.body.classList.contains("nav-open")) { setNav(false); document.querySelector("button[data-nav-toggle]")?.focus(); return; }
    if (e.target.closest && e.target.closest("#cmdk")) {
      if (e.key === "ArrowDown") { e.preventDefault(); cmdk.select(cmdk.active + 1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); cmdk.select(cmdk.active - 1); }
      else if (e.key === "Enter") { e.preventDefault(); cmdk.go(); }
      return;
    }
    const day = e.target.closest && e.target.closest(".chart-bars .day");
    if (day && /^(ArrowLeft|ArrowRight|Home|End)$/.test(e.key)) {
      const all = [...day.parentElement.children];
      const next = e.key === "Home" ? all[0] : e.key === "End" ? all[all.length - 1]
        : all[Math.min(all.length - 1, Math.max(0, all.indexOf(day) + (e.key === "ArrowRight" ? 1 : -1)))];
      e.preventDefault(); all.forEach((d) => (d.tabIndex = d === next ? 0 : -1)); next.focus();
      return;
    }
    if (e.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) && !e.target.isContentEditable && !e.metaKey && !e.ctrlKey) { e.preventDefault(); cmdk.open(); }
  });

  /* ---------- chart tooltips ---------- */
  let tipEl = null;
  function showTip(target) {
    if (!tipEl) { tipEl = document.createElement("div"); tipEl.className = "tip"; tipEl.setAttribute("role", "tooltip"); }
    if (!tipEl.isConnected) document.body.appendChild(tipEl);
    tipEl.textContent = "";
    const b = document.createElement("b"); b.textContent = target.dataset.tip;
    tipEl.appendChild(b);
    if (target.dataset.tipSub) tipEl.appendChild(document.createTextNode(target.dataset.tipSub));
    const r = target.getBoundingClientRect();
    const bar = target.querySelector("i")?.getBoundingClientRect() || r;
    const x = Math.min(Math.max(r.left + r.width / 2, 90), innerWidth - 90);
    tipEl.style.left = x + "px";
    tipEl.style.top = Math.max(bar.top, 60) + "px";
    tipEl.classList.add("on");
  }
  const hideTip = () => tipEl && tipEl.classList.remove("on");
  document.addEventListener("pointerover", (e) => { const t = e.target.closest?.("[data-tip]"); if (t) showTip(t); else hideTip(); }, { passive: true });
  document.addEventListener("focusin", (e) => { const t = e.target.closest?.("[data-tip]"); if (t) showTip(t); else hideTip(); });
  document.addEventListener("pointerdown", (e) => { if (!e.target.closest?.("[data-tip]")) hideTip(); }, { passive: true });
  addEventListener("scroll", () => { hideTip(); document.body.classList.toggle("scrolled", scrollY > 4); }, { passive: true });

  // popstate also fires for in-page #anchors (the skip link). Only reload when the page itself changed.
  let shownPath = location.pathname + location.search;
  addEventListener("popstate", (e) => {
    const now = location.pathname + location.search;
    if (now === shownPath) return;
    visit(location.href, { push: false, restoreY: (e.state && e.state.y) || 0 });
  });

  /* ---------- confirm dialog ---------- */
  function confirmDialog(text, title, okText) {
    const dlg = document.getElementById("confirm-dialog");
    if (!dlg || !dlg.showModal) return Promise.resolve(window.confirm(text));
    dlg.querySelector("[data-confirm-text]").textContent = text;
    dlg.querySelector("[data-confirm-title]").textContent = title || "Are you sure?";
    dlg.querySelector("[data-confirm-ok]").textContent = okText || "Confirm";
    dlg.returnValue = "";
    dlg.showModal();
    return new Promise((resolve) => dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true }));
  }

  /* ---------- toasts ---------- */
  function dismiss(el) {
    if (!el || el.classList.contains("leaving")) return;
    el.classList.add("leaving");
    setTimeout(() => el.remove(), reduceMotion ? 0 : 220);
  }
  function armToast(el) {
    if (el.dataset.armed) return;
    el.dataset.armed = "1";
    if (el.dataset.tone !== "bad") setTimeout(() => dismiss(el), 4500);
  }
  function toast(message, tone = "good") {
    const box = document.querySelector(".toasts");
    if (!box) return alert(message);
    const el = document.createElement("div");
    el.className = "toast";
    el.dataset.tone = tone;
    el.innerHTML = `<svg class="icon" aria-hidden="true"><use href="#i-${tone === "bad" ? "alert" : "check"}"/></svg><span></span>
      <button class="toast-close" type="button" aria-label="Dismiss"><svg class="icon" aria-hidden="true"><use href="#i-x"/></svg></button>`;
    el.querySelector("span").textContent = message;
    box.appendChild(el);
    armToast(el);
  }

  /* ---------- copy ---------- */
  async function copyText(btn) {
    const text = btn.dataset.copy || btn.closest(".codebox")?.innerText || "";
    try { await navigator.clipboard.writeText(text.trim()); } catch (_) {
      const t = document.createElement("textarea"); t.value = text; document.body.appendChild(t); t.select();
      document.execCommand("copy"); t.remove();
    }
    const label = btn.querySelector("span");
    if (label) { const was = label.textContent; label.textContent = "Copied"; setTimeout(() => (label.textContent = was), 1600); }
    else toast("Copied");
  }

  /* ---------- per-page enhancements ---------- */
  function hydrate(root) {
    root.querySelectorAll(".toast").forEach(armToast);
    paintThemeToggle(root);
    if (/Mac|iPhone|iPad/.test(navigator.platform || "")) root.querySelectorAll("kbd[data-mod]").forEach((k) => (k.textContent = "⌘K"));
    document.body.classList.toggle("scrolled", scrollY > 4);
    // forms that track unsaved edits
    root.querySelectorAll("form[data-track]").forEach((f) => {
      const mark = () => f.classList.add("is-dirty");
      f.addEventListener("input", mark, { once: true });
      f.addEventListener("change", mark, { once: true });
    });
    // radio groups / selects that save as soon as they change
    root.querySelectorAll("form[data-autosubmit]").forEach((f) => f.addEventListener("change", () => f.requestSubmit()));
    // business-hours rows dim when "closed" is ticked
    root.querySelectorAll(".hours [data-closed]").forEach((box) => {
      const day = box.dataset.closed;
      const sync = () => root.querySelectorAll(`[data-day="${day}"]`).forEach((el) => el.classList.toggle("row-closed", box.checked));
      box.addEventListener("change", sync); sync();
    });
    // live regions that refresh themselves (e.g. agent build status)
    root.querySelectorAll("[data-poll]").forEach((el) => {
      const id = setInterval(async () => {
        try {
          const r = await fetch(el.dataset.poll, { credentials: "same-origin" });
          const html = await r.text();
          const tmp = document.createElement("div"); tmp.innerHTML = html.trim();
          const fresh = tmp.firstElementChild;
          if (!fresh) return;
          if (fresh.dataset.pollDone !== undefined) { clearInterval(id); visit(location.href, { keepScroll: true, push: false }); }
          else el.innerHTML = fresh.innerHTML;
        } catch (_) { /* keep trying */ }
      }, Number(el.dataset.pollEvery || 3000));
      timers.push(id);
    });
  }

  // first paint
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => hydrate(document));
  else hydrate(document);
  window.deskline = { visit, toast };
})();

/* Video Factory — front end. No framework, no build step.
   Simplified deliberately: no aspect/variant pickers in the main flow --
   the server's own defaults are used, and duration is three plain words
   instead of a number of seconds. Anyone should be able to use this
   without knowing what "aspect ratio" means. Model choice IS exposed
   (grouped budget/standard/premium) -- a user who doesn't mind the price
   should be able to reach the best available quality, not just the
   cheapest one, without that choice being hidden from them. */

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    config: null,
    health: null,
    selectedImage: null,
    activePreset: null,
    selectedSeconds: null,
    selectedTier: "standard",
    selectedModel: null,
    avatarResolution: "1080p",
    polls: new Map(),
    auth: { enabled: false, user: null, balance: null },
    authMode: "login",
  };

  // ---------------------------------------------------------------- utils

  /* The server's error text is always English (see webapp/server.py) -- it
     was never worth teaching the backend three languages just for error
     strings. A handful of the most common ones are recognized and swapped
     for the translated version here, in the one place every error passes
     through, rather than patched at each of the many places an error gets
     shown. Anything not recognized is shown as-is (English) rather than
     hidden -- a real, actionable error the user can still act on beats a
     silently swallowed one. */
  function translateApiError(message) {
    if (!message) return message;
    if (message === "Incorrect email or password.") return t("auth.wrongPassword");
    const priceChange = message.match(/^The price changed to \$([\d.]+)/);
    if (priceChange) return `${t("toast.priceChanged")} ($${priceChange[1]})`;
    return message;
  }

  async function api(path, options) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    let data = null;
    try {
      data = await res.json();
    } catch {
      throw new Error(t("common.connectError"));
    }
    if (!res.ok) throw new Error(translateApiError(data && data.error) || "Request failed.");
    return data;
  }

  let toastTimer;
  function toast(message, isError) {
    const el = $("toast");
    el.textContent = message;
    el.classList.toggle("err", !!isError);
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, isError ? 7000 : 3500);
  }

  const money = (n) => `$${Number(n).toFixed(2)}`;

  /* Build nodes rather than assigning innerHTML: prompts are user text and
     must never be able to inject markup. */
  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined) node.setAttribute(k, v);
    }
    for (const c of children.flat()) if (c) node.append(c);
    return node;
  }

  function setPanel(container, node) {
    container.replaceChildren(node);
  }

  const workingPanel = () =>
    el("div", { class: "state working" },
      el("p", {}, el("strong", { text: t("common.working") })),
      el("p", { class: "muted small", text: t("common.workingHint") }),
      el("div", { class: "bar" }, el("i", {})));

  const errorPanel = (message, onRetry) =>
    el("div", { class: "state error" },
      el("p", {}, el("strong", { text: t("common.errorTitle") })),
      el("p", { class: "muted", text: message }),
      onRetry ? el("p", {}, el("button", {
        class: "btn ghost small", type: "button", onclick: onRetry, text: t("common.retry"),
      })) : null);

  const emptyPanel = (titleKey, bodyKey) =>
    el("div", { class: "empty" },
      el("p", {}, el("strong", { text: t(titleKey) })),
      el("p", { class: "muted", text: t(bodyKey) }));

  // ---------------------------------------------------------------- boot

  async function boot() {
    wireLangPicker();
    applyI18n();
    await handleOAuthRedirect();

    try {
      state.config = await api("/api/config");
    } catch {
      setPanel($("image-results"), errorPanel(t("common.connectError"), boot));
      return;
    }
    renderPresets();
    renderTierChips();
    wireStaticControls();
    wireAuth();
    wirePostprod();
    wireAvatar();
    checkHealth();
    await checkAuth();
    loadHistory();
  }

  function wireLangPicker() {
    const picker = $("lang-picker");
    picker.value = currentLang;
    picker.addEventListener("change", () => {
      setLang(picker.value);
      renderPresets();
      renderTierChips();
      renderAvatarResChips();
      renderAuthBar();
      renderHealthBadge();
      if (state.config) updatePostprodParamsIfOpen();
      if (state.config && !$("view-mcp").hidden) renderMcpView();
    });
  }

  function renderHealthBadge() {
    const h = state.health;
    if (!h) return;
    const problems = [];
    if (!h.fal_key_configured) problems.push(t("health.falKeyMissing"));
    if (!h.ffmpeg_available) problems.push(t("health.ffmpegMissing"));
    const badge = $("health");
    if (problems.length) {
      badge.textContent = problems.join(" · ");
      badge.hidden = false;
    } else {
      badge.hidden = true;
    }
  }

  async function checkHealth() {
    try {
      const h = await api("/api/health");
      state.health = h;
      renderHealthBadge();
      if (h.oauth) {
        $("auth-google").hidden = false;
        $("auth-divider").hidden = false;
      }
    } catch { /* health is advisory only */ }
  }

  // ------------------------------------------------------- google oauth

  /* Supabase's implicit OAuth flow redirects back here with the session in
     the URL fragment (#access_token=...), never a query param. We hand that
     token to our own backend, which verifies it against Supabase itself
     (never trusted blindly) before setting the same session cookie the
     email/password flow uses. */
  async function handleOAuthRedirect() {
    const hash = window.location.hash;
    if (!hash || !hash.includes("access_token=")) return;
    const params = new URLSearchParams(hash.slice(1));
    const accessToken = params.get("access_token");
    const expiresIn = params.get("expires_in");
    history.replaceState(null, "", window.location.pathname + window.location.search);
    if (!accessToken) return;
    try {
      const result = await api("/api/auth/oauth-callback", {
        method: "POST",
        body: JSON.stringify({ access_token: accessToken, expires_in: Number(expiresIn) || 3600 }),
      });
      if (result.user) toast(`${t("toast.signedIn")}, ${result.user.email}`);
    } catch (err) {
      toast(err.message, true);
    }
  }

  function signInWithGoogle() {
    if (!state.health || !state.health.oauth) return;
    const { supabase_url, supabase_anon_key } = state.health.oauth;
    const redirectTo = window.location.origin + window.location.pathname;
    const url = `${supabase_url}/auth/v1/authorize?provider=google`
      + `&redirect_to=${encodeURIComponent(redirectTo)}`
      + `&apikey=${encodeURIComponent(supabase_anon_key)}`;
    window.location.href = url;
  }

  // ----------------------------------------------------------------- auth

  async function checkAuth() {
    try {
      const me = await api("/api/auth/me");
      state.auth = {
        enabled: !!me.auth_enabled,
        user: me.user || null,
        balance: typeof me.balance_usd === "number" ? me.balance_usd : null,
      };
    } catch {
      state.auth = { enabled: false, user: null, balance: null };
    }
    renderAuthBar();
  }

  function renderAuthBar() {
    const bar = $("auth-bar");
    if (!state.auth.enabled) {
      bar.hidden = true;
      return;
    }
    bar.hidden = false;
    if (state.auth.user) {
      const balanceText = state.auth.balance === null ? "" : money(state.auth.balance);
      bar.replaceChildren(
        el("span", { class: "email", text: state.auth.user.email }),
        balanceText ? el("span", { class: "balance", text: balanceText }) : null,
        el("button", { class: "btn ghost small", type: "button", text: t("auth.signout"), onclick: signOut }));
    } else {
      bar.replaceChildren(
        el("button", { class: "btn ghost small", type: "button", text: t("auth.signin"), onclick: () => openAuthModal("login") }));
    }
  }

  /* Returns true if generation may proceed. In single-tenant mode (auth
     disabled) this is always true -- that's what keeps the original
     no-login flow working with zero configuration. */
  function requireSignedIn() {
    if (!state.auth.enabled) return true;
    if (state.auth.user) return true;
    toast(t("toast.signInFirst"), true);
    openAuthModal("login");
    return false;
  }

  function openAuthModal(mode) {
    state.authMode = mode;
    $("auth-title").textContent = t(mode === "signup" ? "auth.title.signup" : "auth.title.signin");
    $("auth-submit").textContent = t(mode === "signup" ? "auth.signup" : "auth.signin");
    $("auth-toggle").textContent = t(mode === "signup" ? "auth.haveAccount" : "auth.needAccount");
    $("auth-error").hidden = true;
    $("auth-modal").hidden = false;
    $("auth-email").focus();
  }
  function closeAuthModal() {
    $("auth-modal").hidden = true;
  }

  function wireAuth() {
    $("auth-toggle").addEventListener("click", () =>
      openAuthModal(state.authMode === "signup" ? "login" : "signup"));
    $("auth-submit").addEventListener("click", submitAuth);
    $("auth-google").addEventListener("click", signInWithGoogle);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !$("auth-modal").hidden) closeAuthModal();
    });
  }

  async function submitAuth() {
    const email = $("auth-email").value.trim();
    const password = $("auth-password").value;
    const btn = $("auth-submit");
    const errEl = $("auth-error");
    errEl.hidden = true;
    btn.disabled = true;
    try {
      const path = state.authMode === "signup" ? "/api/auth/signup" : "/api/auth/login";
      const result = await api(path, { method: "POST", body: JSON.stringify({ email, password }) });
      if (!result.user) {
        errEl.textContent = result.message || t("auth.checkEmail");
        errEl.hidden = false;
        return;
      }
      closeAuthModal();
      $("auth-email").value = "";
      $("auth-password").value = "";
      await checkAuth();
      loadHistory();
      toast(`${t("toast.signedIn")}, ${result.user.email}`);
    } catch (err) {
      errEl.textContent = err.message;
      errEl.hidden = false;
    } finally {
      btn.disabled = false;
    }
  }

  async function signOut() {
    try {
      await api("/api/auth/logout", { method: "POST" });
    } catch { /* clearing client state either way */ }
    await checkAuth();
    loadHistory();
  }

  // -------------------------------------------------------------- presets

  function presetLabel(preset) {
    return {
      name: t(`preset.${preset.id}.name`),
      blurb: t(`preset.${preset.id}.blurb`),
    };
  }

  /* Same pattern as presetLabel(): the server sends a stable id and an
     English fallback, the client owns the actual displayed text via i18n
     keys. Model ids are fal's own (contain slashes/dots), so map them to
     short slugs for the key names rather than translating the raw id. */
  const MODEL_SLUGS = {
    "fal-ai/ltx-2.3/image-to-video": "ltx23",
    "fal-ai/kling-video/v3/standard/image-to-video": "kling3",
    "fal-ai/veo3.1/image-to-video": "veo31",
    "bytedance/seedance-2.0/image-to-video": "seedance20",
    "blackforestlabs/flux-3/image-to-video": "flux3",
    "bytedance/seedance-2.5/image-to-video": "seedance25",
    "fal-ai/minimax/hailuo-02/standard/image-to-video": "hailuo02",
    "fal-ai/pixverse/v6/image-to-video": "pixverse6",
  };

  function modelLabel(model) {
    const slug = MODEL_SLUGS[model.id];
    return {
      name: slug ? t(`model.${slug}.name`) : model.name,
      note: slug ? t(`model.${slug}.note`) : model.note,
    };
  }

  function renderPresets() {
    if (!state.config) return;
    const wrap = $("presets");
    wrap.replaceChildren(...state.config.presets.map((p) => {
      const label = presetLabel(p);
      return el("button", {
        class: "preset" + (state.activePreset === p.id ? " is-active" : ""),
        type: "button", "data-id": p.id,
        onclick: () => applyPreset(p),
      },
        el("strong", { text: label.name }),
        el("span", { text: label.blurb }));
    }));
  }

  function applyPreset(preset) {
    state.activePreset = preset.id;
    $("video-prompt").value = preset.motion;
    if (preset.seconds) {
      // The preset's `seconds` is a suggested length, not guaranteed to be
      // one of the currently-selected model's own valid options -- snap to
      // whichever real option is closest rather than assuming it exists.
      const info = selectedModelInfo();
      const durations = (info && info.durations) || [preset.seconds];
      selectDuration(nearestValidDuration(preset.seconds, durations));
    }
    for (const btn of document.querySelectorAll("#presets .preset")) {
      btn.classList.toggle("is-active", btn.dataset.id === preset.id);
    }
    toast(`${presetLabel(preset).name} ${t("toast.presetApplied")}`);
  }

  // --------------------------------------------------------- static wiring

  /* Durations are per-model (fal's own real constraint differs by model --
     see scripts/factory.py's FINAL_TAKE_DURATIONS) -- rendered as actual
     second counts (5s/10s/15s...) rather than vague Short/Medium/Long, so
     a user making a 30s YouTube clip or a 5s Reel can pick exactly that. */
  function renderDurationChips() {
    if (!state.config) return;
    const info = selectedModelInfo();
    const durations = (info && info.durations) || [4, 6, 8];
    if (state.selectedSeconds === null || !durations.includes(state.selectedSeconds)) {
      state.selectedSeconds = durations[0];
    }
    const wrap = $("duration-chips");
    wrap.replaceChildren(...durations.map((secs) =>
      el("button", {
        class: "chip" + (secs === state.selectedSeconds ? " is-active" : ""),
        type: "button", "data-seconds": String(secs),
        onclick: () => selectDuration(secs),
        text: `${secs}s`,
      })));
  }

  function selectDuration(secs) {
    state.selectedSeconds = secs;
    for (const chip of document.querySelectorAll("#duration-chips .chip")) {
      chip.classList.toggle("is-active", Number(chip.dataset.seconds) === secs);
    }
  }

  function nearestValidDuration(target, options) {
    return options.reduce((best, cur) =>
      Math.abs(cur - target) < Math.abs(best - target) ? cur : best, options[0]);
  }

  // ----------------------------------------------------- model quality tier

  const TIERS_ORDER = ["budget", "standard", "premium"];
  const TIER_LABEL_KEYS = { budget: "tier.budget", standard: "tier.standard", premium: "tier.premium" };

  function renderTierChips() {
    if (!state.config) return;
    const present = TIERS_ORDER.filter((tier) => state.config.models.some((m) => (m.tier || "standard") === tier));
    const wrap = $("tier-chips");
    wrap.replaceChildren(...present.map((tier) =>
      el("button", {
        class: "chip" + (tier === state.selectedTier ? " is-active" : ""),
        type: "button", "data-tier": tier,
        onclick: () => selectTier(tier),
        text: t(TIER_LABEL_KEYS[tier]),
      })));
    renderModelGrid();
  }

  function selectTier(tier) {
    state.selectedTier = tier;
    for (const chip of document.querySelectorAll("#tier-chips .chip")) {
      chip.classList.toggle("is-active", chip.dataset.tier === tier);
    }
    renderModelGrid();
  }

  function renderModelGrid() {
    if (!state.config) return;
    const models = state.config.models.filter((m) => (m.tier || "standard") === state.selectedTier);
    // Keep the selection valid: if nothing's picked yet, or the current pick
    // isn't in the now-visible tier, fall back to this tier's own default
    // (or its first model).
    if (!state.selectedModel || !models.some((m) => m.id === state.selectedModel)) {
      const fallback = models.find((m) => m.default) || models[0];
      state.selectedModel = fallback ? fallback.id : null;
    }
    $("model-grid").replaceChildren(...models.map((m) => {
      const label = modelLabel(m);
      return el("button", {
        class: "preset" + (m.id === state.selectedModel ? " is-active" : ""),
        type: "button", "data-id": m.id,
        onclick: () => selectModel(m.id),
      },
        el("strong", { text: `${label.name} · $${m.rate}/s` }),
        el("span", { text: label.note }));
    }));
    renderDurationChips();
  }

  function selectModel(id) {
    state.selectedModel = id;
    for (const card of document.querySelectorAll("#model-grid .preset")) {
      card.classList.toggle("is-active", card.dataset.id === id);
    }
    renderDurationChips();
  }

  function selectedModelInfo() {
    return (state.config?.models || []).find((m) => m.id === state.selectedModel) || null;
  }

  // ------------------------------------------------------------------ MCP

  /* Code/config snippets are left in their native technical format
     (JSON, shell) rather than translated -- same convention this project
     already uses for every other technical string on the site. Only the
     surrounding explanatory copy is localized. */
  function codeBlock(text) {
    const pre = el("pre", { class: "code-pre" }, el("code", { text }));
    const copyBtn = el("button", {
      class: "code-copy", type: "button", text: t("mcp.copy"),
      onclick: async () => {
        try {
          await navigator.clipboard.writeText(text);
          copyBtn.textContent = t("mcp.copied");
        } catch {
          copyBtn.textContent = t("mcp.copyFailed");
        }
        setTimeout(() => { copyBtn.textContent = t("mcp.copy"); }, 1800);
      },
    });
    return el("div", { class: "code-block" }, copyBtn, pre);
  }

  const MCP_TOOLS = [
    "getInfo", "quoteImages", "createImages", "quoteVideo", "createVideo",
    "quoteEnhancement", "enhanceVideo", "quoteAvatar", "createAvatar",
    "checkJob", "listVideos",
  ];

  function renderMcpView() {
    const origin = window.location.origin;

    setPanel($("mcp-claude-desktop-block"), codeBlock(
`{
  "mcpServers": {
    "video-factory": {
      "command": "python3",
      "args": ["/path/to/server.py"],
      "env": {
        "VIDEO_FACTORY_URL": "${origin}"
      }
    }
  }
}`));

    setPanel($("mcp-claude-code-block"), codeBlock(
`claude mcp add video-factory python3 /path/to/server.py \\
  --env VIDEO_FACTORY_URL=${origin}`));

    const httpUrl = state.config?.mcp?.http_url || "";
    if (httpUrl) {
      setPanel($("mcp-chatgpt-block"),
        el("div", {},
          codeBlock(`${httpUrl}\nAuthorization: Bearer <your-token>`),
          el("p", { class: "muted small", text: t("mcp.chatgpt.tokenNote") })));
    } else {
      setPanel($("mcp-chatgpt-block"),
        el("p", { class: "muted small", text: t("mcp.chatgpt.notDeployed") }));
    }

    $("mcp-tools-list").replaceChildren(...MCP_TOOLS.map((key) =>
      el("li", {},
        el("strong", { text: t(`mcp.tool.${key}.name`) }),
        el("span", { class: "muted", text: ` — ${t(`mcp.tool.${key}.desc`)}` }))));
  }

  function wireStaticControls() {
    $("btn-image").addEventListener("click", generateImages);
    $("btn-video").addEventListener("click", askForVideo);
    $("tab-create").addEventListener("click", () => switchView("create"));
    $("tab-avatar").addEventListener("click", () => switchView("avatar"));
    $("tab-history").addEventListener("click", () => switchView("history"));
    $("tab-mcp").addEventListener("click", () => switchView("mcp"));
  }

  function switchView(which) {
    $("view-create").hidden = which !== "create";
    $("view-avatar").hidden = which !== "avatar";
    $("view-history").hidden = which !== "history";
    $("view-mcp").hidden = which !== "mcp";
    $("tab-create").classList.toggle("is-active", which === "create");
    $("tab-avatar").classList.toggle("is-active", which === "avatar");
    $("tab-history").classList.toggle("is-active", which === "history");
    $("tab-mcp").classList.toggle("is-active", which === "mcp");
    if (which === "history") loadHistory();
    if (which === "mcp") renderMcpView();
  }

  // ------------------------------------------------------------- polling

  function poll(jobId, onDone, onProgress) {
    if (state.polls.has(jobId)) clearInterval(state.polls.get(jobId));
    let misses = 0;
    const timer = setInterval(async () => {
      try {
        const job = await api(`/api/jobs/${jobId}`);
        misses = 0;
        if (job.status === "done" || job.status === "error") {
          clearInterval(timer);
          state.polls.delete(jobId);
          onDone(job);
        } else if (onProgress) {
          onProgress(job);
        }
      } catch {
        /* A dropped poll is usually transient. Give up only after several,
           so a brief blip doesn't kill a generation the server is still running. */
        if (++misses >= 5) {
          clearInterval(timer);
          state.polls.delete(jobId);
          onDone({ status: "error", error: t("common.connectError") });
        }
      }
    }, 2500);
    state.polls.set(jobId, timer);
  }

  // -------------------------------------------------------------- images

  async function generateImages() {
    if (!requireSignedIn()) return;
    const prompt = $("image-prompt").value.trim();
    if (!prompt) {
      toast(t("toast.describeFirst"), true);
      $("image-prompt").focus();
      return;
    }

    const btn = $("btn-image");
    btn.disabled = true;
    setPanel($("image-results"), workingPanel());

    try {
      const job = await api("/api/generate/image", {
        method: "POST",
        body: JSON.stringify({ prompt }),
      });
      poll(job.id,
        (done) => {
          btn.disabled = false;
          if (done.status === "error") {
            setPanel($("image-results"), errorPanel(translateApiError(done.error), generateImages));
            return;
          }
          renderImageChoices(done.outputs || []);
          loadHistory();
          checkAuth();
        },
        () => setPanel($("image-results"), workingPanel()));
    } catch (err) {
      btn.disabled = false;
      setPanel($("image-results"), errorPanel(err.message, generateImages));
    }
  }

  function renderImageChoices(urls) {
    if (!urls.length) {
      setPanel($("image-results"), el("div", { class: "empty" },
        el("p", {}, el("strong", { text: t("toast.noImages") }))));
      return;
    }
    /* Selection is tracked by index, not by URL: two variants can legitimately
       come back with the same URL, and matching on the string would highlight
       both. */
    const cards = urls.map((url, i) => {
      const card = el("div", { class: "card", "data-index": String(i) },
        el("img", { src: url, alt: `${i + 1}`, loading: "lazy" }),
        el("div", { class: "card-body" },
          el("span", { class: "spacer" }),
          el("button", {
            class: "btn primary small", type: "button", text: t("step2.choose"),
            onclick: () => selectImage(url, i),
          })));
      return card;
    });
    $("image-results").replaceChildren(...cards);
    if (urls.length === 1) selectImage(urls[0], 0);
  }

  function selectImage(url, index) {
    state.selectedImage = url;
    for (const card of document.querySelectorAll("#image-results .card")) {
      card.classList.toggle("is-selected", Number(card.dataset.index) === index);
      const btn = card.querySelector(".btn");
      if (btn) btn.textContent = Number(card.dataset.index) === index ? t("step2.chosen") : t("step2.choose");
    }
    $("btn-video").disabled = false;
    $("video-gate").textContent = t("step3.ready");
    $("step-video").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // -------------------------------------------------------------- video

  /* The paid path. The server quotes, the user confirms that exact number,
     and only then does anything get charged — same gate as the CLI. */
  async function askForVideo() {
    if (!requireSignedIn()) return;
    if (!state.selectedImage) {
      toast(t("toast.pickFrame"), true);
      return;
    }
    const prompt = $("video-prompt").value.trim()
      || "Camera holds, faint handheld presence, imperceptible drift; everyone in frame keeps small natural idle motion.";

    let quote;
    try {
      quote = await api("/api/quote", {
        method: "POST",
        body: JSON.stringify({ seconds: state.selectedSeconds, model: state.selectedModel }),
      });
    } catch (err) {
      toast(err.message, true);
      return;
    }

    $("confirm-cost").textContent = money(quote.cost_usd);
    const info = selectedModelInfo();
    $("confirm-detail").textContent = info ? modelLabel(info).name : "";
    openConfirm(() => startVideo(prompt, quote));
  }

  let confirmHandler = null;
  function openConfirm(onOk) {
    confirmHandler = onOk;
    $("confirm").hidden = false;
    $("confirm-ok").focus();
  }
  function closeConfirm() {
    $("confirm").hidden = true;
    confirmHandler = null;
  }
  $("confirm-cancel").addEventListener("click", closeConfirm);
  $("confirm-ok").addEventListener("click", () => {
    const fn = confirmHandler;
    closeConfirm();
    if (fn) fn();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("confirm").hidden) closeConfirm();
  });

  async function startVideo(prompt, quote) {
    const btn = $("btn-video");
    btn.disabled = true;
    setPanel($("video-results"), workingPanel());

    try {
      const job = await api("/api/generate/video", {
        method: "POST",
        body: JSON.stringify({
          prompt,
          image_url: state.selectedImage,
          seconds: quote.seconds,
          model: quote.model,
          approved_cost: quote.cost_usd,
        }),
      });
      poll(job.id,
        (done) => {
          btn.disabled = false;
          if (done.status === "error") {
            setPanel($("video-results"), errorPanel(translateApiError(done.error),
              () => startVideo(prompt, quote)));
            return;
          }
          renderVideo(done);
          loadHistory();
          checkAuth();
          toast(t("toast.videoReady"));
        },
        () => setPanel($("video-results"), workingPanel()));
    } catch (err) {
      btn.disabled = false;
      setPanel($("video-results"), errorPanel(err.message,
        () => startVideo(prompt, quote)));
    }
  }

  function renderVideo(job) {
    const url = (job.outputs || [])[0];
    if (!url) {
      setPanel($("video-results"), el("div", { class: "empty" },
        el("p", {}, el("strong", { text: t("toast.noVideo") }))));
      return;
    }
    setPanel($("video-results"),
      el("div", { class: "card" },
        el("video", { src: url, controls: "", playsinline: "", preload: "metadata" }),
        el("div", { class: "card-body" },
          el("span", { class: "badge ok", text: money(job.cost_usd || 0) }),
          el("span", { class: "spacer" }),
          el("button", { class: "btn ghost small", type: "button", text: t("step4.enhance"),
                          onclick: () => openPostprodModal(url) }),
          el("a", { class: "dl", href: url, download: "", target: "_blank",
                    rel: "noopener", text: t("step4.download") }))));
  }

  // ------------------------------------------------------------ history

  async function loadHistory() {
    const list = $("history-list");
    if (state.auth.enabled && !state.auth.user) {
      setPanel(list, emptyPanel("history.signin.title", "history.signin.body"));
      return;
    }
    let data;
    try {
      data = await api("/api/jobs");
    } catch {
      setPanel(list, errorPanel(t("history.failed"), loadHistory));
      return;
    }

    const done = (data.jobs || []).filter((j) => (j.outputs || []).length || j.status === "error");
    if (!done.length) {
      setPanel(list, emptyPanel("history.empty.title", "history.empty.body"));
      return;
    }

    list.replaceChildren(...done.slice(0, 40).map((job) => {
      const url = (job.outputs || [])[0];
      const isVideo = job.kind === "video" || job.kind === "postprod" || job.kind === "avatar";
      const media = job.status === "error"
        ? el("div", { class: "card-body" },
            el("span", { class: "badge err", text: t("history.failed") }))
        : isVideo
          ? el("video", { src: url, controls: "", playsinline: "", preload: "metadata" })
          : el("img", { src: url, alt: "", loading: "lazy" });
      const label = job.kind === "postprod" ? t("step4.enhance")
        : job.kind === "avatar" ? t("nav.avatar")
        : job.kind === "video" ? t("history.badge.video") : t("history.badge.image");

      return el("div", { class: "card" },
        media,
        el("div", { class: "card-body" },
          el("span", { class: "badge", text: label }),
          el("span", { class: "muted small", text: money(job.cost_usd || 0) }),
          el("span", { class: "spacer" }),
          isVideo && url ? el("button", { class: "btn ghost small", type: "button", text: t("step4.enhance"),
                                            onclick: () => openPostprodModal(url) }) : null,
          url ? el("a", { class: "dl", href: url, download: "", target: "_blank",
                          rel: "noopener", text: t("step4.download") }) : null),
        job.status === "error"
          ? el("div", { class: "card-body" },
              el("span", { class: "muted small", text: translateApiError(job.error) || t("history.failed") }))
          : null);
    }));
  }

  // ------------------------------------------------------- post-production

  function upscaleTierOptions() {
    const tiers = Object.keys(state.config.postprod.upscale_tiers);
    const labelKeys = ["postprod.upscale.q1", "postprod.upscale.q2", "postprod.upscale.q3"];
    return tiers.map((tier, i) => ({ tier, label: t(labelKeys[i] || labelKeys[labelKeys.length - 1]) }));
  }

  const POSTPROD_PARAM_BUILDERS = {
    upscale: () => el("label", { class: "field" },
      el("span", { class: "label", text: t("postprod.upscale.quality") }),
      el("select", { id: "pp-tier", class: "big-input" },
        ...upscaleTierOptions().map(({ tier, label }) => el("option", { value: tier, text: label })))),
    bgremove: () => el("label", { class: "field" },
      el("span", { class: "label", text: t("postprod.bgremove.color") }),
      el("input", { id: "pp-bgcolor", type: "text", value: "Black", class: "big-input" })),
    subtitles: () => el("label", { class: "field" },
      el("span", { class: "label", text: t("postprod.subtitles.lang") }),
      el("input", { id: "pp-lang", type: "text", class: "big-input",
                    placeholder: t("postprod.subtitles.langPlaceholder") })),
    lipsync: () => el("label", { class: "field" },
      el("span", { class: "label", text: t("postprod.lipsync.audio") }),
      el("input", { id: "pp-audio-url", type: "url", class: "big-input",
                    placeholder: t("postprod.lipsync.audioPlaceholder") })),
  };

  function renderPostprodParams() {
    const op = $("postprod-op").value;
    setPanel($("postprod-params"), POSTPROD_PARAM_BUILDERS[op]());
  }

  function updatePostprodParamsIfOpen() {
    if (!$("postprod-modal").hidden) renderPostprodParams();
  }

  function readPostprodParams(op) {
    if (op === "upscale") {
      return { tier: $("pp-tier").value, factor: 2 };
    }
    if (op === "bgremove") {
      return { background_color: $("pp-bgcolor").value.trim() || "Black" };
    }
    if (op === "subtitles") {
      const lang = $("pp-lang").value.trim();
      return lang ? { lang } : {};
    }
    if (op === "lipsync") {
      const audioUrl = $("pp-audio-url").value.trim();
      if (!audioUrl) throw new Error(t("postprod.error.audioRequired"));
      return { audio_url: audioUrl };
    }
    return {};
  }

  function openPostprodModal(fileUrl) {
    if (!requireSignedIn()) return;
    state.postprodFileUrl = fileUrl;
    $("postprod-op").value = "upscale";
    renderPostprodParams();
    $("postprod-error").hidden = true;
    $("postprod-modal").hidden = false;
  }
  function closePostprodModal() {
    $("postprod-modal").hidden = true;
  }

  function wirePostprod() {
    $("postprod-op").addEventListener("change", renderPostprodParams);
    $("postprod-cancel").addEventListener("click", closePostprodModal);
    $("postprod-quote").addEventListener("click", quotePostprod);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !$("postprod-modal").hidden) closePostprodModal();
    });
  }

  async function quotePostprod() {
    const op = $("postprod-op").value;
    const errEl = $("postprod-error");
    errEl.hidden = true;
    let params;
    try {
      params = readPostprodParams(op);
    } catch (err) {
      errEl.textContent = err.message;
      errEl.hidden = false;
      return;
    }
    let quote;
    try {
      quote = await api("/api/postprod/quote", {
        method: "POST",
        body: JSON.stringify({ op, file_url: state.postprodFileUrl, ...params }),
      });
    } catch (err) {
      errEl.textContent = err.message;
      errEl.hidden = false;
      return;
    }
    closePostprodModal();
    $("confirm-cost").textContent = money(quote.cost_usd);
    $("confirm-detail").textContent = "";
    openConfirm(() => runPostprod(op, params, quote));
  }

  async function runPostprod(op, params, quote) {
    const fileUrl = state.postprodFileUrl;
    toast(t("toast.starting"));
    try {
      const job = await api("/api/postprod/run", {
        method: "POST",
        body: JSON.stringify({ op, file_url: fileUrl, ...params, approved_cost: quote.cost_usd }),
      });
      poll(job.id,
        (done) => {
          checkAuth();
          loadHistory();
          if (done.status === "error") {
            toast(translateApiError(done.error) || t("toast.enhanceFailed"), true);
            return;
          }
          toast(t("toast.enhanceReady"));
        },
        () => {});
    } catch (err) {
      toast(err.message, true);
    }
  }

  // ------------------------------------------------- avatar (photo + voice)

  const AVATAR_RES_LABEL_KEYS = { "720p": "avatar.res.720", "1080p": "avatar.res.1080" };

  function renderAvatarResChips() {
    const wrap = $("avatar-res-chips");
    if (!wrap) return;
    const resolutions = state.config?.avatar?.resolutions || ["720p", "1080p"];
    if (!resolutions.includes(state.avatarResolution)) state.avatarResolution = resolutions[resolutions.length - 1];
    wrap.replaceChildren(...resolutions.map((res) =>
      el("button", {
        class: "chip" + (res === state.avatarResolution ? " is-active" : ""),
        type: "button", "data-res": res,
        onclick: () => selectAvatarResolution(res),
        text: t(AVATAR_RES_LABEL_KEYS[res] || res),
      })));
  }

  function selectAvatarResolution(res) {
    state.avatarResolution = res;
    for (const chip of document.querySelectorAll("#avatar-res-chips .chip")) {
      chip.classList.toggle("is-active", chip.dataset.res === res);
    }
  }

  function wireAvatar() {
    renderAvatarResChips();
    $("btn-avatar").addEventListener("click", quoteAvatar);
  }

  async function quoteAvatar() {
    if (!requireSignedIn()) return;
    const errEl = $("avatar-error");
    errEl.hidden = true;
    const imageUrl = $("avatar-image").value.trim();
    const audioUrl = $("avatar-audio").value.trim();
    if (!imageUrl || !audioUrl) {
      errEl.textContent = t("avatar.error.required");
      errEl.hidden = false;
      return;
    }
    const prompt = $("avatar-prompt").value.trim();
    let quote;
    try {
      quote = await api("/api/avatar/quote", {
        method: "POST",
        body: JSON.stringify({ image_url: imageUrl, audio_url: audioUrl }),
      });
    } catch (err) {
      errEl.textContent = err.message;
      errEl.hidden = false;
      return;
    }
    $("confirm-cost").textContent = money(quote.cost_usd);
    $("confirm-detail").textContent = "";
    openConfirm(() => runAvatar(imageUrl, audioUrl, prompt, quote));
  }

  async function runAvatar(imageUrl, audioUrl, prompt, quote) {
    const btn = $("btn-avatar");
    btn.disabled = true;
    setPanel($("avatar-results"), workingPanel());
    try {
      const body = {
        image_url: imageUrl, audio_url: audioUrl,
        resolution: state.avatarResolution, approved_cost: quote.cost_usd,
      };
      if (prompt) body.prompt = prompt;
      const job = await api("/api/avatar/run", { method: "POST", body: JSON.stringify(body) });
      poll(job.id,
        (done) => {
          btn.disabled = false;
          if (done.status === "error") {
            setPanel($("avatar-results"), errorPanel(translateApiError(done.error),
              () => runAvatar(imageUrl, audioUrl, prompt, quote)));
            return;
          }
          renderAvatarResult(done);
          loadHistory();
          checkAuth();
          toast(t("toast.videoReady"));
        },
        () => setPanel($("avatar-results"), workingPanel()));
    } catch (err) {
      btn.disabled = false;
      setPanel($("avatar-results"), errorPanel(err.message, () => runAvatar(imageUrl, audioUrl, prompt, quote)));
    }
  }

  function renderAvatarResult(job) {
    const url = (job.outputs || [])[0];
    if (!url) {
      setPanel($("avatar-results"), el("div", { class: "empty" },
        el("p", {}, el("strong", { text: t("toast.noVideo") }))));
      return;
    }
    setPanel($("avatar-results"),
      el("div", { class: "card" },
        el("video", { src: url, controls: "", playsinline: "", preload: "metadata" }),
        el("div", { class: "card-body" },
          el("span", { class: "badge ok", text: money(job.cost_usd || 0) }),
          el("span", { class: "spacer" }),
          el("a", { class: "dl", href: url, download: "", target: "_blank",
                    rel: "noopener", text: t("step4.download") }))));
  }

  boot();
})();

/* Video Factory — front end. No framework, no build step.
   Simplified deliberately: every video model is shown as one flat list of
   looks/styles, with no dollar amount attached to any of them -- price
   shopping across models isn't something a first-time user should have to
   do. The one place a price is still shown is the confirm dialog right
   before anything is actually charged (see openConfirm/askForVideo) --
   that's the money-safety gate, not a shopping decision, and stays. */

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    config: null,
    health: null,
    mode: null,
    selectedImage: null,
    imageWasUploaded: false,
    activePreset: null,
    presetMotion: null,
    selectedCameraMove: null,
    selectedSeconds: null,
    selectedModel: null,
    imageOnlyRefs: [],
    characters: [],
    selectedCharacterId: null,
    characterRefs: [],
    motionImageDataUrl: null,
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
    renderCameraChips();
    renderAspectSelect("aspect-select");
    renderAspectSelect("image-aspect-select");
    renderModelGrid();
    wireModePicker();
    wireStaticControls();
    wireImageUpload();
    wireImageOnly();
    wireCharacterModal();
    wireMotionTransfer();
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
      renderCameraChips();
      renderAspectSelect("aspect-select");
      renderAspectSelect("image-aspect-select");
      renderModelGrid();
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
    await loadCharacters();
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

  /* Returns true only for a real signed-in user -- there is no bypass for
     single-tenant/unconfigured deployments any more. webapp/server.py's
     _require_funded_user() enforces this same rule server-side (the real
     boundary; this is just the friendly UI path to it), so a request this
     function let through can never actually spend money for real without
     a session anyway. If the server has no auth backend configured at all
     (state.auth.enabled false), opening the login modal would only lead to
     a dead end ("sign-in not enabled on this server"), so that case gets
     its own clear message instead of a modal that can't work. */
  function requireSignedIn() {
    if (state.auth.user) return true;
    if (!state.auth.enabled) {
      toast(t("toast.authNotConfigured"), true);
      return false;
    }
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
    $("auth-password").type = "password";
    $("auth-password-toggle").textContent = "👁";
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
    $("auth-password-toggle").addEventListener("click", togglePasswordVisibility);
    $("auth-password").addEventListener("keydown", (e) => {
      if (e.key === "Enter") submitAuth();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !$("auth-modal").hidden) closeAuthModal();
    });
  }

  function togglePasswordVisibility() {
    const input = $("auth-password");
    const btn = $("auth-password-toggle");
    const showing = input.type === "text";
    input.type = showing ? "password" : "text";
    btn.textContent = showing ? "👁" : "🙈";
    btn.setAttribute("aria-label", t(showing ? "auth.showPassword" : "auth.hidePassword"));
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

  /* Presets are camera/motion styles (see webapp/server.py's PRESETS) --
     there's no separate "motion" box in this UI anymore, so a chosen
     preset is remembered silently and applied later by motionPromptText(),
     instead of overwriting the one visible prompt field (which describes
     the image, or the upload's motion -- see that function). */
  function applyPreset(preset) {
    state.activePreset = preset.id;
    state.presetMotion = preset.motion;
    // A preset and a camera-move chip both answer the same question
    // ("how does this shot move") at different levels of detail -- picking
    // one clears the other rather than trying to combine two motion
    // instructions into one prompt.
    deselectCameraMove();
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

  // --------------------------------------------------------- camera moves

  /* A tested, specific vocabulary (see fal-master-prompt.md section 3) --
     one move, a magnitude, a duration, never stacked -- rather than vague
     terms an image-to-video model interprets inconsistently. "held" is the
     same ambient-motion fallback askForVideo() already used, expressed as
     a selectable option instead of a hidden default, and its text is the
     single source of truth motionPromptText() falls back to. Mutually
     exclusive with presets (see applyPreset) -- both describe the same
     "how does this shot move" choice, one raw, one as a named style. */
  const CAMERA_MOVES = [
    { id: "held", labelKey: "camera.held",
      text: "Camera holds, faint handheld presence, imperceptible drift; everyone in frame keeps small natural idle motion throughout." },
    { id: "pushin", labelKey: "camera.pushIn",
      text: "Slow push in, 15% over the full duration. Subject stays still; no lighting change." },
    { id: "pullout", labelKey: "camera.pullOut",
      text: "Slow pull back, 20% over the full duration, revealing more of the scene." },
    { id: "panleft", labelKey: "camera.panLeft",
      text: "Gentle pan left, 10 degrees over the full duration." },
    { id: "panright", labelKey: "camera.panRight",
      text: "Gentle pan right, 10 degrees over the full duration." },
    { id: "reveal", labelKey: "camera.reveal",
      text: "Camera starts high looking down, then pushes in and drops to a low three-quarter angle, revealing the environment as it settles." },
  ];

  function renderCameraChips() {
    const wrap = $("camera-chips");
    wrap.replaceChildren(...CAMERA_MOVES.map((move) =>
      el("button", {
        class: "chip" + (move.id === state.selectedCameraMove ? " is-active" : ""),
        type: "button", "data-move": move.id,
        onclick: () => selectCameraMove(move.id),
        text: t(move.labelKey),
      })));
  }

  function selectCameraMove(id) {
    state.selectedCameraMove = state.selectedCameraMove === id ? null : id;
    state.activePreset = null;
    state.presetMotion = null;
    for (const chip of document.querySelectorAll("#camera-chips .chip")) {
      chip.classList.toggle("is-active", chip.dataset.move === state.selectedCameraMove);
    }
    for (const btn of document.querySelectorAll("#presets .preset")) {
      btn.classList.remove("is-active");
    }
  }

  function deselectCameraMove() {
    state.selectedCameraMove = null;
    for (const chip of document.querySelectorAll("#camera-chips .chip")) {
      chip.classList.remove("is-active");
    }
  }

  // ------------------------------------------------- size (aspect ratio)

  /* A dropdown of destinations instead of exposing "aspect ratio" as a
     concept -- same philosophy as the rest of this file: anyone should be
     able to use this without knowing what an aspect ratio is. Two
     platforms can share a ratio (Instagram and TikTok are both vertical)
     -- that's fine, the option is labeled by the destination, not the
     number. Both the image-only panel and the video panel get their own
     <select> (image-aspect-select / aspect-select) so picking one doesn't
     affect the other; both default to Instagram. */
  const ASPECT_OPTIONS = [
    { id: "instagram", aspect: "9:16", labelKey: "platform.instagram" },
    { id: "tiktok", aspect: "9:16", labelKey: "platform.tiktok" },
    { id: "youtube", aspect: "16:9", labelKey: "platform.youtube" },
    { id: "square", aspect: "1:1", labelKey: "platform.square" },
    { id: "classic", aspect: "4:3", labelKey: "platform.classic" },
  ];

  function renderAspectSelect(selectId) {
    const sel = $(selectId);
    if (!sel) return;
    const prevValue = sel.value || ASPECT_OPTIONS[0].id;
    sel.replaceChildren(...ASPECT_OPTIONS.map((o) =>
      el("option", { value: o.id, text: t(o.labelKey) })));
    sel.value = ASPECT_OPTIONS.some((o) => o.id === prevValue) ? prevValue : ASPECT_OPTIONS[0].id;
  }

  function selectedAspectRatio(selectId) {
    const sel = $(selectId);
    const chosen = ASPECT_OPTIONS.find((o) => o.id === (sel && sel.value));
    return chosen ? chosen.aspect : "9:16";
  }

  // ---------------------------------------------------------- image upload

  const MAX_UPLOAD_BYTES = 8 * 1024 * 1024;

  function wireImageUpload() {
    $("image-upload").addEventListener("change", handleImageUpload);
  }

  /* Uploading skips AI image generation entirely -- the file becomes the
     video's starting frame directly, sent later as a data: URI (the
     server explicitly allows that for image_url, see webapp/server.py's
     _require_public_url). Does NOT auto-advance to the price-confirm step
     the way picking a *generated* candidate does (see selectImage) --
     someone who just uploaded a photo probably still wants to type a
     motion description before anything is quoted, so this only marks the
     frame as ready and swaps the prompt field's role from "describe the
     image" to "describe the motion" (see motionPromptText). */
  function handleImageUpload(e) {
    const file = e.target.files[0];
    if (!file) return;
    if (!file.type.startsWith("image/")) {
      toast(t("toast.notAnImage"), true);
      e.target.value = "";
      return;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      toast(t("toast.imageTooLarge"), true);
      e.target.value = "";
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      $("upload-filename").textContent = file.name;
      state.activePreset = null;
      state.imageWasUploaded = true;
      renderImageChoices([reader.result], { autoAdvance: false });
      $("video-prompt-hint").textContent = t("step1.motionHint");
      $("image-prompt").placeholder = t("step1.motionPlaceholder");
      toast(t("toast.imageUploaded"));
    };
    reader.onerror = () => toast(t("common.connectError"), true);
    reader.readAsDataURL(file);
  }

  // ------------------------------------------------------------ mode picker

  /* The Create tab opens on a plain choice -- image or video -- instead of
     one combined form, so a video model list never has to appear to
     someone who only wanted a still image, and vice versa. */
  function wireModePicker() {
    $("mode-image").addEventListener("click", () => enterMode("image"));
    $("mode-video").addEventListener("click", () => enterMode("video"));
    $("mode-motion").addEventListener("click", () => enterMode("motion"));
    $("back-from-image").addEventListener("click", () => enterMode(null));
    $("back-from-video").addEventListener("click", () => enterMode(null));
    $("back-from-motion").addEventListener("click", () => enterMode(null));
  }

  function enterMode(mode) {
    state.mode = mode;
    $("mode-picker").hidden = mode !== null;
    $("panel-image").hidden = mode !== "image";
    $("panel-video").hidden = mode !== "video";
    $("panel-motion").hidden = mode !== "motion";
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

  // ----------------------------------------------------------------- model

  /* Every model, one flat list -- no budget/standard/premium grouping and
     no dollar rate on the card (see the file-top comment for why). A user
     picks by look/style, not by price; the final price is quoted and
     confirmed once, right before the video actually generates. */
  function renderModelGrid() {
    if (!state.config) return;
    const models = state.config.models;
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
        el("strong", { text: label.name }),
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
          el("p", { class: "muted small", text: t("mcp.chatgpt.pasteThis") }),
          codeBlock(`${httpUrl}\nAuthorization: Bearer <your-token>`),
          el("p", { class: "muted small", text: t("mcp.chatgpt.tokenNote") })));
    } else {
      setPanel($("mcp-chatgpt-block"),
        el("p", { class: "muted small err", text: t("mcp.chatgpt.notDeployed") }));
    }

    $("mcp-tools-list").replaceChildren(...MCP_TOOLS.map((key) =>
      el("li", {},
        el("strong", { text: t(`mcp.tool.${key}.name`) }),
        el("span", { class: "muted", text: ` — ${t(`mcp.tool.${key}.desc`)}` }))));
  }

  function wireStaticControls() {
    $("btn-video-start").addEventListener("click", startVideoFlow);
    $("tab-create").addEventListener("click", () => switchView("create"));
    $("tab-avatar").addEventListener("click", () => switchView("avatar"));
    $("tab-history").addEventListener("click", () => switchView("history"));
    $("tab-gallery").addEventListener("click", () => switchView("gallery"));
    $("tab-mcp").addEventListener("click", () => switchView("mcp"));
  }

  function switchView(which) {
    $("view-create").hidden = which !== "create";
    $("view-avatar").hidden = which !== "avatar";
    $("view-history").hidden = which !== "history";
    $("view-gallery").hidden = which !== "gallery";
    $("view-mcp").hidden = which !== "mcp";
    $("tab-create").classList.toggle("is-active", which === "create");
    $("tab-avatar").classList.toggle("is-active", which === "avatar");
    $("tab-history").classList.toggle("is-active", which === "history");
    $("tab-gallery").classList.toggle("is-active", which === "gallery");
    $("tab-mcp").classList.toggle("is-active", which === "mcp");
    if (which === "history") loadHistory();
    if (which === "gallery") loadGallery();
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

  // ----------------------------------------------------------- characters

  /* A character is a saved identity (name + description + up to 4
     reference photos) reused across shots -- feeds the same refs/lock-text
     mechanism scripts/factory.py's CLI pipeline has always used
     (CHARACTER_LOCK / IDENTITY_LOCK in templates.json), now reachable from
     the web UI. Requires a real signed-in user: characters live in
     Supabase's `characters` table (see 01-schema.sql /
     03-characters-per-user.sql), which single-tenant/no-auth mode has no
     access to at all -- the section stays hidden rather than showing a
     feature that can't work. */
  async function loadCharacters() {
    if (!state.auth.user) {
      state.characters = [];
      state.selectedCharacterId = null;
      renderCharacterChips();
      return;
    }
    try {
      const data = await api("/api/characters");
      state.characters = data.characters || [];
    } catch {
      state.characters = [];
    }
    if (!state.characters.some((c) => c.id === state.selectedCharacterId)) {
      state.selectedCharacterId = null;
    }
    renderCharacterChips();
  }

  function renderCharacterChips() {
    const section = $("character-section");
    if (!state.auth.user) {
      section.hidden = true;
      return;
    }
    section.hidden = false;
    const wrap = $("character-chips");
    wrap.replaceChildren(
      ...state.characters.map((c) =>
        el("button", {
          class: "chip" + (c.id === state.selectedCharacterId ? " is-active" : ""),
          type: "button", "data-id": c.id,
          onclick: () => selectCharacter(c.id),
          text: c.name,
        })),
      el("button", {
        class: "chip add-character", type: "button",
        onclick: openCharacterModal,
        text: `+ ${t("character.addNew")}`,
      }));
  }

  function selectCharacter(id) {
    state.selectedCharacterId = state.selectedCharacterId === id ? null : id;
    for (const chip of document.querySelectorAll("#character-chips .chip:not(.add-character)")) {
      chip.classList.toggle("is-active", chip.dataset.id === state.selectedCharacterId);
    }
  }

  function selectedCharacter() {
    return state.characters.find((c) => c.id === state.selectedCharacterId) || null;
  }

  function openCharacterModal() {
    state.characterRefs = [];
    $("character-name").value = "";
    $("character-lock-text").value = "";
    $("character-ref-previews").replaceChildren();
    $("character-error").hidden = true;
    $("character-modal").hidden = false;
    $("character-name").focus();
  }

  function closeCharacterModal() {
    $("character-modal").hidden = true;
  }

  function renderCharacterRefPreviews() {
    $("character-ref-previews").replaceChildren(...state.characterRefs.map((dataUrl, i) =>
      el("div", { class: "character-ref-thumb" },
        el("img", { src: dataUrl, alt: "" }),
        el("button", {
          type: "button", text: "×", "aria-label": t("character.removeRef"),
          onclick: () => {
            state.characterRefs.splice(i, 1);
            renderCharacterRefPreviews();
          },
        }))));
  }

  function handleCharacterRefUpload(e) {
    const files = Array.from(e.target.files || []);
    e.target.value = "";
    for (const file of files) {
      if (state.characterRefs.length >= 4) {
        toast(t("character.tooManyRefs"), true);
        break;
      }
      if (!file.type.startsWith("image/")) {
        toast(t("toast.notAnImage"), true);
        continue;
      }
      if (file.size > MAX_UPLOAD_BYTES) {
        toast(t("toast.imageTooLarge"), true);
        continue;
      }
      const reader = new FileReader();
      reader.onload = () => {
        state.characterRefs.push(reader.result);
        renderCharacterRefPreviews();
      };
      reader.readAsDataURL(file);
    }
  }

  async function saveCharacter() {
    const errEl = $("character-error");
    errEl.hidden = true;
    const name = $("character-name").value.trim();
    const lockText = $("character-lock-text").value.trim();
    if (!name || !lockText) {
      errEl.textContent = t("character.error.required");
      errEl.hidden = false;
      return;
    }
    const btn = $("character-save");
    btn.disabled = true;
    try {
      const result = await api("/api/characters", {
        method: "POST",
        body: JSON.stringify({ name, lock_text: lockText, reference_urls: state.characterRefs }),
      });
      state.characters.unshift(result.character);
      state.selectedCharacterId = result.character.id;
      renderCharacterChips();
      closeCharacterModal();
      toast(t("character.saved"));
    } catch (err) {
      errEl.textContent = err.message;
      errEl.hidden = false;
    } finally {
      btn.disabled = false;
    }
  }

  function wireCharacterModal() {
    $("character-ref-upload").addEventListener("change", handleCharacterRefUpload);
    $("character-cancel").addEventListener("click", closeCharacterModal);
    $("character-save").addEventListener("click", saveCharacter);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !$("character-modal").hidden) closeCharacterModal();
    });
  }

  // -------------------------------------------------------------- images

  /* The video panel's single action button: generate a starting frame
     first if there isn't one yet (from the prompt), or -- once a frame
     exists, whether uploaded or picked from generated candidates -- go
     straight to pricing the video. Same button, two jobs, depending on
     where state.selectedImage stands. */
  async function startVideoFlow() {
    if (!requireSignedIn()) return;
    if (state.selectedImage) {
      await askForVideo();
      return;
    }
    await generateImagesForVideo();
  }

  async function generateImagesForVideo() {
    let prompt = $("image-prompt").value.trim();
    if (!prompt) {
      toast(t("toast.describeFirst"), true);
      $("image-prompt").focus();
      return;
    }

    // A selected character's lock-text (what must stay the same -- face,
    // wardrobe, distinguishing features) goes in front of the shot's own
    // description, and its reference photos ride along as refs so the
    // still-image model has something to match, not just words -- same
    // refs/still_edit path an ad-hoc upload already used.
    const character = selectedCharacter();
    const refs = character ? character.reference_urls : [];
    if (character) prompt = `${character.lock_text}. ${prompt}`;

    state.imageWasUploaded = false;
    const btn = $("btn-video-start");
    btn.disabled = true;
    $("image-results").hidden = false;
    setPanel($("image-results"), workingPanel());

    try {
      const job = await api("/api/generate/image", {
        method: "POST",
        body: JSON.stringify({ prompt, aspect: selectedAspectRatio("aspect-select"), refs }),
      });
      poll(job.id,
        (done) => {
          btn.disabled = false;
          if (done.status === "error") {
            setPanel($("image-results"), errorPanel(translateApiError(done.error), generateImagesForVideo));
            return;
          }
          renderImageChoices(done.outputs || []);
          loadHistory();
          checkAuth();
        },
        () => setPanel($("image-results"), workingPanel()));
    } catch (err) {
      btn.disabled = false;
      setPanel($("image-results"), errorPanel(err.message, generateImagesForVideo));
    }
  }

  /* opts.autoAdvance (default true) immediately continues to the price
     quote once a frame is chosen -- right for a generated candidate (the
     prompt already fully described it, nothing left to add), wrong right
     after an upload (see handleImageUpload, which passes false so the
     user can still type a motion description first). */
  function renderImageChoices(urls, opts = {}) {
    const autoAdvance = opts.autoAdvance !== false;
    $("image-results").hidden = false;
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
            onclick: () => selectImage(url, i, autoAdvance),
          })));
      return card;
    });
    $("image-results").replaceChildren(...cards);
    if (urls.length === 1) selectImage(urls[0], 0, autoAdvance);
  }

  function selectImage(url, index, autoAdvance = true) {
    state.selectedImage = url;
    for (const card of document.querySelectorAll("#image-results .card")) {
      card.classList.toggle("is-selected", Number(card.dataset.index) === index);
      const btn = card.querySelector(".btn");
      if (btn) btn.textContent = Number(card.dataset.index) === index ? t("step2.chosen") : t("step2.choose");
    }
    if (autoAdvance) askForVideo();
  }

  /* The single visible prompt field means one field, two possible jobs:
     if the frame came from AI image generation, the prompt already spent
     itself describing that image, so the actual video motion falls back
     to a chosen preset (see applyPreset) or a safe generic default. If the
     frame was uploaded directly, there was nothing else for the prompt to
     describe -- it IS the motion instruction (see handleImageUpload,
     which swaps the field's placeholder/hint to match). */
  function motionPromptText() {
    const prompt = $("image-prompt").value.trim();
    if (state.imageWasUploaded && prompt) return prompt;
    const move = CAMERA_MOVES.find((m) => m.id === state.selectedCameraMove);
    if (move) return move.text;
    return state.presetMotion || CAMERA_MOVES[0].text; // CAMERA_MOVES[0] is "held" -- same ambient fallback as before
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
    const prompt = motionPromptText();

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
    const btn = $("btn-video-start");
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

  // --------------------------------------------------------- image-only mode

  /* Standalone image generation -- no motion, no model choice (this
     project has exactly one still-image model; see webapp/server.py's
     _generate_image), no selection step. Every variant just renders with
     its own download link -- there's no "next step" to pick one for. */
  function wireImageOnly() {
    $("btn-image-only").addEventListener("click", generateImageOnly);
    $("image-ref-upload").addEventListener("change", handleImageRefUpload);
  }

  function handleImageRefUpload(e) {
    const file = e.target.files[0];
    if (!file) return;
    if (!file.type.startsWith("image/")) {
      toast(t("toast.notAnImage"), true);
      e.target.value = "";
      return;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      toast(t("toast.imageTooLarge"), true);
      e.target.value = "";
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      state.imageOnlyRefs = [reader.result];
      $("image-ref-filename").textContent = file.name;
    };
    reader.onerror = () => toast(t("common.connectError"), true);
    reader.readAsDataURL(file);
  }

  async function generateImageOnly() {
    if (!requireSignedIn()) return;
    const prompt = $("image-only-prompt").value.trim();
    if (!prompt) {
      toast(t("toast.describeFirst"), true);
      $("image-only-prompt").focus();
      return;
    }

    const btn = $("btn-image-only");
    btn.disabled = true;
    setPanel($("image-only-results"), workingPanel());

    try {
      const job = await api("/api/generate/image", {
        method: "POST",
        body: JSON.stringify({
          prompt,
          aspect: selectedAspectRatio("image-aspect-select"),
          refs: state.imageOnlyRefs,
        }),
      });
      poll(job.id,
        (done) => {
          btn.disabled = false;
          if (done.status === "error") {
            setPanel($("image-only-results"), errorPanel(translateApiError(done.error), generateImageOnly));
            return;
          }
          renderImageOnlyResults(done.outputs || []);
          loadHistory();
          checkAuth();
        },
        () => setPanel($("image-only-results"), workingPanel()));
    } catch (err) {
      btn.disabled = false;
      setPanel($("image-only-results"), errorPanel(err.message, generateImageOnly));
    }
  }

  function renderImageOnlyResults(urls) {
    if (!urls.length) {
      setPanel($("image-only-results"), el("div", { class: "empty" },
        el("p", {}, el("strong", { text: t("toast.noImages") }))));
      return;
    }
    $("image-only-results").replaceChildren(...urls.map((url) =>
      el("div", { class: "card" },
        el("img", { src: url, alt: "", loading: "lazy" }),
        el("div", { class: "card-body" },
          el("span", { class: "spacer" }),
          el("a", { class: "dl", href: url, download: "", target: "_blank",
                    rel: "noopener", text: t("step4.download") })))));
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
      const isVideo = job.kind === "video" || job.kind === "postprod" || job.kind === "avatar" || job.kind === "motion_transfer";
      const media = job.status === "error"
        ? el("div", { class: "card-body" },
            el("span", { class: "badge err", text: t("history.failed") }))
        : isVideo
          ? el("video", { src: url, controls: "", playsinline: "", preload: "metadata" })
          : el("img", { src: url, alt: "", loading: "lazy" });
      const label = job.kind === "postprod" ? t("step4.enhance")
        : job.kind === "avatar" ? t("nav.avatar")
        : job.kind === "motion_transfer" ? t("mode.motion.title")
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
        job.status === "done" && url
          ? el("div", { class: "card-body" },
              el("span", { class: "spacer" }),
              el("button", {
                class: "btn ghost small" + (job.public ? " is-active" : ""), type: "button",
                text: t(job.public ? "gallery.unpublish" : "gallery.publish"),
                onclick: (e) => togglePublish(job, e.currentTarget),
              }))
          : null,
        job.status === "error"
          ? el("div", { class: "card-body" },
              el("span", { class: "muted small", text: translateApiError(job.error) || t("history.failed") }))
          : null);
    }));
  }

  async function togglePublish(job, btn) {
    btn.disabled = true;
    try {
      const result = await api(`/api/jobs/${job.id}/publish`, {
        method: "POST",
        body: JSON.stringify({ public: !job.public }),
      });
      job.public = result.public;
      btn.textContent = t(job.public ? "gallery.unpublish" : "gallery.publish");
      btn.classList.toggle("is-active", job.public);
      toast(t(job.public ? "gallery.published" : "gallery.unpublished"));
    } catch (err) {
      toast(err.message, true);
    } finally {
      btn.disabled = false;
    }
  }

  // -------------------------------------------------------------- gallery

  /* Public, opt-in only -- a job shows up here exactly because its owner
     clicked "Publish" on it in their own history (see togglePublish). No
     sign-in required to browse: this is public content by the owner's own
     choice, same as any other public feed. /api/gallery strips owner_id
     and cost_usd server-side before this ever sees a row. */
  async function loadGallery() {
    const list = $("gallery-list");
    let data;
    try {
      data = await api("/api/gallery");
    } catch {
      setPanel(list, errorPanel(t("history.failed"), loadGallery));
      return;
    }
    const jobs = data.jobs || [];
    if (!jobs.length) {
      setPanel(list, emptyPanel("gallery.empty.title", "gallery.empty.body"));
      return;
    }
    list.replaceChildren(...jobs.map((job) => {
      const url = (job.outputs || [])[0];
      const isVideo = job.kind !== "image";
      const media = isVideo
        ? el("video", { src: url, controls: "", playsinline: "", preload: "metadata" })
        : el("img", { src: url, alt: "", loading: "lazy" });
      return el("div", { class: "card" }, media);
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

  // ----------------------------------- motion transfer (video's motion -> a photo)

  /* fal-ai/kling-video/v2.6/standard/motion-control -- applies a reference
     video's movement to a static character photo (see
     scripts/config.json's _motion_transfer_note). Same quote-then-confirm
     shape as avatar: the video can be large, so it's a URL field rather
     than an upload (unlike the character photo, which is small enough to
     go as a data: URI the same way every other photo upload in this file
     does). */
  function wireMotionTransfer() {
    $("motion-image-upload").addEventListener("change", handleMotionImageUpload);
    $("btn-motion").addEventListener("click", quoteMotionTransfer);
  }

  function handleMotionImageUpload(e) {
    const file = e.target.files[0];
    if (!file) return;
    if (!file.type.startsWith("image/")) {
      toast(t("toast.notAnImage"), true);
      e.target.value = "";
      return;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      toast(t("toast.imageTooLarge"), true);
      e.target.value = "";
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      state.motionImageDataUrl = reader.result;
      $("motion-image-filename").textContent = file.name;
    };
    reader.onerror = () => toast(t("common.connectError"), true);
    reader.readAsDataURL(file);
  }

  async function quoteMotionTransfer() {
    if (!requireSignedIn()) return;
    const errEl = $("motion-error");
    errEl.hidden = true;
    const imageUrl = state.motionImageDataUrl;
    const videoUrl = $("motion-video-url").value.trim();
    if (!imageUrl || !videoUrl) {
      errEl.textContent = t("motion.error.required");
      errEl.hidden = false;
      return;
    }
    const prompt = $("motion-prompt").value.trim();
    let quote;
    try {
      quote = await api("/api/motion-transfer/quote", {
        method: "POST",
        body: JSON.stringify({ image_url: imageUrl, video_url: videoUrl }),
      });
    } catch (err) {
      errEl.textContent = err.message;
      errEl.hidden = false;
      return;
    }
    $("confirm-cost").textContent = money(quote.cost_usd);
    $("confirm-detail").textContent = "";
    openConfirm(() => runMotionTransfer(imageUrl, videoUrl, prompt, quote));
  }

  async function runMotionTransfer(imageUrl, videoUrl, prompt, quote) {
    const btn = $("btn-motion");
    btn.disabled = true;
    setPanel($("motion-results"), workingPanel());
    try {
      const body = { image_url: imageUrl, video_url: videoUrl, approved_cost: quote.cost_usd };
      if (prompt) body.prompt = prompt;
      const job = await api("/api/motion-transfer/run", { method: "POST", body: JSON.stringify(body) });
      poll(job.id,
        (done) => {
          btn.disabled = false;
          if (done.status === "error") {
            setPanel($("motion-results"), errorPanel(translateApiError(done.error),
              () => runMotionTransfer(imageUrl, videoUrl, prompt, quote)));
            return;
          }
          renderMotionResult(done);
          loadHistory();
          checkAuth();
          toast(t("toast.videoReady"));
        },
        () => setPanel($("motion-results"), workingPanel()));
    } catch (err) {
      btn.disabled = false;
      setPanel($("motion-results"), errorPanel(err.message,
        () => runMotionTransfer(imageUrl, videoUrl, prompt, quote)));
    }
  }

  function renderMotionResult(job) {
    const url = (job.outputs || [])[0];
    if (!url) {
      setPanel($("motion-results"), el("div", { class: "empty" },
        el("p", {}, el("strong", { text: t("toast.noVideo") }))));
      return;
    }
    setPanel($("motion-results"),
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

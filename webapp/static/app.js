/* Video Factory — front end. No framework, no build step. */

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    config: null,
    selectedImage: null,
    activePreset: null,
    polls: new Map(),
    auth: { enabled: false, user: null, balance: null },
    authMode: "login",
  };

  // ---------------------------------------------------------------- utils

  async function api(path, options) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    let data = null;
    try {
      data = await res.json();
    } catch {
      throw new Error("The server returned an unreadable response.");
    }
    if (!res.ok) throw new Error(data && data.error ? data.error : "Request failed.");
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

  const workingPanel = (label) =>
    el("div", { class: "state working" },
      el("p", {}, el("strong", { text: label })),
      el("p", { class: "muted small", text: "You can leave this tab open — progress keeps updating." }),
      el("div", { class: "bar" }, el("i", {})));

  const errorPanel = (message, onRetry) =>
    el("div", { class: "state error" },
      el("p", {}, el("strong", { text: "That didn't work" })),
      el("p", { class: "muted", text: message }),
      onRetry ? el("p", {}, el("button", {
        class: "btn ghost small", type: "button", onclick: onRetry, text: "Try again",
      })) : null);

  const emptyPanel = (title, body) =>
    el("div", { class: "empty" },
      el("p", {}, el("strong", { text: title })),
      el("p", { class: "muted", text: body }));

  // ---------------------------------------------------------------- boot

  async function boot() {
    try {
      state.config = await api("/api/config");
    } catch (err) {
      setPanel($("image-results"), errorPanel(
        "Could not reach the Video Factory server. Is it still running?", boot));
      return;
    }
    renderPresets();
    renderSelects();
    wireAuth();
    updateImageCost();
    updateVideoCost();
    checkHealth();
    await checkAuth();
    loadHistory();
  }

  async function checkHealth() {
    try {
      const h = await api("/api/health");
      const problems = [];
      if (!h.fal_key_configured) problems.push("No fal.ai API key configured");
      if (!h.ffmpeg_available) problems.push("ffmpeg not installed");
      if (problems.length) {
        const badge = $("health");
        badge.textContent = problems.join(" · ");
        badge.hidden = false;
      }
    } catch { /* health is advisory only */ }
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
        el("button", { class: "btn ghost small", type: "button", text: "Sign out", onclick: signOut }));
    } else {
      bar.replaceChildren(
        el("button", { class: "btn ghost small", type: "button", text: "Sign in", onclick: () => openAuthModal("login") }));
    }
  }

  /* Returns true if generation may proceed. In single-tenant mode (auth
     disabled) this is always true -- that's what keeps the original
     no-login flow working with zero configuration. */
  function requireSignedIn() {
    if (!state.auth.enabled) return true;
    if (state.auth.user) return true;
    toast("Sign in to generate.", true);
    openAuthModal("login");
    return false;
  }

  function openAuthModal(mode) {
    state.authMode = mode;
    $("auth-title").textContent = mode === "signup" ? "Create account" : "Sign in";
    $("auth-submit").textContent = mode === "signup" ? "Create account" : "Sign in";
    $("auth-toggle").textContent = mode === "signup"
      ? "Already have an account? Sign in" : "Need an account? Sign up";
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
        errEl.textContent = result.message || "Check your email to confirm your account.";
        errEl.hidden = false;
        return;
      }
      closeAuthModal();
      $("auth-email").value = "";
      $("auth-password").value = "";
      await checkAuth();
      loadHistory();
      toast(`Signed in as ${result.user.email}.`);
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

  function renderPresets() {
    const wrap = $("presets");
    wrap.replaceChildren(...state.config.presets.map((p) =>
      el("button", {
        class: "preset", type: "button", "data-id": p.id,
        onclick: () => applyPreset(p),
      },
        el("strong", { text: p.name }),
        el("span", { text: p.blurb }))));
  }

  function applyPreset(preset) {
    state.activePreset = preset.id;
    $("video-prompt").value = preset.motion;
    if (preset.seconds) $("seconds").value = String(preset.seconds);
    if (preset.model_hint &&
        state.config.models.some((m) => m.id === preset.model_hint)) {
      $("model").value = preset.model_hint;
    }
    for (const btn of document.querySelectorAll(".preset")) {
      btn.classList.toggle("is-active", btn.dataset.id === preset.id);
    }
    updateVideoCost();
    toast(`"${preset.name}" applied to the motion description.`);
  }

  function renderSelects() {
    $("aspect").replaceChildren(...state.config.aspect_ratios.map((a) =>
      el("option", { value: a, text: a })));

    $("seconds").replaceChildren(...state.config.durations.map((d) =>
      el("option", { value: String(d), text: `${d} seconds`, ...(d === 6 ? { selected: "" } : {}) })));

    $("model").replaceChildren(...state.config.models.map((m) =>
      el("option", {
        value: m.id,
        text: `${m.name} — ${money(m.rate)}/s`,
        ...(m.default ? { selected: "" } : {}),
      })));

    $("aspect").addEventListener("change", updateImageCost);
    $("variants").addEventListener("change", updateImageCost);
    $("model").addEventListener("change", updateVideoCost);
    $("seconds").addEventListener("change", updateVideoCost);
    $("btn-image").addEventListener("click", generateImages);
    $("btn-video").addEventListener("click", askForVideo);
    $("tab-create").addEventListener("click", () => switchView("create"));
    $("tab-history").addEventListener("click", () => switchView("history"));
  }

  function switchView(which) {
    const create = which === "create";
    $("view-create").hidden = !create;
    $("view-history").hidden = create;
    $("tab-create").classList.toggle("is-active", create);
    $("tab-history").classList.toggle("is-active", !create);
    if (!create) loadHistory();
  }

  function updateImageCost() {
    const n = Number($("variants").value);
    $("image-cost").textContent = money(state.config.image_cost * n);
  }

  function currentModel() {
    return state.config.models.find((m) => m.id === $("model").value)
        || state.config.models[0];
  }

  function updateVideoCost() {
    const model = currentModel();
    const secs = Number($("seconds").value);
    $("video-cost").textContent = money(model.rate * secs);
    $("model-note").textContent = model.note;
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
          onDone({ status: "error", error: "Lost contact with the server while generating." });
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
      toast("Describe the shot first.", true);
      $("image-prompt").focus();
      return;
    }

    const btn = $("btn-image");
    btn.disabled = true;
    setPanel($("image-results"), workingPanel("Generating images…"));

    try {
      const job = await api("/api/generate/image", {
        method: "POST",
        body: JSON.stringify({
          prompt,
          count: Number($("variants").value),
          aspect: $("aspect").value,
        }),
      });
      poll(job.id,
        (done) => {
          btn.disabled = false;
          if (done.status === "error") {
            setPanel($("image-results"), errorPanel(done.error, generateImages));
            return;
          }
          renderImageChoices(done.outputs || []);
          loadHistory();
          checkAuth();
        },
        (p) => setPanel($("image-results"), workingPanel(p.stage || "Working…")));
    } catch (err) {
      btn.disabled = false;
      setPanel($("image-results"), errorPanel(err.message, generateImages));
    }
  }

  function renderImageChoices(urls) {
    if (!urls.length) {
      setPanel($("image-results"), emptyPanel(
        "No images came back", "The model returned nothing. Try rewording the prompt."));
      return;
    }
    /* Selection is tracked by index, not by URL: two variants can legitimately
       come back with the same URL, and matching on the string would highlight
       both. */
    const cards = urls.map((url, i) => {
      const card = el("div", { class: "card", "data-index": String(i) },
        el("img", { src: url, alt: `Variant ${i + 1}`, loading: "lazy" }),
        el("div", { class: "card-body" },
          el("span", { class: "badge", text: `Variant ${i + 1}` }),
          el("span", { class: "spacer" }),
          el("button", {
            class: "btn ghost small", type: "button", text: "Use this",
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
    }
    $("btn-video").disabled = false;
    $("video-gate").textContent = "Starting frame selected. Describe the motion and generate.";
    $("step-video").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // -------------------------------------------------------------- video

  /* The paid path. The server quotes, the user confirms that exact number,
     and only then does anything get charged — same gate as the CLI. */
  async function askForVideo() {
    if (!requireSignedIn()) return;
    const prompt = $("video-prompt").value.trim();
    if (!prompt) {
      toast("Describe the motion first.", true);
      $("video-prompt").focus();
      return;
    }
    if (!state.selectedImage) {
      toast("Pick a starting frame in step 2.", true);
      return;
    }

    let quote;
    try {
      quote = await api("/api/quote", {
        method: "POST",
        body: JSON.stringify({
          model: $("model").value,
          seconds: Number($("seconds").value),
        }),
      });
    } catch (err) {
      toast(err.message, true);
      return;
    }

    $("confirm-cost").textContent = money(quote.cost_usd);
    $("confirm-detail").textContent =
      `${currentModel().name} · ${quote.shown_as}`;
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
    setPanel($("video-results"), workingPanel("Starting render…"));

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
            setPanel($("video-results"), errorPanel(done.error,
              () => startVideo(prompt, quote)));
            return;
          }
          renderVideo(done);
          loadHistory();
          checkAuth();
          toast("Video ready.");
        },
        (p) => setPanel($("video-results"), workingPanel(p.stage || "Rendering…")));
    } catch (err) {
      btn.disabled = false;
      setPanel($("video-results"), errorPanel(err.message,
        () => startVideo(prompt, quote)));
    }
  }

  function renderVideo(job) {
    const url = (job.outputs || [])[0];
    if (!url) {
      setPanel($("video-results"), emptyPanel(
        "No video came back", "The model finished but returned nothing."));
      return;
    }
    setPanel($("video-results"),
      el("div", { class: "card" },
        el("video", { src: url, controls: "", playsinline: "", preload: "metadata" }),
        el("div", { class: "card-body" },
          el("span", { class: "badge ok", text: money(job.cost_usd || 0) }),
          el("span", { class: "spacer" }),
          el("a", { class: "dl", href: url, download: "", target: "_blank",
                    rel: "noopener", text: "Download" }))));
  }

  // ------------------------------------------------------------ history

  async function loadHistory() {
    const list = $("history-list");
    if (state.auth.enabled && !state.auth.user) {
      setPanel(list, emptyPanel("Sign in to see your history", "Your generations are private to your account."));
      return;
    }
    let data;
    try {
      data = await api("/api/jobs");
    } catch {
      setPanel(list, errorPanel("Could not load history.", loadHistory));
      return;
    }

    const done = (data.jobs || []).filter((j) => (j.outputs || []).length || j.status === "error");
    if (!done.length) {
      setPanel(list, emptyPanel("Nothing generated yet",
        "Images and videos you create will be collected here."));
      return;
    }

    list.replaceChildren(...done.slice(0, 40).map((job) => {
      const url = (job.outputs || [])[0];
      const media = job.status === "error"
        ? el("div", { class: "card-body" },
            el("span", { class: "badge err", text: "Failed" }))
        : job.kind === "video"
          ? el("video", { src: url, controls: "", playsinline: "", preload: "metadata" })
          : el("img", { src: url, alt: "", loading: "lazy" });

      return el("div", { class: "card" },
        media,
        el("div", { class: "card-body" },
          el("span", { class: "badge", text: job.kind === "video" ? "Video" : "Image" }),
          el("span", { class: "muted small", text: money(job.cost_usd || 0) }),
          el("span", { class: "spacer" }),
          url ? el("a", { class: "dl", href: url, download: "", target: "_blank",
                          rel: "noopener", text: "Download" }) : null),
        job.status === "error"
          ? el("div", { class: "card-body" },
              el("span", { class: "muted small", text: job.error || "Failed" }))
          : null);
    }));
  }

  boot();
})();

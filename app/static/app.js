// Copy caption — works on plain http too (clipboard API needs https, so fall back to execCommand).
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-copy]");
  if (!btn) return;
  e.preventDefault();
  const el = document.getElementById(btn.dataset.copy);
  const text = el.value;
  let ok = false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      ok = true;
    }
  } catch (_) {}
  if (!ok) {
    const ro = el.readOnly;
    el.readOnly = false;
    el.focus();
    el.select();
    el.setSelectionRange(0, text.length);
    ok = document.execCommand("copy");
    el.readOnly = ro;
    window.getSelection().removeAllRanges();
  }
  const old = btn.textContent;
  btn.textContent = ok ? "Copied ✓" : "Select & copy manually";
  btn.classList.toggle("copied", ok);
  setTimeout(() => { btn.textContent = old; btn.classList.remove("copied"); }, 1600);
});

// While something is rendering or the caption is still being written, poll and reload when it changes.
(function poll() {
  const pending = [...document.querySelectorAll("[data-post]")].filter((n) => {
    const cap = n.querySelector("textarea.caption");
    return ["queued", "rendering", "none"].includes(n.dataset.render) || (cap && cap.value.trim() === "Writing caption…") || (cap && cap.value.trim() === "");
  });
  if (!pending.length) return;
  const ids = pending.map((n) => n.dataset.post);
  const snapshot = {};
  for (const n of pending) {
    const cap = n.querySelector("textarea.caption");
    const hasCap = cap && cap.value.trim() && cap.value.trim() !== "Writing caption…";
    snapshot[n.dataset.post] = n.dataset.render + "|" + (hasCap ? 1 : 0);
  }
  const tick = async () => {
    for (const id of ids) {
      try {
        const r = await fetch(`/post/${id}/status`, { credentials: "same-origin" });
        const j = await r.json();
        const key = j.render_status + "|" + (j.caption ? 1 : 0);
        if (snapshot[id] === undefined) snapshot[id] = key;
        else if (snapshot[id] !== key) {
          // don't wipe a caption the user is typing
          if (document.activeElement && document.activeElement.tagName === "TEXTAREA" && !document.activeElement.readOnly) return;
          location.reload();
          return;
        }
      } catch (_) {}
    }
    setTimeout(tick, 3000);
  };
  tick();
})();

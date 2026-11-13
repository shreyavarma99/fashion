// Capture screen: boxes appear as each piece is identified; the user taps the
// piece they want and only that piece gets searched. Results slide up in a sheet;
// closing it returns to the screenshot to pick another piece.

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const money = (n) => (n == null ? "" : "$" + Math.round(n).toLocaleString());

const params = new URLSearchParams(location.search);
const savedId = params.get("saved");
const lookId = location.pathname.split("/").pop();

let look = null;
let selected = null;        // index of the piece shown in the sheet
let bucketName = "closest";
let boxesDrawn = 0;
let polling = false;
const started = performance.now();
const ACTIVE = ["searching", "ranking"];

// ---------------------------------------------------------------- storage (best effort)
const store = {
  get() { try { return JSON.parse(localStorage.getItem("seen.closet") || "[]"); } catch { return []; } },
  set(v) { try { localStorage.setItem("seen.closet", JSON.stringify(v)); } catch {} },
};

// ---------------------------------------------------------------- polling
function needsPolling() {
  if (!look) return true;
  if (look.status === "reading") return true;
  return look.pieces.some((p) => ACTIVE.includes(p.status));
}

async function poll() {
  if (polling) return;
  polling = true;
  while (needsPolling()) {
    try {
      const r = await fetch(`/api/looks/${lookId}`, { cache: "no-store" });
      if (r.status === 404) { polling = false; return fail("This look has expired.", "Looks are deleted after an hour. Press the button again on the post."); }
      look = await r.json();
      render();
    } catch {}
    if (needsPolling()) await new Promise((res) => setTimeout(res, 250));
  }
  polling = false;
}

// ---------------------------------------------------------------- stage
function render() {
  const s = look.status;
  $("stage").classList.toggle("found", s !== "reading");
  if (!$("shot").src) $("shot").src = look.shot;
  const n = look.pieces.length;

  if (s === "error") return fail("Something went wrong.", look.error || "Try again in a moment.");
  if (s === "empty") return fail("No outfit found.", "Try again on a post where the outfit is clearly visible.");

  $("pill").textContent = s === "reading" ? (n ? `${n} found` : "reading") : `${n} piece${n === 1 ? "" : "s"}`;
  if (s === "reading") {
    $("pickTitle").textContent = n ? `Found ${n}…` : "Finding pieces";
    $("pickSub").textContent = n ? "Tap one now, or wait for the rest" : "Reading the look…";
  } else {
    $("pickTitle").textContent = `Found ${n} piece${n === 1 ? "" : "s"}`;
    $("pickSub").textContent = "Tap the one you want to shop";
  }

  drawBoxes();
  renderChips();
  if (selected !== null) renderSheet();
}

function drawBoxes() {
  const img = $("shot");
  if (!img.complete || !img.naturalWidth) { img.onload = () => { boxesDrawn = 0; drawBoxes(); }; return; }
  const k = img.clientWidth / look.width;
  for (let i = boxesDrawn; i < look.pieces.length; i++) {
    const p = look.pieces[i];
    const [x0, y0, x1, y1] = p.box;
    const el = document.createElement("button");
    el.className = "box";
    el.dataset.i = i;
    el.setAttribute("aria-label", `Shop ${p.label}`);
    Object.assign(el.style, { left: x0 * k + "px", top: y0 * k + "px", width: (x1 - x0) * k + "px", height: (y1 - y0) * k + "px" });
    el.innerHTML = `<span class="tag"><b>${String(i + 1).padStart(2, "0")}</b>${esc(p.label)}</span>`;
    $("wrap").appendChild(el);
    requestAnimationFrame(() => el.classList.add("on"));
  }
  boxesDrawn = look.pieces.length;
  for (const el of document.querySelectorAll(".box")) {
    el.classList.toggle("picked", +el.dataset.i === selected);
    el.classList.toggle("dim", selected !== null && +el.dataset.i !== selected);
  }
}

function renderChips() {
  $("chips").innerHTML = look.pieces
    .map((p, i) => `<button class="pchip ${p.status !== "idle" ? "seen" : ""}" data-i="${i}">
        <span class="sw" style="background:${esc(p.color)}"></span>${esc(p.label)}</button>`)
    .join("");
}

// ---------------------------------------------------------------- picking a piece
async function pick(i) {
  selected = i;
  bucketName = "closest";
  const p = look.pieces[i];
  if (p.status === "idle" || p.status === "error") {
    p.status = "searching";  // optimistic, so the sheet shows skeletons immediately
    fetch(`/api/looks/${lookId}/pieces/${i}/search`, { method: "POST" }).catch(() => {});
  }
  drawBoxes();
  $("picker").classList.add("hide");
  $("sheet").classList.add("open");
  renderSheet();
  $("list").scrollTop = 0;
  poll();
}

function closeSheet() {
  selected = null;
  $("sheet").classList.remove("open");
  $("picker").classList.remove("hide");
  drawBoxes();
  renderChips();
}

// ---------------------------------------------------------------- results sheet
function renderSheet() {
  const p = look.pieces[selected];
  if (!p) return;
  const n = look.pieces.length;
  $("headImg").src = p.crop || p.thumb || "";
  $("headTitle").textContent = p.label;
  $("headSub").innerHTML = `${n > 1 ? `piece ${selected + 1} of ${n}` : "1 piece"}` + (look.creator ? ` · from <span class="a">@${esc(look.creator)}</span>` : "");

  $("tabs").hidden = n < 2;
  $("tabs").innerHTML = look.pieces
    .map((q, i) => `<button class="tab ${i === selected ? "on" : ""} ${ACTIVE.includes(q.status) ? "wait" : ""}" data-i="${i}">
        <span class="sw" style="background:${esc(q.color)}"></span>${esc(q.label)}</button>`)
    .join("");
  for (const b of $("seg").children) b.classList.toggle("on", b.dataset.b === bucketName);

  $("list").innerHTML = productList(p);

  const buyable = (rows) => rows.find((r) => r.in_stock !== false);
  const best = p.status === "done" ? buyable(p.results.closest) || buyable(p.results.similar) : null;
  $("best").classList.toggle("off", !best);
  $("best").href = best ? best.link : "#";
  $("bestPrice").textContent = best ? best.price_label || money(best.price) : "";
  $("save").classList.toggle("on", isSaved(selected));
}

function productList(p) {
  if (p.status === "searching" || p.status === "idle") {
    return `<div class="checking">Searching for this ${esc(p.label.toLowerCase())}…</div><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>`;
  }
  if (p.status === "error") return `<div class="empty">Couldn't search for this piece.<br><button class="retry" data-i="${selected}">Try again</button></div>`;
  const rows = p.results[bucketName];
  // Only say "checking" while what's on screen is still unverified.
  const pending = ["closest", "similar", "under"].some((b) => p.results[b].some((r) => r.match === "pending"));
  const checking = p.status === "ranking" && pending ? `<div class="checking">Checking matches…</div>` : "";
  if (!rows.length) {
    if (checking) return checking + `<div class="skeleton"></div><div class="skeleton"></div>`;
    const why = { closest: "No close matches found.<br>Check Similar for alternatives.", similar: "No similar pieces found.", under: "Nothing under $50 that matches well." };
    return `<div class="empty">${why[bucketName]}</div>`;
  }
  return checking + rows.map((r, i) => row(r, i === 0 && bucketName === "closest")).join("");
}

function row(r, first) {
  const soldOut = r.in_stock === false;
  const chip = r.match === "exact" ? "Exact match" : first && r.match !== "pending" ? "Closest match" : "";
  const tags = [
    chip && `<span class="chip">${esc(chip)}</span>`,
    soldOut && `<span class="chip muted">Sold out</span>`,
    r.condition === "pre-owned" && `<span class="chip muted">Pre-owned</span>`,
  ].filter(Boolean).join(" ");
  const was = r.old_price && !soldOut ? `<s class="was">${esc(r.old_price)}</s>` : "";
  return `<a class="prod ${soldOut ? "soldout" : ""}" href="${esc(r.link)}" target="_blank" rel="noopener">
    <img src="${esc(r.thumbnail)}" alt="" loading="lazy" referrerpolicy="no-referrer">
    <div class="info">
      <div class="src">${esc(r.source)}</div>
      <div class="name">${esc(r.title)}</div>
      ${tags ? `<div class="tags">${tags}</div>` : ""}
    </div>
    <div class="price">${esc(r.price_label || money(r.price))}${was}</div><span class="chev">›</span>
  </a>`;
}

// ---------------------------------------------------------------- saving (per piece)
const saveKey = (i) => `${lookId}:${i}`;
const isSaved = (i) => store.get().some((x) => x.id === saveKey(i));

$("save").addEventListener("click", () => {
  const p = look.pieces[selected];
  const closet = store.get();
  const k = saveKey(selected);
  const at = closet.findIndex((x) => x.id === k);
  if (at >= 0) { closet.splice(at, 1); toast("Removed"); }
  else {
    if (p.status !== "done") return toast("Wait for the matches first");
    const thumb = (p.results.closest[0] || p.results.similar[0])?.thumbnail;
    closet.unshift({ id: k, savedAt: Date.now(), creator: look.creator, thumb,
                     pieces: [{ label: p.label, color: p.color, status: "done", results: p.results, thumb }] });
    toast(`Saved ${p.label.toLowerCase()}`);
  }
  store.set(closet);
  renderSheet();
});

// ---------------------------------------------------------------- events
$("wrap").addEventListener("click", (e) => { const b = e.target.closest(".box"); if (b) pick(+b.dataset.i); });
$("chips").addEventListener("click", (e) => { const b = e.target.closest(".pchip"); if (b) pick(+b.dataset.i); });
$("tabs").addEventListener("click", (e) => { const t = e.target.closest(".tab"); if (t) pick(+t.dataset.i); });
$("list").addEventListener("click", (e) => { const b = e.target.closest(".retry"); if (b) { e.preventDefault(); pick(+b.dataset.i); } });
$("seg").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  bucketName = b.dataset.b;
  renderSheet();
  $("list").scrollTop = 0;
});
$("best").addEventListener("click", (e) => { if ($("best").classList.contains("off")) e.preventDefault(); });
$("close").addEventListener("click", () => (savedId ? (location.href = "/") : closeSheet()));
window.addEventListener("resize", () => { document.querySelectorAll(".box").forEach((b) => b.remove()); boxesDrawn = 0; if (look?.width) drawBoxes(); });

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("on");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => t.classList.remove("on"), 1600);
}

function fail(title, msg) {
  $("picker").classList.add("hide");
  if (!document.querySelector(".err")) {
    const el = document.createElement("div");
    el.className = "err";
    el.innerHTML = `<strong>${esc(title)}</strong><p>${esc(msg)}</p><a class="btn" href="/">OK</a>`;
    $("stage").appendChild(el);
  }
}

setInterval(() => {
  if (!look || look.status === "reading") $("timer").textContent = ((performance.now() - started) / 1000).toFixed(1) + "s";
  else if (look.timings?.all_pieces) $("timer").textContent = look.timings.all_pieces.toFixed(1) + "s";
}, 100);

// ---------------------------------------------------------------- boot
if (savedId) {
  const s = store.get().find((x) => x.id === savedId);
  if (!s) fail("Not found.", "It may have been removed.");
  else {
    look = { status: "done", creator: s.creator, pieces: s.pieces.map((p) => ({ ...p, crop: p.thumb || s.thumb })), shot: null };
    $("stage").classList.add("found");
    $("picker").classList.add("hide");
    selected = 0;
    $("sheet").classList.add("open");
    renderSheet();
  }
} else {
  poll();
}

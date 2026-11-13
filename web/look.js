// Capture screen: the screenshot "develops" like a polaroid while pieces are
// spotted; tap a piece to search just that one; results slide up as a rack of
// tagged cards. Closing the sheet goes back to the photo to pick another piece.

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const money = (n) => (n == null ? "" : "$" + Math.round(n).toLocaleString());
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

const params = new URLSearchParams(location.search);
const savedId = params.get("saved");
const lookId = location.pathname.split("/").pop();

let look = null;
let selected = null;        // index of the piece shown in the sheet
let bucketName = "closest";
let boxesDrawn = 0;
let chipsDrawn = -1;
let polling = false;
const started = performance.now();
const ACTIVE = ["searching", "ranking"];

const SPOTTING = ["hold still, darling", "squinting at the stitching", "reading the labels", "admiring the fit"];
const HUNTING = ["Rifling through the racks…", "Asking every boutique in town…", "Comparing the stitching…", "Checking price tags…", "Flipping through the catalogues…"];

// ---------------------------------------------------------------- storage (best effort)
const store = {
  get() { try { return JSON.parse(localStorage.getItem("seen.closet") || "[]"); } catch { return []; } },
  set(v) { try { localStorage.setItem("seen.closet", JSON.stringify(v)); } catch {} },
};

// ---------------------------------------------------------------- polling
const requested = new Set();  // pieces we asked to search; keep polling until the server agrees
function needsPolling() {
  if (!look) return true;
  if (look.status === "reading") return true;
  return look.pieces.some((p, i) => ACTIVE.includes(p.status) || (requested.has(i) && p.status === "idle"));
}

async function poll() {
  if (polling) return;
  polling = true;
  while (needsPolling()) {
    try {
      const r = await fetch(`/api/looks/${lookId}`, { cache: "no-store" });
      if (r.status === 404) { polling = false; return fail("This look has faded.", "Looks are kept for an hour. Press the button on the post again."); }
      look = await r.json();
      render();
    } catch {}
    if (needsPolling()) await new Promise((res) => setTimeout(res, 250));
  }
  polling = false;
}

// ---------------------------------------------------------------- the darkroom
function render() {
  const s = look.status;
  const n = look.pieces.length;
  const stage = $("stage");
  stage.classList.toggle("found", s !== "reading");
  if (n || s !== "reading") stage.classList.add("developed");
  if (!$("shot").src) $("shot").src = look.shot;
  $("capHandle").textContent = look.creator ? `@${look.creator}` : "spotted ✶";

  if (s === "error") return fail("Well, that's embarrassing.", look.error || "Something went sideways. Try again in a moment.");
  if (s === "empty") return fail("No outfit here?", "I couldn't spot any clothes. Try a post where the fit is clearly visible.");

  $("pill").textContent = s === "reading" ? (n ? `${n} spotted` : "developing…") : plural(n, "piece");
  if (s === "reading") {
    $("pickTitle").textContent = n ? `Found ${n} so far…` : "Spotting the goods…";
    if (n) $("pickSub").textContent = "tap one now if you can't wait";
  } else {
    $("pickTitle").textContent = `Found ${plural(n, "treasure")}`;
    $("pickSub").textContent = n === 1 ? "tap it to go shopping" : "tap the one you're coveting";
  }

  drawBoxes();
  renderChips();
  if (selected !== null) renderSheet();
}

// Size the photo to fit the space between the pill and the tray, keeping its
// aspect ratio, in exact pixels, so the boxes (in image pixels) line up with it.
function fitPhoto() {
  if (!look?.width) return;
  const img = $("shot");
  const top = 60, tray = 200, framePad = 10 * 2, caption = 42;
  const maxW = window.innerWidth - 32 - framePad;
  const maxH = window.innerHeight - top - tray - framePad - caption - 8;
  const s = Math.min(maxW / look.width, maxH / look.height);
  img.style.width = Math.round(look.width * s) + "px";
  img.style.height = Math.round(look.height * s) + "px";
}

function drawBoxes() {
  const img = $("shot");
  fitPhoto();
  if (!img.complete || !img.naturalWidth) { img.onload = () => { boxesDrawn = 0; drawBoxes(); }; return; }
  const k = img.clientWidth / look.width;
  if (boxesDrawn === 0) placedTags = [];
  for (let i = boxesDrawn; i < look.pieces.length; i++) {
    const p = look.pieces[i];
    const [x0, y0, x1, y1] = p.box;
    const el = document.createElement("button");
    el.className = "box";
    el.dataset.i = i;
    el.setAttribute("aria-label", `Shop the ${p.label}`);
    Object.assign(el.style, { left: x0 * k + "px", top: y0 * k + "px", width: (x1 - x0) * k + "px", height: (y1 - y0) * k + "px" });
    el.innerHTML = `<span class="tag"><b>${String(i + 1).padStart(2, "0")}</b>${esc(p.label)}</span>`;
    $("wrap").appendChild(el);
    placeTag(el, x0 * k, y0 * k, (y1 - y0) * k);
    requestAnimationFrame(() => el.classList.add("on"));
  }
  boxesDrawn = look.pieces.length;
  for (const el of document.querySelectorAll(".box")) {
    el.classList.toggle("picked", +el.dataset.i === selected);
    el.classList.toggle("dim", selected !== null && +el.dataset.i !== selected);
  }
}

let chipsKey = "";
// Labels sit on the top edge of their box; when two boxes start near the same
// spot, nudge the later label down until it no longer covers an earlier one.
let placedTags = [];
function placeTag(box, left, top, height) {
  const tag = box.querySelector(".tag");
  const w = tag.offsetWidth, h = tag.offsetHeight + 4;
  let dy = 0;
  const hits = (y) => placedTags.some((r) => left + 8 < r.x + r.w && r.x < left + 8 + w && top - 13 + y < r.y + r.h && r.y < top - 13 + y + h);
  while (hits(dy) && dy < Math.max(0, height - 10)) dy += h;
  if (dy) tag.style.top = `${-13 + dy}px`;
  placedTags.push({ x: left + 8, y: top - 13 + dy, w, h });
}

function renderChips() {
  // Re-render only when something changed, and let only new chips play the drop-in animation.
  const key = look.status + look.pieces.map((p) => (p.status === "idle" ? 0 : 1)).join("");
  if (key === chipsKey) return;
  chipsKey = key;
  const before = Math.max(chipsDrawn, 0);
  chipsDrawn = look.pieces.length;
  const chips = look.pieces.map((p, i) => `<button class="ctag pchip ${p.status !== "idle" ? "seen" : ""}" data-i="${i}" ${i < before ? 'style="animation:none"' : ""}>
      <span class="sw" style="background:${esc(p.color)}"></span>${esc(p.label)}</button>`);
  if (look.status === "reading") chips.push(`<span class="ghost-chip"></span>`);
  $("chips").innerHTML = chips.join("");
}

// ---------------------------------------------------------------- picking a piece
function pick(i) {
  selected = i;
  bucketName = "closest";
  const p = look.pieces[i];
  if (p.status === "idle" || p.status === "error") {
    p.status = "searching";  // optimistic, so the sheet shows the hunt immediately
    requested.add(i);
    fetch(`/api/looks/${lookId}/pieces/${i}/search`, { method: "POST" }).catch(() => {}).finally(poll);
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
  chipsKey = "";
  drawBoxes();
  renderChips();
}

// ---------------------------------------------------------------- results sheet
function renderSheet() {
  const p = look.pieces[selected];
  if (!p) return;
  const n = look.pieces.length;
  $("headImg").src = p.crop || p.thumb || "";
  $("headTitle").textContent = `The ${p.label.charAt(0).toLowerCase()}${p.label.slice(1)}`;
  $("headSub").innerHTML = `${n > 1 ? `no. ${selected + 1} of ${n}` : "the one"}` + (look.creator ? ` · seen on <span class="a">@${esc(look.creator)}</span>` : "");

  $("tabs").hidden = n < 2;
  $("tabs").innerHTML = look.pieces
    .map((q, i) => `<button class="ctag tab ${i === selected ? "on" : ""} ${ACTIVE.includes(q.status) ? "wait" : ""}" data-i="${i}">
        <span class="sw" style="background:${esc(q.color)}"></span>${esc(q.label)}</button>`)
    .join("");
  for (const b of $("seg").children) b.classList.toggle("on", b.dataset.b === bucketName);

  $("list").innerHTML = productList(p);

  const buyable = (rows) => rows.find((r) => r.in_stock !== false);
  const verified = (rows) => rows.filter((r) => r.match !== "pending");
  const best = buyable(verified(p.results.closest)) || buyable(verified(p.results.similar)) || null;
  $("best").classList.toggle("off", !best);
  $("best").href = best ? best.link : "#";
  $("bestPrice").textContent = best ? best.price_label || money(best.price) : "";
  $("save").classList.toggle("on", isSaved(selected));
}

function productList(p) {
  if (p.status === "searching" || p.status === "idle") {
    return `<div class="checking" id="hunt">${HUNTING[0]}</div><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>`;
  }
  if (p.status === "error") {
    return `<div class="empty"><div class="big">Couldn't track this one down.</div><p>The racks came up empty. Give it another go?</p>
      <button class="btn retry" data-i="${selected}">Try again</button></div>`;
  }
  const rows = p.results[bucketName];
  const pending = ["closest", "similar", "under"].some((b) => p.results[b].some((r) => r.match === "pending"));
  const checking = p.status === "ranking" && pending ? `<div class="checking">Double-checking these…</div>` : "";
  if (!rows.length) {
    if (p.status === "ranking") return `<div class="checking">Checking a few more shops…</div><div class="skeleton"></div><div class="skeleton"></div>`;
    const why = {
      closest: ["No twins this time.", "Peek at Same vibe: the close cousins live there."],
      similar: ["Nothing in the same spirit.", "This one might be truly one of a kind."],
      under: ["Nothing under $50 worth your time.", "Champagne taste, darling."],
    }[bucketName];
    return `<div class="empty"><div class="big">${why[0]}</div><p>${why[1]}</p></div>`;
  }
  return checking + rows.map((r) => row(r)).join("");
}

function row(r) {
  const soldOut = r.in_stock === false;
  const stamp = r.match === "exact" ? (soldOut ? "Sold out" : "The real deal") : "";
  const tags = [
    r.match === "close" && `<span class="chip twin">Near twin</span>`,
    soldOut && !stamp && `<span class="chip muted">Sold out</span>`,
    r.condition === "pre-owned" && `<span class="chip muted">Pre-loved</span>`,
    r.old_price && !soldOut && `<span class="chip">On sale</span>`,
  ].filter(Boolean).join("");
  const price = r.price_label || money(r.price);
  const was = r.old_price && !soldOut ? `<span class="was">${esc(r.old_price)}</span>` : "";
  return `<a class="prod ${soldOut ? "soldout" : ""} ${stamp ? "has-stamp" : ""}" href="${esc(r.link)}" target="_blank" rel="noopener">
    <div class="ph"><img src="${esc(r.thumbnail)}" alt="" loading="lazy" referrerpolicy="no-referrer"></div>
    <div class="info">
      <div class="src">${esc(r.source)}</div>
      <div class="name">${esc(r.title)}</div>
      ${tags ? `<div class="tags">${tags}</div>` : ""}
    </div>
    <div class="price ${price ? "" : "none"}">${price ? esc(price) : "see price"}${was}</div>
    ${stamp ? `<span class="stamp">${stamp}</span>` : ""}
  </a>`;
}

// ---------------------------------------------------------------- the closet (saved pieces)
const saveKey = (i) => `${lookId}:${i}`;
const isSaved = (i) => store.get().some((x) => x.id === saveKey(i));

$("save").addEventListener("click", () => {
  const p = look.pieces[selected];
  const closet = store.get();
  const k = saveKey(selected);
  const at = closet.findIndex((x) => x.id === k);
  if (at >= 0) { closet.splice(at, 1); toast("Taken off the rail"); }
  else {
    if (!["closest", "similar"].some((b) => p.results[b].some((r) => r.match !== "pending"))) return toast("One sec, still hunting");
    const thumb = (p.results.closest[0] || p.results.similar[0])?.thumbnail;
    closet.unshift({ id: k, savedAt: Date.now(), creator: look.creator, thumb,
                     pieces: [{ label: p.label, color: p.color, status: "done", results: p.results, thumb }] });
    toast("Hung in your closet ✶");
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
  toast.t = setTimeout(() => t.classList.remove("on"), 1700);
}

function fail(title, msg) {
  $("picker").classList.add("hide");
  $("stage").classList.add("developed", "found");
  if (!document.querySelector(".err")) {
    const el = document.createElement("div");
    el.className = "err";
    el.innerHTML = `<strong>${esc(title)}</strong><p>${esc(msg)}</p><a class="btn" href="/">Back home</a>`;
    $("stage").appendChild(el);
  }
}

// little bits of life: the timer, rotating status lines
let tick = 0;
setInterval(() => {
  if (!look || look.status === "reading") $("timer").textContent = ((performance.now() - started) / 1000).toFixed(1) + "s";
  else if (look.timings?.all_pieces) $("timer").textContent = look.timings.all_pieces.toFixed(1) + "s";
}, 100);
setInterval(() => {
  tick++;
  if (look?.status === "reading" && !look.pieces.length) $("pickSub").textContent = SPOTTING[tick % SPOTTING.length];
  const hunt = $("hunt");
  if (hunt) hunt.textContent = HUNTING[tick % HUNTING.length];
}, 1500);
$("capTime").textContent = new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }).toLowerCase();

// ---------------------------------------------------------------- boot
if (savedId) {
  const s = store.get().find((x) => x.id === savedId);
  document.querySelector(".flash")?.remove();
  if (!s) fail("Not in your closet.", "It may have been taken off the rail.");
  else {
    look = { status: "done", creator: s.creator, pieces: s.pieces.map((p) => ({ ...p, crop: p.thumb || s.thumb })), shot: null };
    $("stage").classList.add("found", "developed");
    $("picker").classList.add("hide");
    $("polaroid").hidden = true;
    selected = 0;
    $("sheet").classList.add("open");
    renderSheet();
  }
} else {
  poll();
}

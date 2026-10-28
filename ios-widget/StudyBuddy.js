// Study Buddy — iOS widget for the free "Scriptable" app.
// Home Screen: small / medium / large.  Lock Screen: rectangular / circular / inline.
// Setup: paste this into a new Scriptable script, set WIDGET_URL below (or put a URL in the
// widget's "Parameter" field to point one widget at another server).

const WIDGET_URL = "PASTE_YOUR_WIDGET_URL_HERE";   // https://<host>/dash/widget/<token>.json
const NAME = "Study Buddy";

// ── Apple system colours (light / dark) ─────────────────────────────────────────────────────────
const C = {
  label: Color.dynamic(new Color("#000000"), new Color("#FFFFFF")),
  label2: Color.dynamic(new Color("#3C3C43", 0.6), new Color("#EBEBF5", 0.6)),
  label3: Color.dynamic(new Color("#3C3C43", 0.32), new Color("#EBEBF5", 0.3)),
  blue: Color.dynamic(new Color("#007AFF"), new Color("#0A84FF")),
  red: Color.dynamic(new Color("#FF3B30"), new Color("#FF453A")),
  orange: Color.dynamic(new Color("#FF9500"), new Color("#FF9F0A")),
  green: Color.dynamic(new Color("#34C759"), new Color("#30D158")),
  indigo: Color.dynamic(new Color("#5856D6"), new Color("#5E5CE6")),
  fill: Color.dynamic(new Color("#787880", 0.12), new Color("#787880", 0.24)),
};

// ── data (network first, cached copy if offline) ─────────────────────────────────────────────────
const url = (args.widgetParameter || "").trim().startsWith("http") ? args.widgetParameter.trim() : WIDGET_URL;
const fm = FileManager.local();
const cache = fm.joinPath(fm.cacheDirectory(), "studybuddy-" + url.replace(/\W+/g, "").slice(-24) + ".json");
let d = null, stale = false;
try {
  const r = new Request(url); r.timeoutInterval = 12;
  d = await r.loadJSON();
  if (!d || d.detail) throw new Error(d && d.detail || "bad response");
  fm.writeString(cache, JSON.stringify(d));
} catch (e) {
  if (fm.fileExists(cache)) { d = JSON.parse(fm.readString(cache)); stale = true; }
}

const fam = config.widgetFamily || "medium";
const now = Date.now() / 1000;

// ── helpers ──────────────────────────────────────────────────────────────────────────────────────
const t = (stack, text, size, weight = "regular", color = C.label, lines = 1) => {
  const x = stack.addText(String(text));
  x.font = weight === "bold" ? Font.boldSystemFont(size) : weight === "semibold" ? Font.semiboldSystemFont(size)
    : weight === "rounded" ? Font.boldRoundedSystemFont(size) : weight === "medium" ? Font.mediumSystemFont(size)
    : Font.systemFont(size);
  x.textColor = color; x.lineLimit = lines; x.minimumScaleFactor = 0.75;
  return x;
};
const sym = (stack, name, size, color) => {
  const s = SFSymbol.named(name); s.applyFont(Font.semiboldSystemFont(size));
  const i = stack.addImage(s.image); i.imageSize = new Size(size + 2, size + 2); i.tintColor = color; return i;
};
const rel = (ts) => {
  const s = ts - now, a = Math.abs(s), past = s < 0;
  const v = a < 3600 ? `${Math.max(1, Math.round(a / 60))}m` : a < 86400 ? `${Math.round(a / 3600)}h` : `${Math.round(a / 86400)}d`;
  return past ? `${v} late` : `in ${v}`;
};
const hm = (s) => { // "14:20" -> "2:20 pm"
  if (!s) return ""; const [h, m] = s.split(":").map(Number);
  return `${((h + 11) % 12) + 1}:${String(m).padStart(2, "0")}${h < 12 ? " am" : " pm"}`;
};
const short = (name = "") => {
  const stop = new Set(["and", "of", "for", "the", "using", "in", "to", "&"]);
  const w = name.split(/\s+/).filter((x) => x && !stop.has(x.toLowerCase()));
  return name.length <= 16 ? name : w.map((x) => x[0].toUpperCase()).join("");
};
const dueColor = (it) => it.due_at < now ? C.red : it.due_at - now < 24 * 3600 ? C.orange : C.blue;
const kindSym = (k) => k === "quiz" ? "checklist" : k === "exam" ? "graduationcap.fill" : "doc.text.fill";

// ── widget shell: Liquid-Glass-style gradient that adapts to light/dark ─────────────────────────
const w = new ListWidget();
const g = new LinearGradient();
g.colors = [Color.dynamic(new Color("#FFFFFF"), new Color("#2C2C2E")), Color.dynamic(new Color("#F2F2F7"), new Color("#1C1C1E"))];
g.locations = [0, 1];
if (!fam.startsWith("accessory")) w.backgroundGradient = g;
w.url = d?.app_url || url.replace(/\/dash\/widget\/.*/, "/dash/");
w.refreshAfterDate = new Date(Date.now() + 15 * 60 * 1000);
const pad = fam === "small" ? 14 : 16;
w.setPadding(pad, pad, pad, pad);

if (!d) {
  if (fam.startsWith("accessory")) { t(w, "Study Buddy offline", 12, "semibold"); }
  else {
    const h = w.addStack(); sym(h, "wifi.exclamationmark", 14, C.orange); h.addSpacer(6); t(h, NAME, 13, "semibold", C.label2);
    w.addSpacer(8); t(w, "Can't reach your server", 15, "semibold", C.label, 2); w.addSpacer(4);
    t(w, url.includes("PASTE_") ? "Set WIDGET_URL in the script." : "Check it's online.", 12, "regular", C.label2, 2);
  }
  Script.setWidget(w); Script.complete(); return;
}

// ── Lock Screen ─────────────────────────────────────────────────────────────────────────────────
if (fam === "accessoryCircular") {
  w.addAccessoryWidgetBackground = true;
  const s = w.addStack(); s.layoutVertically(); s.centerAlignContent();
  const n = d.overdue || d.due_48h;
  t(s, n, 22, "rounded", Color.white()).centerAlignText();
  t(s, d.overdue ? "late" : "due", 10, "semibold", Color.white()).centerAlignText();
  Script.setWidget(w); Script.complete(); return;
}
if (fam === "accessoryInline") {
  const c = d.current || d.next;
  t(w, c ? `${short(c.subject)} ${d.current ? "now" : hm(c.start)} · ${d.due_48h} due` : `${d.due_48h} due in 48h`, 12, "medium");
  Script.setWidget(w); Script.complete(); return;
}
if (fam === "accessoryRectangular") {
  w.setPadding(0, 0, 0, 0);
  const c = d.current || d.next;
  const top = w.addStack(); top.centerAlignContent();
  sym(top, d.current ? "dot.radiowaves.left.and.right" : "clock", 11, Color.white()); top.addSpacer(4);
  t(top, c ? (d.current ? "Now · " : hm(c.start) + " · ") + short(c.subject) : "No more classes", 12, "semibold", Color.white());
  const it = d.deadlines[0];
  if (it) { t(w, it.title, 12, "regular", Color.white()); t(w, `${it.code} · ${rel(it.due_at)}`, 11, "regular", new Color("#FFFFFF", 0.7)); }
  else t(w, "Nothing due — all clear", 12, "regular", Color.white());
  Script.setWidget(w); Script.complete(); return;
}

// ── Home Screen ─────────────────────────────────────────────────────────────────────────────────
const header = w.addStack(); header.centerAlignContent();
sym(header, "graduationcap.fill", fam === "small" ? 12 : 13, C.blue); header.addSpacer(5);
t(header, NAME, fam === "small" ? 12 : 13, "semibold", C.blue);
header.addSpacer();
if (stale) sym(header, "icloud.slash", 11, C.label3);
else if (d.unread) { const u = header.addStack(); u.backgroundColor = C.red; u.cornerRadius = 8; u.setPadding(1, 6, 1, 6); t(u, d.unread, 11, "semibold", Color.white()); }
w.addSpacer(fam === "small" ? 8 : 10);

// class card
function classBlock(parent, big) {
  const c = d.current || d.next;
  const lab = parent.addStack(); lab.centerAlignContent();
  if (d.current) { const dot = lab.addStack(); dot.size = new Size(7, 7); dot.cornerRadius = 3.5; dot.backgroundColor = C.green; lab.addSpacer(5); }
  t(lab, d.current ? "In class now" : c ? (c.date && c.date !== new Date().toISOString().slice(0, 10) ? "Next class · later" : "Next class") : "Classes", 12, "semibold", d.current ? C.green : C.label2);
  parent.addSpacer(2);
  if (!c) { t(parent, "Nothing scheduled", big ? 17 : 15, "semibold"); return; }
  t(parent, big ? c.subject : short(c.subject), big ? 17 : 16, "bold", C.label, big ? 1 : 2);
  const end = d.current ? Math.max(0, Math.round((c.end_ts - now) / 60)) : null;
  t(parent, d.current ? `${end} min left · ends ${hm(c.end)}` : `${hm(c.start)} – ${hm(c.end)} · ${c.subject_code}`, 12, "regular", C.label2);
}

function deadlineRow(parent, it, compact) {
  const r = parent.addStack(); r.centerAlignContent(); r.url = it.url || w.url;
  const dot = r.addStack(); dot.size = new Size(8, 8); dot.cornerRadius = it.type === "exam" ? 1.5 : 4; dot.backgroundColor = dueColor(it);
  r.addSpacer(8);
  const col = r.addStack(); col.layoutVertically();
  t(col, it.title, compact ? 13 : 14, "semibold");
  if (!compact) t(col, it.code || (it.type || "").toUpperCase(), 11, "regular", C.label2);
  r.addSpacer(6);
  t(r, rel(it.due_at), 12, "semibold", dueColor(it));
}

function pill(parent, value, label, color) {
  const p = parent.addStack(); p.layoutVertically(); p.backgroundColor = C.fill; p.cornerRadius = 12; p.setPadding(7, 10, 7, 10);
  t(p, value, 18, "rounded", color); t(p, label, 10, "medium", C.label2);
  return p;
}

if (fam === "small") {
  classBlock(w, false);
  w.addSpacer();
  const row = w.addStack(); row.bottomAlignContent();
  const n = d.overdue || d.due_48h;
  t(row, n, 30, "rounded", d.overdue ? C.red : n ? C.orange : C.green);
  row.addSpacer(5);
  const col = row.addStack(); col.layoutVertically();
  t(col, d.overdue ? "overdue" : "due", 11, "semibold", C.label2); t(col, d.overdue ? "now" : "in 48h", 11, "regular", C.label2);
  if (d.deadlines[0]) { w.addSpacer(2); t(w, d.deadlines[0].title, 11, "regular", C.label2); }
} else {
  const top = w.addStack(); top.topAlignContent();
  const left = top.addStack(); left.layoutVertically(); classBlock(left, fam === "large");
  top.addSpacer(10);
  const stats = top.addStack(); stats.layoutVertically();
  pill(stats, d.overdue || d.due_48h, d.overdue ? "overdue" : "due 48h", d.overdue ? C.red : d.due_48h ? C.orange : C.green);
  if (d.attendance != null && fam === "large") { stats.addSpacer(6); pill(stats, `${Math.round(d.attendance)}%`, "attendance", d.attendance < d.attendance_target ? C.red : C.green); }

  w.addSpacer(fam === "large" ? 14 : 10);
  const sep = w.addStack(); sep.size = new Size(0, 0.5); sep.backgroundColor = C.label3; // hairline
  w.addSpacer(fam === "large" ? 12 : 8);

  const list = d.deadlines.slice(0, fam === "large" ? 5 : 2);
  if (!list.length) { const s = w.addStack(); sym(s, "checkmark.circle.fill", 14, C.green); s.addSpacer(6); t(s, "All caught up", 14, "semibold"); }
  list.forEach((it, i) => { if (i) w.addSpacer(fam === "large" ? 9 : 6); deadlineRow(w, it, fam !== "large"); });

  if (fam === "large") {
    w.addSpacer();
    if (d.attendance_low.length) {
      const s = w.addStack(); s.backgroundColor = Color.dynamic(new Color("#FF3B30", 0.12), new Color("#FF453A", 0.18)); s.cornerRadius = 12; s.setPadding(8, 10, 8, 10);
      sym(s, "exclamationmark.triangle.fill", 12, C.red); s.addSpacer(6);
      t(s, "Below " + d.attendance_target + "%: " + d.attendance_low.map((a) => `${short(a.subject)} ${Math.round(a.pct)}%`).join(" · "), 12, "medium", C.label, 2);
    } else {
      const f = w.addStack(); t(f, `${d.classes_left} class${d.classes_left === 1 ? "" : "es"} left today · ${d.due_week} due this week`, 12, "regular", C.label2);
    }
  } else w.addSpacer();
}

Script.setWidget(w);
if (config.runsInApp) await (fam === "small" ? w.presentSmall() : fam === "large" ? w.presentLarge() : w.presentMedium());
Script.complete();

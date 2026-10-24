// Static checks of the deck client files (no DOM needed):
//   jsc -m tests/web/test_static.mjs   (from the repo root; tests/web/run.sh runs everything)
const ROOT = ".";
let passed = 0;
let failed = 0;
function check(name, ok, detail = "") {
  if (ok) passed++;
  else {
    failed++;
    print(`FAIL ${name}${detail ? `: ${detail}` : ""}`);
  }
}
function exists(path) {
  try {
    read(path);
    return true;
  } catch {
    return false;
  }
}

const JS = ["util", "demo", "facilitator", "talk", "socket", "deck", "audio", "captions", "status", "keys", "panels", "meeting", "main"].map(
  (n) => `web/js/${n}.js`,
);
const WORKLETS = ["web/worklets/pcm-player.js", "web/worklets/mic-capture.js"];
const html = read(`${ROOT}/web/index.html`);
const css = read(`${ROOT}/web/css/relay.css`);
const mock = read(`${ROOT}/tests/web/mock/meeting.html`);

// ---- every script is checked JS, offline (no URLs it would fetch)
for (const file of [...JS, ...WORKLETS]) {
  const src = read(`${ROOT}/${file}`);
  check(`${file} starts with // @ts-check`, src.startsWith("// @ts-check"));
  check(`${file} fetches nothing remote`, !/["'`]https?:\/\//.test(src), (src.match(/["'`]https?:\/\/[^"'`]*/) ?? [""])[0]);
  check(`${file} has no console.log left`, !/console\.log\(/.test(src));
}

// ---- index.html
check("no remote URLs in index.html", !/(src|href)="https?:/.test(html));
check("favicon request suppressed", html.includes('<link rel="icon" href="data:,"'));
check("markdown section", html.includes('<section data-markdown="/api/slides.md" data-separator="^---[ \\t]*$"'));
const order = ["/web/vendor/reveal/reveal.js", "/web/vendor/reveal/plugin/markdown/markdown.js", "/web/js/main.js"].map((s) =>
  html.indexOf(`src="${s}"`),
);
check("scripts: reveal, markdown plugin, then main", order.every((i, k) => i > 0 && (k === 0 || i > order[k - 1])), String(order));
check("main.js is a module", html.includes('<script type="module" src="/web/js/main.js">'));
// scripts/e2e_chrome.py clicks the join button by role and exact name
check(
  "Join is a <button> named exactly Join with Computer Audio",
  /<button type="button" class="arm-button"( autofocus)?>Join with Computer Audio<\/button>/.test(html),
);
check("one Join-with-audio button", (html.match(/<button[^>]*>[^<]*Join with Computer Audio[^<]*<\/button>/g) ?? []).length === 1);
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]);
check("own element ids are prefixed (slide ids live in the same document)", ids.every((id) => id.startsWith("relay-")), ids.join(","));
check("icons are inline SVG (no images to fetch)", !/<img\b/i.test(html) && (html.match(/<svg /g) ?? []).length >= 10);
for (const path of [...html.matchAll(/(?:src|href)="(\/web\/[^"]+)"/g)].map((m) => m[1])) {
  check(`index.html asset ${path} exists`, exists(`${ROOT}${path}`));
}

// ---- the meeting window (contract D55, §6.2)
const inside = (outer, inner, close) => {
  const a = html.indexOf(outer);
  const b = html.indexOf(inner, a);
  const c = html.indexOf(close, a);
  return a >= 0 && b > a && c > b;
};
check("the deck is Relay's shared screen", inside('<main class="share"', '<div class="reveal">', "</main>"));
check("captions sit inside the share", inside('<main class="share"', '<section class="captions"', "</main>"));
check("the tool ticker sits in the Relay tile", inside('<article class="tile tile-relay"', '<ol class="ticker"', "</article>"));
check("the status strip is the hidden Meeting info panel", /<section class="strip" aria-label="Meeting info"[^>]* hidden>/.test(html));
check("chat hidden at start", /<section class="chat"[^>]* hidden>/.test(html));
check("left card hidden at start", /<div class="left-card"[^>]* hidden>/.test(html));
for (const cls of ["mic-button", "people-button", "chat-button", "cc-button", "prev-button", "next-button", "more-button", "leave-button", "rejoin-button", "info-button"]) {
  check(`toolbar/topbar button .${cls}`, new RegExp(`<button type="button" class="[^"]*\\b${cls}\\b`).test(html));
}
check("More is a menu", /class="more-menu" role="menu"/.test(html));
const actions = [...html.matchAll(/role="menuitem" data-action="([^"]+)"/g)].map((m) => m[1]);
check(
  "More menu items (Restart meeting last, for a meeting talk)",
  JSON.stringify(actions) === JSON.stringify(["pause", "human", "relay", "prev", "next", "type", "checklist", "info", "reset"]),
  actions.join(","),
);
check("Restart meeting is hidden until the talk is a meeting", /data-action="reset"[^>]* hidden>/.test(html));
check("the agenda strip is hidden until the talk is a meeting", /<nav class="agenda"[^>]* hidden>/.test(html));
check("the evidence callout sits in the share, hidden", inside('<main class="share"', '<aside class="callout"', "</main>") && /<aside class="callout"[^>]* hidden>/.test(html));
check("the side panel's tabs sit in the chat panel, hidden", inside('<section class="chat"', '<div class="tabs" role="tablist"', "</section>") && /<div class="tabs"[^>]* hidden>/.test(html));
const tabs = [...html.matchAll(/class="tab" role="tab" data-tab="(\w+)"/g)].map((m) => m[1]);
check("tabs: Evidence, Notes, Transcript, Chat", JSON.stringify(tabs) === JSON.stringify(["evidence", "notes", "transcript", "chat"]), tabs.join(","));
check("panes hidden at start", ["evidence-pane", "notes-pane", "transcript-pane"].every((c) => new RegExp(`class="pane ${c}"[^>]* hidden>`).test(html)));
check("the speaker picker sits by the mic, hidden", inside('<div class="toolbar-left">', '<div class="speakers"', "</footer>") && /<div class="speakers"[^>]* hidden>/.test(html));
const rows = [...html.matchAll(/<li data-item="(\w+)"[^>]*>\s*<span class="ic"[^>]*><\/span><span class="label">([^<]+)<\/span/g)].map((m) => `${m[1]}=${m[2]}`);
check(
  "join dialog rows, labelled for people",
  JSON.stringify(rows) ===
    JSON.stringify(["server=Connected to Relay", "talk=Talk loaded", "audio=Speaker", "mic=Microphone", "narration=Backup narration", "clips=Participant clips", "realtime=Higgs Realtime"]),
  rows.join(" | "),
);
// a simulated online meeting (contract v3.1 §4): everything it adds is hidden until the talk has a demo in clips mode
check("the participant clips row is hidden until the talk is a simulated meeting", /<li data-item="clips"[^>]* hidden>/.test(html));
check("the join dialog's disclosure is hidden, in the dialog's head", inside('<header class="panel-head">', '<p class="panel-demo"', "</header>") && /<p class="panel-demo"[^>]*hidden>/.test(html));
check("the disclosure badge sits in the topbar, hidden", inside('<div class="topbar-center">', 'class="pill demo-pill"', '<div class="topbar-right">') && /class="pill demo-pill"[^>]*\shidden\s*>Simulated participants · prerecorded audio</.test(html));
check("the demo script's controls sit in the agenda strip before the phase button, hidden", inside('<nav class="agenda"', '<div class="demo"', '<button type="button" class="phase-button"') && /<div class="demo"[^>]* hidden>/.test(html));
check("Run demo and Play next are buttons named exactly so", />Run demo<\/button>/.test(html) && />Play next<\/button>/.test(html));
check("the routing line sits in Meeting info, hidden", inside('<section class="strip"', '<p class="strip-routing" hidden>', "</section>") && html.includes("participant clips only (facilitator audio is not sent)"));

// ---- assets referenced from JS
for (const file of JS) {
  for (const path of [...read(`${ROOT}/${file}`).matchAll(/["'](\/web\/[^"']+)["']/g)].map((m) => m[1])) {
    check(`${file}: ${path} exists`, exists(`${ROOT}${path}`));
  }
}

// ---- vendored reveal.js (offline) and its fonts
for (const f of ["reveal.js", "reveal.css", "reset.css", "theme/black.css", "plugin/markdown/markdown.js", "LICENSE", "VERSION"]) {
  check(`vendor ${f}`, exists(`${ROOT}/web/vendor/reveal/${f}`));
}
check("vendored reveal is 5.1.0", read(`${ROOT}/web/vendor/reveal/VERSION`).startsWith("reveal.js 5.1.0"));
const fontCss = read(`${ROOT}/web/vendor/reveal/theme/fonts/source-sans-pro/source-sans-pro.css`);
for (const font of new Set([...fontCss.matchAll(/url\('\.\/([^'?#]+)/g)].map((m) => m[1]))) {
  check(`font ${font}`, exists(`${ROOT}/web/vendor/reveal/theme/fonts/source-sans-pro/${font}`));
}

// ---- relay.css
const bare = css.replace(/\/\*[\s\S]*?\*\//g, "").replace(/"[^"]*"/g, '""');
check("css braces balance", (bare.match(/{/g) ?? []).length === (bare.match(/}/g) ?? []).length);
check("no remote URLs in css", !/url\(\s*["']?https?:/.test(css) && !/@import/.test(css));
check("system font stack, no web fonts of our own", /--font:\s*-apple-system,/.test(css) && !/@font-face/.test(css));
const defined = new Set([...bare.matchAll(/(--[\w-]+)\s*:/g)].map((m) => m[1]));
for (const name of ["--level", "--mic-level"]) defined.add(name); // set from JS (style.setProperty)
const used = new Set([...bare.matchAll(/var\((--[\w-]+)/g)].map((m) => m[1]));
const reveal = new Set(["--r-background-color"]);
for (const name of used) check(`css var ${name} is defined`, defined.has(name) || reveal.has(name));
check("captions ≥ 28px", /--cap:\s*max\(28px,/.test(css));
check("hidden attribute wins over display rules", /\[hidden\]\s*{\s*display:\s*none !important;/.test(css));

/** Declarations of the rule whose selector list is exactly `selector` ("" when there is none). */
function rule(selector) {
  for (const m of bare.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (m[1].trim().replace(/\s+/g, " ") === selector) return m[2];
  }
  return "";
}
// Stray clicks: reveal jumps to the slide under the pointer when its progress bar (or the 10 px above it) is
// clicked, behind the backend's back. The bar stays visible, for display only.
check("reveal progress bar is display-only", /pointer-events:\s*none/.test(rule(".reveal .progress, .reveal .progress::after")));
check("reveal progress bar still shown", /progress:\s*true/.test(read(`${ROOT}/web/js/deck.js`)));
check("reveal is embedded in the page", /embedded:\s*true/.test(read(`${ROOT}/web/js/deck.js`)));
// The layout: the share and the side column between the topbar and the toolbar; tiles above the share when narrow.
// The real layout at 1280×720 and 1920×1080: open tests/web/mock/meeting.html in Chrome (it checks itself).
const withStrings = css.replace(/\/\*[\s\S]*?\*\//g, ""); // grid areas are strings
check("meeting grid: share | side between topbar and toolbar", /\n\.meeting \{[^}]*"top top"\s*"share side"\s*"bar bar"/.test(withStrings));
check(
  "meeting grid: no side column when both panels are hidden",
  /\n\.meeting\.side-off \{[^}]*"top"\s*"share"\s*"bar"/.test(withStrings) && /display:\s*none/.test(rule(".meeting.side-off .side")),
);
check("narrow windows: the tiles go above the share", /@media \(max-width: 899px\)[\s\S]*"side"\s*"share"/.test(withStrings));
check("the slides keep 16:9 in the share", /width:\s*min\(100cqw, 100cqh \* 16 \/ 9\)/.test(rule(".share .reveal")) && /container-type:\s*size/.test(rule(".screen")));
check("captions off frees their room", /display:\s*none/.test(rule(".meeting.captions-off .captions")));
// On the Same Page: the agenda strip is a grid row of its own, only for a meeting talk (.has-agenda)
check("meeting talk grid: the agenda row under the topbar", /\n\.meeting\.has-agenda \{[^}]*"top top"\s*"agenda agenda"\s*"share side"\s*"bar bar"/.test(withStrings));
check("meeting talk grid without the side column keeps the agenda row", /\n\.meeting\.has-agenda\.side-off \{[^}]*"top"\s*"agenda"\s*"share"\s*"bar"/.test(withStrings));
check("narrow meeting talk: agenda, tiles, share", /@media \(max-width: 899px\)[\s\S]*\.meeting\.has-agenda \{[^}]*"agenda"\s*"side"\s*"share"/.test(withStrings));
check("the callout sits above the captions", /var\(--cap-h\)/.test(rule(".callout")) && /bottom:/.test(rule(".meeting.captions-off .callout")));
check("plain talks keep the old toolbar-left (the meeting's flex row is scoped)", rule(".toolbar-left").trim() === "justify-self: start;");
check("ticker: newest chip ellipsises", /min-width:\s*0/.test(rule(".tool:first-child")) && /text-overflow:\s*ellipsis/.test(rule(".tool .tool-name")));
// a simulated online meeting: the tiles in a 2 × 2 grid, the badge in the viewing badge's room, the strip folds
check("demo: the tiles share a 2 × 2 grid", /display:\s*grid/.test(rule(".meeting.has-demo .people")) && /repeat\(2,/.test(rule(".meeting.has-demo .people")));
check("demo: the disclosure badge takes the viewing badge's room", /display:\s*none/.test(rule(".meeting.has-demo .viewing")));
check("demo: the next step's text ellipsises", /text-overflow:\s*ellipsis/.test(rule(".demo-next")) && /min-width:\s*0/.test(rule(".demo-next")));
check("demo: the other steps' titles fold", /display:\s*none/.test(rule('.meeting.has-demo .agenda-step:not([data-state=""]) .step-title, .meeting.has-demo .agenda-prompt'))); // strings are "" in `bare`
check("icons never take a click from their button", /pointer-events:\s*none/.test(rule(".ico")));

// ---- every class the JS looks up exists in index.html (or is created by the JS itself), on an HTML element: an
//      <svg> is not an HTMLElement, so part()/el() would throw on it in a real browser
const lookedUp = new Set();
const created = new Set();
for (const file of JS) {
  const src = read(`${ROOT}/${file}`);
  for (const m of src.matchAll(/(?:part\([^,]+,\s*|el\(|querySelector\()"(\.[\w-]+(?:\s+\.[\w-]+)*)"/g)) lookedUp.add(m[1]);
  for (const m of src.matchAll(/className = "([\w -]+)"/g)) for (const c of m[1].split(/\s+/)) created.add(c);
  for (const m of src.matchAll(/make\("\w+", "([\w -]+)"/g)) for (const c of m[1].split(/\s+/)) created.add(c); // panels.js
}
const SVG_TAGS = new Set(["svg", "path", "rect", "circle", "line", "g"]);
for (const sel of lookedUp) {
  const last = sel.split(/\s+/).at(-1).slice(1);
  const tags = [...html.matchAll(new RegExp(`<(\\w+)[^>]*\\bclass="[^"]*\\b${last}\\b`, "g"))].map((m) => m[1]);
  check(`index.html has ${sel}`, created.has(last) || tags.length > 0);
  check(`${sel} is an HTML element`, tags.every((t) => !SVG_TAGS.has(t)), tags.join(","));
}
check("found the selector lookups", lookedUp.size >= 40, String(lookedUp.size));

// ---- the layout mock stays a copy of the real meeting window
const meetingHtml = html.slice(html.indexOf('<div class="meeting"'), html.indexOf("<form class=\"typed\""));
const classes = new Set([...meetingHtml.matchAll(/class="([^"]+)"/g)].flatMap((m) => m[1].split(/\s+/)));
classes.delete("empty"); // a state the JS toggles: the mock's live caption has text
const missing = [...classes].filter((c) => !new RegExp(`class="[^"]*\\b${c}\\b`).test(mock));
check("mock/meeting.html has every class of the meeting window", missing.length === 0, missing.join(", "));

print(`static: ${passed} passed, ${failed} failed`);
if (failed) throw new Error(`${failed} static check(s) failed`);

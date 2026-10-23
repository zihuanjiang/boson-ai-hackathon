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

const JS = ["util", "talk", "socket", "deck", "audio", "captions", "status", "keys", "main"].map((n) => `web/js/${n}.js`);
const WORKLETS = ["web/worklets/pcm-player.js", "web/worklets/mic-capture.js"];
const html = read(`${ROOT}/web/index.html`);
const css = read(`${ROOT}/web/css/relay.css`);

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
check("Arm is a <button> named exactly Arm", /<button type="button" class="arm-button"( autofocus)?>Arm<\/button>/.test(html));
check("one Arm-like button", (html.match(/<button[^>]*>[^<]*arm[^<]*<\/button>/gi) ?? []).length === 1);
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]);
check("own element ids are prefixed (slide ids live in the same document)", ids.every((id) => id.startsWith("relay-")), ids.join(","));
for (const path of [...html.matchAll(/(?:src|href)="(\/web\/[^"]+)"/g)].map((m) => m[1])) {
  check(`index.html asset ${path} exists`, exists(`${ROOT}${path}`));
}

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
const defined = new Set([...bare.matchAll(/(--[\w-]+)\s*:/g)].map((m) => m[1]));
for (const name of ["--level", "--mic-level"]) defined.add(name); // set from JS (style.setProperty)
const used = new Set([...bare.matchAll(/var\((--[\w-]+)/g)].map((m) => m[1]));
const reveal = new Set(["--r-background-color"]);
for (const name of used) check(`css var ${name} is defined`, defined.has(name) || reveal.has(name));
check("captions ≥ 28px", /--cap:\s*max\(28px,/.test(css));
check("5% safe area", /--safe-x:\s*5vw/.test(css) && /--safe-y:\s*5vh/.test(css));
check("hidden attribute wins over display rules", /\[hidden\]\s*{\s*display:\s*none !important;/.test(css));

// ---- every class the JS looks up exists in index.html (or is created by the JS itself)
const lookedUp = new Set();
const created = new Set();
for (const file of JS) {
  const src = read(`${ROOT}/${file}`);
  for (const m of src.matchAll(/(?:part\([^,]+,\s*|el\(|querySelector\()"(\.[\w-]+(?:\s+\.[\w-]+)*)"/g)) lookedUp.add(m[1]);
  for (const m of src.matchAll(/className = "([\w -]+)"/g)) for (const c of m[1].split(/\s+/)) created.add(c);
}
for (const sel of lookedUp) {
  const last = sel.split(/\s+/).at(-1).slice(1);
  check(`index.html has ${sel}`, created.has(last) || new RegExp(`class="[^"]*\\b${last}\\b`).test(html));
}
check("found the selector lookups", lookedUp.size >= 20, String(lookedUp.size));

print(`static: ${passed} passed, ${failed} failed`);
if (failed) throw new Error(`${failed} static check(s) failed`);

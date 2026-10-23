#!/usr/bin/env bash
# Vendor reveal.js (UMD build, black theme + its fonts, markdown plugin) into web/vendor/reveal/ so the deck runs
# offline (doc/design.md §8). Needs only curl and raw.githubusercontent.com.
#
#   scripts/vendor_reveal.sh            # tag 5.1.0; if that tag 404s, falls back to other 5.x tags (printed)
#   scripts/vendor_reveal.sh 5.2.1      # a specific tag
#
# Result (dist/ is flattened: the repo .gitignore ignores dist/ folders, and the client loads these paths):
#   web/vendor/reveal/reveal.js, reveal.css, reset.css, theme/black.css, theme/fonts/…, plugin/markdown/markdown.js,
#   LICENSE, VERSION (tag, source URL, sha256 of every file)
set -eo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/web/vendor/reveal"
STAGE="$ROOT/web/vendor/.reveal-staging"
REPO="https://raw.githubusercontent.com/hakimel/reveal.js"
WANT="${1:-5.1.0}"
CANDIDATES="$WANT 5.1.0 5.2.1 5.2.0 5.0.5 5.0.4"

cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

# fetch <tag> <path in repo> <file under $STAGE>
fetch() {
  mkdir -p "$(dirname "$STAGE/$3")"
  curl -fsSL --retry 2 --connect-timeout 15 -o "$STAGE/$3" "$REPO/$1/$2"
}

# Collapse "./" and "dir/../" in a relative path (macOS bash 3.2 + BSD sed compatible).
normalize() {
  local path="$1" before=""
  while [ "$path" != "$before" ]; do
    before="$path"
    path="$(printf '%s' "$path" |
      sed -E 's#^\./##; s#/\./#/#g; s#(^|/)([^/.][^/]*|\.[^/.][^/]*|\.\.[^/]+)/\.\./#\1#')"
  done
  printf '%s' "$path"
}

# The url(...) targets of a CSS file: local, relative ones only, without quotes, query or fragment.
css_urls() {
  grep -oE 'url\([^)]*\)' "$1" | sed -E "s/^url\\([[:space:]]*['\"]?//; s/['\"]?[[:space:]]*\\)\$//; s/[?#].*\$//" |
    grep -vE '^(data:|https?:|//|/|$)' | sort -u || true
}

# fetch_css <tag> <dist-relative css path>: download a stylesheet and, recursively, everything its url()s reference.
fetch_css() {
  local tag="$1" css="$2" dir ref target
  fetch "$tag" "dist/$css" "$css"
  dir="$(dirname "$css")"
  for ref in $(css_urls "$STAGE/$css"); do
    target="$(normalize "$dir/$ref")"
    [ -f "$STAGE/$target" ] && continue
    echo "  $target"
    case "$target" in
      *.css) fetch_css "$tag" "$target" ;;
      *) fetch "$tag" "dist/$target" "$target" ;;
    esac
  done
}

rm -rf "$STAGE"
mkdir -p "$STAGE"

TAG=""
for candidate in $CANDIDATES; do
  if fetch "$candidate" "dist/reveal.js" "reveal.js" 2>/dev/null; then
    TAG="$candidate"
    break
  fi
  echo "reveal.js tag $candidate: not found, trying the next one" >&2
done
if [ -z "$TAG" ]; then
  echo "error: could not download reveal.js from $REPO (tried: $CANDIDATES)" >&2
  exit 1
fi
[ "$TAG" != "$WANT" ] && echo "warning: using reveal.js $TAG instead of $WANT" >&2
echo "reveal.js $TAG from $REPO/$TAG"

fetch "$TAG" "dist/reveal.css" "reveal.css"
fetch "$TAG" "dist/reset.css" "reset.css"
fetch "$TAG" "plugin/markdown/markdown.js" "plugin/markdown/markdown.js"
fetch "$TAG" "LICENSE" "LICENSE"
echo "theme/black.css and the files it references:"
fetch_css "$TAG" "theme/black.css"
for css in reveal.css reset.css; do
  for ref in $(css_urls "$STAGE/$css"); do
    echo "warning: $css references $ref (not vendored)" >&2
  done
done

if command -v shasum >/dev/null 2>&1; then SHA="shasum -a 256"; else SHA="sha256sum"; fi
{
  echo "reveal.js $TAG"
  echo "source: $REPO/$TAG (dist/ flattened into this folder)"
  echo "fetched: $(date -u +%Y-%m-%dT%H:%M:%SZ) by scripts/vendor_reveal.sh"
  echo
  (cd "$STAGE" && find . -type f ! -name VERSION | sed 's#^\./##' | sort | while read -r file; do $SHA "$file"; done)
} > "$STAGE/VERSION"

rm -rf "$DEST"
mkdir -p "$(dirname "$DEST")"
mv "$STAGE" "$DEST"
echo "vendored into ${DEST#"$ROOT"/}:"
(cd "$DEST" && find . -type f | sed 's#^\./#  #' | sort)

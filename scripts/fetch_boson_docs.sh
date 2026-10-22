#!/usr/bin/env bash
# Re-download the Boson AI docs as raw Markdown (Mintlify serves every page at <page-url>.md)
# plus the OpenAPI spec, into .scratch/boson-docs-snapshot/ (or a directory given as $1).
# Usage:  scripts/fetch_boson_docs.sh [out_dir]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/.scratch/boson-docs-snapshot}"
mkdir -p "$OUT"

curl -sSL https://docs.boson.ai/llms.txt -o "$OUT/llms.txt"
curl -sSL https://docs.boson.ai/openapi.json -o "$OUT/openapi.json"

grep -o 'https://docs.boson.ai/[^) ]*\.md' "$OUT/llms.txt" | sort -u | while read -r url; do
  name="${url#https://docs.boson.ai/}"
  name="${name//\//__}"
  curl -sSL "$url" -o "$OUT/$name"
  echo "fetched $name"
done

echo "snapshot in $OUT ($(date -u +%Y-%m-%dT%H:%MZ))"

#!/usr/bin/env bash
set -euo pipefail

: "${QUICKPRICE_BASE_URL:?Set QUICKPRICE_BASE_URL, for example https://price.example.com}"
: "${QUICKPRICE_API_KEY:?Set QUICKPRICE_API_KEY to the raw QuickPrice API key}"
command -v curl >/dev/null

base_url="${QUICKPRICE_BASE_URL%/}"
if [[ "${base_url}" =~ ^https://[^/?#@]+$ ]]; then
  :
elif [[ "${base_url}" =~ ^http://(localhost|127[.]0[.]0[.]1|\[::1\])(:[0-9]+)?$ ]]; then
  :
else
  printf 'QUICKPRICE_BASE_URL must be an HTTPS origin or a loopback HTTP development origin.\n' >&2
  exit 2
fi

case "${QUICKPRICE_API_KEY}" in
  *$'\r'* | *$'\n'*)
    printf 'QUICKPRICE_API_KEY must not contain a newline.\n' >&2
    exit 2
    ;;
esac

escaped_api_key="${QUICKPRICE_API_KEY//\\/\\\\}"
escaped_api_key="${escaped_api_key//\"/\\\"}"

if (($# > 0)); then
  symbols=("$@")
elif [[ -n "${QUICKPRICE_SYMBOLS:-}" ]]; then
  IFS=',' read -r -a symbols <<<"${QUICKPRICE_SYMBOLS}"
else
  printf 'Pass symbols as arguments or set QUICKPRICE_SYMBOLS.\n' >&2
  exit 2
fi

shopt -s extglob
clean_symbols=()
for symbol in "${symbols[@]}"; do
  symbol="${symbol##+([[:space:]])}"
  symbol="${symbol%%+([[:space:]])}"
  [[ -n "${symbol}" ]] || continue
  if [[ ! "${symbol}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*:[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    printf 'Invalid symbol: %s\n' "${symbol}" >&2
    exit 2
  fi
  clean_symbols+=("${symbol}")
done
symbols=("${clean_symbols[@]}")
if ((${#symbols[@]} == 0)); then
  printf 'At least one non-empty symbol is required.\n' >&2
  exit 2
fi

batch_size=100
for ((offset = 0; offset < ${#symbols[@]}; offset += batch_size)); do
  batch=("${symbols[@]:offset:batch_size}")
  joined="$(IFS=,; printf '%s' "${batch[*]}")"
  printf 'header = "X-API-Key: %s"\n' "${escaped_api_key}" | curl \
    --config - \
    --fail-with-body \
    --silent \
    --show-error \
    --get \
    --header "Accept: application/json" \
    --data-urlencode "symbols=${joined}" \
    "${base_url}/v1/quotes"
  printf '\n'
done

#!/usr/bin/env bash
set -euo pipefail

PYTHON_VERSION="3.14.6"
PYTHON_SHA256="143b1dddefaec3bd2e21e3b839b34a2b7fb9842272883c576420d605e9f30c63"
PREFIX="${QUICKPRICE_PYTHON_PREFIX:-/opt/python-${PYTHON_VERSION}t}"
BUILD_JOBS="${QUICKPRICE_BUILD_JOBS:-2}"
WORK_DIR="${QUICKPRICE_BUILD_DIR:-${TMPDIR:-/tmp}/quickprice-python-build}"
ARCHIVE="Python-${PYTHON_VERSION}.tar.xz"
URL="https://www.python.org/ftp/python/${PYTHON_VERSION}/${ARCHIVE}"

command -v curl >/dev/null
command -v sha256sum >/dev/null
command -v make >/dev/null
command -v tar >/dev/null
command -v cc >/dev/null

if [[ ! "${BUILD_JOBS}" =~ ^[1-9][0-9]*$ ]]; then
  printf 'QUICKPRICE_BUILD_JOBS must be a positive integer.\n' >&2
  exit 2
fi
if [[ -z "${WORK_DIR}" || "${WORK_DIR}" == "/" ]]; then
  printf 'QUICKPRICE_BUILD_DIR must identify a dedicated non-root directory.\n' >&2
  exit 2
fi
if [[ -z "${PREFIX}" || "${PREFIX}" == "/" ]]; then
  printf 'QUICKPRICE_PYTHON_PREFIX must identify a dedicated non-root prefix.\n' >&2
  exit 2
fi

mkdir -p "${WORK_DIR}"
WORK_DIR="$(cd "${WORK_DIR}" && pwd -P)"
SOURCE_DIR="${WORK_DIR}/Python-${PYTHON_VERSION}"
cd "${WORK_DIR}"
curl \
  --fail \
  --location \
  --proto '=https' \
  --tlsv1.2 \
  --retry 3 \
  --retry-all-errors \
  --remove-on-error \
  --output "${ARCHIVE}" \
  "${URL}"
printf '%s  %s\n' "${PYTHON_SHA256}" "${ARCHIVE}" | sha256sum --check --strict
if [[ "$(dirname "${SOURCE_DIR}")" != "${WORK_DIR}" ]]; then
  printf 'Refusing to remove a source directory outside QUICKPRICE_BUILD_DIR.\n' >&2
  exit 2
fi
rm -rf -- "${SOURCE_DIR}"
tar --extract --file "${ARCHIVE}"
cd "${SOURCE_DIR}"
./configure \
  --prefix="${PREFIX}" \
  --disable-gil \
  --enable-optimizations \
  --with-lto \
  --with-ensurepip=install
make -j"${BUILD_JOBS}"
make altinstall

"${PREFIX}/bin/python3.14t" - <<'PY'
import sys
import sysconfig

assert sysconfig.get_config_var("Py_GIL_DISABLED") == 1
assert sys._is_gil_enabled() is False
print(sys.version)
PY

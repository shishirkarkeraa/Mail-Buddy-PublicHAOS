#!/bin/sh
set -eu

output_dir="${1:-sbom}"
python_bin="${PYTHON:-python3}"
mkdir -p "$output_dir"

"$python_bin" scripts/generate-python-sbom.py \
  requirements.lock "$output_dir/python-runtime.spdx.json"

if command -v syft >/dev/null 2>&1; then
  syft dir:. -o spdx-json="$output_dir/source.spdx.json"
  syft mail-buddy:0.1.0 -o spdx-json="$output_dir/container.spdx.json"
elif docker sbom --help >/dev/null 2>&1; then
  docker sbom --format spdx-json --output "$output_dir/container.spdx.json" mail-buddy:0.1.0
else
  echo "Python runtime SBOM created; install Syft to add source and container SBOMs." >&2
fi

if "$python_bin" -c 'import piplicenses' >/dev/null 2>&1; then
  "$python_bin" -m piplicenses \
    --format=json --with-urls \
    --output-file="$output_dir/python-licenses.json"
else
  echo "Install requirements-dev.lock to refresh the JSON license inventory." >&2
fi
echo "SBOM and dependency-license inventory written to $output_dir"

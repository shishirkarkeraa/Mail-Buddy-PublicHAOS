#!/bin/sh
# Create a self-contained local HAOS add-on from the checked-out Mail-Buddy
# source. Invoke with /bin/sh; it intentionally refuses to overwrite a folder.
set -eu

if [ "$#" -ne 1 ]; then
    echo "Usage: /bin/sh scripts/package-haos-addon.sh OUTPUT_DIRECTORY" >&2
    exit 2
fi

project_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
output_dir=$1

if [ -e "$output_dir" ]; then
    echo "Refusing to overwrite existing output directory: $output_dir" >&2
    exit 1
fi

parent_dir=$(dirname -- "$output_dir")
if [ ! -d "$parent_dir" ]; then
    echo "Output parent directory does not exist: $parent_dir" >&2
    exit 1
fi

mkdir "$output_dir"
cleanup() {
    rm -rf "$output_dir"
}
trap cleanup HUP INT TERM

cp "$project_dir/mail-buddy/config.yaml" "$output_dir/config.yaml"
cp "$project_dir/mail-buddy/Dockerfile.local" "$output_dir/Dockerfile"
cp "$project_dir/mail-buddy/run.sh" "$output_dir/run.sh"
cp "$project_dir/pyproject.toml" "$project_dir/requirements.lock" "$project_dir/README.md" "$project_dir/LICENSE" "$output_dir/"
cp "$project_dir/scripts/verify-model-manifest.sh" "$output_dir/verify-model-manifest.sh"
cp -R "$project_dir/src" "$output_dir/src"

trap - HUP INT TERM
echo "Created self-contained HAOS add-on: $output_dir"
echo "Copy this folder's contents to /addons/mail-buddy on Home Assistant OS."

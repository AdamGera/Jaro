#!/bin/sh
set -eu

checksum=dd681dfddaa3902a5f359056a60f94917651cc10134e465cc3063847002d52fc
ios=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
destination="$ios/JaroKeyboard/CleanupTagger/JaroCleanupTagger.mlpackage"
if [ -f "$destination/Manifest.json" ]; then
    printf 'Cleanup model already present at %s\n' "$destination"
    exit 0
fi

if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
    printf 'Usage: %s /path/to/the-local-cleanup-model.zip\n' "$0" >&2
    exit 2
fi
archive=$(CDPATH= cd -- "$(dirname -- "$1")" && printf '%s/%s\n' "$PWD" "$(basename -- "$1")")
staging=$(mktemp -d "$ios/JaroKeyboard/CleanupTagger/.model.XXXXXX")
trap 'rm -rf "$staging"' EXIT
printf '%s  %s\n' "$checksum" "$archive" | shasum -a 256 -c -
unzip -q "$archive" -d "$staging"
set -- "$staging"/*.mlpackage
if [ "$#" -ne 1 ] || [ ! -f "$1/Manifest.json" ]; then
    printf 'The archive must contain one Core ML package.\n' >&2
    exit 1
fi
mv "$1" "$destination"

#!/bin/sh
set -eu

checksum=dd681dfddaa3902a5f359056a60f94917651cc10134e465cc3063847002d52fc
ios=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
destination="$ios/JaroKeyboard/CleanupTagger/JaroCleanupTagger.mlpackage"
if [ -f "$destination/Manifest.json" ]; then
    printf 'Cleanup model already present at %s\n' "$destination"
    exit 0
fi

staging=$(mktemp -d "$ios/JaroKeyboard/CleanupTagger/.model.XXXXXX")
trap 'rm -rf "$staging"' EXIT
archive="$staging/model.zip"
curl -fsSL --retry 3 -o "$archive" \
    'https://github.com/anomalyco/hex/releases/download/ios-model-cleanup-tagger-1/HexCleanupTagger-1.mlpackage.zip'
printf '%s  %s\n' "$checksum" "$archive" | shasum -a 256 -c -
unzip -q "$archive" -d "$staging"
test -f "$staging/HexCleanupTagger.mlpackage/Manifest.json"
mv "$staging/HexCleanupTagger.mlpackage" "$destination"

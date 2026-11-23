#!/bin/bash
# Cross-compile static ASS rendering dependencies for Windows FFmpeg.
set -euo pipefail
cd "$1"
cache="$PWD"
tools=$(cd "$(dirname "$0")" && pwd)
prefix="$cache/windows-ink"
jobs=$2
mkdir -p "$prefix"
cross_file="$prefix/mingw.ini"
cat > "$cross_file" <<'EOF'
[binaries]
c = 'x86_64-w64-mingw32-gcc'
cpp = 'x86_64-w64-mingw32-g++'
ar = 'x86_64-w64-mingw32-ar'
strip = 'x86_64-w64-mingw32-strip'
windres = 'x86_64-w64-mingw32-windres'
pkg-config = 'pkg-config'

[properties]
needs_exe_wrapper = true

[host_machine]
system = 'windows'
cpu_family = 'x86_64'
cpu = 'x86_64'
endian = 'little'
EOF

while read -r sha archive url; do
    if [[ -z "$sha" || "$sha" == \#* ]]; then continue; fi
    if [[ ! -f "$archive" ]]; then
        curl --fail --location --retry 3 "$url" --output "$archive.part"
        mv "$archive.part" "$archive"
    fi
    printf '%s  %s\n' "$sha" "$archive" | shasum -a 256 -c -
    if [[ "$archive" == liberation-fonts-* ]]; then continue; fi
    source_dir="$prefix/${archive%.tar.gz}"
    if [[ ! -f "$source_dir/meson.build" ]]; then
        mkdir -p "$source_dir"
        tar -xzf "$archive" --strip-components=1 -C "$source_dir"
    fi
done < "$tools/windows-ink-sources.txt"

export PKG_CONFIG_LIBDIR="$prefix/lib/pkgconfig"
unset PKG_CONFIG_PATH
compile() {
    local component=$1
    shift
    local build="$prefix/$component-build"
    local args=()
    [[ ! -d "$build/meson-private" ]] || args+=(--reconfigure)
    meson setup "${args[@]}" "$build" "$prefix/$component" --cross-file "$cross_file" \
        --prefix "$prefix" --libdir lib --buildtype release --default-library static \
        --auto-features disabled --wrap-mode nodownload "$@"
    meson compile -C "$build" -j "$jobs"
    meson install -C "$build" --no-rebuild
}
compile freetype-2.13.3 -Dzlib=disabled
compile fribidi-1.0.16 -Dbin=false -Ddocs=false -Dtests=false
compile harfbuzz-10.4.0 -Dtests=disabled -Dutilities=disabled -Dwith_libstdcxx=true
compile libass-0.17.4 -Ddirectwrite=enabled

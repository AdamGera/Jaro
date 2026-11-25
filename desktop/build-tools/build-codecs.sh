#!/bin/bash
# Minimal LGPL FFmpeg and FFprobe, built from the pinned upstream tarball.
set -euo pipefail
cache=$1
target=${2:-macos}
tools=$(cd "$(dirname "$0")" && pwd)
screen_flags=()
case "$target" in
    macos)
        platform_flags=(--cc=clang)
        jobs=$(sysctl -n hw.logicalcpu)
        programs=(ffmpeg ffprobe)
        ;;
    windows)
        platform_flags=(--arch=x86_64 --target-os=mingw32 --enable-cross-compile
            --cross-prefix=x86_64-w64-mingw32- --disable-x86asm --extra-ldflags=-static)
        jobs=${JARO_CODEC_JOBS:-4}
        programs=(ffmpeg.exe ffprobe.exe)
        screen_flags=(--enable-mediafoundation --enable-demuxer=rawvideo --enable-decoder=rawvideo,h264
            --enable-encoder=h264_mf,libopenh264,mjpeg,rawvideo --enable-muxer=mpjpeg,rawvideo --enable-libopenh264
            --enable-filter=adelay,ass,format,fps,scale,showinfo,split,trim --enable-libass
            --pkg-config=pkg-config --pkg-config-flags=--static --extra-libs=-lstdc++)
        ;;
    *) printf 'Unsupported codec target: %s\n' "$target" >&2; exit 1 ;;
esac
archive="$cache/ffmpeg-7.1.1.tar.xz"
mkdir -p "$cache"
if [[ "$target" == windows ]]; then
    bash "$tools/build-windows-ink-deps.sh" "$cache" "$jobs"
    export PKG_CONFIG_LIBDIR="$(cd "$cache" && pwd)/windows-ink/lib/pkgconfig"
    unset PKG_CONFIG_PATH
fi
if [[ ! -f "$archive" ]]; then
    curl --fail --location --retry 3 https://ffmpeg.org/releases/ffmpeg-7.1.1.tar.xz --output "$archive.part"
    mv "$archive.part" "$archive"
fi
(cd "$cache" && echo "733984395e0dbbe5c046abda2dc49a5544e7e0e1e2366bba849222ae9e3a03b1  ffmpeg-7.1.1.tar.xz" | shasum -a 256 -c -)
cd "$cache"
[[ -d ffmpeg-7.1.1 ]] || tar -xf "$archive"
cd ffmpeg-7.1.1
if [[ "$target" == windows ]]; then
    # FFmpeg 7.1.2 backport for the H.264-only build.
    # https://github.com/FFmpeg/FFmpeg/commit/276bd388f33b5071799691aee6e0cd99c7133d61
    sed -i '/^OBJS-\$(CONFIG_H264_SEI).* h2645_sei\.o$/s/$/ aom_film_grain.o/' libavcodec/Makefile
fi
./configure "${platform_flags[@]}" --disable-autodetect --disable-doc --disable-debug --disable-network \
  --disable-everything --disable-ffplay --enable-ffmpeg --enable-ffprobe \
  --enable-protocol=file,pipe --enable-demuxer=aac,aiff,caf,flac,matroska,mov,mp3,ogg,wav,pcm_s16le,pcm_f32le \
  --enable-decoder='aac*,alac,flac,mp3*,opus,vorbis,pcm_*,adpcm_*' \
  --enable-parser=aac,aac_latm,flac,mpegaudio,opus,vorbis \
  --enable-filter=aresample,aformat,amix,anull,apad,atrim,volume --enable-muxer=ipod,mp4,wav,pcm_f32le,pcm_s16le \
  --enable-encoder=aac,pcm_s16le,pcm_f32le ${screen_flags[@]+"${screen_flags[@]}"}
make -j "$jobs" "${programs[@]}"

#!/bin/bash
# Minimal LGPL FFmpeg and FFprobe, built from the pinned upstream tarball.
set -euo pipefail
cache=$1
archive="$cache/ffmpeg-7.1.1.tar.xz"
mkdir -p "$cache"
if [[ ! -f "$archive" ]]; then
    curl --fail --location --retry 3 https://ffmpeg.org/releases/ffmpeg-7.1.1.tar.xz --output "$archive.part"
    mv "$archive.part" "$archive"
fi
(cd "$cache" && echo "733984395e0dbbe5c046abda2dc49a5544e7e0e1e2366bba849222ae9e3a03b1  ffmpeg-7.1.1.tar.xz" | shasum -a 256 -c -)
cd "$cache"
[[ -d ffmpeg-7.1.1 ]] || tar -xf "$archive"
cd ffmpeg-7.1.1
./configure --cc=clang --disable-autodetect --disable-doc --disable-debug --disable-network \
  --disable-everything --disable-ffplay --enable-ffmpeg --enable-ffprobe \
  --enable-protocol=file,pipe --enable-demuxer=aac,aiff,caf,flac,matroska,mov,mp3,ogg,wav,pcm_s16le,pcm_f32le \
  --enable-decoder='aac*,alac,flac,mp3*,opus,vorbis,pcm_*,adpcm_*' \
  --enable-parser=aac,aac_latm,flac,mpegaudio,opus,vorbis \
  --enable-filter=aresample,aformat,amix,anull,apad,atrim,volume --enable-muxer=ipod,mp4,wav,pcm_f32le,pcm_s16le \
  --enable-encoder=aac,pcm_s16le,pcm_f32le
make -j "$(sysctl -n hw.logicalcpu)" ffmpeg ffprobe

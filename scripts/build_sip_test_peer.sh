#!/usr/bin/env bash
# Build the independently maintained SIP peer used by HA protocol tests.
set -euo pipefail
if [[ $# != 1 ]]; then
  echo "Usage: $0 INSTALL_PREFIX" >&2
  exit 2
fi
prefix=$(realpath -m "$1")
mkdir -p "$prefix"
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
re_revision=2117298320abbf981211b405c9d6b7f3b1f961b4
baresip_revision=22bf3b588bb1e569d53288f99c11bce8485c4e18
for project in re baresip; do
  revision=$re_revision
  if [[ "$project" == baresip ]]; then revision=$baresip_revision; fi
  mkdir "$scratch/$project"
  curl --fail --location --retry 2 \
    "https://api.github.com/repos/baresip/$project/tarball/$revision" \
    --output "$scratch/$project.tar.gz"
  tar -xzf "$scratch/$project.tar.gz" --strip-components=1 -C "$scratch/$project"
done
cmake -S "$scratch/re" -B "$scratch/re-build" \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$prefix" \
  -DCMAKE_INSTALL_LIBDIR=lib -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DLIBRE_BUILD_SHARED=ON
cmake --build "$scratch/re-build" --parallel 2
cmake --install "$scratch/re-build"
cmake -S "$scratch/baresip" -B "$scratch/baresip-build" \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$prefix" \
  -DCMAKE_INSTALL_LIBDIR=lib -DCMAKE_PREFIX_PATH="$prefix" -DCMAKE_INSTALL_RPATH="$prefix/lib" \
  '-DMODULES=account;stdio;menu;g711;l16;auconv;auresamp;ausine;aubridge;sndfile;mwi;avcodec;fakevideo'
cmake --build "$scratch/baresip-build" --parallel 2
cmake --install "$scratch/baresip-build"
for module in account stdio menu g711 l16 auconv auresamp ausine aubridge sndfile mwi avcodec fakevideo; do
  test -s "$prefix/lib/baresip/modules/$module.so"
done

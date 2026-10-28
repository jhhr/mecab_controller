#!/bin/bash
# Builds libmecab for Android arm64-v8a into libmecab-android-arm64-v8a.so, next to this script.
#
# An Android app that runs Python cannot start the bundled mecab executable: the folder an
# add-on lives in is usually on shared storage, which is mounted noexec, and an app targeting
# Android 10 or later may not exec files from its private storage either. It can still load a
# shared library from private storage, so mecab_controller loads this one in process through
# ctypes instead (mecab_library.py). Kioku, which runs the Anki desktop app on Android, is the
# environment that needed it.
#
# Usage: support/build_libmecab_android.sh [NDK directory]
#   The NDK is the argument, else $ANDROID_NDK_HOME, $ANDROID_NDK_LATEST_HOME or
#   $ANDROID_NDK_ROOT. Needs git and network access to github.com for MeCab's source.
#   The build is reproducible: the same MeCab commit and NDK give the same bytes. The
#   committed library was built with NDK r29 (29.0.14206865) on Linux x86_64.
#
# What is built, and why:
# - MeCab 0.996, the last release and the version of the bundled macOS library, from
#   taku910/mecab at MECAB_COMMIT. The bundled Linux and Windows builds are older (0.98 and
#   0.98pre3); over every string in jp_text_processing's tests, 0.996 tags exactly as they do.
#   The commits after the 0.996 release change Windows paths and the SWIG bindings, fix typos
#   in messages, and move set_sentence() after initRequestType() in parse(), which only
#   matters with --allocate-sentence. None changes how text is tagged.
# - API level 28 (Android 9), the oldest Android Kioku runs on.
# - libc++ linked in statically, and only the C API (mecab_*) exported. The process already
#   holds a libc++ of its own (Qt's libc++_shared.so); with the copy here hidden, MeCab's calls
#   cannot bind to that one. Only bionic's libc, libm and libdl are left NEEDED.
# - Segments aligned to 16 KB: devices with 16 KB pages refuse to load a library whose LOAD
#   segments are aligned to 4 KB, and 4 KB devices load either.
# - No iconv. MeCab converts charsets only while compiling a dictionary or training a model;
#   tagging reads the compiled dictionaries as they are, and the bundled ones are UTF-8.
# - config.h is written here instead of by MeCab's configure: its config.sub dates from 2011
#   and does not know aarch64, and the build needs nothing configure could detect that is not
#   fixed by the target. The values are what configure finds on Linux, minus HAVE_ICONV; bionic
#   has everything else at API 28.
# - -O3, as MeCab's configure chooses. Sources are compiled from their own directory, so
#   __FILE__ in MeCab's error messages is a bare file name ("tagger.cpp"), as in the other
#   builds, and no path of the machine that built it ends up in the library.
set -euo pipefail

MECAB_REPO=https://github.com/taku910/mecab.git
MECAB_COMMIT=c6e0609e95edfc2b1e4f7db5e7bfee94cd1e3e95
API=28
ABI=arm64-v8a
TRIPLE=aarch64-linux-android

HERE=$(cd "$(dirname "$0")" && pwd)
OUT=$HERE/libmecab-android-$ABI.so

NDK=${1:-${ANDROID_NDK_HOME:-${ANDROID_NDK_LATEST_HOME:-${ANDROID_NDK_ROOT:-}}}}
if [ -z "$NDK" ] || [ ! -d "$NDK" ]; then
    echo "usage: $0 <NDK directory> (or set ANDROID_NDK_HOME)" >&2
    exit 2
fi
case "$(uname -s)" in
    Linux) HOST_TAG=linux-x86_64 ;;
    Darwin) HOST_TAG=darwin-x86_64 ;;
    *) echo "unsupported build host: $(uname -s)" >&2; exit 2 ;;
esac
BIN=$NDK/toolchains/llvm/prebuilt/$HOST_TAG/bin
CXX=$BIN/$TRIPLE$API-clang++
STRIP=$BIN/llvm-strip
READELF=$BIN/llvm-readelf
NDK_VERSION=$(sed -n 's/^Pkg.Revision *= *//p' "$NDK/source.properties")

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# A commit is fetched by its hash, so the source is exactly what MECAB_COMMIT names.
git -C "$WORK" init -q mecab
git -C "$WORK/mecab" fetch -q --depth 1 "$MECAB_REPO" "$MECAB_COMMIT"
git -C "$WORK/mecab" checkout -q FETCH_HEAD
SRC=$WORK/mecab/mecab/src

cat >"$SRC/config.h" <<'EOF'
#define HAVE_CTYPE_H 1
#define HAVE_DIRENT_H 1
#define HAVE_DLFCN_H 1
#define HAVE_FCNTL_H 1
#define HAVE_GCC_ATOMIC_OPS 1
#define HAVE_GETENV 1
#define HAVE_GETPAGESIZE 1
#define HAVE_INTTYPES_H 1
#define HAVE_MEMORY_H 1
#define HAVE_MMAP 1
#define HAVE_OPENDIR 1
#define HAVE_PTHREAD_H 1
#define HAVE_STDINT_H 1
#define HAVE_STDLIB_H 1
#define HAVE_STRINGS_H 1
#define HAVE_STRING_H 1
#define HAVE_SYS_MMAN_H 1
#define HAVE_SYS_PARAM_H 1
#define HAVE_SYS_STAT_H 1
#define HAVE_SYS_TIMES_H 1
#define HAVE_SYS_TYPES_H 1
#define HAVE_TLS_KEYWORD 1
#define HAVE_UNISTD_H 1
#define HAVE_UNSIGNED_LONG_LONG_INT 1
#define PACKAGE "mecab"
#define VERSION "0.996"
#define SIZEOF_CHAR 1
#define SIZEOF_SHORT 2
#define SIZEOF_INT 4
#define SIZEOF_LONG 8
#define SIZEOF_LONG_LONG 8
#define SIZEOF_SIZE_T 8
#define STDC_HEADERS 1
EOF

# libmecab_la_SOURCES in src/Makefile.am, the .cpp files of it.
SOURCES=(
    viterbi.cpp tagger.cpp utils.cpp eval.cpp iconv_utils.cpp dictionary_rewriter.cpp
    dictionary_generator.cpp dictionary_compiler.cpp context_id.cpp connector.cpp
    nbest_generator.cpp writer.cpp string_buffer.cpp param.cpp tokenizer.cpp
    char_property.cpp dictionary.cpp feature_index.cpp lbfgs.cpp learner_tagger.cpp
    learner.cpp libmecab.cpp
)
# The defines src/Makefile.am passes. The default rc file is only read when neither
# --rcfile nor $MECABRC names one; mecab_controller always passes --rcfile.
CXXFLAGS=(
    -O3 -Wall -fPIC -DHAVE_CONFIG_H -DDIC_VERSION=102
    '-DMECAB_DEFAULT_RC="/usr/local/etc/mecabrc"'
    # MeCab is C++98; C++17 dropped the `register` storage class it still uses.
    -std=gnu++14 -Wno-register -Wno-deprecated-declarations
    -ffunction-sections -fdata-sections
    -ffile-prefix-map="$WORK"=.
)
cat >"$WORK/exports.map" <<'EOF'
{
  global: mecab_*;
  local: *;
};
EOF

OBJECTS=()
for source in "${SOURCES[@]}"; do
    object=$WORK/${source%.cpp}.o
    (cd "$SRC" && "$CXX" "${CXXFLAGS[@]}" -c "$source" -o "$object")
    OBJECTS+=("$object")
done

"$CXX" -shared -o "$WORK/libmecab.so" "${OBJECTS[@]}" \
    -static-libstdc++ \
    -Wl,-soname,libmecab.so \
    -Wl,--version-script="$WORK/exports.map" \
    -Wl,--exclude-libs,ALL \
    -Wl,--no-undefined \
    -Wl,--gc-sections \
    -Wl,-z,max-page-size=16384 \
    -Wl,--build-id=sha1
"$STRIP" --strip-unneeded "$WORK/libmecab.so"
mv "$WORK/libmecab.so" "$OUT"
chmod 644 "$OUT"  # like the other bundled libraries

echo "built $OUT"
echo "  MeCab $MECAB_COMMIT, NDK $NDK_VERSION, API $API"
echo "  sha256 $(sha256sum "$OUT" | cut -d' ' -f1)"
"$READELF" -h "$OUT" | grep -E 'Class|Machine'
"$READELF" -d "$OUT" | grep -E 'NEEDED|SONAME'
"$READELF" -lW "$OUT" | grep -E '^ +LOAD'

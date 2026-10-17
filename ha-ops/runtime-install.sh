#!/bin/sh
# Fail closed: no dependency fallback when an exact retained artifact vanishes.
set -eu
arch=${1:?Supervisor BUILD_ARCH is required}
case "$arch" in aarch64|amd64) ;; *) echo 'Unsupported App architecture; rebuild and requalify a maintained runtime profile.' >&2; exit 1;; esac
lock="/tmp/runtime-apk-$arch.lock"
mkdir /tmp/runtime-apks
while read -r checksum url; do
    case "$url" in https://dl-cdn.alpinelinux.org/alpine/v3.24/main/*.apk|https://dl-cdn.alpinelinux.org/alpine/v3.24/community/*.apk) ;; *) echo 'Unreviewed APK source' >&2; exit 1;; esac
    archive="/tmp/runtime-apks/${url##*/}"
    if ! wget -q -O "$archive" "$url"; then
        echo 'Pinned runtime artifact unavailable. Resolve a new reviewed profile and rerun runtime qualification; never use latest. Retained archives are evidence, not an automatic fallback.' >&2
        exit 1
    fi
    printf '%s  %s\n' "$checksum" "$archive" | sha256sum -c -
done < "$lock"
# APK's trusted keyring verifies package signatures; no allow-untrusted/network.
apk add --no-network /tmp/runtime-apks/*.apk
pip3 install --no-cache-dir --break-system-packages --no-deps --require-hashes -r /tmp/runtime-pip.lock
mkdir -p /usr/share/ha-ops
apk info -v | sort > /usr/share/ha-ops/apk-installed.txt
rm -rf /tmp/runtime-apks /tmp/runtime-apk-*.lock /tmp/runtime-pip.lock /tmp/runtime-install.sh

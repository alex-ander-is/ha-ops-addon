#!/bin/sh
# Install only the retained, signed artifacts from the reviewed runtime profile.
set -eu
arch=${1:?Supervisor BUILD_ARCH is required}
case "$arch" in
    aarch64) bundle_sha=a0d6fc2a3966f257b147b63787f89b695f1fcd1402764b45961a886ddb646ac5 ;;
    amd64) bundle_sha=b30dc7eb370ce6aa74ba895890c12c8858a53184d8b51a24a540a3cb3951e5b2 ;;
    *) echo 'Unsupported App architecture; rebuild and requalify a maintained runtime profile.' >&2; exit 1 ;;
esac
lock="/tmp/runtime-apk-$arch.lock"
scratch=$(mktemp -d /tmp/runtime-apks.XXXXXX)
trap 'rm -rf "$scratch"' EXIT HUP INT TERM
bundle="$scratch/runtime-apks.tar.gz"
url="https://github.com/alex-ander-is/ha-ops-addon/releases/download/2.6.1/runtime-apk-$arch.tar.gz"
if ! wget -q -O "$bundle" "$url"; then
    echo 'Pinned runtime bundle unavailable; restore the reviewed release asset. Never use latest.' >&2
    exit 1
fi
# Authenticate the complete archive BEFORE listing or extracting any entry.
printf '%s  %s\n' "$bundle_sha" "$bundle" | sha256sum -c -
mkdir "$scratch/apks"
# Require a flat, unique collection of regular APK files, with no links or paths.
tar -tzf "$bundle" > "$scratch/members"
tar -tvzf "$bundle" > "$scratch/details"
if ! awk 'BEGIN {ok=1} !/^[a-zA-Z0-9][a-zA-Z0-9+_.-]*[.]apk$/ || seen[$0]++ {ok=0} END {exit !ok}' "$scratch/members" ||
   ! awk 'substr($0,1,1)!="-" {exit 1}' "$scratch/details"; then
    echo 'Invalid runtime bundle entries' >&2; exit 1
fi
# Original Alpine URLs remain source provenance only; they are never fetched.
awk 'NF!=2 || length($1)!=64 || $1 ~ /[^0-9a-f]/ || $2 !~ /^https:\/\/dl-cdn[.]alpinelinux[.]org\/alpine\/v3[.]24\/(main|community)\/[^\/]+\/[^\/]+[.]apk$/ {exit 1}
     {n=split($2,a,"/"); if(seen[a[n]]++) exit 1; print a[n]}' "$lock" > "$scratch/expected"
sort "$scratch/expected" > "$scratch/expected.sorted"
sort "$scratch/members" > "$scratch/members.sorted"
if ! cmp -s "$scratch/expected.sorted" "$scratch/members.sorted"; then
    echo 'Runtime bundle APK closure mismatch' >&2; exit 1
fi
tar -xzf "$bundle" -C "$scratch/apks"
while read -r checksum provenance; do
    archive="$scratch/apks/${provenance##*/}"
    test -f "$archive" && test ! -L "$archive"
    printf '%s  %s\n' "$checksum" "$archive" | sha256sum -c -
done < "$lock"
# APK's trusted keyring verifies package signatures; no allow-untrusted/network.
apk add --no-network "$scratch"/apks/*.apk
pip3 install --no-cache-dir --break-system-packages --no-deps --require-hashes -r /tmp/runtime-pip.lock
mkdir -p /usr/share/ha-ops
apk info -v | sort > /usr/share/ha-ops/apk-installed.txt
rm -f /tmp/runtime-apk-*.lock /tmp/runtime-pip.lock /tmp/runtime-install.sh

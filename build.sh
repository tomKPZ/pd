#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ROOT="$(mktemp -d)"

cleanup() {
	rm -rf "$ROOT"
}
trap cleanup EXIT

add_file() {
	FILE="$1"
	if [ ! -r "$ROOT/$FILE" ]; then
		mkdir -p "$ROOT/$(dirname "$FILE")"
		cp "$FILE" "$ROOT/$FILE"
		# TODO: strip
	fi
}

add_binary() {
	BIN="$1"
	add_file "$BIN"

	dependencies=$( (ldd "$BIN" 2>/dev/null || true) | awk '{ if ($3 ~ /^\//) print $3 }')
	for dep in $dependencies; do
		add_file "$dep"
	done
}

install -dm755 "$ROOT"/{proc,sys,dev,run,tmp,var,etc,usr/{local{,/bin,/sbin,/lib},lib,bin}}
ln -s "usr/lib" "$ROOT/lib"
ln -s "bin" "$ROOT/usr/sbin"
ln -s "usr/bin" "$ROOT/bin"
ln -s "usr/bin" "$ROOT/sbin"
ln -s "../run" "$ROOT/var/run"
ln -s "lib" "$ROOT/usr/lib64"
ln -s "usr/lib" "$ROOT/lib64"

# add_binary /usr/bin/blkid
add_binary /usr/bin/cryptsetup
add_binary /usr/bin/dmsetup
add_binary /usr/bin/kexec
add_binary /usr/bin/lvm
add_binary /usr/lib/ossl-modules/legacy.so

add_binary /usr/bin/busybox
for applet in $(/usr/bin/busybox --list | grep -v busybox); do
	ln -s busybox "$ROOT/usr/bin/$applet"
done

ln -s ../proc/self/mounts "$ROOT/etc/mtab"
touch "$ROOT/etc/fstab"
touch "$ROOT/etc/ld.so.conf"

mkdir "$ROOT/etc/lvm"
touch "$ROOT/etc/lvm/lvm.conf"

ldconfig -r "$ROOT"

cp "$SCRIPT_DIR/init" "$ROOT"

tree "$ROOT"
SAVE_DIR="$(pwd)"
(cd "$ROOT" && find . -print | cpio -ov | gzip -9 >"$SAVE_DIR/archive.cpio.gz")

exit 0

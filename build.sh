#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ROOT="$(mktemp -d)"

cleanup() {
	rm -rf "$ROOT"
}
trap cleanup EXIT

add_file() {
	local file="$1"
	mkdir -p "$ROOT/$(dirname "$file")"
	cp "$file" "$ROOT/$file"
}

add_library() {
	local lib="$1"
	if [ ! -r "$ROOT/$lib" ]; then
		add_file "$lib"
		strip --strip-unneeded "$ROOT/$lib"
	fi
}

add_binary() {
	local bin="$1"
	add_file "$bin"
	strip --strip-all "$ROOT/$bin"

	find -L "$(dirname "$bin")" -samefile "$bin" -xtype l | while read -r link; do
		ln -s "$(basename "$bin")" "$ROOT/$link"
	done

	dependencies=$( (ldd "$bin" 2>/dev/null || true) | awk '{ if ($3 ~ /^\//) print $3 }')
	for dep in $dependencies; do
		add_library "$dep"
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

add_binary /usr/bin/argon2
add_binary /usr/bin/cryptsetup
add_binary /usr/bin/dmsetup
add_binary /usr/bin/integritysetup
add_binary /usr/bin/kexec
add_binary /usr/bin/lvm

add_library /usr/lib/ossl-modules/legacy.so

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
(cd "$ROOT" && find . -print0 | cpio --null -ov --format=newc | gzip -9 >"$SAVE_DIR/archive.cpio.gz")

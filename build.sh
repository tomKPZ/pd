#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(dirname "$(realpath -e "${BASH_SOURCE[0]}")")"

INSTALL_ROOT="$(mktemp -d)"
echo "$INSTALL_ROOT"

# cd "$SCRIPT_DIR/libaio"
# make -j "$(nproc)"
# echo make install "DESTDIR=$INSTALL_ROOT"
# make install DESTDIR="$INSTALL_ROOT"
#
# cd "$SCRIPT_DIR/lvm2"
# ./configure --enable-static_link --without-udev --without-blkid --without-systemd --disable-readline LDFLAGS="-L$INSTALL_ROOT/usr/lib"
# make -j32
# make install DESTDIR="$INSTALL_ROOT"
#
# cd "$SCRIPT_DIR/util-linux"
# meson --reconfigure build --prefix /usr
# ninja -C build -j32
# DESTDIR="$INSTALL_ROOT" ninja -C build install

cd "$SCRIPT_DIR/json-c"
cmake . -DCMAKE_INSTALL_PREFIX=/usr -DBUILD_SHARED_LIBS=off -GNinja
ninja -j32
DESTDIR="$INSTALL_ROOT" ninja install

cd "$SCRIPT_DIR/cryptsetup"
LDFLAGS="-L$INSTALL_ROOT/usr/lib" meson --reconfigure build --prefix /usr -Denable-static=true -Dstatic-cryptsetup=true -Dcrypto-backend=kernel -Dblkid=false -Dudev=false
ninja -C build -j32
ninja -C build install DESTDIR="$INSTALL_ROOT"

tree "$INSTALL_ROOT"

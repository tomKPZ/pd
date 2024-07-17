#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(dirname "$(realpath -e "${BASH_SOURCE[0]}")")"

INSTALL_ROOT="$(mktemp -d)"

# export CC=musl-clang
# export CFLAGS='-static'
export CC=clang
export DESTDIR="$INSTALL_ROOT"
export LDFLAGS="-L$INSTALL_ROOT/usr/lib"
export PKG_CONFIG_PATH="$INSTALL_ROOT/usr/lib/pkgconfig"

cd "$SCRIPT_DIR/libaio"
git clean -fdx
make -j"$(nproc)"
echo make install "DESTDIR=$INSTALL_ROOT"
make install DESTDIR="$INSTALL_ROOT"

cd "$SCRIPT_DIR/lvm2"
git clean -fdx
./configure --enable-static_link --without-udev --without-blkid --without-systemd --disable-readline --enable-pkgconfig LDFLAGS="$LDFLAGS"
make -j"$(nproc)"
make install DESTDIR="$INSTALL_ROOT"

cd "$SCRIPT_DIR/util-linux"
git clean -fdx
meson build --prefix /usr
ninja -C build -j"$(nproc)"
ninja -C build install

cd "$SCRIPT_DIR/json-c"
git clean -fdx
cmake . -DCMAKE_INSTALL_PREFIX=/usr -DBUILD_SHARED_LIBS=off -GNinja
ninja -j"$(nproc)"
ninja install

cd "$SCRIPT_DIR/cryptsetup"
git clean -fdx
meson build --prefix /usr -Denable-static=true -Dstatic-cryptsetup=true -Dcrypto-backend=kernel -Dblkid=false -Dudev=false
ninja -C build -j"$(nproc)"
ninja -C build install
cp build/src/{crypt,integrity,verity}setup "$INSTALL_ROOT/usr/bin"

cd "$SCRIPT_DIR/phc-winner-argon2"
git clean -fdx
CC=musl-clang CFLAGS='-static' LDFLAGS='-static' make -j"$(nproc)"
make install DESTDIR="$INSTALL_ROOT"

tree "$INSTALL_ROOT"

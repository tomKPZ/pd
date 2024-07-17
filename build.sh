#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(dirname "$(realpath -e "${BASH_SOURCE[0]}")")"

ROOT="$(mktemp -d)"

install -dm755 "$ROOT"/{new_root,proc,sys,dev,run,tmp,var,etc,usr/{local{,/bin,/sbin,/lib},lib,bin}}
ln -s "usr/lib" "$ROOT/lib"
ln -s "bin" "$ROOT/usr/sbin"
ln -s "usr/bin" "$ROOT/bin"
ln -s "usr/bin" "$ROOT/sbin"
ln -s "../run" "$ROOT/var/run"
ln -s "lib" "$ROOT/usr/lib64"
ln -s "usr/lib" "$ROOT/lib64"

# export CC=musl-clang
# export CFLAGS='-static'
export CC=clang
export DESTDIR="$ROOT"
export CFLAGS="-flto"
export LDFLAGS="-flto -L$ROOT/usr/lib"
export PKG_CONFIG_PATH="$ROOT/usr/lib/pkgconfig"

cd "$SCRIPT_DIR/libaio"
git clean -fdx
make -j"$(nproc)"
echo make install "DESTDIR=$ROOT"
make install

cd "$SCRIPT_DIR/lvm2"
git clean -fdx
./configure --enable-static_link --without-udev --without-blkid --without-systemd --disable-readline --enable-pkgconfig LDFLAGS="$LDFLAGS"
make -j"$(nproc)"
make install

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
cp build/src/{crypt,integrity,verity}setup "$ROOT/usr/bin"

cd "$SCRIPT_DIR/phc-winner-argon2"
git clean -fdx
CC="musl-$CC" CFLAGS="$CFLAGS -static" LDFLAGS="$LDFLAGS -static" make -j"$(nproc)"
make install

cd "$SCRIPT_DIR/kexec-tools"
git clean -fdx
./bootstrap
LDFLAGS="$LDFLAGS -static" ./configure
make -j"$(nproc)"
make install

tree "$ROOT"

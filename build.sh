#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(dirname "$(realpath -e "${BASH_SOURCE[0]}")")"

ROOT="$(mktemp -d)"

install -dm755 "$ROOT"/{new_root,proc,sys,dev,run,tmp,var,etc,usr/{lib,bin}}
ln -s "usr/lib" "$ROOT/lib"
ln -s "bin" "$ROOT/usr/sbin"
ln -s "usr/bin" "$ROOT/bin"
ln -s "usr/bin" "$ROOT/sbin"
ln -s "../run" "$ROOT/var/run"
ln -s "lib" "$ROOT/usr/lib64"
ln -s "usr/lib" "$ROOT/lib64"
ln -s ".." "$ROOT/usr/local"

export CC=clang
export DESTDIR="$ROOT"
export CFLAGS="-flto -fPIC"
export LDFLAGS="-flto -L$ROOT/usr/lib"
export PKG_CONFIG_PATH="$ROOT/usr/lib/pkgconfig"

cd "$SCRIPT_DIR/libaio"
git clean -fdx
make -j"$(nproc)"
make install

cd "$SCRIPT_DIR/lvm2"
git clean -fdx
./configure --enable-static_link --without-udev --without-blkid --without-systemd --disable-readline --enable-pkgconfig LDFLAGS="$LDFLAGS"
make -j"$(nproc)"
make install
mv -f "$ROOT/usr/bin/dmsetup.static" "$ROOT/usr/bin/dmsetup"
mv -f "$ROOT/usr/bin/lvm.static" "$ROOT/usr/bin/lvm"
rm "$ROOT/usr/bin/dmstats.static"

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
CC="musl-$CC" CFLAGS="$CFLAGS -static" LDFLAGS="$LDFLAGS -static" make argon2 -j"$(nproc)"
make install

cd "$SCRIPT_DIR/kexec-tools"
git clean -fdx
./bootstrap
CC=gcc CFLAGS="$CFLAGS -static -fcommon" LDFLAGS="$LDFLAGS -static" ./configure --prefix=/usr --sbindir=/usr/bin --without-lzma
make -j"$(nproc)"
make install DESTDIR="$ROOT"

cd "$SCRIPT_DIR/busybox"
git clean -fdx
make defconfig
sed -i 's/CONFIG_TC=y/CONFIG_TC=n/' .config
sed -i "s|CONFIG_PREFIX=.*|CONFIG_PREFIX=\"$ROOT\"|" .config
sed -i "s|CONFIG_EXTRA_CFLAGS=.*|CONFIG_EXTRA_CFLAGS=\"$CFLAGS -static\"|" .config
sed -i "s|CONFIG_EXTRA_LDFLAGS=.*|CONFIG_EXTRA_LDFLAGS=\"$LDFLAGS -static\"|" .config
echo CONFIG_STATIC=y >>.config
echo CONFIG_PIE=y >>.config
CC="musl-$CC" make -j32
make install DESTDIR="$ROOT"

tree "$ROOT"

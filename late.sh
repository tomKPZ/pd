#!/usr/bin/ash

set -euo pipefail

find /mnt/plain/root ! -type d | while read -r ITEM; do
  REL_PATH="${ITEM#/mnt/plain/root/}"
  mkdir -p "$(dirname "$REL_PATH")"
  cp -a "$ITEM" "/$REL_PATH"
done

dmsetup create dm-luks /mnt/plain/table.txt
dmsetup mknodes dm-luks
cryptsetup open --key-file=/key.bin --keyfile-offset=64 --header=/mnt/plain/header.dat /dev/mapper/dm-luks luks
VG=$(vgs --headings none | awk '{print $1;}' | head -n 1)
ln -s /dev/mapper/luks /dev/mapper/$VG-thinpool_tmeta
vgchange -ay --ignoremonitoring -qq $VG
vgmknodes $VG
mkdir /mnt/luks
mount /dev/mapper/$VG-test_lv /mnt/luks -o,defaults,nodiscard,ro

cp /mnt/luks/boot/initramfs-linux.img /
size=$(stat -c%s /initramfs-linux.img)
padding=$(((4 - (size % 4)) % 4))
dd if=/dev/zero bs=1 count=$padding >>/initramfs-linux.img
cd /tmp
cp /mnt/plain/table.txt .
cp /mnt/plain/header.dat .
cp /key.bin .
find . -print0 | cpio --null -ov --format=newc >>/initramfs-linux.img
cd /
kexec -l /mnt/luks/boot/vmlinuz-linux --initrd=/initramfs-linux.img --command-line="$(cat /mnt/luks/boot/cmdline-linux.txt)"
kexec -e

#!/usr/bin/bash

set -euo pipefail

if [ -n "${DEBUG:-}" ]; then
    exit 0
fi

source /mnt/plain/late_env.sh

dmsetup create --readonly dm-luks /mnt/plain/table.txt
cryptsetup open --key-file=/key.bin --keyfile-offset=64 --header=/mnt/plain/header.dat /dev/mapper/dm-luks luks
lvm vgchange -ay --ignoremonitoring -qq $VG
mkdir /mnt/luks
mount -t btrfs /dev/mapper/$VG-root /mnt/luks -o,defaults,nodiscard,ro,subvol=@

cp /mnt/luks/boot/initramfs-linux.img /
size=$(stat -c%s /initramfs-linux.img)
padding=$(((4 - (size % 4)) % 4))
dd if=/dev/zero bs=1 count=$padding status=none >>/initramfs-linux.img

cp /mnt/plain/table.txt /tmp
cp /mnt/plain/header.dat /tmp
cp /key.bin /tmp
cp -P /boot_disk /tmp
(cd /tmp && find . -print0 | cpio --null -o --format=newc >>/initramfs-linux.img)

kexec -l /mnt/luks/boot/vmlinuz-linux --initrd=/initramfs-linux.img --command-line="$(cat /mnt/luks/boot/cmdline-linux.txt)"
shred -u /key.bin /tmp/* /initramfs-linux.img
kexec -e

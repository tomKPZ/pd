#!/usr/bin/bash

set -euo pipefail

if [ -n "${DEBUG:-}" ]; then
    exit 0
fi

source /mnt/plain/late_env.sh

dmsetup create --readonly dm-luks /mnt/plain/table.txt
cryptsetup open --key-file=/key.bin --keyfile-offset=64 --header=/mnt/plain/header.dat /dev/mapper/dm-luks luks
lvm vgchange -ay --ignoremonitoring -qq "$VG"
mkdir /mnt/luks
mount -t btrfs "/dev/$VG/$LV_ROOT" /mnt/luks -o,defaults,nodiscard,ro,subvol=@

cp "/mnt/luks/$INITRD" /initrd
size=$(stat -c%s /initrd)
padding=$(((4 - (size % 4)) % 4))
dd if=/dev/zero bs=1 count=$padding status=none >>/initrd

cp /mnt/plain/table.txt /tmp
cp /mnt/plain/header.dat /tmp
cp /key.bin /tmp
cp -P /boot_disk /tmp
(cd /tmp && find . -print0 | cpio --null -o --format=newc >>/initrd)

kexec -l "/mnt/luks/$KERNEL" --initrd=/initrd --command-line="$CMDLINE"
shred -u /key.bin /tmp/* /initrd
kexec -e

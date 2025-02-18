#!/usr/bin/bash

set -euo pipefail

if /mnt/plain/detect_shift; then
    cat <(echo default) <(find /mnt/luks/systems -maxdepth 1 -mindepth 1 | grep -v default | sort) |
        SYSTEM=$(fzf --border --color=16 --color='border:-1' --layout=reverse --border-label="Boot System")
else
    SYSTEM=/mnt/luks/systems/default
fi

source "/mnt/plain/systems/$SYSTEM/env.sh"

dmsetup create --readonly dm-luks /mnt/plain/table.txt
cryptsetup open --key-file=/boot_tmp/key.bin --keyfile-offset=64 --header=/mnt/plain/header.dat /dev/mapper/dm-luks luks
lvm vgchange -ay --ignoremonitoring -qq "$VG"
mkdir -p /mnt/luks/boot
mount -t btrfs "/dev/$VG/$LV_ROOT" /mnt/luks/boot -o,defaults,nodiscard,ro,subvol=@boot

source "/mnt/plain/systems/$SYSTEM/extra.sh"

cp "/mnt/luks/$INITRD" /tmp/initrd
size=$(stat -c%s /tmp/initrd)
padding=$(((4 - (size % 4)) % 4))
dd if=/dev/zero bs=1 count=$padding status=none >>/tmp/initrd

cp /mnt/plain/table.txt /mnt/plain/header.dat /boot_tmp
find /boot_tmp -print0 | cpio --null -o --format=newc >>/tmp/initrd

kexec -l "/mnt/luks/$KERNEL" --initrd=/tmp/initrd --command-line="$CMDLINE"
rm /boot_tmp/disk
shred -u /boot_tmp/* /tmp/initrd
kexec -e

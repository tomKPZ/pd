#!/bin/sh
# Runs in the target OS's initrd.  The boot environment appended
# /boot_tmp/{data.key,table.txt,crypt.env} to this initrd.  Find the disk
# whose data region decrypts to an LVM PV with these, open it as
# /dev/mapper/cryptlvm, and destroy the key.  The distro's own initrd then
# activates LVM and mounts root.

set -eu

H=/boot_tmp
[ -r "$H/data.key" ] || exit 0
. "$H/crypt.env"

found=
for dev in /sys/block/*; do
    dev=/dev/${dev##*/}
    case "$dev" in /dev/loop* | /dev/ram* | /dev/dm-* | /dev/sr* | /dev/zram*) continue ;; esac
    [ -b "$dev" ] || continue
    sed "s|DISK|$dev|g" "$H/table.txt" | dmsetup create cryptlvm-raw 2>/dev/null || continue
    if cryptsetup open --type plain --batch-mode $CRYPT_ARGS \
        --key-file "$H/data.key" /dev/mapper/cryptlvm-raw cryptlvm 2>/dev/null; then
        label=$(dd if=/dev/mapper/cryptlvm bs=512 skip=1 count=1 2>/dev/null | head -c 8)
        if [ "$label" = LABELONE ]; then
            found=$dev
            break
        fi
        cryptsetup close cryptlvm
    fi
    dmsetup remove cryptlvm-raw
done

shred -u "$H"/* 2>/dev/null || rm -f "$H"/*
rmdir "$H"

if [ -z "$found" ]; then
    echo "pd: no disk matched the boot keys" >&2
    exit 1
fi

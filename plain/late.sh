#!/bin/sh
# Stage 2, run from the encrypted boot volume: open the data region, pick a
# system, and kexec into its kernel with the keys appended to its initrd.

set -eu

B=/mnt/boot
R=/mnt/root
export PATH="$B/bin:/bin"

disk=$(cat /run/pd/disk)
. "$B/crypt.env"

if [ -n "${LATE:-}" ]; then
    SYSTEM=$LATE
elif detect_shift; then
    SYSTEM=$({ echo default; ls "$B/systems" | grep -vx default; } |
        fzf --border --color=16 --color='border:-1' --layout=reverse --border-label="Boot System")
else
    SYSTEM=default
fi
. "$B/systems/$SYSTEM/env.sh"

# Open the data region.  Every layer is read-only: nothing here may write to
# the disk.
sed "s|DISK|$disk|g" "$B/table.txt" | dmsetup create --readonly cryptlvm-raw
dmsetup mknodes cryptlvm-raw
cryptsetup open --type plain --readonly --batch-mode $CRYPT_ARGS \
    --key-file "$B/data.key" /dev/mapper/cryptlvm-raw cryptlvm
. "$B/thin-ro.sh"
thin_ro_open "$VG" "$LV_ROOT" root-ro
ROOT_DEV=/dev/mapper/root-ro

mkdir -p "$R/boot"
mount -t btrfs -o ro,nodiscard,rescue=nologreplay,subvol=@boot "$ROOT_DEV" "$R/boot"

if [ -x "$B/systems/$SYSTEM/extra.sh" ]; then
    . "$B/systems/$SYSTEM/extra.sh"
fi

# The kernel accepts concatenated initrds; each must start 4-byte aligned.
cp "$R$INITRD" /run/initrd
size=$(stat -c %s /run/initrd)
pad=$(((4 - size % 4) % 4))
[ "$pad" -eq 0 ] || dd if=/dev/zero bs=1 count="$pad" status=none >>/run/initrd

mkdir -p /run/handoff/boot_tmp
chmod 700 /run/handoff/boot_tmp
cp "$B/data.key" "$B/table.txt" "$B/crypt.env" /run/handoff/boot_tmp/
(cd /run/handoff && find boot_tmp | cpio -o -H newc --quiet) >>/run/initrd

kexec -l "$R$KERNEL" --initrd=/run/initrd --command-line="$CMDLINE"
shred -u /run/handoff/boot_tmp/* /run/initrd /run/pd/boot.key
kexec -e


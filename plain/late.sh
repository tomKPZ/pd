#!/bin/sh
# Stage 2, run from the encrypted boot volume: open the data region, pick a
# system, and kexec into its kernel with the keys appended to its initrd.
#
# Run by early.sh on pd's boot USB, or by hand as `late.sh DISK` from a live
# system (docs/stock-boot.md).

set -eu

B=/mnt/boot
R=/mnt/root
export PATH="$B/bin:$PATH"

if [ $# -gt 0 ]; then
    mkdir -p /run/pd
    echo "$1" >/run/pd/disk
fi
disk=$(cat /run/pd/disk)
. "$B/crypt.env"

# FIDO2 (docs/design.md §4.1): this side's data key (and, on the hidden side,
# the decoy's, for the frontier monitor) is wrapped for each enrolled security
# key in fido2.txt, under the boot volume's key.
DATA_KEY=$B/data.key
DECOY_KEY=$B/decoy.key
if [ -s "$B/fido2.txt" ]; then
    tries=0
    while :; do
        printf "Security key PIN: "
        stty -echo 2>/dev/null || true
        IFS= read -r PIN
        stty echo 2>/dev/null || true
        echo
        rc=0
        printf '%s\n' "$PIN" | fido2-hmac unlock "$B/fido2.txt" /run/pd/boot.key /run/pd || rc=$?
        PIN=
        case $rc in
        0) break ;;
        3) echo "Wrong PIN." ;;
        4) echo "PIN blocked: remove and reinsert the security key."; exit 1 ;;
        5) echo "Not touched." ;;
        *) exit 1 ;;
        esac
        tries=$((tries + 1))
        [ "$tries" -lt 3 ] || exit 1
    done
    DATA_KEY=/run/pd/slot0.key
    DECOY_KEY=/run/pd/slot1.key
fi

# On a live system, udev would probe the devices created below, and LVM could
# activate the volume groups on them read-write.  Stop udev for the rest of
# the session (kexec follows), and have libdevmapper make device nodes itself.
if [ -d /run/systemd/system ]; then
    systemctl stop systemd-udevd-control.socket systemd-udevd-kernel.socket \
        systemd-udevd.service 2>/dev/null || true
fi
export DM_DISABLE_UDEV=1

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
    --key-file "$DATA_KEY" /dev/mapper/cryptlvm-raw cryptlvm
. "$B/thin-ro.sh"
thin_ro_open "$VG" "$LV_ROOT" root-ro
ROOT_DEV=/dev/mapper/root-ro

mkdir -p "$R/boot"
mount -t btrfs -o ro,nodiscard,rescue=nologreplay,subvol=@boot "$ROOT_DEV" "$R/boot"

if [ -x "$B/systems/$SYSTEM/extra.sh" ]; then
    . "$B/systems/$SYSTEM/extra.sh"
fi

# Optional per-volume hooks, run before handing over.
for hook in "$B"/hooks/*.sh; do
    [ -r "$hook" ] && . "$hook"
done

# The kernel accepts concatenated initrds; each must start 4-byte aligned.
cp "$R$INITRD" /run/initrd
size=$(stat -c %s /run/initrd)
pad=$(((4 - size % 4) % 4))
[ "$pad" -eq 0 ] || dd if=/dev/zero bs=1 count="$pad" status=none >>/run/initrd

mkdir -p /run/handoff/boot_tmp
chmod 700 /run/handoff/boot_tmp
cp "$DATA_KEY" /run/handoff/boot_tmp/data.key
cp "$B/table.txt" "$B/crypt.env" /run/handoff/boot_tmp/
(cd /run/handoff && find boot_tmp | cpio -o -H newc --quiet) >>/run/initrd

kexec -l "$R$KERNEL" --initrd=/run/initrd --command-line="$CMDLINE"
shred -u /run/handoff/boot_tmp/* /run/initrd
for k in /run/pd/boot.key /run/pd/slot0.key /run/pd/slot1.key; do
    [ ! -e "$k" ] || shred -u "$k"
done
kexec -e


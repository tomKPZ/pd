#!/bin/sh
# Stage 1: find the disk whose encrypted boot volume the password opens, and
# hand over to the late.sh stored inside it.  Each disk has its own salt (the
# 32 bytes below 1 MiB), so the key is derived per disk (docs/design.md §7.2).
#
# On pd's USB it runs from the initrd's root.  From a live system, boot.sh in
# the release's pd-live bundle runs it with PD_BOOT_DIR pointing at a
# directory holding the same files (docs/booting.md).

D=${PD_BOOT_DIR:-}
. "$D/early_env.sh"
. "$D/disk_class.sh"

# Optional per-install files next to the boot image (EFI/BOOT/BOOTX64.EFI.extra.d/*.cred),
# which systemd-stub passes in (docs/design.md §7.1).
CRED=$D/.extra/credentials
SALT=
if [ -r $CRED/salt.cred ]; then
    SALT=$(cat $CRED/salt.cred)
    case $SALT in "" | *[!0-9A-Za-z._-]*) echo "Ignoring salt.cred."; SALT= ;; esac
fi
if [ -r $CRED/argon2.cred ]; then
    a=$(cat $CRED/argon2.cred)
    case $a in "" | *[!-0-9a-z\ ]*) echo "Ignoring argon2.cred." ;; *) ARGON2_ARGS=$a ;; esac
fi
FIDO2=$CRED/fido2.cred

forget_boot_entry() {
    # Delete the NVRAM boot entry the firmware used to start this system,
    # and remove it from BootOrder.
    vars=/sys/firmware/efi/efivars
    guid=8be4df61-93ca-11d2-aa0d-00e098032b8c
    [ -r "$vars/BootCurrent-$guid" ] || return 0
    cur=$(od -An -tx1 -j4 -N2 "$vars/BootCurrent-$guid" |
        awk '{ printf "%s%s", toupper($2), toupper($1) }')
    [ -n "$cur" ] || return 0
    entry="$vars/Boot$cur-$guid"
    if [ -e "$entry" ]; then
        chattr -i "$entry" 2>/dev/null
        rm -f "$entry"
    fi
    order="$vars/BootOrder-$guid"
    [ -r "$order" ] || return 0
    lo=$(echo "$cur" | cut -c3-4 | tr 'A-F' 'a-f')
    hi=$(echo "$cur" | cut -c1-2 | tr 'A-F' 'a-f')
    old=$(od -An -v -tx1 "$order" | tr -s ' \n' ' ')
    new=$(echo "$old" | awk -v lo="$lo" -v hi="$hi" '{
        out = $1 " " $2 " " $3 " " $4
        for (i = 5; i + 1 <= NF; i += 2)
            if (!($i == lo && $(i + 1) == hi)) out = out " " $i " " $(i + 1)
        print out
    }')
    [ "$new" = "$(echo $old)" ] && return 0
    # efivarfs takes the 4 attribute bytes and the data in a single write.
    for b in $new; do printf "\\$(printf %03o "0x$b")"; done >/run/bootorder
    chattr -i "$order" 2>/dev/null
    cat /run/bootorder >"$order" 2>/dev/null
    rm -f /run/bootorder
}

read_secret() {
    printf "%s: " "$2"
    stty -echo 2>/dev/null
    IFS= read -r "$1"
    stty echo 2>/dev/null
    echo
}

# With FIDO2 (fido2.cred next to the boot image), a security key unwraps the
# boot volume key from one of two slots; see docs/design.md §4.1.  The Argon2
# output alone is tried too, for a side without FIDO2.  The PIN is asked
# once, and each disk tried needs a touch (its Argon2 output differs).
PIN=
unlock_fido2() {
    tries=0
    while [ $tries -lt 3 ]; do
        [ -n "$PIN" ] || read_secret PIN "Security key PIN"
        printf '%s\n' "$PIN" | fido2-hmac unlock "$FIDO2" /run/pd/boot.key /run/pd
        rc=$?
        case $rc in
        0) KEYS="$KEYS /run/pd/slot0.key /run/pd/slot1.key"; return ;;
        3) echo "Wrong PIN."; PIN= ;;
        4) echo "PIN blocked: remove and reinsert the security key."; PIN=; return ;;
        5) echo "Not touched." ;;
        *) return ;;
        esac
        tries=$((tries + 1))
    done
}

KEYS=
forget_keys() {
    for k in $KEYS; do
        [ "$k" = "${1:-}" ] || shred -u "$k" 2>/dev/null
    done
    KEYS=
}

# The disks to try, in order: random-looking ones first, then partitioned or
# formatted ones (a pd disk whose first sectors something overwrote), largest
# first within each.  Never: disks too small for pd, and disks whose salt
# bytes are all the same (empty disks; unless a salt comes from salt.cred).
candidates() {
    if [ -n "${PD_DISK:-}" ]; then
        echo "$PD_DISK"
        return
    fi
    list=$(for d in /sys/block/*; do
        dev=/dev/${d##*/}
        case "$dev" in /dev/loop* | /dev/ram* | /dev/dm-* | /dev/sr* | /dev/zram*) continue ;; esac
        [ -b "$dev" ] || continue
        size=$(blockdev --getsize64 "$dev" 2>/dev/null)
        case $(disk_class "$dev") in
        plain) echo "1 $size $dev" ;;
        used) echo "2 $size $dev" ;;
        blank) [ -n "$SALT" ] && echo "1 $size $dev" ;;
        esac
    done)
    for rank in 1 2; do
        echo "$list" | awk -v r=$rank '$1 == r { print $2, $3 }' | sort -rn | awk '{ print $2 }'
    done
}

# Try the current keys on disk $1; on success, never returns.
try_disk() {
    for key in $KEYS; do
        if cryptsetup open --type plain --readonly --batch-mode $CRYPT_ARGS \
            --offset "$PLAIN_OFFSET" --size "$PLAIN_SIZE" \
            --key-file "$key" "$1" cryptboot 2>/dev/null; then
            if mount -t btrfs -o ro,nodiscard,rescue=nologreplay /dev/mapper/cryptboot /mnt/boot 2>/dev/null; then
                if [ -x /mnt/boot/late.sh ]; then
                    echo "$1" >/run/pd/disk
                    forget_keys "$key"
                    [ "$key" = /run/pd/boot.key ] || mv "$key" /run/pd/boot.key
                    SECRET= PIN=
                    unset SECRET PIN
                    exec /mnt/boot/late.sh
                fi
                umount /mnt/boot
            fi
            cryptsetup close cryptboot
        fi
    done
}

# Not on a live system: its boot entry is its own business.
[ -n "$D" ] || forget_boot_entry

mkdir -p /run/pd /mnt/boot
chmod 700 /run/pd
read_secret SECRET "Password"
if [ -z "$SECRET" ]; then
    echo "No bootable disk found."
    exit 1
fi

# The password stays in this shell's memory until a disk opens (exec frees
# it, and init_on_free=1 zeroes it) or every disk has failed.
prev=
for disk in $(candidates); do
    salt=${SALT:-$(disk_salt "$disk")}
    if [ "$salt" != "$prev" ]; then
        forget_keys
        printf %s "$SECRET" | argon2 "$salt" $ARGON2_ARGS | xxd -r -p >/run/pd/boot.key
        KEYS=/run/pd/boot.key
        [ -s "$FIDO2" ] && unlock_fido2
        prev=$salt
    fi
    try_disk "$disk"
done
SECRET= PIN=
unset SECRET PIN

forget_keys
echo "No bootable disk found."
exit 1

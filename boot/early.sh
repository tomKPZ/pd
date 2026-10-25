#!/bin/sh
# Stage 1: derive the key from the password, find the encrypted boot volume
# on one of the disks, and hand over to the late.sh stored inside it.

. /early_env.sh

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

read_password() {
    printf "Password: "
    stty -echo 2>/dev/null
    IFS= read -r PASSWORD
    stty echo 2>/dev/null
    echo
}

forget_boot_entry

mkdir -p /run/pd /mnt/boot
chmod 700 /run/pd
read_password
if [ -z "$PASSWORD" ]; then
    echo "No bootable disk found."
    exit 1
fi
printf %s "$PASSWORD" | argon2 "$(cat /salt.txt)" $ARGON2_ARGS | xxd -r -p >/run/pd/boot.key
PASSWORD=
unset PASSWORD

for disk in /sys/block/*; do
    disk=/dev/${disk##*/}
    case "$disk" in /dev/loop* | /dev/ram* | /dev/dm-* | /dev/sr*) continue ;; esac
    [ -b "$disk" ] || continue
    if cryptsetup open --type plain --readonly --batch-mode $CRYPT_ARGS \
        --offset "$PLAIN_OFFSET" --size "$PLAIN_SIZE" \
        --key-file /run/pd/boot.key "$disk" cryptboot 2>/dev/null; then
        if mount -t btrfs -o ro,nodiscard,rescue=nologreplay /dev/mapper/cryptboot /mnt/boot 2>/dev/null; then
            if [ -x /mnt/boot/late.sh ]; then
                echo "$disk" >/run/pd/disk
                exec /mnt/boot/late.sh
            fi
            umount /mnt/boot
        fi
        cryptsetup close cryptboot
    fi
done

shred -u /run/pd/boot.key
echo "No bootable disk found."
exit 1

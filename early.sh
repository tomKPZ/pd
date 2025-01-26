#!/usr/bin/bash

set -euo pipefail

source /early_env.sh

read_password

mkdir /boot_tmp
echo -n "$PASSWORD" |
    argon2 "$(cat /salt.txt)" $ARGON_ARGS |
    xxd -r -p >/boot_tmp/key.bin
unset PASSWORD

mkdir -p /mnt/plain

for dev in /dev/disk/by-id/*; do
    echo "Trying disk $dev"
    ln -s "${dev}" /boot_tmp/disk
    if dmsetup create --readonly dm-plain /table.txt 2>/dev/null; then
        if cryptsetup open --batch-mode --type=plain $CRYPT_ARGS --key-file=/boot_tmp/key.bin /dev/mapper/dm-plain plain 2>/dev/null; then
            if mount -t btrfs /dev/mapper/plain /mnt/plain -o defaults,nodiscard,ro 2>/dev/null; then
                if [ -x /mnt/plain/late.sh ]; then
                    exec /mnt/plain/late.sh
                fi
                umount /mnt/plain || true
            fi
            cryptsetup close plain || true
        fi
        dmsetup remove dm-plain || true
    fi
    rm /boot_tmp/disk || true
done

shred -u /boot_tmp/key.bin

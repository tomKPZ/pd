#!/usr/bin/bash

set -euo pipefail

read_password() {
    IFS= read -rsp "Password: " PASSWORD
    echo
}

source /early_env.sh

read_password
echo -n "$PASSWORD" |
    argon2 "$(cat /salt.txt)" $ARGON_ARGS |
    xxd -r -p >/key.bin
unset PASSWORD

mkdir -p /mnt/plain
for dev in $(tail /proc/partitions -n +3 | tr -s ' ' '\t' | cut -f 5); do
    ln -s "/dev/$dev" /boot_disk &&
        dmsetup create --readonly dm-plain /table.txt &&
        cryptsetup open --batch-mode --type=plain $CRYPT_ARGS --key-file=/key.bin /dev/mapper/dm-plain plain &&
        mount -t btrfs /dev/mapper/plain /mnt/plain -o defaults,nodiscard,ro &&
        exec /mnt/plain/late.sh

    umount /mnt/plain || true
    cryptsetup close plain || true
    dmsetup remove dm-plain || true
    rm /boot_disk || true
done

shred -u /key.bin

#!/usr/bin/ash

set -euo pipefail

{read_password}
echo -n "$password" | argon2 $(cat /salt.txt) {argon_args} | xxd -r -p >/key.bin
unset password

mkdir -p /mnt/plain
for dev in $(cat /proc/partitions | tail -n +3 | tr -s ' ' '\t' | cut -f 5); do
    ln -s "/dev/$dev" /dev/boot_disk
    if dmsetup create --readonly dm-plain /table.txt; then
        dmsetup mknodes dm-plain
        if cryptsetup open --type=plain {crypt_args} --key-file=/key.bin /dev/mapper/dm-plain plain; then
            if integritysetup open /dev/mapper/plain integrity; then
                if mount /dev/mapper/integrity /mnt/plain -o defaults,nodiscard,ro; then
                    cp /mnt/plain/late.sh /
                    exec /late.sh
                fi
                integritysetup close integrity
            fi
            cryptsetup close plain
        fi
        dmsetup remove dm-plain
    fi
    rm /dev/boot_disk
done

shred -u /key.bin

#!/usr/bin/ash

set -euo pipefail

{read_password}
echo -n "$password" |
    argon2 $(cat /salt.txt) {argon_args} |
    xxd -r -p >/key.bin
unset password

mkdir -p /mnt/plain
for dev in $(cat /proc/partitions | tail -n +3 | tr -s ' ' '\t' | cut -f 5); do
    ln -s "/dev/$dev" /boot_disk &&
        dmsetup create --readonly dm-plain /table.txt &&
        dmsetup mknodes dm-plain &&
        cryptsetup open --type=plain {crypt_args} --key-file=/key.bin /dev/mapper/dm-plain plain &&
        integritysetup open /dev/mapper/plain integrity &&
        mount /dev/mapper/integrity /mnt/plain -o defaults,nodiscard,ro &&
        exec /mnt/plain/late.sh

    umount /mnt/plain || true
    integritysetup close integrity || true
    cryptsetup close plain || true
    dmsetup remove dm-plain || true
    rm /boot_disk || true
done

shred -u /key.bin

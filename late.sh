#!/usr/bin/ash

set -euo pipefail

echo '0 1835008 linear /dev/sda 262144' | dmsetup create dm-luks
dmsetup mknodes dm-luks
cryptsetup open --key-file=/key.bin --keyfile-offset=64 --header=/mnt/plain/header.dat /dev/mapper/dm-luks luks
VG=$(vgs --headings none | awk '{print $1;}' | head -n 1)
ln -s /dev/mapper/luks /dev/mapper/$VG-thinpool_tmeta
vgchange -ay --ignoremonitoring -qq $VG
vgmknodes $VG
mkdir /mnt/luks
mount /dev/mapper/$VG-test_lv /mnt/luks -o,defaults,nodiscard,ro
ls /mnt/luks
kexec -l /mnt/luks/boot/vmlinuz-linux --initrd=/mnt/luks/boot/initramfs-linux.img --command-line="$(cat /mnt/luks/boot/cmdline-linux.txt)"
# kexec -e

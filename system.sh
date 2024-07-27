#!/usr/bin/ash

set -euo pipefail

dmsetup create dm-luks /table.txt
dmsetup mknodes dm-luks
rm /boot_disk
cryptsetup open --key-file=/key.bin --keyfile-offset=64 --header=/header.dat /dev/mapper/dm-luks luks
shred -u /table.txt /header.dat /key.bin
VG=$(vgs --headings none | awk '{print $1;}' | head -n 1)
# ln -s /dev/mapper/luks /dev/mapper/$VG-thinpool_tmeta
vgchange -ay --ignoremonitoring --noudevsync -qq $VG
vgmknodes $VG

mkdir /mnt/luks
mount /dev/mapper/$VG-test_lv /mnt/luks -o,defaults,nodiscard,ro,subvol=@
cat /mnt/luks/message.txt

#!/usr/bin/ash

set -euo pipefail

source /system_env.sh

dmsetup create dm-luks /table.txt
dmsetup mknodes dm-luks
rm /boot_disk
cryptsetup open --key-file=/key.bin --keyfile-offset=64 --header=/header.dat /dev/mapper/dm-luks luks
shred -u /table.txt /header.dat /key.bin
link_tmeta
vgchange -ay --ignoremonitoring --noudevsync -qq $VG
vgmknodes $VG

mkdir /mnt/luks
mount /dev/mapper/$VG-root /mnt/luks -o,defaults,nodiscard,ro,subvol=@
cat /mnt/luks/message.txt

{ pkgs, modulesClosure, bootTools, plainTools, src }:
let
  # A minimal stand-in for an installed OS's initrd, used by end-to-end
  # tests: it runs the shared target hook, activates LVM, mounts root, and
  # reports what it found.
  init = pkgs.writeText "faketarget-init" ''
    #!/bin/sh
    export PATH=/bin
    mount -t proc proc /proc
    mount -t sysfs sysfs /sys
    mount -t devtmpfs devtmpfs /dev
    mkdir -p /run /tmp /etc/lvm /mnt
    mount -t tmpfs tmpfs /run
    printf 'activation { udev_sync = 0 udev_rules = 0 monitoring = 0 }\nbackup { backup = 0 archive = 0 }\nglobal { thin_check_executable = \"\" }\n' >/etc/lvm/lvm.conf
    modprobe -a virtio_pci virtio_blk nvme dm-mod dm-crypt dm-thin-pool btrfs 2>/dev/null
    sleep 1
    echo "PD-TARGET: kernel up"
    if sh /hook.sh; then echo "PD-TARGET: hook ok"; else echo "PD-TARGET: hook failed"; fi
    [ -e /boot_tmp ] && echo "PD-TARGET: boot_tmp still present" || echo "PD-TARGET: boot_tmp removed"
    root=; flags=
    for arg in $(cat /proc/cmdline); do
      case "$arg" in root=*) root=''${arg#root=} ;; rootflags=*) flags=''${arg#rootflags=} ;; esac
    done
    lvm vgchange -ay -qq
    if mount -t btrfs -o ro,$flags "$root" /mnt; then
      echo "PD-TARGET: marker=$(cat /mnt/etc/pd-marker 2>/dev/null)"
    else
      echo "PD-TARGET: mount failed"
    fi
    echo "PD-TARGET: done"
    poweroff -f
  '';
in
pkgs.runCommand "pd-faketarget-initrd.cpio.zst" {
  nativeBuildInputs = [ pkgs.cpio pkgs.zstd ];
} ''
  root=$PWD/root
  mkdir -p $root/{bin,dev,proc,sys,lib}
  cp ${bootTools}/bin/* ${plainTools}/bin/lvm ${plainTools}/bin/dmsetup $root/bin/
  # The kernel runs /sbin/modprobe to load crypto algorithms on demand.
  ln -s bin $root/sbin
  for applet in $($root/bin/busybox --list); do
    [ -e $root/bin/$applet ] || ln -s busybox $root/bin/$applet
  done
  cp -r ${modulesClosure}/lib/modules $root/lib/
  install -m755 ${init} $root/init
  install -m755 ${src}/target/hook.sh $root/hook.sh
  chmod -R u+w $root
  (cd $root && find . -print0 | sort -z |
    cpio --null -o -H newc --reproducible --owner=0:0 --quiet) | zstd -3 -q >$out
''


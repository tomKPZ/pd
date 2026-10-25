{ pkgs, kernel, modulesClosure, plainTree, bootTools, fido2Hmac }:
let
  # Tools for running setup and tests inside a VM.  The VM mounts the host's
  # /nix/store read-only over 9p, so nothing here is copied into the initrd.
  toolEnv = pkgs.buildEnv {
    name = "pd-tool-env";
    paths = with pkgs; [
      (python3.withPackages (p: [ ]))
      cryptsetup
      lvm2.bin
      thin-provisioning-tools
      btrfs-progs
      e2fsprogs
      xfsprogs
      f2fs-tools
      util-linux
      coreutils
      findutils
      gnugrep
      gnused
      gawk
      diffutils
      libargon2
      kmod
      cpio
      zstd
      xxd
      bash
      procps
      kexec-tools
      dosfstools
      mtools
      file
      # For installing Arch Linux in tests.
      pacman
      arch-install-scripts
      gnutar
      iproute2
      fido2Hmac
      # The stock-boot test derives the hidden key with OpenSSL, as a live system would.
      openssl.bin
    ];
    pathsToLink = [ "/bin" "/sbin" "/lib" "/share" ];
  };

  lvmConf = pkgs.writeText "lvm.conf" ''
    devices {
      obtain_device_list_from_udev = 0
      external_device_info_source = "none"
    }
    activation {
      udev_sync = 0
      udev_rules = 0
      monitoring = 0
    }
    backup {
      backup = 0
      archive = 0
    }
    global {
      use_lvmetad = 0
      thin_check_executable = "${pkgs.thin-provisioning-tools}/bin/thin_check"
      thin_dump_executable = "${pkgs.thin-provisioning-tools}/bin/thin_dump"
      thin_repair_executable = "${pkgs.thin-provisioning-tools}/bin/thin_repair"
    }
  '';

  init = pkgs.writeText "toolvm-init" ''
    #!/bin/sh
    export PATH=/bin
    mount -t proc proc /proc
    mount -t sysfs sysfs /sys
    mount -t devtmpfs devtmpfs /dev
    ln -s /proc/self/fd /dev/fd
    ln -s /proc/self/fd/0 /dev/stdin; ln -s /proc/self/fd/1 /dev/stdout; ln -s /proc/self/fd/2 /dev/stderr
    mkdir -p /dev/pts /dev/shm /run /tmp /nix/store /src /work /etc/lvm
    mount -t devpts devpts /dev/pts
    mount -t tmpfs tmpfs /run
    mount -t tmpfs tmpfs /tmp
    modprobe -a virtio_pci virtio_blk virtio_scsi virtio_net 9p 9pnet_virtio virtio_rng dm-mod dm-crypt dm-thin-pool btrfs ext4 xfs f2fs efivarfs vfat nls_cp437 nls_iso8859-1 \
      xhci_pci usbhid hid_generic 2>/dev/null
    opts=trans=virtio,version=9p2000.L,msize=1048576
    mount -t 9p -o $opts,ro,cache=loose store /nix/store
    mount -t 9p -o $opts,ro src /src
    mount -t 9p -o $opts work /work
    if mkdir -p /cache && mount -t 9p -o $opts cache /cache 2>/dev/null; then :; else rmdir /cache; fi
    if grep -q pd.net /proc/cmdline; then
      modprobe virtio_net
      ip link set eth0 up
      ip addr add 10.0.2.15/24 dev eth0
      ip route add default via 10.0.2.2
      echo "nameserver 10.0.2.3" > /etc/resolv.conf
    fi
    cp ${lvmConf} /etc/lvm/lvm.conf
    hostname "$(cat /work/hostname 2>/dev/null || echo localhost)"
    export PATH=${toolEnv}/bin:${toolEnv}/sbin:/bin
    export PD_PLAIN_TREE=${plainTree} PD_BOOT_TOOLS=${bootTools} LVM_SUPPRESS_FD_WARNINGS=1
    export PYTHONPATH=/src PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
    export SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt
    cd /work
    setsid cttyhack sh -c 'bash /work/run.sh; echo "PD-EXIT $?"'
    sync
    poweroff -f
  '';

  initrd = pkgs.runCommand "pd-toolvm-initrd.cpio.zst" {
    nativeBuildInputs = [ pkgs.cpio pkgs.zstd ];
  } ''
    root=$PWD/root
    mkdir -p $root/{bin,dev,proc,sys,lib}
    cp ${pkgs.pkgsStatic.busybox}/bin/busybox $root/bin/
    # The kernel runs /sbin/modprobe to load crypto algorithms on demand.
    ln -s bin $root/sbin
    for applet in $($root/bin/busybox --list); do
      [ -e $root/bin/$applet ] || ln -s busybox $root/bin/$applet
    done
    cp -r ${modulesClosure}/lib/modules $root/lib/
    install -m755 ${init} $root/init
    chmod -R u+w $root
    (cd $root && find . -print0 | sort -z |
      cpio --null -o -H newc --reproducible --owner=0:0 --quiet) | zstd -3 -q >$out
  '';
in
{
  inherit toolEnv initrd;
}


{ pkgs, kernel, modulesClosure, plainTree, src }:
# A RAM-only setup environment: boot it (as a UKI from a USB stick) on the
# target machine to run `pd randomize`, `pd init-decoy`, `pd init-hidden`.
# Nothing is written anywhere but the target disk, and nothing persists.
let
  inherit (pkgs) lib;

  pdSrc = pkgs.runCommand "pd-src" { } ''
    mkdir -p $out
    cp -r ${src}/pd ${src}/plain ${src}/target $out/
  '';

  env = pkgs.buildEnv {
    name = "pd-setup-env";
    paths = with pkgs; [
      python3Minimal
      cryptsetup
      lvm2.bin
      thin-provisioning-tools
      btrfs-progs
      util-linux
      coreutils
      findutils
      gnugrep
      gnused
      gawk
      libargon2
      kmod
      xxd
      bashInteractive
      procps
      less
      pciutils
      (writeShellScriptBin "pd" ''
        export PYTHONPATH=${pdSrc} PD_PLAIN_TREE=${plainTree}
        exec ${python3Minimal}/bin/python3 -m pd "$@"
      '')
    ];
    pathsToLink = [ "/bin" "/sbin" "/share" ];
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
      thin_check_executable = "${pkgs.thin-provisioning-tools}/bin/thin_check"
      thin_dump_executable = "${pkgs.thin-provisioning-tools}/bin/thin_dump"
      thin_repair_executable = "${pkgs.thin-provisioning-tools}/bin/thin_repair"
    }
  '';

  init = pkgs.writeText "setup-init" ''
    #!/bin/sh
    export PATH=/bin
    mount -t proc proc /proc
    mount -t sysfs sysfs /sys
    mount -t devtmpfs devtmpfs /dev
    ln -s /proc/self/fd /dev/fd
    mkdir -p /dev/pts /run /tmp /etc/lvm /mnt
    mount -t devpts devpts /dev/pts
    mount -t tmpfs tmpfs /run
    mount -t tmpfs tmpfs /tmp
    modprobe -a -q dm-mod dm-crypt dm-thin-pool btrfs vfat nls_cp437 nls_iso8859-1 2>/dev/null
    prev=-1
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      find /sys/devices -name modalias -exec cat {} + 2>/dev/null | sort -u |
        xargs -r modprobe -a -q -b 2>/dev/null
      count=$(wc -l </proc/modules)
      [ "$count" -eq "$prev" ] && break
      prev=$count
      sleep 0.5
    done
    hostname localhost
    export PATH=${env}/bin:${env}/sbin:/bin HOME=/root TERM=linux LVM_SUPPRESS_FD_WARNINGS=1
    mkdir -p /root
    cat <<'EOF'

    pd setup environment (RAM only; nothing here persists)

      pd salt                              print a new salt
      pd randomize   --config pd.toml      fill the disk with random data
      pd init-decoy  --config pd.toml --run
      pd init-hidden --config pd.toml --run

    See docs/design.md section 9.  Power off when done (poweroff -f).

    EOF
    cd /root
    setsid cttyhack ${env}/bin/bash -l
    poweroff -f
  '';

  closure = pkgs.closureInfo { rootPaths = [ env ]; };

  initrd = pkgs.runCommand "pd-setup-initrd.cpio.zst" {
    nativeBuildInputs = [ pkgs.cpio pkgs.zstd ];
  } ''
    root=$PWD/root
    mkdir -p $root/{bin,dev,proc,sys,lib,nix/store,etc}
    cp ${pkgs.pkgsStatic.busybox}/bin/busybox $root/bin/
    ln -s bin $root/sbin
    for applet in $($root/bin/busybox --list); do
      [ -e $root/bin/$applet ] || ln -s busybox $root/bin/$applet
    done
    cp -r ${modulesClosure}/lib/modules $root/lib/
    for p in $(cat ${closure}/store-paths); do
      cp -a $p $root/nix/store/
    done
    install -m755 ${init} $root/init
    install -Dm644 ${lvmConf} $root/etc/lvm/lvm.conf
    chmod -R u+w $root
    (cd $root && find . -print0 | sort -z |
      cpio --null -o -H newc --reproducible --owner=0:0 --quiet) | zstd -10 -T0 -q >$out
  '';

  uki = cmdline: pkgs.runCommand "pd-setup.efi" { nativeBuildInputs = [ pkgs.systemdUkify ]; } ''
    ukify build --linux ${kernel}/bzImage --initrd ${initrd} \
      --stub ${pkgs.systemd}/lib/systemd/boot/efi/linuxx64.efi.stub \
      --cmdline "${cmdline}" --output $out
  '';
in
{
  inherit initrd env;
  uki = uki "init_on_free=1 loglevel=4";
}

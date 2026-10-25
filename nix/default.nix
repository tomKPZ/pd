{ pkgs, src }:
let
  inherit (pkgs) lib;
  static = pkgs.pkgsStatic;

  lvm2Static = (static.lvm2.override {
    onlyLib = false;
    udevSupport = false;
  }).overrideAttrs (old: {
    # Static builds produce no shared library for the "lib" output.
    postInstall = (old.postInstall or "") + ''
      mkdir -p $lib
    '';
  });

  detectShift = static.stdenv.mkDerivation {
    pname = "detect_shift";
    version = "1";
    src = src + "/tools";
    dontConfigure = true;
    buildPhase = ''
      $CC -static -O2 -Wall -Wextra -Werror detect_shift.c -o detect_shift
    '';
    installPhase = ''
      install -Dm755 detect_shift $out/bin/detect_shift
    '';
  };

  # Copy statically linked binaries into one bin/ directory, stripped, and
  # refuse anything that is dynamically linked.
  staticBin = name: bins: pkgs.runCommand name { nativeBuildInputs = [ pkgs.binutils pkgs.file ]; } ''
    mkdir -p $out/bin
    ${lib.concatMapStrings (b: ''
      cp -L ${b} $out/bin/$(basename ${b} .static)
    '') bins}
    chmod -R u+w $out/bin
    for f in $out/bin/*; do
      strip --strip-all "$f"
      if file "$f" | grep -q 'dynamically linked'; then
        echo "$f is dynamically linked" >&2
        exit 1
      fi
    done
  '';

  # Tools on the boot USB.  Keep this list minimal: anything here is visible
  # to someone who has the USB but no password.
  bootTools = staticBin "pd-boot-tools" [
    "${static.busybox}/bin/busybox"
    "${lib.getBin static.cryptsetup}/bin/cryptsetup"
    "${static.libargon2}/bin/argon2"
  ];

  # Tools stored in the encrypted boot volume, used by late.sh.
  plainTools = staticBin "pd-plain-tools" [
    "${lvm2Static}/sbin/lvm.static"
    "${lvm2Static}/sbin/dmsetup.static"
    "${static.thin-provisioning-tools}/bin/pdata_tools"
    "${static.kexec-tools}/bin/kexec"
    "${lib.getBin static.jq}/bin/jq"
    "${static.fzf}/bin/fzf"
    "${detectShift}/bin/detect_shift"
  ];

  kernel = pkgs.linuxPackages.kernel;

  # Broad hardware coverage: a missing keyboard or storage driver means a
  # machine that cannot boot.
  rootModules = import ./modules.nix;

  # busybox modprobe can't reliably load compressed modules, so decompress
  # them and regenerate the dependency files.
  mkModulesClosure = rootModules: let
    closure = pkgs.makeModulesClosure {
      kernel = pkgs.aggregateModules [ kernel.modules ];
      firmware = pkgs.linux-firmware;
      inherit rootModules;
      allowMissing = true;
    };
  in pkgs.runCommand "pd-modules" { nativeBuildInputs = [ pkgs.xz pkgs.kmod ]; } ''
    cp -r ${closure} $out
    chmod -R u+w $out
    find $out/lib/modules -name '*.ko.xz' -exec xz -d {} +
    ver=$(ls $out/lib/modules)
    depmod -b $out -a $ver
  '';
  modulesClosure = mkModulesClosure rootModules;
  # Setup formats extra volumes as XFS; the boot environment never mounts them.
  setupModulesClosure = mkModulesClosure (rootModules ++ [ "xfs" ]);
  # The tool VM also has ext4 and f2fs, for the filesystem survey.
  toolModulesClosure = mkModulesClosure (rootModules ++ [ "xfs" "ext4" "f2fs" ]);

  # The boot environment's initrd, without per-install files (salt and
  # early_env.sh, which `pd usb` appends as a second cpio).
  bootInitrd = pkgs.runCommand "pd-boot-initrd.cpio.zst" {
    nativeBuildInputs = [ pkgs.cpio pkgs.zstd pkgs.findutils ];
  } ''
    root=$PWD/root
    mkdir -p $root/{bin,dev,proc,sys,run,tmp,mnt,etc,lib}
    cp ${bootTools}/bin/* $root/bin/
    # The kernel runs /sbin/modprobe to load crypto algorithms on demand.
    ln -s bin $root/sbin
    for applet in $($root/bin/busybox --list); do
      [ -e $root/bin/$applet ] || ln -s busybox $root/bin/$applet
    done
    cp -r ${modulesClosure}/lib/modules $root/lib/
    if [ -d ${modulesClosure}/lib/firmware ]; then
      cp -r ${modulesClosure}/lib/firmware $root/lib/
    fi
    install -m755 ${src}/boot/init $root/init
    install -m755 ${src}/boot/early.sh $root/early.sh
    chmod -R u+w $root
    (cd $root && find . -print0 | sort -z |
      cpio --null -o -H newc --reproducible --owner=0:0 --quiet) |
      zstd -19 -T0 -q >$out
  '';

  # Contents of the encrypted boot volume, minus per-install files.
  plainTree = pkgs.runCommand "pd-plain-tree" { } ''
    mkdir -p $out/bin
    cp ${plainTools}/bin/* $out/bin/
    ln -s pdata_tools $out/bin/thin_check
    ln -s pdata_tools $out/bin/thin_dump
    ln -s pdata_tools $out/bin/thin_rmap
    install -m755 ${src}/plain/late.sh $out/late.sh
    install -m644 ${src}/plain/thin-ro.sh $out/thin-ro.sh
    cp -r ${src}/plain/systems $out/systems
  '';
  # Console keymaps in the binary format busybox loadkmap reads, one per
  # kbd keymap (`pd usb` puts the configured one on the USB).
  keymaps = pkgs.runCommand "pd-keymaps" { nativeBuildInputs = [ pkgs.kbd ]; } ''
    mkdir -p $out
    for f in ${pkgs.kbd}/share/keymaps/i386/*/*.map.gz; do
      name=$(basename "$f" .map.gz)
      loadkeys -b "$f" > "$out/$name.bmap" 2>/dev/null </dev/null || rm -f "$out/$name.bmap"
    done
    test -s $out/de.bmap
  '';

  # pd as a single file, for live ISOs: `python3 pd.pyz open ...`.
  pdPyz = pkgs.runCommand "pd.pyz" { nativeBuildInputs = [ pkgs.python3 ]; } ''
    mkdir src
    cp -r --no-preserve=mode ${src}/pd src/
    rm -rf src/pd/__pycache__
    python3 -m zipapp src -o $out -p "/usr/bin/env python3" -m "pd.cli:entry"
  '';

  toolvm = import ./toolvm.nix { inherit pkgs kernel plainTree bootTools; modulesClosure = toolModulesClosure; };

  setupEnv = import ./setup-env.nix { inherit pkgs kernel plainTree src; modulesClosure = setupModulesClosure; };

  faketarget = import ./faketarget.nix { inherit pkgs modulesClosure bootTools plainTools src; };

  # Everything the test harness needs, at fixed names.
  testenv = pkgs.linkFarm "pd-testenv" [
    { name = "bzImage"; path = "${kernel}/bzImage"; }
    { name = "boot-initrd.cpio.zst"; path = bootInitrd; }
    { name = "toolvm-initrd.cpio.zst"; path = toolvm.initrd; }
    { name = "toolenv"; path = toolvm.toolEnv; }
    { name = "plain"; path = plainTree; }
    { name = "boot-tools"; path = bootTools; }
    { name = "stub.efi"; path = "${pkgs.systemd}/lib/systemd/boot/efi/linuxx64.efi.stub"; }
    { name = "ukify"; path = pkgs.systemdUkify; }
    { name = "ovmf"; path = pkgs.OVMF.fd; }
    { name = "busybox"; path = pkgs.pkgsStatic.busybox; }
    { name = "faketarget-initrd.cpio.zst"; path = faketarget; }
    { name = "setup-initrd.cpio.zst"; path = setupEnv.initrd; }
    { name = "keymaps"; path = keymaps; }
  ];
in
{
  inherit testenv toolvm setupEnv pdPyz;
  inherit bootTools plainTools bootInitrd plainTree modulesClosure detectShift lvm2Static;
  kernelImage = "${kernel}/bzImage";
  kernel = kernel;
  ukify = pkgs.systemdUkify;
  stub = "${pkgs.systemd}/lib/systemd/boot/efi/linuxx64.efi.stub";
  ovmf = pkgs.OVMF.fd;
}

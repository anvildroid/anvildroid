# NixOS integration for AnvilDroid.
#
# The controller and its helpers verify that their payload lives in a real,
# root-owned /usr/local/lib/anvildroid-controller (no symlinks, no group or
# world write) and write download caches next to it, so the payload is copied
# there on activation instead of being run from the read-only Nix store. The
# few absolute programs the release binaries call (/usr/bin/python3, ...) are
# provided as symlinks.
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.programs.anvildroid;
  pkg = cfg.package;
  payload = "/usr/local/lib/anvildroid-controller";
  python = pkg.python;
  waydroid = config.virtualisation.waydroid.package;

  # Programs the controller, its workers and in-process Waydroid run.
  # Mirrors COMMAND_PACKAGES in services/runtime-host.py plus Waydroid's own
  # runtime (its wrapper is bypassed when its modules are imported).
  runtimePath = with pkgs; [
    python
    waydroid
    lxc
    util-linux
    iproute2
    nftables
    iptables
    dnsmasq
    getent
    slirp4netns
    kmod
    curl
    jre_headless
    patchelf
    systemd
    coreutils
    findutils
    gnugrep
    gnused
    gawk
    gnutar
    gzip
    xz
    zstd
    bash
    procps
    psmisc
    e2fsprogs
    android-tools
    wl-clipboard
  ];
  servicePath = "${payload}/tools:${lib.makeBinPath runtimePath}:${lib.makeSearchPathOutput "bin" "sbin" runtimePath}";

  # Same default as nixos/modules/virtualisation/waydroid.nix.
  baseWaydroid =
    if config.networking.nftables.enable then pkgs.waydroid-nftables else pkgs.waydroid;

  # The fd fix of scripts/patch-waydroid.py, applied at build time because the
  # store is read-only.
  patchedWaydroid = baseWaydroid.overrideAttrs (old: {
    postPatch = (old.postPatch or "") + ''
      ${pkgs.python3.interpreter} - <<'EOF'
      import importlib.util, os
      from pathlib import Path
      spec = importlib.util.spec_from_file_location('patch', '${pkg}/${pkg.payload}/scripts/patch-waydroid.py')
      patch = importlib.util.module_from_spec(spec)
      spec.loader.exec_module(patch)
      patch.TARGET = Path('tools/helpers/run_core.py').resolve()
      patch.os.geteuid = lambda: 0
      patch.main()
      EOF
      rm -f tools/helpers/run_core.py.anvildroid-fd-backup
    '';
  });
in
{
  options.programs.anvildroid = {
    enable = lib.mkEnableOption "AnvilDroid, a desktop manager for Waydroid";

    package = lib.mkOption {
      type = lib.types.package;
      description = "The AnvilDroid package.";
    };

    systemWaydroidIntegration = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Start the system waydroid-container service through AnvilDroid's
        wrapper, as the other distribution packages do. Needed to manage
        System Waydroid (NVIDIA renderer, Binder preparation) from AnvilDroid.
      '';
    };

    patchWaydroid = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Apply AnvilDroid's Waydroid fix (close the selector and pipe of every
        helper command) to the Waydroid package. This rebuilds the Waydroid
        Python package locally, which only copies files and takes seconds.
        Ignored when virtualisation.waydroid.package is set explicitly.
      '';
    };

  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = pkgs.stdenv.hostPlatform.system == "x86_64-linux";
        message = "AnvilDroid release packages are built for x86_64-linux only.";
      }
    ];

    virtualisation.waydroid.enable = true;
    virtualisation.waydroid.package = lib.mkIf cfg.patchWaydroid (lib.mkOverride 900 patchedWaydroid);
    security.polkit.enable = true;

    environment.systemPackages = [ pkg ];


    # Absolute programs used by the release binaries and scripts.
    systemd.tmpfiles.rules = [
      "L+ /usr/bin/python3 - - - - ${python}/bin/python3"
      "L+ /usr/bin/java - - - - ${pkgs.jre_headless}/bin/java"
      "L+ /usr/bin/waydroid - - - - ${waydroid}/bin/waydroid"
      "L+ /usr/bin/prlimit - - - - ${pkgs.util-linux}/bin/prlimit"
      "L+ /usr/bin/systemctl - - - - /run/current-system/sw/bin/systemctl"
      # Desktop entries written by the GUI point here, not into the store.
      "L+ /usr/bin/anvildroid-gui - - - - /run/current-system/sw/bin/anvildroid-gui"
      "L+ /usr/bin/anvildroid - - - - /run/current-system/sw/bin/anvildroid"
    ];

    # Copy the payload when the package changes (the unit changes with it, so
    # nixos-rebuild switch reruns it). Files the controller downloaded at
    # runtime (ARM translation, NVIDIA payloads) are kept.
    systemd.services.anvildroid-payload = {
      description = "Install the AnvilDroid controller payload";
      wantedBy = [ "multi-user.target" ];
      requiredBy = [ "anvildroid-controller.service" ]
        ++ lib.optional cfg.systemWaydroidIntegration "waydroid-container.service";
      before = [
        "anvildroid-controller.service"
        "waydroid-container.service"
      ];
      after = [ "local-fs.target" ];
      path = [
        pkgs.coreutils
        pkgs.findutils
      ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        UMask = "0022";
      };
      script = ''
        src=${pkg}/${pkg.payload}
        dest=${payload}
        for dir in /usr /usr/local /usr/local/lib; do
          if [ -L "$dir" ]; then rm -f "$dir"; fi
          install -d -m 0755 -o root -g root "$dir"
          chown root:root "$dir"
          chmod 0755 "$dir"
        done
        if [ -L "$dest" ]; then rm -f "$dest"; fi
        install -d -m 0755 -o root -g root "$dest"
        if [ "$(cat "$dest/.nix-source" 2>/dev/null || true)" = "$src" ]; then
          exit 0
        fi
        if [ -f "$dest/.nix-manifest" ]; then
          while IFS= read -r file; do
            rm -f "$dest/$file"
          done < "$dest/.nix-manifest"
        fi
        cp -R --no-preserve=ownership "$src/." "$dest/"
        chown -R root:root "$dest"
        chmod -R u+w,go-w "$dest"
        (cd "$src" && find . \( -type f -o -type l \) -print) > "$dest/.nix-manifest"
        printf '%s\n' "$src" > "$dest/.nix-source"
      '';
    };

    systemd.sockets.anvildroid-controller = {
      description = "AnvilDroid runtime controller socket";
      wantedBy = [ "sockets.target" ];
      socketConfig = {
        # Authorization is SO_PEERCRED inside the controller.
        ListenStream = "/run/anvildroid/control.sock";
        SocketMode = "0666";
        DirectoryMode = "0755";
        Backlog = 64;
      };
    };

    systemd.services.anvildroid-controller = {
      description = "AnvilDroid runtime controller";
      requires = [ "anvildroid-controller.socket" ];
      after = [
        "local-fs.target"
        "anvildroid-controller.socket"
      ];
      # Like the distribution packages' try-restart: load the new payload on
      # upgrade. Runtime workers run in their own scopes and survive it.
      restartTriggers = [ pkg ];
      environment = {
        PATH = lib.mkForce servicePath;
        PYTHONNOUSERSITE = "1";
      };
      serviceConfig = {
        Type = "simple";
        ExecStart = "/usr/bin/python3 -B ${payload}/runtime-controller.py --store /var/lib/anvildroid-controller --socket /run/anvildroid/control.sock --idle-exit 120";
        User = "root";
        UMask = "0077";
        Delegate = true;
        StateDirectory = "anvildroid-controller";
        StateDirectoryMode = "0700";
        RuntimeDirectory = "anvildroid";
        RuntimeDirectoryMode = "0755";
        RuntimeDirectoryPreserve = true;
        Restart = "on-failure";
        RestartSec = 3;
        KillMode = "control-group";
        TimeoutStopSec = 240;
      };
    };

    systemd.services.waydroid-container = lib.mkIf cfg.systemWaydroidIntegration {
      environment = {
        PATH = lib.mkForce servicePath;
        # runtime-system.py loads GLib with ctypes before importing Waydroid.
        LD_LIBRARY_PATH = "${lib.getLib pkgs.glib}/lib";
      };
      serviceConfig = {
        ExecStart = lib.mkForce "/usr/bin/python3 -B ${payload}/runtime-system.py";
        RuntimeDirectory = "anvildroid-system";
        RuntimeDirectoryMode = "0700";
      };
    };
  };
}

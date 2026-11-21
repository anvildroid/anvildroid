# AnvilDroid on NixOS

| File | Purpose |
|---|---|
| `../../flake.nix` | Flake at the repository root: package, overlay, NixOS module, checks |
| `package.nix` | Repackages the prebuilt release (no compilation) |
| `sources.json` | Release asset and sha256 the package uses (`scripts/update-nix-sources.py`) |
| `module.nix` | `programs.anvildroid` — controller, Waydroid integration, payload install |
| `test.nix` | NixOS VM test (`nix build .#checks.x86_64-linux.vm`) |

## Install

In `/etc/nixos/flake.nix`:

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    anvildroid = {
      url = "github:anvildroid/anvildroid";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { nixpkgs, anvildroid, ... }: {
    nixosConfigurations.<hostname> = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        ./configuration.nix
        anvildroid.nixosModules.default
        { programs.anvildroid.enable = true; }
      ];
    };
  };
}
```

```bash
sudo nixos-rebuild switch --flake /etc/nixos#<hostname>
```

Then open **AnvilDroid** from the application menu. The module enables
`virtualisation.waydroid`, polkit and the socket-activated controller; nothing
else needs to be configured.

### Update

```bash
sudo nix flake update anvildroid --flake /etc/nixos
sudo nixos-rebuild switch --flake /etc/nixos#<hostname>
```

The in-app updater only reports new releases on NixOS. The switch restarts
the controller, as the other packages do on upgrade; running Android devices
keep running.

## Options

| Option | Default | Meaning |
|---|---|---|
| `programs.anvildroid.enable` | `false` | Install AnvilDroid |
| `programs.anvildroid.package` | release package | The AnvilDroid package |
| `programs.anvildroid.systemWaydroidIntegration` | `true` | Run `waydroid-container` through AnvilDroid's wrapper, as on other distributions (System Waydroid management, NVIDIA renderer) |
| `programs.anvildroid.patchWaydroid` | `true` | Apply AnvilDroid's Waydroid fd fix to `virtualisation.waydroid.package` (unless you set that package yourself) |

## What gets built on your machine

Nothing is compiled. Besides the usual NixOS system files, two derivations
are realised locally because they are not in cache.nixos.org:

- `anvildroid`: downloads the release's `.pkg.tar.zst`, runs `patchelf` on the
  host binaries and wraps the GUI (seconds).
- `waydroid` with the fd fix (`patchWaydroid`): copies Waydroid's Python
  files (seconds). Set `patchWaydroid = false` to use the cached Waydroid.

## How it works

The release binaries and the controller expect a regular Linux layout:

- The controller payload must live in a real, root-owned
  `/usr/local/lib/anvildroid-controller` (it refuses symlinked or writable
  parents) and stores downloads (ARM translation, NVIDIA payloads) next to
  it. `anvildroid-payload.service` copies the payload there from the store
  whenever the package changes and leaves downloaded files in place.
- `/usr/bin/python3`, `/usr/bin/java`, `/usr/bin/waydroid`,
  `/usr/bin/prlimit`, `/usr/bin/systemctl`, `/usr/bin/anvildroid` and
  `/usr/bin/anvildroid-gui` are symlinks created by systemd-tmpfiles. The
  Python one carries Waydroid's Python modules (gbinder, dbus, GObject),
  because System Waydroid imports Waydroid in-process.
- Desktop entries written by the GUI point at `/usr/bin/anvildroid-gui`,
  never at a store path that a later garbage collection would remove.
- The native X11 helper and the NVIDIA renderer are patched with `patchelf`;
  the derivation rewrites the hashes in their payload manifests, which the
  controller verifies before running them.

## Maintainers

After a release, the `nixos` job of `.github/workflows/release.yml` runs
`scripts/update-nix-sources.py`, builds the package, the module and the VM
test, and opens a pull request into `main` with the new pin. By hand:

```bash
python3 scripts/update-nix-sources.py 0.1.15
nix build -L .#checks.x86_64-linux.package .#checks.x86_64-linux.module .#checks.x86_64-linux.vm
```

Without Nix installed, the same commands run in the `nixos/nix` container
(`--device /dev/kvm` for the VM test; make `/dev/kvm` accessible to the
build users inside the container, or QEMU falls back to slow emulation).

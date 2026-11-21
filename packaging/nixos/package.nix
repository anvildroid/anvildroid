# AnvilDroid repackaged from the prebuilt Arch package of a GitHub release.
#
# Nothing is compiled: the derivation unpacks the release archive, points the
# host ELF files at Nix libraries (patchelf) and wraps the GUI. The NixOS module
# (module.nix) installs the controller payload where the controller expects it.
{
  lib,
  stdenv,
  fetchurl,
  zstd,
  autoPatchelfHook,
  wrapGAppsHook3,
  python3,
  # GUI
  webkitgtk_4_1,
  gtk3,
  glib,
  libsoup_3,
  cairo,
  pango,
  gdk-pixbuf,
  dbus,
  wayland,
  libayatana-appindicator,
  gobject-introspection,
  # Host helpers inside the controller payload
  libepoxy,
  libdrm,
  libgbm,
  libglvnd,
  libxkbcommon,
  libx11,
  libxcursor,
  libxi,
  libxfixes,
  libxext,
  libxrandr,
  libxscrnsaver,
  # Programs the GUI runs
  curl,
  xdg-utils,
  libnotify,
  sources ? lib.importJSON ./sources.json,
}:

let
  # /usr/bin/python3 for the controller and its helpers. Waydroid's own
  # modules are imported in-process (System Waydroid), so carry its Python
  # dependencies too.
  python = python3.buildEnv.override {
    extraLibs = with python3.pkgs; [
      dbus-python
      gbinder-python
      pyclip
      pygobject3
    ];
    makeWrapperArgs = [
      "--prefix"
      "GI_TYPELIB_PATH"
      ":"
      (lib.makeSearchPath "lib/girepository-1.0" [
        (lib.getLib glib)
        gobject-introspection
      ])
    ];
  };
  payload = "lib/anvildroid-controller";
in
stdenv.mkDerivation {
  pname = "anvildroid";
  inherit (sources) version;

  src = fetchurl { inherit (sources) url sha256; };

  nativeBuildInputs = [
    zstd
    autoPatchelfHook
    wrapGAppsHook3
    python3
  ];

  buildInputs = [
    webkitgtk_4_1
    gtk3
    glib
    libsoup_3
    cairo
    pango
    gdk-pixbuf
    dbus
    wayland
    libepoxy
    libdrm
    libgbm
    libglvnd
    libxkbcommon
    libx11
    libxcursor
    libxi
    libxfixes
    libxext
    libxrandr
    libxscrnsaver
    stdenv.cc.cc.lib
  ];

  # Loaded with dlopen by the tray icon.
  runtimeDependencies = [ libayatana-appindicator ];

  unpackPhase = ''
    runHook preUnpack
    mkdir source
    tar --zstd -xf "$src" -C source --exclude=.PKGINFO --exclude=.INSTALL
    runHook postUnpack
  '';
  sourceRoot = "source";

  dontConfigure = true;
  dontBuild = true;
  # Most ELF files in the payload are Android binaries; patch host ones only.
  dontAutoPatchelf = true;

  installPhase = ''
    runHook preInstall
    mkdir -p $out/bin $out/lib
    install -m 0755 usr/bin/anvildroid usr/bin/anvildroid-gui $out/bin/
    cp -R usr/local/lib/anvildroid-controller $out/${payload}
    cp -R usr/share $out/share
    # Units come from the NixOS module.
    substituteInPlace $out/share/applications/org.anvildroid.gui.desktop \
      --replace-fail 'Exec=anvildroid-gui' "Exec=$out/bin/anvildroid-gui"
    runHook postInstall
  '';

  preFixup = ''
    gappsWrapperArgs+=(
      --suffix PATH : ${lib.makeBinPath [ python curl xdg-utils libnotify glib dbus ]}
    )
  '';

  postFixup = ''
    p=$out/${payload}
    autoPatchelf -- $out/bin $p/tools $p/native-x11 \
      $p/nvidia-*/virgl_render_server $p/nvidia-*/virgl_test_server $p/nvidia-*/libvirglrenderer.so.1

    # The controller verifies payload files against their manifests; record
    # the hashes of the patched binaries.
    python3 - "$p" <<'EOF'
    import hashlib, json, sys
    from pathlib import Path
    root = Path(sys.argv[1])
    for manifest in [root / 'native-x11/manifest.json', *root.glob('nvidia-*/manifest.json')]:
        data = json.loads(manifest.read_text())
        for name in data['files']:
            data['files'][name] = hashlib.sha256((manifest.parent / name).read_bytes()).hexdigest()
        manifest.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')
    EOF
  '';

  passthru = {
    inherit python payload;
  };

  meta = {
    description = "Desktop manager for Waydroid: isolated Android devices and apps as native windows";
    homepage = "https://github.com/anvildroid/anvildroid";
    license = with lib.licenses; [
      mit
      bsd3
      asl20
    ];
    sourceProvenance = [ lib.sourceTypes.binaryNativeCode ];
    mainProgram = "anvildroid-gui";
    platforms = [ "x86_64-linux" ];
  };
}

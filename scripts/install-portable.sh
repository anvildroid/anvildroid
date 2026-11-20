#!/bin/sh
# Install only the user-level GUI/CLI. No sudo, runtime, or Android data changes.
set -eu
bundle=${1:-$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)}
bundle=$(CDPATH= cd -- "$bundle" && pwd)
test -x "$bundle/bin/anvildroid-gui" || { echo "Invalid AnvilDroid bundle: $bundle" >&2; exit 1; }
prefix=${ANVILDROID_PREFIX:-$HOME/.local/share/anvildroid}
mkdir -p "$prefix/bin" "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.local/share/mime/packages"
cp "$bundle/bin/anvildroid-gui" "$prefix/bin/anvildroid-gui"
cp "$bundle/bin/anvildroid" "$prefix/bin/anvildroid"
chmod 755 "$prefix/bin/anvildroid-gui" "$prefix/bin/anvildroid"
cat > "$HOME/.local/share/applications/org.anvildroid.gui.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=AnvilDroid
Comment=Manage isolated Waydroid runtimes
Exec="$prefix/bin/anvildroid-gui" %F
Terminal=false
Categories=System;Utility;
MimeType=application/vnd.android.package-archive;application/x-android-package-bundle;
EOF
# Split-package bundles (.apks, .xapk, .apkm) need a MIME type of their own.
# Keep this in step with packaging/mime/org.anvildroid.gui.xml.
cat > "$HOME/.local/share/mime/packages/org.anvildroid.gui.xml" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">
  <mime-type type="application/x-android-package-bundle">
    <comment>Android package bundle</comment>
    <sub-class-of type="application/zip"/>
    <glob pattern="*.apks"/>
    <glob pattern="*.xapk"/>
    <glob pattern="*.apkm"/>
  </mime-type>
</mime-info>
EOF
if command -v update-mime-database >/dev/null 2>&1; then
  update-mime-database "$HOME/.local/share/mime" || true
fi
# File managers find the file associations through the desktop database.
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$HOME/.local/share/applications" || true
fi
ln -sfn "$prefix/bin/anvildroid" "$HOME/.local/bin/anvildroid"
echo "Installed GUI and CLI to $prefix"
echo "If Waydroid is absent, the GUI still opens and explains the prerequisite."
echo "Optional controller install (requires root): sudo python3 $bundle/scripts/install-runtime-controller.py"

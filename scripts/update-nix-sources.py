#!/usr/bin/env python3
"""Point packaging/nixos/sources.json at a published release.

The Nix package repackages the release's Arch package, pinned by the sha256
that the release lists in SHA256SUMS. Usage: update-nix-sources.py VERSION
[--sums FILE]; without --sums the release's SHA256SUMS is downloaded.
"""
import argparse
import json
from pathlib import Path
import re
import sys
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / 'packaging/nixos/sources.json'
REPO = 'anvildroid/anvildroid'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version')
    parser.add_argument('--sums', type=Path, help='local SHA256SUMS of the release')
    args = parser.parse_args()
    version = args.version.removeprefix('v')
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(-(alpha|beta|rc)\.[0-9]+)?', version):
        sys.exit(f'Invalid version: {args.version}')
    base = f'https://github.com/{REPO}/releases/download/v{version}'
    asset = f'anvildroid-controller-{version}-1-x86_64.pkg.tar.zst'
    if args.sums:
        sums = args.sums.read_text()
    else:
        with urllib.request.urlopen(f'{base}/SHA256SUMS', timeout=60) as response:
            sums = response.read(1024 * 1024).decode()
    digests = [line.split()[0] for line in sums.splitlines()
               if len(line.split()) == 2 and line.split()[1].removeprefix('./') == asset]
    if len(digests) != 1 or not re.fullmatch('[0-9a-f]{64}', digests[0]):
        sys.exit(f'SHA256SUMS has no single entry for {asset}')
    data = {'version': version, 'url': f'{base}/{asset}', 'sha256': digests[0]}
    SOURCES.write_text(json.dumps(data, indent=2) + '\n')
    print(f'{SOURCES.relative_to(ROOT)}: {version} {digests[0]}')


if __name__ == '__main__':
    main()

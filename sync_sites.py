#!/usr/bin/env python3
"""Synchronize data.json with the upstream Sherlock project.

Adds sites that exist upstream but not locally. Sites that only exist
locally (added manually, e.g. Facebook, Weibo, Threads, Tagged...) are
NEVER removed. Existing entries are left untouched unless
--update-existing is passed, in which case entries changed upstream are
refreshed with the upstream version.

A timestamped backup of data.json is written before any modification.

Usage:
  python sync_sites.py                   # add new upstream sites
  python sync_sites.py --dry-run         # show what would change, write nothing
  python sync_sites.py --update-existing # also pull upstream changes to
                                         # entries present in both files
"""
import argparse
import json
import shutil
import sys
import time
import urllib.request
from pathlib import Path

UPSTREAM_URL = (
    'https://raw.githubusercontent.com/sherlock-project/sherlock/'
    'master/sherlock_project/resources/data.json'
)


def load_json_bytes(raw):
    """Parse JSON from bytes, raising a clear error if invalid."""
    return json.loads(raw.decode('utf-8'))


def main():
    ap = argparse.ArgumentParser(
        description='Sync data.json with the upstream Sherlock site list.',
    )
    ap.add_argument('--url', default=UPSTREAM_URL,
                    help='Upstream data.json URL (default: official master)')
    ap.add_argument('--file', default=None,
                    help='Local data.json path (default: repo data.json)')
    ap.add_argument('--update-existing', action='store_true',
                    help='Also refresh entries changed upstream (default: keep local)')
    ap.add_argument('--dry-run', action='store_true',
                    help='Only show what would change; write nothing')
    args = ap.parse_args()

    local_path = Path(args.file) if args.file else (
        Path(__file__).resolve().parent / 'sherlock_project' / 'resources' / 'data.json'
    )

    local = json.loads(local_path.read_text(encoding='utf-8'))

    print(f'Downloading upstream: {args.url}')
    with urllib.request.urlopen(args.url, timeout=30) as resp:
        upstream = load_json_bytes(resp.read())

    up_sites = {k: v for k, v in upstream.items() if k != '$schema'}
    local_sites = {k: v for k, v in local.items() if k != '$schema'}

    added = sorted(up_sites.keys() - local_sites.keys())
    local_only = sorted(local_sites.keys() - up_sites.keys())
    changed = sorted(
        k for k in up_sites.keys() & local_sites.keys()
        if up_sites[k] != local_sites[k]
    )

    print(f'Local sites   : {len(local_sites)}')
    print(f'Upstream sites: {len(up_sites)}')
    print()
    print(f'New upstream sites to ADD ({len(added)}):')
    for s in added:
        print(f'  + {s}')
    print()
    print(f'Local-only sites to KEEP ({len(local_only)}):')
    for s in local_only:
        print(f'  * {s}')
    if changed:
        mode = 'will refresh (--update-existing)' if args.update_existing else \
               'ignored (pass --update-existing to refresh)'
        print()
        print(f'Entries changed upstream ({len(changed)}): {mode}')
        for s in changed:
            print(f'  ~ {s}')
    print()

    if args.dry_run:
        print('Dry run: nothing written.')
        return 0

    if not added and not (changed and args.update_existing):
        print('Already in sync: nothing to do.')
        return 0

    # Backup before touching the file
    stamp = time.strftime('%Y%m%d-%H%M%S')
    backup = local_path.with_name(f'{local_path.name}.bak-{stamp}')
    shutil.copy2(local_path, backup)
    print(f'Backup written: {backup.name}')

    # Preserve the local file's key order; append new sites at the end.
    for s in added:
        local[s] = up_sites[s]
    if args.update_existing:
        for s in changed:
            local[s] = up_sites[s]

    # Sanity check before overwriting: it must serialize and parse back clean.
    serialized = json.dumps(local, ensure_ascii=False, indent=2)
    json.loads(serialized)

    local_path.write_text(serialized + '\n', encoding='utf-8')
    total = len([k for k in local if k != '$schema'])
    print(f'Written {local_path}: {total} sites '
          f'(+{len(added)} added, {len(changed) if args.update_existing else 0} refreshed, '
          f'{len(local_only)} local-only kept)')


if __name__ == '__main__':
    sys.exit(main())

"""Standalone offline CI entrypoint for local RPE Registry bundle governance."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .catalog import validate_release_bundle
from .identity import canonical_json
from .manifest import _directory
from .store import _write_at


def write_validation_report(report_path, report, bundle_path):
    """Write new external evidence without changing the validated bundle or old reports."""
    target = Path(os.path.abspath(Path(report_path).expanduser()))
    bundle = Path(os.path.abspath(Path(bundle_path).expanduser()))
    if target.is_relative_to(bundle):
        raise ValueError('validation report must be outside the release bundle')
    with _directory(target.parent) as fd:
        try:
            os.stat(target.name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ValueError('report destination already exists')
        _write_at(fd, target.name, canonical_json(report))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--keyring', type=Path, required=True)
    parser.add_argument('--mode', choices=('integrity', 'acceptance'), default='integrity')
    parser.add_argument('--owned-root', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    report = validate_release_bundle(args.bundle, keyring=args.keyring, mode=args.mode,
                                     owned_root=args.owned_root)
    if args.report is not None:
        try:
            write_validation_report(args.report, report, args.bundle)
        except (ValueError, OSError) as error:
            report['passed'] = False
            report['report_error'] = str(error)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report['passed'] is True else 1


if __name__ == '__main__':
    raise SystemExit(main())

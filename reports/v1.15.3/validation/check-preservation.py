#!/usr/bin/env python3
"""Compare bounded daily state and remove only the marked validation directory."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def git(path, *args):
    return subprocess.check_output(['git', '--no-optional-locks', '-C', path, *args], text=True)


def descendants(root):
    command = subprocess.check_output(['ps', '-axo', 'pid=,ppid=,command='], text=True)
    table = {}
    for line in command.splitlines():
        row = line.strip().split(None, 2)
        if len(row) == 3 and row[0].isdigit() and row[1].isdigit():
            table[int(row[0])] = (int(row[1]), row[2])
    ancestors = {os.getpid()}
    pid = os.getpid()
    while pid in table and table[pid][0] not in ancestors:
        pid = table[pid][0]
        ancestors.add(pid)
    pattern = re.compile(r'(?<![\w.-])' + re.escape(str(root)) + r'(?![\w.-])')
    return [pid for pid, (_, args) in table.items() if pid not in ancestors and pattern.search(args)]


def owned_cwds(root):
    probe = subprocess.run(['/usr/sbin/lsof', '-a', '-d', 'cwd', '-Fpn'],
                           capture_output=True, text=True, timeout=15)
    if probe.returncode not in (0, 1) or not probe.stdout:
        raise SystemExit('cannot read process working directories')
    matches = set()
    pid = None
    for line in probe.stdout.splitlines():
        if line.startswith('p') and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith('n/') and pid is not None and Path(line[1:]).resolve().is_relative_to(root):
            matches.add(pid)
    return sorted(matches)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('owned_root', type=Path)
    cli.add_argument('receipt', type=Path)
    args = cli.parse_args()
    root = args.owned_root.resolve(strict=True)
    expected = {'owned_root': str(root), 'purpose': 'rpe-v1153-hermes-compatibility'}
    if (root.parent != Path('/private/tmp') or not re.fullmatch(r'rpe-v1153\.[a-zA-Z0-9]+', root.name)
            or args.owned_root != root or root.stat().st_mode & 0o077
            or json.loads((root/'owner.json').read_bytes()) != expected
            or args.receipt.resolve().is_relative_to(root)):
        raise SystemExit('private owner or receipt mismatch')
    before = json.loads((root/'preservation-before.json').read_bytes())
    home = Path.home()
    skill_root = home/'.hermes/skills'
    if Path(before['skill_root']) != skill_root:
        raise SystemExit('skill root changed')
    skills = {str(p.relative_to(skill_root)): digest(p) for p in skill_root.rglob('*')
              if not p.is_symlink() and p.is_file()}
    paths = list(root.rglob('*'))
    active = descendants(root)
    cwds = owned_cwds(root)
    if active or cwds:
        raise SystemExit(f'validation processes still present: {sorted(set(active + cwds))}')
    receipt = {
        'daily_files': {str(Path(p).relative_to(home)): {'unchanged': digest(Path(p)) == sha}
                        for p, sha in before['daily_files'].items()},
        'skill_tree': {'before_file_count': len(before['skills']), 'after_file_count': len(skills),
                       'changed_paths': sorted(p for p in set(skills) | set(before['skills'])
                                               if skills.get(p) != before['skills'].get(p))},
        'repositories_before_integration': {
            Path(path).name: {'head_unchanged': git(path, 'rev-parse', 'HEAD').strip() == old['head'],
                              'status_unchanged': git(path, 'status', '--porcelain') == old['status']}
            for path, old in before['repositories'].items()},
        'owned_root': str(root), 'regular_files': sum(p.is_file() and not p.is_symlink() for p in paths),
        'regular_bytes': sum(p.stat().st_size for p in paths if p.is_file() and not p.is_symlink()),
        'symlinks': sum(p.is_symlink() for p in paths), 'owned_pids_before': active,
        'owned_cwd_pids_before': cwds,
        'private_settings_existed': (root/'private/host-settings.json').is_file(),
    }
    with args.receipt.open('x', encoding='utf-8') as output:
        shutil.rmtree(root)
        receipt.update(root_removed=not root.exists(), owned_pids_after=descendants(root),
                       owned_cwd_pids_after=owned_cwds(root))
        json.dump(receipt, output, ensure_ascii=False, indent=2)
        output.write('\n')


if __name__ == '__main__':
    main()

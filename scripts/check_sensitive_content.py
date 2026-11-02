#!/usr/bin/env python3
"""Check staged files, tracked files, or reachable Git history without printing values."""
import argparse
import datetime
import ipaddress
from pathlib import Path
import re
import shlex
import subprocess


PRIVATE_KEY = re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----")
IPV4 = re.compile(rb"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.-])")
IPV4_DASHED = re.compile(rb"\b(?:\d{1,3}-){3}\d{1,3}(?=\.(?:sslip|nip)\.io)")
IPV6 = re.compile(rb"\b[23][0-9a-fA-F]{3}:[0-9a-fA-F:]{3,}\b")
API_KEY = re.compile(rb"\b(?:AKIA[0-9A-Z]{16}|sk-proj-[A-Za-z0-9_-]{30,})\b")


def git(*args, input_data=None):
    return subprocess.check_output(['git', *args], input=input_data)


def private_identifiers():
    config = Path.home() / '.config/app_englishlearning/deploy.env'
    identifiers = []
    if config.is_file():
        for line in config.read_text().splitlines():
            key, separator, value = line.partition('=')
            if separator and key.strip() in {'DEPLOY_HOST', 'DEPLOY_HOST_IPV6', 'DEPLOY_SSH_KEY', 'PUBLIC_ORIGIN'}:
                parts = shlex.split(value, comments=True)
                if parts:
                    identifiers.append(parts[0].encode())
    return identifiers


def public_address(value):
    try:
        decoded = value.decode().replace('-', '.')
        parts = decoded.split('.')
        # Application versions use YY.M.D.build, which can resemble IPv4.
        if len(parts) == 4 and len(parts[0]) == 2:
            try:
                datetime.date(2000 + int(parts[0]), int(parts[1]), int(parts[2]))
                return False
            except ValueError:
                pass
        address = ipaddress.ip_address(decoded)
        return address.is_global and not address.is_multicast
    except ValueError:
        return False


def findings(content, identifiers):
    for number, line in enumerate(content.splitlines(), 1):
        credential = PRIVATE_KEY.search(line) or API_KEY.search(line)
        address = any(public_address(m.group()) for pattern in (IPV4, IPV4_DASHED, IPV6) for m in pattern.finditer(line))
        configured = any(value in line for value in identifiers)
        if credential or address or configured:
            yield number


def blobs_from_history():
    objects = git('rev-list', '--objects', '--all').splitlines()
    names = {}
    for row in objects:
        oid, _, name = row.partition(b' ')
        names[oid] = name.decode(errors='replace') or 'commit metadata'
    process = subprocess.Popen(['git', 'cat-file', '--batch'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        for requested_oid, name in names.items():
            process.stdin.write(requested_oid + b'\n')
            process.stdin.flush()
            oid, kind, size = process.stdout.readline().split()
            content = process.stdout.read(int(size))
            process.stdout.read(1)
            if kind in {b'blob', b'commit', b'tag'}:
                yield name, content
    finally:
        process.stdin.close()
        process.stdout.close()
        process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--tracked', action='store_true')
    group.add_argument('--history', action='store_true')
    args = parser.parse_args()
    identifiers = private_identifiers()
    if args.history:
        records = blobs_from_history()
    elif args.tracked:
        names = git('ls-files', '-z').decode().split('\0')
        records = ((name, Path(name).read_bytes()) for name in names if Path(name).is_file())
    else:
        names = git('diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z').decode().split('\0')
        records = ((name, git('show', ':' + name)) for name in names if name)
    count = 0
    for name, content in records:
        for line in findings(content, identifiers):
            print(f'{name}:{line}: possible sensitive deployment or credential content')
            count += 1
    if count:
        print(f'Blocked: {count} matches. Move private values to the external configuration.')
        return 1
    print('Sensitive-content check passed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

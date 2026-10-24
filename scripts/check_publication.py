"""Check public source / release boundaries without printing private values.

This is a publication gate, not a claim that automated scanning finds all PII.
Review documentation and publish only the intended branch and release tag.

Coverage:
  * every tracked file must be scannable -- text is decoded (UTF-8/GB18030) and
    matched against credentials, personal paths and personal tokens; images are
    only accepted when listed in REVIEWED_IMAGES (they cannot be text-scanned,
    so a human must have opened them); any other binary type fails closed;
  * ``--history`` additionally scans commit identities and messages, so a push
    cannot publish a personal email address or a nickname buried in history.

Nothing is published until both checks pass: CI runs this file first, and
``.githooks/pre-push`` blocks the push locally (``git config core.hooksPath
.githooks``).
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_ROOTS = {'.git', '.venv', '.tmp', 'data', 'imports', 'exports', 'reports', 'release', 'build', 'instances'}
SECRET = re.compile(r'(?:sk-[A-Za-z0-9]{24,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)')
PERSONAL_PATH = re.compile(r'[A-Za-z]:[\\/]Users[\\/](?!Public(?:[\\/]|\b)|Default(?:[\\/]|\b))[^\\/\s<>]+', re.I)

# --- personal tokens: derived from the private store, never hardcoded --------
# Real QQ numbers, group ids, nicknames and persona names are NEVER written in
# this file. They are derived at run time from the maintainer's private store
# (default ~/.water-group, override with WATER_GROUP_HOME or --private-home):
#
#   config.json                     self_account / self_names / groups /
#                                   member_mappings (QQ, name, card, aliases)
#   personas/*.md                   private persona card names (file stems)
#   data/*memories*.sqlite3         member_profiles.user_id / primary_name /
#                                   aliases -- grows as the bot learns members,
#                                   so new members are covered automatically
#   rules/water_group.md            numeric ids mentioned in the private SOP
#   publication_denylist.txt        extra literal tokens (handle, personal domain)
#   publication_allow.txt           generic words that must not trip the gate
#                                   (e.g. a member alias that is also a common
#                                   word); kept private on purpose
#
# CI has no private store: the scan then covers credentials, personal paths,
# image allowlists and history, and reports private_store='missing'. The local
# pre-push / commit-msg hooks run with --require-private, so publishing from the
# maintainer's machine is always checked against the real data.
SKIP_TOKEN = re.compile(r'^seed:')          # documented test-seed convention
MIN_NUMERIC = 5                             # shorter digit runs are ids, not QQ
MIN_RULES_NUMERIC = 6                       # prose heuristic: 5-digit ports (43000) are noise


def private_home(explicit=None):
    for cand in (explicit, os.environ.get('WATER_GROUP_HOME'), os.environ.get('IDLEJOT_WG_HOME'),
                 Path.home() / '.water-group'):
        if cand and Path(cand).is_dir():
            return Path(cand)
    return None


def _add_token(tokens, value):
    if value is None:
        return
    token = str(value).strip()
    if len(token) < 2 or SKIP_TOKEN.match(token):
        return
    if token.isdigit() and len(token) < MIN_NUMERIC:
        return
    tokens.add(token)


def private_tokens(home):
    """Collect personal tokens from the private store; never from the repo."""
    tokens = set()
    cfg = Path(home) / 'config.json'
    if not cfg.is_file():
        cfg = ROOT / 'water-group' / 'config.json'   # gitignored runtime copy
    if cfg.is_file():
        try:
            data = json.loads(cfg.read_text('utf-8'))
        except (OSError, ValueError):
            data = {}
        _add_token(tokens, data.get('self_account'))
        for name in data.get('self_names') or []:
            _add_token(tokens, name)
        for gid, group in (data.get('groups') or {}).items():
            _add_token(tokens, gid)
            _add_token(tokens, group.get('name'))
            for qq, member in (group.get('member_mappings') or {}).items():
                _add_token(tokens, qq)
                _add_token(tokens, member.get('name'))
                _add_token(tokens, member.get('card'))
                for alias in member.get('aliases') or []:
                    _add_token(tokens, alias)
    for card in (Path(home) / 'personas').glob('*.md'):
        _add_token(tokens, card.stem)
    for db in (Path(home) / 'data').glob('*memories*.sqlite3'):
        try:
            con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
            for uid, name, aliases in con.execute(
                    'SELECT user_id, primary_name, aliases FROM member_profiles'):
                _add_token(tokens, uid)
                _add_token(tokens, name)
                try:
                    for alias in json.loads(aliases or '[]'):
                        _add_token(tokens, alias)
                except ValueError:
                    pass
            con.close()
        except sqlite3.Error:
            pass
    rules = Path(home) / 'rules' / 'water_group.md'
    if rules.is_file():
        for hit in re.finditer(r'(?<!\d)[1-9]\d{%d,10}(?!\d)' % (MIN_RULES_NUMERIC - 1),
                               rules.read_text('utf-8', 'replace')):
            tokens.add(hit.group(0))
    extra = Path(home) / 'publication_denylist.txt'
    if extra.is_file():
        for line in extra.read_text('utf-8').splitlines():
            _add_token(tokens, line.split('#', 1)[0].strip())
    allow = Path(home) / 'publication_allow.txt'
    if allow.is_file():
        for line in allow.read_text('utf-8').splitlines():
            tokens.discard(line.split('#', 1)[0].strip())
    return tokens


def personal_pattern(tokens):
    if not tokens:
        return None
    parts = sorted((re.escape(t) for t in tokens), key=len, reverse=True)
    return re.compile('|'.join(parts))


TEXT_EXT = {'.py', '.js', '.cjs', '.mjs', '.css', '.html', '.md', '.txt', '.json', '.toml',
            '.yml', '.yaml', '.cs', '.ps1', '.pyw', '.cmd', '.bat', '.sh', '.svg', '.lock',
            '.ini', '.cfg', '.jsonc', '.example', '.config'}
TEXT_NAMES = {'LICENSE', '.gitignore', '.gitattributes', '.githooks/pre-push', '.githooks/commit-msg'}
IMAGE_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.ico'}
# Images cannot be text-scanned. Every entry below was opened and checked by a
# human (no nicknames, QQ numbers, group names or paths). A new image fails the
# gate until someone reviews it and adds the path here.
REVIEWED_IMAGES = {
    'doc/assets/title.png',
    'doc/assets/agents/claude.png',
    'doc/assets/agents/deepseek.png',
    'doc/assets/agents/openai.png',
    'doc/assets/agents/workbuddy.png',
}
NOREPLY_EMAIL = re.compile(
    r'^(?:\d+\+)?[A-Za-z0-9._-]+@users\.noreply\.github\.com$|^noreply@github\.com$', re.I)


def allowed_path(name, *, archive=False):
    p = PurePosixPath(name)
    if not name or '\\' in name or ':' in name or p.is_absolute() or '..' in p.parts:
        return False
    if p.parts[0] in PRIVATE_ROOTS:
        return False
    if p.name == '.env' or (p.name.startswith('.env.') and p.name != '.env.example'):
        return False
    public_ca = archive and name == 'runtime/Lib/site-packages/certifi/cacert.pem'
    if p.suffix.lower() in {'.db', '.sqlite', '.sqlite3', '.log', '.dmp', '.bundle', '.pem', '.key', '.bak'} and not public_ca:
        return False
    if archive:
        if 'snowluma' in p.parts or any(part.lower().startswith('snowluma-') for part in p.parts):
            return False
        if p.name.endswith('.QA.exe') or p.name == 'direct_url.json':
            return False
        if p.parts[0] == '.cache' and (len(p.parts) < 3 or p.parts[1] not in {'rapidocr', 'voice-models'}):
            return False
    elif p.parts[0] in {'.cache', 'tools', 'node_modules'} or p.suffix.lower() in {'.zip', '.7z', '.exe', '.dll'}:
        return False
    return True


def check_text(name, data, personal=None):
    if PurePosixPath(name).suffix not in TEXT_EXT and name not in TEXT_NAMES:
        return []
    try:
        text = data.decode('utf-8-sig')
    except UnicodeError:
        try:
            text = data.decode('gb18030')   # legacy logs and hand-written notes
        except UnicodeError:
            return [f'undecodable text file: {name}']
    return check_string(name, text, personal)


def check_string(name, text, personal=None):
    """Scan one text blob; `name` only labels the finding."""
    result = []
    for label, pattern in [('credential', SECRET), ('personal path', PERSONAL_PATH)]:
        if pattern.search(text):
            result.append(f'{label}: {name}')
    if personal is not None:
        hit = personal.search(text)
        if hit:
            result.append(f'personal token {hit.group(0)!r}: {name}')
    return result


def source_check(private=None, require_private=False):
    home = private_home(private)
    if require_private and home is None:
        raise RuntimeError('private store not found: pass --private-home or set WATER_GROUP_HOME')
    tokens = private_tokens(home) if home else set()
    personal = personal_pattern(tokens)
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode('utf-8').split('\0')
    errors = []
    count = 0
    for name in filter(None, names):
        file = ROOT / name
        if not file.is_file():  # Worktree deletions are allowed before staging.
            continue
        count += 1
        if not allowed_path(name):
            errors.append('private source path: ' + name)
        if file.is_symlink() or file.is_junction():
            errors.append('linked source: ' + name)
        suffix = PurePosixPath(name).suffix.lower()
        if suffix in IMAGE_EXT:
            if name not in REVIEWED_IMAGES:
                errors.append('unreviewed image (open it, then add to REVIEWED_IMAGES): ' + name)
            continue
        if suffix not in TEXT_EXT and name not in TEXT_NAMES:
            errors.append('unscannable file type: ' + name)
            continue
        errors.extend(check_text(name, file.read_bytes(), personal))
    for name in ('README.md', 'LICENSE', 'doc/THIRD_PARTY.md', 'doc/SECURITY.md'):
        if not (ROOT / name).is_file():
            errors.append('missing: ' + name)
    # Document links to source files should work in the public repository.
    for doc in (ROOT / name for name in names if name.endswith('.md') and (ROOT / name).is_file()):
        for link in re.findall(r'\]\(([^)]+)\)', doc.read_text('utf-8')):
            if '://' in link or link.startswith(('#', 'mailto:')):
                continue
            target = link.split('#', 1)[0]
            if target and not (doc.parent / target).exists():
                errors.append(f'broken local link: {doc.name} -> {target}')
    if errors:
        raise RuntimeError('\n'.join(errors))
    return dict(source_files=count, source_scan='passed',
                private_store='ok' if home else 'missing',
                private_tokens=len(tokens))


def history_check(personal=None):
    """Scan commit identities and messages: publishing history publishes them too."""
    errors = []
    raw = subprocess.check_output(
        ['git', 'log', 'HEAD', '--format=%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%B%x1e'], cwd=ROOT)
    for record in raw.decode('utf-8', 'replace').split('\x1e'):
        if not record.strip():
            continue
        try:
            sha, _an, ae, _cn, ce, message = record.strip('\n').split('\x1f', 5)
        except ValueError:
            continue
        for role, email in (('author', ae), ('committer', ce)):
            if email and not NOREPLY_EMAIL.match(email):
                errors.append(f'non-noreply {role} email {email} in {sha[:12]}')
        errors.extend(check_string(f'commit {sha[:12]}', message, personal))
    if errors:
        raise RuntimeError('\n'.join(errors))
    return dict(history_scan='passed')


def text_check(text, label, personal):
    """Gate for anything typed into GitHub: issue body, PR body, commit message.

    Usage:
      python scripts/check_publication.py --text draft.md
      gh issue create ... | python scripts/check_publication.py --text -
    """
    errors = check_string(label, text, personal)
    if errors:
        raise RuntimeError('\n'.join(errors))
    return dict(text_scan='passed', text_label=label)


def archive_check(path):
    if path.name.endswith('.tar.gz'):
        return linux_archive_check(path)
    errors = []
    with zipfile.ZipFile(path) as z:
        names = [info.filename for info in z.infolist() if not info.is_dir()]
        if len(names) != len(set(names)) or any(not n.startswith('Tulpa/') for n in names):
            raise RuntimeError('Unexpected archive root or duplicate entries')
        manifest = json.loads(z.read('Tulpa/build-manifest.json'))
        expected = {'Tulpa/' + name for name in manifest['files']} | {'Tulpa/build-manifest.json'}
        if set(names) != expected:
            errors.append('archive does not match build allowlist')
        if manifest.get('product') != 'Tulpa' or manifest.get('working_tree_overlay'):
            errors.append('unexpected product / working-tree overlay')
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        if manifest.get('checkpoint') != head:
            errors.append('archive was not built from current source commit')
        hashes = manifest.get('file_sha256', {})
        if set(hashes) != set(manifest['files']):
            errors.append('missing per-file upgrade integrity manifest')
        for name, sha in hashes.items():
            if hashlib.sha256(z.read('Tulpa/' + name)).hexdigest() != sha:
                errors.append('upgrade hash mismatch: ' + name)
        for required in ('Tulpa.exe', 'Tulpa.Support.exe', 'LICENSE', 'doc/THIRD_PARTY.md', 'doc/assets/title.png', 'web/tulpa-logo.png',
                         'tools/qq-reader/LICENSE', 'tools/wechat-reader/LICENSE'):
            if 'Tulpa/' + required not in expected:
                errors.append('missing package file: ' + required)
        for name in names:
            rel = name.removeprefix('Tulpa/')
            if not allowed_path(rel, archive=True):
                errors.append('private archive path: ' + rel)
            if rel == 'runtime/Lib/site-packages/certifi/cacert.pem':
                ca = z.read(name).decode('ascii')
                if '-----BEGIN CERTIFICATE-----' not in ca or 'PRIVATE KEY' in ca:
                    errors.append('invalid public CA certificate bundle')
            # Audit our files; third-party distributions retain their original
            # test fixtures, licenses and source URLs (not our personal data).
            if not rel.startswith(('runtime/', '_internal/', 'tools/')):
                if PurePosixPath(rel).suffix in TEXT_EXT or rel == 'LICENSE':
                    errors.extend(check_text(rel, z.read(name)))
        corrupt = z.testzip()
        if corrupt:
            errors.append('archive CRC failure: ' + corrupt)
    if errors:
        raise RuntimeError('\n'.join(errors))
    with path.open('rb') as stream:
        sha = hashlib.file_digest(stream, 'sha256').hexdigest()
    return dict(archive=path.name, version=manifest['version'], commit=head, files=len(names),
                bytes=path.stat().st_size, sha256=sha, archive_scan='passed', crc='passed')


def linux_archive_check(path):
    """Validate the actual Linux deliverable, including modes and every byte."""
    errors = []
    with tarfile.open(path, 'r:gz') as archive:
        members = archive.getmembers()
        names = [m.name for m in members]
        if len(names) != len(set(names)) or any(not m.isfile() or not m.name.startswith('Tulpa/') for m in members):
            raise RuntimeError('Unexpected Linux archive root, link or duplicate entry')
        manifest = json.load(archive.extractfile('Tulpa/build-manifest.json'))
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        expected = {'Tulpa/'+name for name in manifest['files']} | {'Tulpa/build-manifest.json'}
        if set(names) != expected or set(manifest['file_sha256']) != set(manifest['files']):
            errors.append('Linux archive does not match integrity manifest')
        if (manifest.get('product'), manifest.get('platform'), manifest.get('edition'), manifest.get('checkpoint')) != ('Tulpa', 'linux', 'source', head):
            errors.append('Unexpected Linux product, platform or source commit')
        for name in ('tulpa', 'setup-linux.sh', 'LICENSE', 'README.md', 'requirements/linux.txt',
                     'requirements/tui.txt', 'chatlocal/terminal_cli.py', 'chatlocal/tui/app.py'):
            if 'Tulpa/'+name not in expected: errors.append('Missing Linux entry: '+name)
        for member in members:
            rel = member.name.removeprefix('Tulpa/')
            raw = archive.extractfile(member).read()
            if not allowed_path(rel, archive=True): errors.append('Private Linux path: '+rel)
            errors.extend(check_text(rel, raw))
            if rel != 'build-manifest.json' and hashlib.sha256(raw).hexdigest() != manifest['file_sha256'].get(rel):
                errors.append('Linux hash mismatch: '+rel)
            if rel in ('tulpa', 'setup-linux.sh') and (not member.mode & 0o111 or b'\r\n' in raw):
                errors.append('Linux launcher must be executable with LF line endings: '+rel)
    if errors: raise RuntimeError('\n'.join(errors))
    with path.open('rb') as stream: sha = hashlib.file_digest(stream, 'sha256').hexdigest()
    return dict(archive=path.name, version=manifest['version'], commit=head, files=len(names),
                bytes=path.stat().st_size, sha256=sha, archive_scan='passed')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--archive', type=Path)
    p.add_argument('--private-home', type=Path, help='private store (default: WATER_GROUP_HOME or ~/.water-group)')
    p.add_argument('--require-private', action='store_true',
                   help='fail when the private store is missing (use in local hooks)')
    p.add_argument('--history', action='store_true', help='also scan commit identities and messages')
    p.add_argument('--text', metavar='FILE', help='scan one text draft (issue/PR body/commit message); - reads stdin')
    args = p.parse_args()
    home = private_home(args.private_home)
    if args.text is not None:
        if args.require_private and home is None:
            raise RuntimeError('private store not found: pass --private-home or set WATER_GROUP_HOME')
        personal = personal_pattern(private_tokens(home)) if home else None
        if args.text == '-':
            import sys
            result = text_check(sys.stdin.read(), '<stdin>', personal)
        else:
            path = Path(args.text)
            result = text_check(path.read_text('utf-8'), str(path), personal)
        result['private_store'] = 'ok' if home else 'missing'
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        result = source_check(args.private_home, args.require_private)
        if args.history:
            personal = personal_pattern(private_tokens(home)) if home else None
            result.update(history_check(personal))
        if args.archive:
            result.update(archive_check(args.archive))
        print(json.dumps(result, ensure_ascii=False, indent=2))

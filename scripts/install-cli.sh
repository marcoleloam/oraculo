#!/bin/sh
# Bootstrap from a reviewed manifest and built wheelhouse, without an installed CLI.
set -eu
if [ "$#" -lt 1 ]; then
    echo 'Usage: install-cli.sh VERIFIED_BUNDLE [runtime install options]' >&2
    echo 'Requires existing Python 3.12 and uv. Prepares only; never starts a service.' >&2
    exit 2
fi
bundle=$1
shift
exec "${ORACULO_PYTHON:-python3.12}" - "$bundle" "$@" <<'PY'
import hashlib
import json
import pathlib
import sys
import zipfile

bundle = pathlib.Path(sys.argv[1]).expanduser().resolve()
options = sys.argv[2:]
if sys.version_info[:2] != (3, 12):
    raise SystemExit('Python 3.12 required; bootstrap does not download Python.')
manifest = json.loads((bundle / 'manifest.json').read_text())
files = {p.name: p for p in bundle.iterdir() if p.name != 'manifest.json'}
if any(not p.is_file() or p.is_symlink() for p in files.values()) or set(files) != set(manifest['files']):
    raise SystemExit('Invalid bundle inventory.')
for name, path in files.items():
    with path.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != manifest['files'][name]:
            raise SystemExit('Bundle integrity check failed.')
wheels = [p for n, p in files.items() if n.startswith('oraculo-') and n.endswith('.whl')]
if len(wheels) != 1:
    raise SystemExit('Bundle must contain exactly one Oraculo wheel.')
# The installer is taken from the verified artifact, never the working checkout.
with zipfile.ZipFile(wheels[0]) as wheel:
    code = wheel.read('oraculo/runtime.py')
filename = str(wheels[0]) + '!/oraculo/runtime.py'
sys.argv = [filename, 'runtime', 'install', '--bundle', str(bundle), *options]
exec(compile(code, filename, 'exec'), {'__name__': '__main__', '__file__': filename})
PY

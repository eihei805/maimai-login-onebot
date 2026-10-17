"""Download public song catalogs for a fresh installation; no account access."""
from __future__ import annotations
import json
import os
from pathlib import Path
import tempfile
import urllib.request

SOURCES = {
    'music_data.json': ('https://www.diving-fish.com/api/maimaidxprober/music_data', None),
    'songs.json': ('https://maimai.lxns.net/api/v0/maimai/song/list', 'songs'),
}
MAX_BYTES = 32 * 1024 * 1024


def fetch_catalogs(destination: Path):
    # Validate both downloads before replacing either existing cache.
    catalogs = {}
    for name, (url, key) in SOURCES.items():
        request = urllib.request.Request(url, headers={'User-Agent': 'maimai-login-catalogs/1'})
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('catalog_too_large')
        data = json.loads(raw)
        rows = data.get(key) if key and isinstance(data, dict) else data
        if not isinstance(rows, list) or not 1 <= len(rows) <= 100000 or any(not isinstance(r, dict) for r in rows):
            raise ValueError('invalid_catalog')
        if name == 'music_data.json':
            if any(not isinstance(r.get('id'), (str, int)) or isinstance(r.get('id'), bool)
                   or not isinstance(r.get('title'), str) or r.get('type') not in ('SD', 'DX')
                   or not isinstance(r.get('charts'), list) for r in rows):
                raise ValueError('invalid_catalog')
        else:
            if any(type(r.get('id')) is not int or not isinstance(r.get('title'), str)
                   or not isinstance(r.get('difficulties'), dict) for r in rows):
                raise ValueError('invalid_catalog')
        catalogs[name] = rows
    destination.mkdir(parents=True, exist_ok=True)
    for name, rows in catalogs.items():
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=destination,
                                             prefix='.catalog-', delete=False) as output:
                temporary = Path(output.name)
                json.dump(rows, output, ensure_ascii=False, separators=(',', ':'))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination / name)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return {name: len(rows) for name, rows in catalogs.items()}


if __name__ == '__main__':
    try:
        counts = fetch_catalogs(Path(__file__).with_name('data'))
    except Exception as exc:
        raise SystemExit('Public catalog setup failed: ' + type(exc).__name__) from None
    print(json.dumps(counts))

"""Native QR score conversion and bounded, explicitly bound site uploads.
No file/credential discovery, game access, logging, or network at import time.
See SCORE_UPLOAD.md for ownership and completion contracts.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import json
import re
import math
import time
import urllib.request
import urllib.error

FISH = 'https://www.diving-fish.com/api/maimaidxprober/player/'
LXNS = 'https://maimai.lxns.net/api/v0/'
MAX_ROWS = 10000
MAX_BYTES = 8 * 1024 * 1024
FC = ('', 'fc', 'fcp', 'ap', 'app')
FS = ('', 'sync', 'fs', 'fsp', 'fsd', 'fsdp')
# DLL constants plus scoring branches (see evidence references in docs).
COMBO = {0: '', 1: 'fc', 2: 'fcp', 3: 'ap', 4: 'app'}
# ChainLow/Hi and SyncLow/Hi are NOT mapped just by their names.
SYNC = {0: '', 5: 'sync'}

class UploadError(ValueError):
    """Messages are fixed codes, never upstream bodies or credentials."""

@dataclass(repr=False, frozen=True)
class Credentials:
    fish_import_token: str | None = field(default=None, repr=False)
    lxns_personal_token: str | None = field(default=None, repr=False)
    lxns_developer_token: str | None = field(default=None, repr=False)
    lxns_friend_code: int | None = None

@dataclass(repr=False, frozen=True)
class Prepared:
    fish: list[dict] = field(repr=False)
    lxns: list[dict] = field(repr=False)
    omitted_badges: int = 0


def _integer(value, low, high, code):
    if type(value) is not int or not low <= value <= high:
        raise UploadError(code)
    return value


def _token(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 4096 or any(not 33 <= ord(c) <= 126 for c in value):
        raise UploadError('invalid_credential')
    return value


def prepare_scores(rows, fish_catalog, lxns_catalog):
    """Pure conversion of flattened UserMusicDetail dicts. No modulo ID fallback."""
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
        raise UploadError('invalid_rows')
    if isinstance(lxns_catalog, dict):
        lxns_catalog = lxns_catalog.get('songs')
    if not isinstance(fish_catalog, list) or not isinstance(lxns_catalog, list):
        raise UploadError('invalid_catalog')
    native = {}
    for song in fish_catalog:
        try:
            sid = int(song['id'])
            if str(sid) != str(song['id']) or sid <= 0 or song['type'] not in ('SD', 'DX'):
                raise ValueError()
            native.setdefault(sid, []).append(song)
        except (KeyError, TypeError, ValueError):
            raise UploadError('invalid_catalog') from None
    fish, lxns, seen, omitted = [], [], set(), 0
    for row in rows:
        if not isinstance(row, dict):
            raise UploadError('invalid_row')
        if 'playCount' in row:
            plays = _integer(row['playCount'], 0, 2**31-1, 'invalid_play_count')
            if plays == 0: continue
        sid = _integer(row.get('musicId'), 1, 99999, 'invalid_music_id')
        level = _integer(row.get('level'), 0, 4, 'unsupported_difficulty')
        achievement = _integer(row.get('achievement'), 0, 1010000, 'invalid_achievement') / 10000.0
        dx = _integer(row.get('deluxscoreMax'), 0, 1000000, 'invalid_dx_score')
        matches = native.get(sid, [])
        if len(matches) != 1:
            raise UploadError('unknown_or_ambiguous_music_id')
        song = matches[0]
        title, kind = song.get('title'), song['type']
        if not isinstance(title, str) or not title or level >= len(song.get('charts', [])):
            raise UploadError('unknown_chart')
        # Fish's write endpoint resolves title/type, so reject title collisions too.
        if sum(s.get('title') == title and s.get('type') == kind for s in fish_catalog) != 1:
            raise UploadError('ambiguous_title')
        chart = song['charts'][level]
        notes = chart.get('notes')
        if isinstance(notes, list) and notes and all(type(n) is int and n >= 0 for n in notes):
            if dx > 3 * sum(notes):
                raise UploadError('dx_score_exceeds_chart')
        key = (sid, level)
        if key in seen:
            raise UploadError('duplicate_chart')
        seen.add(key)
        chart_type = {'SD': 'standard', 'DX': 'dx'}[kind]
        candidates = [s for s in lxns_catalog if s.get('title') == title and any(
            d.get('difficulty') == level for d in s.get('difficulties', {}).get(chart_type, []))]
        if len(candidates) != 1:
            raise UploadError('unknown_or_ambiguous_lxns_chart')
        lxid = _integer(candidates[0].get('id'), 1, 9999, 'invalid_lxns_id')
        badges = {}
        for source, dest, mapping in [('comboStatus', 'fc', COMBO), ('syncStatus', 'fs', SYNC)]:
            value = row.get(source)
            if type(value) is int and value in mapping:
                if mapping[value]: badges[dest] = mapping[value]
            else:
                # Unknown/unsupported native optional flags are deliberately omitted;
                # the pre-write read/merge preserves any existing site badge.
                omitted += 1
        fish.append(dict(title=title, type=kind, level_index=level, achievements=achievement, dxScore=dx, **badges))
        lxns.append(dict(id=lxid, type=chart_type, level_index=level, achievements=achievement, dx_score=dx, **badges))
    if not fish: raise UploadError('no_played_scores')
    return Prepared(fish, lxns, omitted)


def _allowed(method, url):
    return ((method, url) in {('GET', FISH+'records'), ('POST', FISH+'update_records'),
            ('GET', LXNS+'user/maimai/player'), ('GET', LXNS+'user/maimai/player/scores'),
            ('POST', LXNS+'user/maimai/player/scores')}
        or method in ('GET', 'POST') and re.fullmatch(re.escape(LXNS)+r'maimai/player/[1-9][0-9]{0,19}/scores', url) is not None)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def https_transport(method, url, headers, body):
    """Exact HTTPS allowlist, TLS validation, no redirects/retries/env auth/proxies."""
    if not _allowed(method, url): raise UploadError('destination_rejected')
    raw = None if body is None else json.dumps(body, allow_nan=False).encode()
    if raw is not None and len(raw) > MAX_BYTES: raise UploadError('request_too_large')
    headers = dict(headers)
    if raw is not None: headers['Content-Type'] = 'application/json'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    req = urllib.request.Request(url, data=raw, headers=headers, method=method)
    try:
        with opener.open(req, timeout=20) as response:
            chunks, size, started = [], 0, time.monotonic()
            while True:
                chunk = response.read(65536)
                if not chunk: break
                size += len(chunk)
                if size > MAX_BYTES or time.monotonic() - started > 30: raise UploadError('response_limit')
                chunks.append(chunk)
            return response.status, dict(response.headers), b''.join(chunks)
    except urllib.error.HTTPError as exc:
        status, headers = exc.code, dict(exc.headers)
        exc.close()
        return status, headers, b''


def _call(transport, method, url, headers, body=None):
    if not _allowed(method, url): raise UploadError('destination_rejected')
    try:
        status, response_headers, raw = transport(method, url, headers, body)
    except Exception:
        raise UploadError('transport_failed_outcome_unknown' if method == 'POST' else 'read_failed') from None
    if status == 429: raise UploadError('rate_limited_no_retry')
    if status in (401, 403): raise UploadError('credential_or_permission_denied')
    if type(status) is not int or not 200 <= status < 300:
        raise UploadError('http_failure_unconfirmed')
    try:
        if not isinstance(raw, (bytes, str)) or len(raw) > MAX_BYTES: raise ValueError()
        data = json.loads(raw)
    except (ValueError, TypeError): raise UploadError('invalid_response') from None
    if url.startswith(LXNS):
        if not isinstance(data, dict) or data.get('success') is not True or type(data.get('code')) is not int or data['code'] != 200:
            raise UploadError('lxns_rejected')
        return data.get('data')
    return data


def _key(row, site):
    return (row.get('title'), row.get('type'), row.get('level_index')) if site == 'fish' else (row.get('id'), row.get('type'), row.get('level_index'))


def _merge(rows, existing, site):
    """Preserve site's previous best values, including unknown native badges."""
    if not isinstance(existing, list) or len(existing) > MAX_ROWS:
        raise UploadError('invalid_existing_scores')
    index = {}
    for old in existing:
        if not isinstance(old, dict): raise UploadError('invalid_existing_scores')
        key = _key(old, site)
        if key in index: raise UploadError('duplicate_existing_scores')
        index[key] = old
    result = []
    dxkey = 'dxScore' if site == 'fish' else 'dx_score'
    for row in rows:
        out = dict(row)
        old = index.get(_key(row, site), {})
        for flag, values in [('fc', FC), ('fs', FS)]:
            before, after = old.get(flag) or '', row.get(flag) or ''
            if before not in values or after not in values:
                raise UploadError('unknown_existing_badge')
            best = max((before, after), key=values.index)
            if best: out[flag] = best
            else: out.pop(flag, None)  # absent is safe only after same-token read
        for value in ('achievements', dxkey):
            if value in old:
                v = old[value]
                valid = type(v) in (int, float) and math.isfinite(v) and 0 <= v <= (101 if value == 'achievements' else 1000000)
                if not valid or value == dxkey and type(v) is not int:
                    raise UploadError('invalid_existing_score')
                out[value] = max(out[value], v)
        result.append(out)
    return result


def upload_scores(rows, fish_catalog, lxns_catalog, credentials, *, complete=False,
                  transport=None):
    """Shared worker entry point. Explicit complete=True only after QR fetch closure.
    At most 2 fish + 3 LXNS requests, no retry. Returns independent site results.
    Credentials MUST belong to the QQ job owner; this module cannot prove ownership.
    """
    result = {s: {'state': 'not_configured', 'submitted': 0} for s in ('fish', 'lxns')}
    if complete is not True:
        return {s: {'state': 'blocked', 'reason': 'incomplete_fetch', 'submitted': 0} for s in result}
    try:
        prepared = prepare_scores(rows, fish_catalog, lxns_catalog)
    except (UploadError, KeyError, TypeError, ValueError):
        return {s: {'state': 'blocked', 'reason': 'invalid_scores_or_catalog', 'submitted': 0} for s in result}
    transport = transport or https_transport
    if not isinstance(credentials, Credentials):
        return {s: {'state': 'blocked', 'reason': 'invalid_credentials', 'submitted': 0} for s in result}
    for site in ('fish', 'lxns'):
        if site == 'fish' and credentials.fish_import_token is None: continue
        if site == 'lxns' and credentials.lxns_personal_token is None and credentials.lxns_developer_token is None: continue
        try:
            if site == 'fish':
                headers = {'Import-Token': _token(credentials.fish_import_token)}
                old = _call(transport, 'GET', FISH+'records', headers)
                if not isinstance(old, dict) or 'records' not in old: raise UploadError('invalid_existing_scores')
                body = _merge(prepared.fish, old['records'], site)
                answer = _call(transport, 'POST', FISH+'update_records', headers, body)
                if not isinstance(answer, dict) or any(type(answer.get(k)) is not int or answer[k] < 0 for k in ('updates', 'creates')) or answer['updates'] + answer['creates'] != len(body):
                    raise UploadError('write_count_mismatch_unconfirmed')
            else:
                if credentials.lxns_personal_token is not None and credentials.lxns_developer_token is not None:
                    raise UploadError('ambiguous_lxns_auth')
                if credentials.lxns_personal_token is not None:
                    headers = {'X-User-Token': _token(credentials.lxns_personal_token)}
                    player = _call(transport, 'GET', LXNS+'user/maimai/player', headers)
                    if not isinstance(player, dict):
                        raise UploadError('invalid_player_identity')
                    friend = _integer(player.get('friend_code'), 1, 10**20-1, 'invalid_player_identity')
                    requested = credentials.lxns_friend_code
                    if requested is not None and _integer(requested, 1, 10**20-1, 'invalid_requested_friend_code') != friend:
                        raise UploadError('friend_code_mismatch')
                    url = LXNS+'user/maimai/player/scores'
                else:
                    friend = _integer(credentials.lxns_friend_code, 1, 10**20-1, 'confirmed_friend_code_required')
                    headers = {'Authorization': _token(credentials.lxns_developer_token)}
                    url = LXNS+f'maimai/player/{friend}/scores'
                old = _call(transport, 'GET', url, headers)
                body = _merge(prepared.lxns, old, site)
                _call(transport, 'POST', url, headers, {'scores': body})
            result[site] = {'state': 'accepted_unverified', 'submitted': len(body),
                            'requires_readback': True, 'omitted_badges': prepared.omitted_badges}
        except UploadError as exc:
            result[site] = {'state': 'failed', 'reason': str(exc), 'submitted': 0}
    return result

"""Private QR capture orchestration. No network on import; no secret logging.

Upload integration contract: CaptureResult(requester, path, scores, summary).
Scores retain the live client's game field names, not site-specific identities.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import contextlib
import fcntl
import math
import functools
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import threading
import time
import uuid

CLIENT = Path(os.getenv('MAIMAI_CLIENT', str(Path(__file__).resolve().parent.parent / 'maimai-live-client.py')))
PRIVATE = Path(os.getenv('MAIMAI_PRIVATE_DIR', str(Path(__file__).resolve().parent.parent / 'private')))
CAPTURE_GATE = threading.BoundedSemaphore(1)
FIELDS = ('musicId', 'level', 'playCount', 'achievement', 'deluxscoreMax',
          'comboStatus', 'syncStatus', 'scoreRank', 'extNum1')
CONSENT = ('取分会将二维码发送到游戏的原始 HTTP 接口（未加密，可能被网络中间人读取），'
           '并登录游戏读取成绩；所有取分均保留会话，登录成功 60 秒后可 /logout，14 分钟后自动尝试登出。仅发送你有权使用的二维码。'
           '如接受，请私聊发送 /同意HTTP取分，然后重新发送 /login <SGWCMAID二维码文本>。'
           '可用 /撤销HTTP取分 撤回后续取分授权；取分结果会私密保存在服务器供后续上传使用。')
ONE_USE = ('二维码为一次性码，请在公众号显示的有效期内使用；本机器人已尝试的码（包括失败或超时）不会再次提交，'
           '本地尝试记录不代表官方已消费。取分登录后，上机前请从公众号重新获取新码。')
HELP = '私聊用法：/login 或 /取分 <SGWCMAID二维码文本>，或直接发送二维码文本：取分并保留会话；登录成功 60 秒后可 /logout，14 分钟后自动尝试正常登出，失败保留记录并通知，可 /logout 重试。/清理会话 归档可确认失效的记录。仅支持文本。/上传物品 类型 id；/上传成绩 名称/id 等级 成绩（1010000=101.0000%）。' + ONE_USE


class CaptureError(RuntimeError):
    """Fixed diagnostic only: never attach subprocess output or QR text."""


@dataclass(repr=False)
class CaptureResult:
    requester: str
    path: Path
    scores: list[dict] = field(repr=False)
    summary: dict = field(repr=False)
    upload_context: object = field(default=None, repr=False)


def sensitive_text(text: str) -> bool:
    # Broad guard, including malformed QR and secret-bearing commands.
    return 'SGWCMAID' in text.upper() or text.lower().startswith(('/login', '/logout', '/清理会话')) or text.startswith(('/取分', '/同意HTTP取分', '/撤销HTTP取分', '/绑定', '/解绑', '/上传', '/查券', '/查劵', '/查卷', '/购券', '/购劵', '/购卷'))


def event_text(event: dict) -> str:
    segments = event.get('message')
    if isinstance(segments, list):
        parts = []
        for segment in segments:
            if not isinstance(segment, dict) or segment.get('type') != 'text':
                return ''
            data = segment.get('data')
            if not isinstance(data, dict) or not isinstance(data.get('text'), str):
                return ''
            parts.append(data['text'])
        text = ''.join(parts).strip()
    else:
        text = str(event.get('raw_message') or '').strip()
    # Normalize only the command prefix, never credentials or arbitrary text.
    return re.sub(r'^/(购|查)[劵卷](?=\s|$)', r'/\1券', text)


def group_sensitive(event: dict) -> bool:
    if sensitive_text(str(event.get('raw_message') or '').strip()):
        return True
    segments = event.get('message')
    return isinstance(segments, list) and any(
        isinstance(s, dict) and isinstance(s.get('data'), dict)
        and sensitive_text(str(s['data'].get('text', '')).strip()) for s in segments)


def private_directory(path: Path) -> None:
    s = path.lstat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o700:
        raise CaptureError('private_directory_permissions')


def read_private(path: Path, limit: int = 16 * 1024 * 1024):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as f:
        s = os.fstat(f.fileno())
        if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o600:
            raise CaptureError('private_file_permissions')
        raw = f.read(limit + 1)
    if len(raw) > limit:
        raise CaptureError('private_file_size')
    return json.loads(raw)


_session_local = threading.local()

@contextlib.contextmanager
def session_lock(private=PRIVATE):
    private_directory(private)
    fd = os.open(private / '.session.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_nlink != 1 or stat.S_IMODE(s.st_mode) != 0o600:
            raise CaptureError('session_lock_permissions')
        fcntl.flock(fd, fcntl.LOCK_EX)
        _session_local.fd = fd
        yield
    finally:
        _session_local.fd = None
        os.close(fd)

def locked_subprocess_args():
    fd = getattr(_session_local, 'fd', None)
    if fd is None:
        return {}
    return {'pass_fds': (fd,), 'env': dict(os.environ, MAIMAI_SESSION_LOCK_FD=str(fd))}

def serialized_worker(fn):
    @functools.wraps(fn)
    def wrapped(self, *args, **kwargs):
        with session_lock(self.private):
            return fn(self, *args, **kwargs)
    return wrapped

def register_qr_attempt(qr: str, private: Path) -> bool:
    """Durable global tombstone, called under session_lock before any runner.

    An empty 0600 file named only by SHA256 never stores QR or QQ. Keep forever:
    local time/expiry cannot prove a previously attempted code safe to replay.
    O_EXCL also fails closed for existing files/symlinks or a crash mid-commit.
    """
    if getattr(_session_local, 'fd', None) is None or not valid_qr(qr):
        raise CaptureError('qr_attempt_lock_required')
    private_directory(private)
    directory = private / 'qr-attempts'
    directory.mkdir(mode=0o700, exist_ok=True)
    private_directory(directory)
    digest = hashlib.sha256(qr.encode('ascii')).hexdigest()
    try:
        fd = os.open(directory / digest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return False
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    # Persist both the marker and the newly created directory before networking.
    for path in (directory, private):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return True


def logout_remaining(record, now=None):
    stamp = record.get('login_success_epoch')
    if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp <= 0:
        return 0
    return max(0, math.ceil(stamp + 60 - (time.time() if now is None else now)))

def capture(requester: str, qr: str, *, private: Path = PRIVATE,
            client: Path = CLIENT, timeout: int = 210, retain: bool = False) -> CaptureResult:
    """Call the live adapter with QR on stdin only. Results remain private."""
    if not re.fullmatch(r'[1-9][0-9]{4,11}', requester) or not valid_qr(qr):
        raise CaptureError('invalid_input')
    try:
        private_directory(private)
        if session_exists(requester, private):
            raise CaptureError('session_already_owned')
        path = private / ('capture-' + uuid.uuid4().hex + '.json')
        lifecycle = ['--session-owner', requester]  # Every QR entry retains its session.
        completed = subprocess.run(
            [sys.executable, str(client), '--live', '--qr-stdin', '--allow-http-qr',
             '--preload-profile', *lifecycle, '--result', str(path)],
            input=qr.encode('ascii'), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout, check=False, shell=False, **locked_subprocess_args())
        if completed.returncode not in (0, 3):
            raise CaptureError('capture_failed_logout_unknown')
        data = read_private(path)
        summary = data.get('summary') if isinstance(data, dict) else None
        scores = data.get('scores', []) if isinstance(data, dict) else None
        if (not isinstance(summary, dict) or summary.get('status') not in ('complete', 'partial', 'failed')
                or not isinstance(scores, list) or len(scores) > 100000
                or not all(isinstance(r, dict) and all(k in FIELDS and type(v) is int for k, v in r.items()) for r in scores)
                or type(summary.get('rows')) is not int or summary['rows'] != len(scores)):
            raise CaptureError('invalid_result_logout_unknown')
        # Stable whitelist export; never propagate transport diagnostics to consumers.
        clean = {'status': summary['status'], 'rows': len(scores),
                 'logout': 'confirmed' if summary.get('logout') == 'confirmed' else 'unconfirmed'}
        if summary.get('login') == 'not_started' and summary['status'] == 'failed':
            clean['login'] = 'not_started'
        if (retain or clean['logout'] != 'confirmed') and clean.get('login') != 'not_started':
            record = read_private(session_file(requester, private), 65536)
            clean['session'] = 'active' if record.get('owner') == requester and record.get('state') == 'active' and summary.get('session') == 'active' else 'unknown'
        return CaptureResult(requester, path, scores, clean)
    except subprocess.TimeoutExpired:
        raise CaptureError('timeout_logout_unknown') from None
    except CaptureError:
        raise
    except Exception:
        raise CaptureError('capture_failed_logout_unknown') from None


def valid_qr(text: str) -> bool:
    return bool(re.fullmatch(r'SGWCMAID[!-~]{12}[0-9A-F]{64}', text))

def session_file(user, private=PRIVATE):
    if not re.fullmatch(r'[1-9][0-9]{4,11}', user):
        raise CaptureError('invalid_owner')
    return private / ('session-' + user + '.json')

def session_exists(user, private=PRIVATE):
    private_directory(private)
    try:
        session_file(user, private).lstat()
    except FileNotFoundError:
        return False
    return True  # Includes corrupt files/symlinks: fail closed.


def session_record(user, private=PRIVATE):
    """Return the parsed record, or None when this QQ has no record at all."""
    if not session_exists(user, private):
        return None
    try:
        record = read_private(session_file(user, private), 65536)
    except CaptureError:
        raise
    except Exception:
        raise CaptureError('session_record_unreadable') from None
    if not isinstance(record, dict):
        raise CaptureError('session_record_unreadable')
    return record


ARCHIVE = Path(os.getenv('MAIMAI_ORPHAN_ARCHIVE', str(PRIVATE / 'archive')))


def _private_backup_dir(path=ARCHIVE):
    """Same ownership/mode rule as the live private dir; never create loose parents."""
    parent = path.parent
    s = parent.lstat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o700:
        raise CaptureError('orphan_archive_unavailable')
    path.mkdir(mode=0o700, exist_ok=True)
    s = path.lstat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o700:
        raise CaptureError('orphan_archive_unavailable')
    return path


def _archive_record(user, *, private, archive, prefix, stamp):
    """Rename a record into the archive. Never deletes; never touches the game."""
    destination = _private_backup_dir(ARCHIVE if archive is None else archive) / (prefix + user + '-' + stamp + '.json')
    source = session_file(user, private)
    if destination.exists():
        raise CaptureError('orphan_archive_collision')
    os.rename(source, destination)
    os.chmod(destination, 0o600)
    return str(destination)


def _stamp(now=None):
    return time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now if now is not None else time.time()))


def reap_orphan(user, *, private=PRIVATE, archive=None, now=None):
    """Archive a record that carries no game session. Never fakes a logout.

    Only `unknown` records are eligible: they hold no login fields, so no
    UserLogoutApi can be sent and no `closed` state is reachable for them. The
    record is renamed into the archive instead of deleted, so the trail survives.
    Nothing is written to the game and no network call is made.
    """
    private_directory(private)
    record = session_record(user, private)
    if record is None:
        return {'reaped': False, 'reason': 'no_record'}
    if record.get('owner') != user or record.get('version') != 1 or record.get('state') not in ('unknown', 'active', 'closed'):
        raise CaptureError('session_record_unreadable')
    if record.get('state') != 'unknown':
        return {'reaped': False, 'reason': 'state_' + record['state']}
    destination = _archive_record(user, private=private, archive=archive,
                                  prefix='orphan-session-', stamp=_stamp(now))
    return {'reaped': True, 'reason': 'unknown_without_session', 'archive': destination}


def probe(user, *, private=PRIVATE, client=CLIENT, timeout=75):
    """Read-only liveness probe for a retained session: 'alive'/'dead'/'indeterminate'.

    A single GetUserMusicApi page, nothing written back. 'dead' means the game
    answered with a well-formed empty envelope, i.e. it no longer knows this
    session; network/TLS/HTTP trouble is 'indeterminate' and proves nothing.
    """
    private_directory(private)
    record = session_record(user, private)
    if record is None or record.get('owner') != user or record.get('version') != 1 or record.get('state') != 'active':
        raise CaptureError('probe_unavailable')
    completed = subprocess.run([sys.executable, str(client), '--probe-session', '--session-owner', user],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=timeout, check=False, shell=False, **locked_subprocess_args())
    return {0: 'alive', 4: 'dead'}.get(completed.returncode, 'indeterminate')


def inventory(user, *, private=PRIVATE, client=CLIENT, timeout=75):
    """Read-only ticket inventory for a retained session: one GetUserChargeApi.

    Returns observed rows (chargeId/stock/purchaseDate/validDate). It never
    logs in, logs out, writes stock or refreshes the session record, so the
    60/840-second logout policy is unaffected. Observed stock is not an
    entitlement, payment or order proof.
    """
    private_directory(private)
    record = session_record(user, private)
    if record is None or record.get('owner') != user or record.get('version') != 1 or record.get('state') != 'active':
        raise CaptureError('inventory_unavailable')
    completed = subprocess.run([sys.executable, str(client), '--ticket-inventory', '--session-owner', user],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=timeout, check=False, shell=False, **locked_subprocess_args())
    if completed.returncode != 0:
        raise CaptureError('inventory_unavailable')
    try:
        payload = json.loads(completed.stdout.decode('utf-8'))
    except (UnicodeError, ValueError):
        raise CaptureError('inventory_unavailable') from None
    tickets = payload.get('tickets') if isinstance(payload, dict) and payload.get('status') == 'ok' else None
    if not isinstance(tickets, list) or len(tickets) > 4096:
        raise CaptureError('inventory_unavailable')
    rows, seen = [], set()
    nonneg = lambda v: type(v) is int and 0 <= v <= 2**64 - 1
    for row in tickets:
        if (not isinstance(row, dict) or not nonneg(row.get('chargeId')) or not row['chargeId']
                or not nonneg(row.get('stock')) or row['chargeId'] in seen):
            raise CaptureError('inventory_unavailable')
        for key in ('purchaseDate', 'validDate'):
            value = row.get(key)
            if not isinstance(value, str) or len(value) > 64 or any(ord(c) < 32 for c in value):
                raise CaptureError('inventory_unavailable')
        seen.add(row['chargeId'])
        rows.append({key: row[key] for key in ('chargeId', 'stock', 'purchaseDate', 'validDate')})
    return rows


# Fixed display labels for known paid tickets. Unknown IDs stay as raw IDs;
# the multiplier is never guessed from an unrecognised chargeId.
TICKET_LABELS = {2: '2 倍券', 3: '3 倍券', 4: '4 倍券', 5: '5 倍券', 6: '6 倍券'}
INVENTORY_HEADER = '券库存（游戏侧观测值，不代表已付款、到账或可用权益）：'


def format_inventory(rows):
    """Fixed-format summary. Never signals payment or entitlement."""
    if not rows:
        return INVENTORY_HEADER + '\n未观测到任何券记录（列表为空）。\n如刚购买，请稍后在机台或重新 /查券 确认。'
    lines = [INVENTORY_HEADER]
    for row in sorted(rows, key=lambda r: (r['stock'] <= 0, r['chargeId'])):
        label = TICKET_LABELS.get(row['chargeId'], 'chargeId %d' % row['chargeId'])
        line = '· %s：%d 张' % (label, row['stock'])
        if row['purchaseDate']:
            line += '，购买 %s' % row['purchaseDate']
        if row['validDate']:
            line += '，有效期至 %s' % row['validDate']
        lines.append(line)
    lines.append('共 %d 条记录，stock 合计 %d。' % (len(rows), sum(r['stock'] for r in rows)))
    lines.append('日期为游戏返回的原样文本；未推断时区或可用性。')
    return '\n'.join(lines)


def reap_expired(user, *, private=PRIVATE, archive=None, now=None):
    """Archive an `active` record only after the caller proved it is dead.

    The caller must have run `probe` and observed 'dead'. No logout is sent and
    no `closed` state is written: the game already dropped the session, so the
    local record is the stale part. Renamed, never deleted.
    """
    private_directory(private)
    record = session_record(user, private)
    if record is None:
        return {'reaped': False, 'reason': 'no_record'}
    if record.get('owner') != user or record.get('version') != 1 or record.get('state') not in ('unknown', 'active', 'closed'):
        raise CaptureError('session_record_unreadable')
    if record.get('state') != 'active':
        return {'reaped': False, 'reason': 'state_' + record['state']}
    destination = _archive_record(user, private=private, archive=archive,
                                  prefix='expired-session-', stamp=_stamp(now))
    return {'reaped': True, 'reason': 'session_expired', 'archive': destination}


def logout(user, *, private=PRIVATE, client=CLIENT, timeout=75):
    private_directory(private)
    record = read_private(session_file(user, private), 65536)
    if record.get('owner') != user or record.get('version') != 1 or record.get('state') not in ('active', 'closed'):
        raise CaptureError('logout_unconfirmed')
    remaining = logout_remaining(record)
    if remaining and record.get('state') != 'closed':
        raise CaptureError('logout_wait_' + str(remaining))
    completed = subprocess.run([sys.executable, str(client), '--logout-session', '--session-owner', user],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=timeout, check=False, shell=False, **locked_subprocess_args())
    if completed.returncode != 0 or session_exists(user, private):
        raise CaptureError('logout_unconfirmed')
    return True


def prepare_ticket_plan(user, charge_id, private=PRIVATE):
    private_directory(private)
    session_file(user, private)  # validate owner before naming files
    if type(charge_id) is not int or charge_id not in (2, 3):
        raise CaptureError('invalid_ticket_target')
    plan = private / ('settlement-plan-' + user + '-ticket-hold-' + str(charge_id) + '.json')
    try:
        return read_private(plan, 65536)
    except FileNotFoundError:
        pass
    profile = read_private(private / 'ticket-machine-profile.json', 65536)
    if type(profile) is not dict or type(profile.get('machine')) is not dict or type(profile.get('extend_flags')) is not dict:
        raise CaptureError('ticket_profile_invalid')
    data = dict(owner=user, machine=profile['machine'], extend_flags=profile['extend_flags'],
        auth_date_time='session', kind=1, item_ids=[], ticket=dict(charge_id=charge_id, action='hold'))
    fd = os.open(plan, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    return data


def ticket_purchase(user, charge_id, *, private=PRIVATE, client=CLIENT, timeout=210):
    if type(charge_id) is not int or charge_id not in (2, 3):
        raise CaptureError('invalid_ticket_target')
    plan = private / ('settlement-plan-' + user + '-ticket-hold-' + str(charge_id) + '.json')
    # Read and validate permissions before any subprocess/possible write.
    data = read_private(plan, 65536)
    if type(data) is not dict or data.get('owner') != user:
        raise CaptureError('ticket_plan_binding')
    if data.get('ticket') != {'charge_id': charge_id, 'action': 'hold'}:
        raise CaptureError('ticket_plan_target')
    completed = subprocess.run([sys.executable, str(client), '--ticket-purchase',
        '--session-owner', user,
        '--settlement-plan', str(plan)], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout,
        check=False, shell=False, **locked_subprocess_args())
    if len(completed.stdout) > 65536:
        raise CaptureError('ticket_response_size')
    try:
        result = json.loads(completed.stdout)
    except Exception:
        raise CaptureError('ticket_response_invalid') from None
    if type(result) is not dict or result.get('status') not in (
            'verified', 'rejected', 'ambiguous', 'acknowledged_unverified',
            'purchase_verified_settlement_not_sent'):
        raise CaptureError('ticket_response_invalid')
    return result


def format_ticket_result(result, charge_id):
    # Only fixed labels; never expose arbitrary response/error strings.
    status = result.get('status')
    if status == 'verified':
        if result.get('outcome') == 'purchased_and_consumed_in_play':
            return f'{charge_id} 倍券购买及本次测试局使用已获服务器应答并通过读回核对；该券已在测试结算中消耗，不会留作下次使用。'
        return f'{charge_id} 倍券购买已获服务器应答并通过库存读回核对；未执行用券游玩。'
    if status == 'purchase_verified_settlement_not_sent':
        return '购买已通过库存读回核对，但测试结算未发送；请先 /查券 或在机台核对，不要重复购买。'
    if status == 'rejected':
        if result.get('reason') == 'already_claimed':
            return '当前会话已有一次写入尝试，本次购券被防重复保护拦截，没有再次发送购买请求。上次结果可能未确认，请先 /查券 或在机台核对；不要重新登录反复购买。'
        return '购券前校验未通过，本次没有发送购买请求；请核对会话、券库存与机台配置。'
    if status == 'acknowledged_unverified':
        return '服务器已应答，但购券或结算读回尚未全部确认；请先 /查券 或在机台核对，不要重复购买。'
    return '购券结果不确定，可能已经写入；请先 /查券 或在机台核对，不要重复购买。'


def game_score_upload(user, *, private=PRIVATE, client=CLIENT, timeout=210):
    session_file(user, private)
    document = private / ('game-score-' + user + '.json')
    read_private(document, 8*1024*1024)
    result = subprocess.run([sys.executable, str(client), '--upload-game-scores',
        '--session-owner', user, '--score-document', str(document)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=timeout, check=False, shell=False, **locked_subprocess_args())
    if len(result.stdout) > 65536:
        raise CaptureError('game_score_response_size')
    data = json.loads(result.stdout)
    if type(data) is not dict or data.get('status') not in (
            'verified', 'rejected', 'ambiguous', 'acknowledged_unverified'):
        raise CaptureError('game_score_response_invalid')
    return data

def typed_upload(user, operation, *, private=PRIVATE, client=CLIENT, timeout=210):
    session_file(user, private)
    result = subprocess.run([sys.executable, str(client), '--upload-typed',
        '--session-owner', user, '--upload-operation', json.dumps(operation, ensure_ascii=False)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=timeout, check=False, shell=False, **locked_subprocess_args())
    if len(result.stdout) > 65536:
        raise CaptureError('upload_response_size')
    payload = json.loads(result.stdout)
    if type(payload) is not dict or payload.get('status') not in (
            'verified', 'unchanged', 'rejected', 'ambiguous', 'acknowledged_unverified'):
        raise CaptureError('upload_response_invalid')
    return payload


class QRSync:
    def __init__(self, private: Path = PRIVATE, *, runner=capture, cooldown=60, on_result=None,
                 before_capture=None, logout_runner=logout, archive=None, probe_runner=probe,
                 inventory_runner=inventory, ticket_runner=None):
        self.private = private
        self.runner = runner
        self.logout_runner = logout_runner
        self.probe_runner = probe_runner
        self.inventory_runner = inventory_runner
        self.ticket_runner = ticket_purchase if ticket_runner is None else ticket_runner
        self.archive = archive
        self.cooldown = cooldown
        self.on_result = on_result
        self.before_capture = before_capture
        self.gate = CAPTURE_GATE
        self.state_lock = threading.Lock()
        self.next_allowed = {}

    def _consent(self, user: str, value=None) -> bool:
        private_directory(self.private)
        path = self.private / 'qr-consents.json'
        with self.state_lock:
            try:
                data = read_private(path, 1024 * 1024)
            except FileNotFoundError:
                data = {}
            if not isinstance(data, dict) or any(not re.fullmatch(r'[1-9][0-9]{4,11}', k) or v is not True for k, v in data.items()):
                raise CaptureError('invalid_consent_store')
            if value is not None:
                if value:
                    data[user] = True
                else:
                    data.pop(user, None)
                temporary = self.private / ('consent-' + uuid.uuid4().hex + '.tmp')
                try:
                    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, 'w') as f:
                        json.dump(data, f)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
            return data.get(user) is True

    def handle(self, event: dict, send) -> bool:
        if event.get('post_type') != 'message' or event.get('message_type') != 'private':
            return False
        text = event_text(event)
        if not sensitive_text(text):
            return False
        user = str(event.get('user_id') or '')
        if not re.fullmatch(r'[1-9][0-9]{4,11}', user):
            return True
        try:
            if text == '/logout':
                if not self.gate.acquire(blocking=False):
                    send(int(user), '取分或登出正在进行，请稍后重试。')
                    return True
                try:
                    worker = threading.Thread(target=self._logout_work, args=(user, send), daemon=True, name='qr-logout')
                    worker.start()
                except Exception:
                    self.gate.release()
                    raise
                return True
            if text == '/清理会话':
                # Only ever archives a record the bot can prove holds no game session.
                if not self.gate.acquire(blocking=False):
                    send(int(user), '取分或登出正在进行，请稍后重试。')
                    return True
                try:
                    worker = threading.Thread(target=self._cleanup_work, args=(user, send), daemon=True, name='qr-cleanup')
                    worker.start()
                except Exception:
                    self.gate.release()
                    raise
                return True
            if text.startswith(('/上传物品', '/上传成绩')):
                from upload_commands import load_catalog, parse_command
                try:
                    catalog = load_catalog() if text.startswith('/上传成绩') else []
                    operation = parse_command(text, catalog)
                except ValueError as exc:
                    send(int(user), str(exc))
                    return True
                except Exception:
                    send(int(user), '曲目资料暂不可用，本次未上传。')
                    return True
                if not self.gate.acquire(blocking=False):
                    send(int(user), '当前有游戏会话操作正在进行，请稍后再试。')
                    return True
                try:
                    threading.Thread(target=self._typed_work, args=(user, operation, send),
                                     daemon=True, name='qr-typed-upload').start()
                except Exception:
                    self.gate.release()
                    raise
                return True
            if text == '/上传游戏成绩':
                if not self.gate.acquire(blocking=False):
                    send(int(user), '当前有游戏会话操作正在进行，请稍后再试。')
                    return True
                try:
                    threading.Thread(target=self._game_score_work, args=(user, send),
                                     daemon=True, name='qr-game-score').start()
                except Exception:
                    self.gate.release()
                    raise
                return True
            if text.startswith('/购券'):
                match = re.fullmatch(r'/购券\s+([23])', text)
                if not match:
                    send(int(user), '私聊用法：/购券 2 或 /购券 3。此操作只购买并保留券，不打歌、不消耗券；到账以库存读回为准。')
                    return True
                if not self.gate.acquire(blocking=False):
                    send(int(user), '当前有游戏会话操作正在进行，请稍后再试。')
                    return True
                try:
                    worker = threading.Thread(target=self._ticket_work, args=(user, int(match[1]), send), daemon=True, name='qr-ticket')
                    worker.start()
                except Exception:
                    self.gate.release()
                    raise
                return True
            if text == '/查券':
                # Read-only inventory read against the retained session.
                if not self.gate.acquire(blocking=False):
                    send(int(user), '取分或登出正在进行，请稍后重试。')
                    return True
                try:
                    worker = threading.Thread(target=self._inventory_work, args=(user, send), daemon=True, name='qr-inventory')
                    worker.start()
                except Exception:
                    self.gate.release()
                    raise
                return True
            if text in ('/同意HTTP取分', '/撤销HTTP取分'):
                accepted = text == '/同意HTTP取分'
                self._consent(user, accepted)
                send(int(user), '已保存 HTTP 取分授权，请重新发送二维码。' if accepted else '已撤销后续 HTTP 取分授权；已开始的任务仍会尝试完成登出。')
                return True
            if text == '/取分':
                send(int(user), HELP + '\n' + CONSENT)
                return True
            retain = True
            login_command = text.startswith(('/login ', '/绑定账号 '))
            qr = text.split(' ', 1)[1].strip() if login_command else text[3:].strip() if text.startswith('/取分 ') else text
            if not valid_qr(qr):
                send(int(user), HELP)
                return True
            if not self._consent(user):
                send(int(user), CONSENT)
                return True
            if not self.gate.acquire(blocking=False):
                send(int(user), '取分正在进行，请稍后重试；二维码未加入队列。')
                return True
            acquired = True
            try:
                if session_exists(user, self.private):
                    send(int(user), '已有保留或状态未确认的会话，请先 /logout；新二维码未使用。')
                    return True
                now = time.monotonic()
                with self.state_lock:
                    self.next_allowed = {u: t for u, t in self.next_allowed.items() if t > now}
                    limited = user in self.next_allowed
                    if not limited:
                        self.next_allowed[user] = now + self.cooldown
                if limited:
                    send(int(user), '取分请求过于频繁，请等待 60 秒后重试。')
                    return True
                context = self.before_capture(user) if self.before_capture is not None else None
                send(int(user), '正在验证二维码并尝试登录；有效性以服务端结果为准。')
                worker = threading.Thread(target=self._work, args=(user, qr, send, context, retain), daemon=True, name='qr-capture')
                worker.start()
                acquired = False  # worker owns the gate now
            finally:
                if acquired:
                    self.gate.release()
        except Exception:
            # Never include QR, local paths or an exception message in feedback.
            try:
                send(int(user), '取分服务暂不可用，请稍后重试。')
            except Exception:
                pass
        return True

    @serialized_worker
    def _logout_work(self, user, send):
        try:
            if not session_exists(user, self.private):
                send(int(user), '没有本机器人保留的会话；未执行游戏登出。')
                return
            remaining = logout_remaining(session_record(user, self.private) or {})
            if remaining:
                send(int(user), f'登录成功后 60 秒内不能登出；还需等待 {remaining} 秒。')
                return
            if self.logout_runner(user, private=self.private) is not True:
                raise CaptureError('logout_unconfirmed')
            send(int(user), '登出接口已接受；游戏侧会话释放尚未核验，如仍提示占用，请等待游戏侧解除后再登录。')
        except Exception:
            send(int(user), '登出未确认，会话记录保留；请检查游戏状态后重试 /logout。')
        finally:
            self.gate.release()

    @serialized_worker
    def _typed_work(self, user, operation, send):
        try:
            record = session_record(user, self.private)
            if record is None or record.get('state') != 'active':
                send(int(user), '没有可用的保留会话；请先 /login，本次未上传。')
                return
            send(int(user), '正在收集账号数据并核对上传条件，最长约 3 分半钟；请勿重复提交。')
            result = typed_upload(user, operation, private=self.private)
            status = result['status']
            label = '物品' if operation['operation'] == 'item' else '成绩'
            if status == 'verified':
                text = label + '已提交，并通过目标数据和券库存读回核对；未使用券。'
            elif status == 'unchanged':
                text = '目标已拥有或成绩未超过历史最佳，本次没有发送写入。'
            elif status == 'rejected':
                reason = result.get('reason')
                messages = {'snapshot_preflight_failed': '账号数据收集或保存格式校验失败，本次未发送写入。',
                    'score_not_improved': '成绩未超过历史最佳，本次没有发送写入。',
                    'already_claimed': '当前会话已执行过一次写入尝试；不会重复提交，请先核对账号。'}
                text = messages.get(reason, label + '上传前校验未通过，本次未发送结算。')
            else:
                text = label + '写入结果未全部确认，请先核对账号；不要重复上传。'
            send(int(user), text)
        except Exception:
            send(int(user), '上传结果未确认，请先核对账号；不要重复提交。')
        finally:
            self.gate.release()

    @serialized_worker
    def _game_score_work(self, user, send):
        try:
            record = session_record(user, self.private)
            if record is None or record.get('state') != 'active':
                send(int(user), '没有可用的保留会话；本次未上传游戏成绩。')
                return
            try:
                read_private(self.private / ('game-score-' + user + '.json'), 8*1024*1024)
            except FileNotFoundError:
                send(int(user), '尚未准备当前会话的原生游玩结算数据；本次未上传。仅有查分器成绩或历史最佳不足以生成完整结算。')
                return
            result = game_score_upload(user, private=self.private)
            status = result['status']
            if status == 'verified':
                text = '游戏成绩已提交并通过成绩与券库存读回核对；未使用券。'
            elif status == 'rejected':
                text = '游戏成绩上传前校验未通过，本次未发送结算；需核对原生结算数据与当前会话。'
            else:
                text = '游戏成绩写入结果未全部确认；请先核对成绩，不要重复上传。'
            send(int(user), text)
        except Exception:
            send(int(user), '游戏成绩上传结果未确认；请先核对成绩，不要重复上传。')
        finally:
            self.gate.release()

    @serialized_worker
    def _ticket_work(self, user, charge_id, send):
        try:
            record = session_record(user, self.private)
            if record is None or record.get('state') != 'active':
                send(int(user), '没有可用的保留会话；本次未购券。')
                return
            try:
                prepare_ticket_plan(user, charge_id, self.private)
            except FileNotFoundError:
                send(int(user), '机台测试配置尚未准备好，本次未发送购券或结算请求。')
                return
            result = self.ticket_runner(user, charge_id, private=self.private)
            # Fixed, non-account diagnostic fields only; no raw stdout/stderr.
            diag = result.get('diagnostics', {}).get('UpsertUserChargelogApi', {})
            outcome = diag.get('outcome')
            if outcome in ('accepted', 'response_rejected', 'exception'):
                event = {'event': 'ticket_purchase_diagnostic', 'outcome': outcome}
                code = diag.get('return_code')
                if type(code) is int:
                    event['return_code'] = code
                category = diag.get('category')
                if category in ('network_or_tls_failure', 'http_status_rejected',
                                'response_size_limit', 'total_timeout', 'destination_rejected',
                                'identity_unavailable', 'ticket_purchase_not_authorized_or_already_attempted',
                                'protocol_failure', 'local_failure'):
                    event['category'] = category
                print(json.dumps(event), file=sys.stderr, flush=True)
            send(int(user), format_ticket_result(result, charge_id))
        except Exception:
            send(int(user), '购券结果未确认，请先 /查券 或在机台核对；不要重复购买。')
        finally:
            self.gate.release()

    @serialized_worker
    def _inventory_work(self, user, send):
        try:
            record = session_record(user, self.private)
            if record is None:
                send(int(user), '没有本机器人保留的会话；请先发送 /login 二维码文本。')
                return
            if record.get('state') != 'active':
                send(int(user), '当前会话记录不是可用登录态，无法查询；请检查游戏状态或 /清理会话。')
                return
            rows = self.inventory_runner(user, private=self.private)
            send(int(user), format_inventory(rows))
        except Exception:
            send(int(user), '暂时无法查询券库存，请稍后重试；未做任何修改。')
        finally:
            self.gate.release()

    @serialized_worker
    def _cleanup_work(self, user, send):
        try:
            if not session_exists(user, self.private):
                send(int(user), '没有本机器人保留的会话记录。')
                return
            state = (session_record(user, self.private) or {}).get('state')
            if state == 'unknown':
                outcome = reap_orphan(user, private=self.private, archive=self.archive)
                send(int(user), '未确认记录已归档（此前登录未成功，没有会话需要登出）；可以重新 /login。'
                     if outcome.get('reaped') else
                     '记录状态未变，请稍后重试或联系管理员。')
                return
            if state != 'active':
                send(int(user), '记录需管理员处理；未做任何清理。')
                return
            # `active` is only archived on positive read-only proof that the game
            # no longer knows this session. Network trouble proves nothing.
            liveness = self.probe_runner(user, private=self.private)
            if liveness == 'dead':
                outcome = reap_expired(user, private=self.private, archive=self.archive)
                send(int(user), '该会话在游戏侧已失效，已归档记录；可以重新 /login。'
                     if outcome.get('reaped') else
                     '记录状态未变，请稍后重试或联系管理员。')
            elif liveness == 'alive':
                send(int(user), '会话在游戏侧仍然有效，请先 /logout；未做任何清理。')
            else:
                send(int(user), '暂时无法确认会话状态，未做任何清理；请稍后重试。')
        except Exception:
            send(int(user), '清理暂不可用，请稍后重试。')
        finally:
            self.gate.release()

    def _work(self, user, qr, send, context=None, retain=True):
        try:
            with session_lock(self.private):
                # Repeat mutable preflight checks after acquiring the process lock.
                if not self._consent(user):
                    send(int(user), CONSENT)
                    return
                if session_exists(user, self.private):
                    send(int(user), '已有保留或状态未确认的会话，请先 /logout；新二维码未使用。')
                    return
                try:
                    registered = register_qr_attempt(qr, self.private)
                except Exception:
                    send(int(user), '二维码尝试记录暂不可用，未提交取分；请联系管理员。')
                    return
                if not registered:
                    send(int(user), '此二维码已被本机器人尝试过，不会再次提交（包括失败或超时）；这不代表官方已消费。请从公众号重新获取新码，并在显示的有效期内使用。')
                    return
                result = self.runner(user, qr, private=self.private, **({'retain':True} if retain else {}))
                if result.requester != user:
                    raise CaptureError('capture_owner_mismatch')
                result.upload_context = context
                if result.summary.get('login') == 'not_started':
                    send(int(user), '二维码验证失败：可能为空码、已过期、已使用或验证服务异常；未进入游戏登录，无需 /logout。请从公众号重新获取新码，并在显示的有效期内使用；本地尝试不代表官方已消费。')
                    return
                label = {'complete': '完成', 'partial': '部分完成', 'failed': '失败'}[result.summary['status']]
                logout = '登出接口已接受，游戏侧会话释放尚未核验' if result.summary['logout'] == 'confirmed' else '登出未确认，请检查游戏会话状态'
                if retain:
                    logout = '会话已保留；登录成功 60 秒后可 /logout，14 分钟后自动尝试登出' if result.summary.get('session') == 'active' else '会话状态未确认，请检查游戏状态；新登录已阻止'
                elif result.summary.get('session') == 'active':
                    logout = '登出未确认，会话记录已保留，可使用 /logout 重试；新登录已阻止'
                send(int(user), f'取分{label}：{len(result.scores)} 条成绩；{logout}。取分登录后，上机前请从公众号重新获取新码。')
                if self.on_result is not None and result.summary['status'] == 'complete':
                    try:
                        self.on_result(result, send)
                    except Exception:
                        send(int(user), '取分结果已保存，但后续上传未完成。')
        except Exception:
            try:
                send(int(user), '取分失败或超时；登出状态未确认，请检查游戏会话状态。此码不会再次提交，本地尝试不代表官方已消费；需要再次取分时，请从公众号重新获取新码并在显示的有效期内使用。')
            except Exception:
                pass
        finally:
            self.gate.release()


DEFAULT_SYNC = QRSync()

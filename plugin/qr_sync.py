"""Private QR capture orchestration. No network on import; no secret logging.

Upload integration contract: CaptureResult(requester, path, scores, summary).
Scores retain the live client's game field names, not site-specific identities.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
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
           '并登录游戏读取成绩；/login 保留会话直到 /logout，/取分 自动尝试登出。仅发送你有权使用的二维码。'
           '如接受，请私聊发送 /同意HTTP取分，然后重新发送 /login <SGWCMAID二维码文本>。'
           '可用 /撤销HTTP取分 撤回后续取分授权；取分结果会私密保存在服务器供后续上传使用。')
HELP = '私聊用法：/login <SGWCMAID二维码文本> 登录取分并保留会话，/logout 实际登出；/取分 <SGWCMAID二维码文本> 或直接发送二维码文本取分后自动登出；/清理会话 用于归档「登录未成功」的未确认记录。仅支持文本。'


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
    return 'SGWCMAID' in text.upper() or text.lower().startswith(('/login', '/logout', '/清理会话')) or text.startswith(('/取分', '/同意HTTP取分', '/撤销HTTP取分', '/绑定', '/解绑', '/上传'))


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
        return ''.join(parts).strip()
    return str(event.get('raw_message') or '').strip()


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
        lifecycle = ['--session-owner', requester] if retain else ['--logout']
        completed = subprocess.run(
            [sys.executable, str(client), '--live', '--qr-stdin', '--allow-http-qr',
             '--preload-profile', *lifecycle, '--result', str(path)],
            input=qr.encode('ascii'), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout, check=False, shell=False)
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
        if retain and clean.get('login') != 'not_started':
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
    archive = ARCHIVE if archive is None else archive
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now if now is not None else time.time()))
    destination = _private_backup_dir(archive) / ('orphan-session-' + user + '-' + stamp + '.json')
    source = session_file(user, private)
    if destination.exists():
        raise CaptureError('orphan_archive_collision')
    os.rename(source, destination)
    os.chmod(destination, 0o600)
    return {'reaped': True, 'reason': 'unknown_without_session', 'archive': str(destination)}

def logout(user, *, private=PRIVATE, client=CLIENT, timeout=75):
    private_directory(private)
    record = read_private(session_file(user, private), 65536)
    if record.get('owner') != user or record.get('version') != 1 or record.get('state') not in ('active', 'closed'):
        raise CaptureError('logout_unconfirmed')
    completed = subprocess.run([sys.executable, str(client), '--logout-session', '--session-owner', user],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=timeout, check=False, shell=False)
    if completed.returncode != 0 or session_exists(user, private):
        raise CaptureError('logout_unconfirmed')
    return True


class QRSync:
    def __init__(self, private: Path = PRIVATE, *, runner=capture, cooldown=60, on_result=None,
                 before_capture=None, logout_runner=logout, archive=None):
        self.private = private
        self.runner = runner
        self.logout_runner = logout_runner
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
                # Only ever archives a record that provably holds no game session.
                if not self.gate.acquire(blocking=False):
                    send(int(user), '取分或登出正在进行，请稍后重试。')
                    return True
                try:
                    if not session_exists(user, self.private):
                        send(int(user), '没有本机器人保留的会话记录。')
                        return True
                    state = (session_record(user, self.private) or {}).get('state')
                    if state == 'unknown':
                        outcome = reap_orphan(user, private=self.private, archive=self.archive)
                        send(int(user), '未确认记录已归档（此前登录未成功，没有会话需要登出）；可以重新 /login。'
                             if outcome.get('reaped') else
                             '记录状态未变，请稍后重试或联系管理员。')
                    elif state == 'active':
                        send(int(user), '存在已保留的游戏会话，请先 /logout；未做任何清理。')
                    else:
                        send(int(user), '记录需管理员处理；未做任何清理。')
                except Exception:
                    send(int(user), '清理暂不可用，请稍后重试。')
                finally:
                    self.gate.release()
                return True
            if text in ('/同意HTTP取分', '/撤销HTTP取分'):
                accepted = text == '/同意HTTP取分'
                self._consent(user, accepted)
                send(int(user), '已保存 HTTP 取分授权，请重新发送二维码。' if accepted else '已撤销后续 HTTP 取分授权；已开始的任务仍会尝试完成登出。')
                return True
            if text == '/取分':
                send(int(user), HELP + '\n' + CONSENT)
                return True
            retain = text.startswith(('/login ', '/绑定账号 '))
            qr = text.split(' ', 1)[1].strip() if retain else text[3:].strip() if text.startswith('/取分 ') else text
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

    def _logout_work(self, user, send):
        try:
            if not session_exists(user, self.private):
                send(int(user), '没有本机器人保留的会话；未执行游戏登出。')
                return
            if self.logout_runner(user, private=self.private) is not True:
                raise CaptureError('logout_unconfirmed')
            send(int(user), '已确认游戏登出。')
        except Exception:
            send(int(user), '登出未确认，会话记录保留；请检查游戏状态后重试 /logout。')
        finally:
            self.gate.release()

    def _work(self, user, qr, send, context=None, retain=False):
        try:
            result = self.runner(user, qr, private=self.private, **({'retain':True} if retain else {}))
            if result.requester != user:
                raise CaptureError('capture_owner_mismatch')
            result.upload_context = context
            if result.summary.get('login') == 'not_started':
                send(int(user), '二维码验证失败：可能为空码、已过期或验证服务异常；未进入游戏登录，无需 /logout，请重新获取二维码。')
                return
            label = {'complete': '完成', 'partial': '部分完成', 'failed': '失败'}[result.summary['status']]
            logout = '已确认登出' if result.summary['logout'] == 'confirmed' else '登出未确认，请检查游戏会话状态'
            if retain:
                logout = '会话已保留，使用 /logout 登出' if result.summary.get('session') == 'active' else '会话状态未确认，请检查游戏状态；新登录已阻止'
            send(int(user), f'取分{label}：{len(result.scores)} 条成绩；{logout}。')
            if self.on_result is not None and result.summary['status'] == 'complete':
                try:
                    self.on_result(result, send)
                except Exception:
                    send(int(user), '取分结果已保存，但后续上传未完成。')
        except Exception:
            try:
                send(int(user), '取分失败或超时；登出状态未确认，请检查游戏会话状态后重试。')
            except Exception:
                pass
        finally:
            self.gate.release()


DEFAULT_SYNC = QRSync()

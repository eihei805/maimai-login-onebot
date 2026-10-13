"""Private per-QQ site credentials and upload glue.

The module has no network or credential discovery side effects at import time.
Only private OneBot events are handled. Secrets are stored as one JSON file per
QQ below a 0700 directory, with 0600 files and atomic replacement.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
from typing import Any, Callable

PRIVATE = Path(os.getenv('MAIMAI_PRIVATE_DIR', str(Path(__file__).resolve().parent.parent / 'private')))
QQ_RE = re.compile(r'[1-9][0-9]{4,11}\Z')
MAX_TOKEN = 2048
TOKEN_HELP = ('水鱼成绩导入 Token：https://maimai.diving-fish.com/\n'
              '登录后在个人资料中找到「成绩导入 Token」，复制或生成。\n'
              '落雪个人 API 密钥：https://maimai.lxns.net/user/profile\n'
              '登录后在账号详情生成个人 API 密钥。\n'
              '私聊绑定：/绑定水鱼 TOKEN 或 /绑定落雪 PERSONAL_TOKEN。')


class BindingError(ValueError):
    pass


@dataclass(frozen=True, repr=False)
class UploadContext:
    requester: str
    bindings: tuple[tuple[str, str], ...] = field(repr=False)


def _qq(value: Any) -> str:
    text = str(value or '')
    if not QQ_RE.fullmatch(text):
        raise BindingError('invalid_qq')
    return text


def _token(value: str) -> str:
    # Keep the adapter's Bearer/Import-Token interpretation exact. This layer
    # accepts only visible, non-whitespace ASCII and never normalizes it.
    if (not isinstance(value, str) or not 1 <= len(value) <= MAX_TOKEN
            or any(ord(ch) < 33 or ord(ch) > 126 for ch in value)
            or any(ch in value for ch in '\r\n\x00')):
        raise BindingError('invalid_token')
    return value


def _private_dir(path: Path) -> None:
    # Reject symlinks in every path component before creating the final dir.
    # Parent directories must already exist; never create permissive ancestors.
    if not path.is_absolute() or '..' in path.parts:
        raise BindingError('invalid_directory')
    for parent in reversed(path.parents):
        s = parent.lstat()
        if not stat.S_ISDIR(s.st_mode):
            raise BindingError('invalid_directory')
    path.mkdir(mode=0o700, exist_ok=True)
    s = path.lstat()
    if (not stat.S_ISDIR(s.st_mode) or s.st_uid != os.getuid()
            or stat.S_IMODE(s.st_mode) != 0o700):
        raise BindingError('private_directory_permissions')


def _safe_name(qq: str) -> str:
    _qq(qq)
    return f'qq-{qq}.json'


@contextmanager
def _lock(root: Path):
    lock_path = root / '.bindings.lock'
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        s = os.fstat(fd)
        if (not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid()
                or s.st_nlink != 1 or stat.S_IMODE(s.st_mode) != 0o600):
            raise BindingError('lock_permissions')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class BindingStore:
    def __init__(self, root: Path = PRIVATE):
        self.root = Path(root)
        self._thread_lock = threading.RLock()

    def _path(self, qq: str) -> Path:
        return self.root / _safe_name(qq)

    def _read_unlocked(self, qq: str) -> dict[str, str]:
        path = self._path(qq)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {}
        with os.fdopen(fd, 'rb') as stream:
            s = os.fstat(stream.fileno())
            if (not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid()
                    or s.st_nlink != 1 or stat.S_IMODE(s.st_mode) != 0o600):
                raise BindingError('credential_file_permissions')
            raw = stream.read(16 * 1024 + 1)
        if len(raw) > 16 * 1024:
            raise BindingError('credential_file_size')
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            raise BindingError('credential_file_invalid') from None
        if not isinstance(data, dict) or any(k not in ('fish_import_token', 'lxns_personal_token')
                                             or not isinstance(v, str) for k, v in data.items()):
            raise BindingError('credential_file_invalid')
        for value in data.values():
            _token(value)
        return data

    def _write_unlocked(self, qq: str, data: dict[str, str]) -> None:
        path = self._path(qq)
        fd, temporary = tempfile.mkstemp(prefix='.qq-', suffix='.tmp', dir=self.root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(data, stream, separators=(',', ':'), sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            # os.replace preserves the already checked directory, and the
            # target name is generated only from validated decimal QQ digits.
            directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def get(self, qq: str) -> dict[str, str]:
        qq = _qq(qq)
        with self._thread_lock:
            _private_dir(self.root)
            with _lock(self.root):
                return dict(self._read_unlocked(qq))

    def set(self, qq: str, site: str, token: str) -> None:
        qq, token = _qq(qq), _token(token)
        if site not in ('fish_import_token', 'lxns_personal_token'):
            raise BindingError('invalid_site')
        with self._thread_lock:
            _private_dir(self.root)
            with _lock(self.root):
                data = self._read_unlocked(qq)
                data[site] = token
                self._write_unlocked(qq, data)

    def remove(self, qq: str, site: str) -> bool:
        qq = _qq(qq)
        if site not in ('fish_import_token', 'lxns_personal_token'):
            raise BindingError('invalid_site')
        with self._thread_lock:
            _private_dir(self.root)
            with _lock(self.root):
                data = self._read_unlocked(qq)
                existed = site in data
                data.pop(site, None)
                if data:
                    self._write_unlocked(qq, data)
                else:
                    try:
                        os.unlink(self._path(qq))
                    except FileNotFoundError:
                        pass
                return existed


class SiteBindings:
    def __init__(self, root: Path = PRIVATE):
        self.store = BindingStore(root)
        self._catalog_provider: Callable[[Any], Any] | None = None
        self._uploader = None

    def configure_upload(self, *, uploader=None, catalog_provider=None) -> None:
        """Inject adapter(module, result, bindings, catalogs) and provider(result).

        Adapter returns per-site dictionaries with a fixed ``state`` field.
        score_upload is imported lazily only when an upload is invoked.
        """
        if any(value is not None and not callable(value)
               for value in (uploader, catalog_provider)):
            raise TypeError('upload configuration must be callable')
        self._uploader, self._catalog_provider = uploader, catalog_provider

    def credentials(self, qq: str) -> dict[str, str]:
        """Return a fresh snapshot. Caller must never log or display this value."""
        return self.store.get(qq)

    def snapshot(self, qq: str) -> UploadContext:
        """Freeze owner and credentials before the game capture starts."""
        qq = _qq(qq)
        return UploadContext(qq, tuple(sorted(self.credentials(qq).items())))

    def handle(self, event: dict, send) -> bool:
        if event.get('post_type') != 'message' or event.get('message_type') != 'private':
            return False
        segments = event.get('message')
        if isinstance(segments, list):
            parts = []
            for segment in segments:
                if (not isinstance(segment, dict) or segment.get('type') != 'text'
                        or not isinstance(segment.get('data'), dict)
                        or not isinstance(segment['data'].get('text'), str)):
                    return False
                parts.append(segment['data']['text'])
            text = ''.join(parts)
        else:
            text = event.get('raw_message')
        if not isinstance(text, str):
            return False
        text = text.lstrip(' ')
        if text == '/绑定账号' or text.startswith('/绑定账号 '):
            return False
        if not (text.startswith('/绑定') or text.startswith('/解绑') or text in ('/上传状态', '/token', '/获取token')):
            return False
        try:
            qq = _qq(event.get('user_id'))
        except BindingError:
            return True
        try:
            if text in ('/绑定', '/绑定水鱼', '/绑定落雪', '/token', '/获取token'):
                send(int(qq), TOKEN_HELP)
            elif text.startswith('/绑定水鱼 '):
                self.store.set(qq, 'fish_import_token', text[len('/绑定水鱼 '):])
                send(int(qq), '水鱼凭据已绑定。')
            elif text.startswith('/绑定落雪 '):
                self.store.set(qq, 'lxns_personal_token', text[len('/绑定落雪 '):])
                send(int(qq), '落雪个人凭据已绑定。')
            elif text == '/解绑水鱼':
                self.store.remove(qq, 'fish_import_token')
                send(int(qq), '水鱼凭据已解绑。')
            elif text == '/解绑落雪':
                self.store.remove(qq, 'lxns_personal_token')
                send(int(qq), '落雪个人凭据已解绑。')
            elif text == '/上传状态':
                data = self.store.get(qq)
                send(int(qq), '上传凭据状态：水鱼%s，落雪%s。' %
                     ('已配置' if 'fish_import_token' in data else '未配置',
                      '已配置' if 'lxns_personal_token' in data else '未配置') + '\n' + TOKEN_HELP)
            else:
                send(int(qq), '命令格式：/绑定水鱼 TOKEN、/绑定落雪 PERSONAL_TOKEN、/解绑水鱼、/解绑落雪、/上传状态。\n' + TOKEN_HELP)
        except Exception:
            send(int(qq), '凭据未保存：令牌格式无效或本地存储不可用。')
        return True

    # Fixed Chinese labels for blocked causes. Never the raw internal code.
    _BLOCK_LABELS = {'incomplete_fetch': '取分未完整',
                     'invalid_scores_or_catalog': '成绩或曲库校验未通过',
                     'invalid_credentials': '上传凭据无效'}

    @staticmethod
    def _skip_summary(outcome) -> str:
        """Fixed-label summary of skipped rows. Never echoes titles, IDs or bodies.

        Both sites share one conversion pass, so the same skip set is reported
        once rather than counted per site.
        """
        if not isinstance(outcome, dict):
            return ''
        best = None
        for site in ('fish', 'lxns'):
            item = outcome.get(site)
            if not isinstance(item, dict):
                continue
            detailed = item.get('skipped_reasons')
            count = item.get('skipped_rows')
            if not isinstance(detailed, dict) or type(count) is not int or count <= 0:
                continue
            if best is None or count > best[1]:
                best = (detailed, count)
        if best is None:
            return ''
        reasons = ['%s %d' % (label, value) for label, value in best[0].items()
                   if isinstance(label, str) and type(value) is int and value > 0]
        if not reasons:
            return ''
        return ' 已跳过 %d 行（%s）。' % (best[1], '、'.join(reasons))

    def upload_callback(self, result, send) -> None:
        """QRSync callback, using only an explicitly injected upload adapter."""
        qq = _qq(result.requester)
        if not isinstance(result.summary, dict) or result.summary.get('status') != 'complete':
            send(int(qq), '成绩取分未完整完成，未上传。')
            return
        try:
            context = getattr(result, 'upload_context', None)
            if not isinstance(context, UploadContext) or context.requester != qq:
                send(int(qq), '上传任务缺少有效的取分前凭据快照，未上传。')
                return
            bindings = dict(context.bindings)
            if not bindings:
                send(int(qq), '未配置上传凭据；可私聊绑定水鱼或落雪个人令牌。')
                return
            if self._catalog_provider is None or self._uploader is None:
                send(int(qq), '取分已完成；上传适配器尚未配置。')
                return
            catalogs = self._catalog_provider(result)
            # Import only at invocation: the parent adapter is responsible for
            # calling the uploader's documented contract with these inputs.
            import score_upload
            outcome = self._uploader(score_upload, result, bindings, catalogs)
            # Only fixed, locally recognized states may reach QQ. No upstream
            # body, exception, reason, identity, catalog or token is displayed.
            labels = {'success': '成功', 'accepted_unverified': '已接受，待核验',
                      'not_configured': '未配置', 'disabled': '未配置',
                      'blocked': '已阻止', 'failed': '失败',
                      'transport_unknown': '结果未确认'}
            parts = []
            for site, label in (('fish', '水鱼'), ('lxns', '落雪')):
                item = outcome.get(site, {}) if isinstance(outcome, dict) else {}
                state = item.get('state') if isinstance(item, dict) else None
                text = label + '：' + labels.get(state, '结果未确认')
                if state == 'failed' and isinstance(item, dict):
                    cause = score_upload.FAILURE_LABELS.get(item.get('reason'))
                    if cause:
                        text += '（' + cause + '）'
                if state == 'blocked' and isinstance(item, dict):
                    cause = self._BLOCK_LABELS.get(item.get('reason'))
                    if cause:
                        text += '（' + cause + '）'
                parts.append(text)
            send(int(qq), '上传结果：' + '，'.join(parts) + '。' + self._skip_summary(outcome))
        except Exception:
            send(int(qq), '取分已完成，但上传未完成；请检查站点后再重试。')


DEFAULT_BINDINGS = SiteBindings()

def handle(event: dict, send) -> bool:
    return DEFAULT_BINDINGS.handle(event, send)

def upload_callback(result, send) -> None:
    return DEFAULT_BINDINGS.upload_callback(result, send)

def configure_upload(*, uploader=None, catalog_provider=None) -> None:
    DEFAULT_BINDINGS.configure_upload(uploader=uploader, catalog_provider=catalog_provider)

def snapshot(qq: str) -> UploadContext:
    return DEFAULT_BINDINGS.snapshot(qq)

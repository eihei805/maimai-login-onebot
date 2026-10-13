#!/usr/bin/env python3
"""One systemd timer pass. Imports are offline; no legacy auto logout."""
import json
import math
import os
import re
import time
import uuid
import qr_sync as q


def write_record(path, record):
    temp = path.with_name('session-tmp-' + uuid.uuid4().hex)
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(record, f, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


def run_once(*, private=q.PRIVATE, now=None, logout_runner=q.logout, send=None):
    now = time.time() if now is None else now
    attempted = 0
    with q.session_lock(private):
        for path in sorted(private.glob('session-*.json')):
            match = re.fullmatch(r'session-([1-9][0-9]{4,11})\.json', path.name)
            if not match:
                continue
            user = match[1]
            try:
                record = q.session_record(user, private)
                stamp = record.get('login_success_epoch') if record else None
                if (not record or record.get('owner') != user or record.get('version') != 1
                        or record.get('state') != 'active'
                        or type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp <= 0
                        or now < stamp + 840 or 'auto_logout_attempt_epoch' in record):
                    continue
                # At-most-once even on crash after dispatch, before response or notification.
                record['auto_logout_attempt_epoch'] = now
                write_record(path, record)
            except Exception:
                continue  # Unreadable/corrupt/legacy never sends a game request.
            attempted += 1
            try:
                if logout_runner(user, private=private) is not True:
                    raise q.CaptureError('logout_unconfirmed')
                text = '登录已满 14 分钟，自动登出接口已接受；游戏侧会话释放尚未核验。'
            except Exception:
                text = '登录已满 14 分钟，自动登出未确认；会话记录保留，请检查游戏状态后用 /logout 重试。'
            if send is not None:
                try:
                    send(int(user), text)
                except Exception:
                    # No game retries if OneBot is unavailable.
                    print('timed logout notification unavailable', flush=True)
    return attempted


def main():
    from main import send_private
    run_once(send=send_private)


if __name__ == '__main__':
    main()

"""Standalone OneBot maimai login and user binding plugin."""
from __future__ import annotations
import json, os, sys, time
import urllib.request, urllib.parse
from pathlib import Path
from typing import Any
ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
OB_BASE = os.getenv('ONEBOT_HTTP', 'http://127.0.0.1:3000').rstrip('/')
OB_WS = os.getenv('ONEBOT_WS', 'ws://127.0.0.1:3001')
ALLOWED_GROUP_IDS = frozenset(int(v.strip()) for v in os.getenv('ALLOWED_GROUP_IDS', '').split(',') if v.strip())
class OneBotError(RuntimeError):
    pass

def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default

def send_group(group_id, text):
    onebot_api('send_group_msg', {'group_id': group_id, 'message': [{'type': 'text', 'data': {'text': text[:3800]}}]})


def http_json(url: str, *, method: str = "GET", body: Any = None, headers: dict[str, str] | None = None, timeout: int = 20) -> Any:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    h = {"Accept": "application/json", "User-Agent": "llbot-maimai/0.1"}
    if data is not None:
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    # Local OneBot traffic must bypass the machine proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if urllib.parse.urlsplit(url).hostname in {"127.0.0.1", "localhost", "::1"} else urllib.request.build_opener()
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def onebot_headers(channel: str = "HTTP") -> dict[str, str]:
    """Channel-specific settings override shared token; read again on reconnect."""
    prefix = f"ONEBOT_{channel}"
    if prefix + "_TOKEN" in os.environ or prefix + "_TOKEN_FILE" in os.environ:
        token = os.getenv(prefix + "_TOKEN")
        filename = os.getenv(prefix + "_TOKEN_FILE")
    else:
        token = os.getenv("ONEBOT_TOKEN")
        filename = os.getenv("ONEBOT_TOKEN_FILE")
    if token is None and filename:
        try:
            token = Path(filename).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise OneBotError("OneBot token file unavailable") from None
    token = (token or "").strip()
    if not token or "\n" in token or "\r" in token:
        raise OneBotError("OneBot requires a non-empty single-line token")
    return {"Authorization": f"Bearer {token}"}

def check_onebot_response(response: Any) -> Any:
    if not isinstance(response, dict):
        raise OneBotError("Invalid OneBot API response")
    retcode = response.get("retcode")
    if type(retcode) is not int or retcode != 0 or response.get("status") != "ok":
        code = str(retcode) if type(retcode) is int else "invalid"
        raise OneBotError(f"OneBot API failed (retcode={code})")
    return response.get("data")

def onebot_api(action: str, body: dict[str, Any], timeout: int = 20) -> Any:
    headers = onebot_headers("HTTP")
    try:
        response = http_json(f"{OB_BASE}/{action}", method="POST", body=body,
                             headers=headers, timeout=timeout)
    except Exception:
        raise OneBotError("OneBot HTTP request failed") from None
    return check_onebot_response(response)

def send_private(user_id: int, text: str) -> None:
    # Text segments prevent CQ-code interpretation; never quote QR messages.
    onebot_api("send_private_msg", {"user_id": user_id, "message": [
        {"type": "text", "data": {"text": text[:3800]}}]}, timeout=15)

def _group_qr_text(event: dict[str, Any]) -> str:
    """Return only a plain text QR command; never parse CQ or mention data."""
    from qr_sync import event_text, valid_qr
    text = event_text(event)
    if text.startswith(('/login ', '/绑定账号 ')):
        qr = text.split(' ', 1)[1].strip()
    elif text.startswith('/取分 '):
        qr = text[4:].strip()
    else:
        qr = text
    return qr if valid_qr(qr) else ''

def _group_qr_hint(group_id: int, text: str = '机器人没有群管理员权限，无法安全处理群二维码；请轮换二维码。') -> None:
    try:
        send_group(group_id, text)
    except Exception:
        pass

def _group_qr_capture(event: dict[str, Any], qr: str = '') -> bool:
    """Verify group input; recall when possible, otherwise warn and continue."""
    from qr_sync import DEFAULT_SYNC, event_text
    group_id = int(event.get('group_id') or 0)
    message_id = int(event.get('message_id') or 0)
    user_id = str(event.get('user_id') or (event.get('sender') or {}).get('user_id') or '')
    self_id = int(event.get('self_id') or 0)
    recovery = '请轮换二维码。' if qr else '公开的 Token 请撤销并重新生成。'
    if not group_id or not message_id or not user_id or not self_id:
        _group_qr_hint(group_id, '无法确认机器人权限，未处理；' + recovery)
        return True
    try:
        member = onebot_api('get_group_member_info',
                            {'group_id': group_id, 'user_id': self_id}, timeout=10)
        role = member.get('role') if isinstance(member, dict) else None
        original = onebot_api('get_msg', {'message_id': message_id}, timeout=10)
        if not isinstance(original, dict) or int(original.get('group_id') or 0) != group_id \
                or int(original.get('message_id') or 0) != message_id \
                or str(original.get('user_id') or (original.get('sender') or {}).get('user_id') or '') != user_id \
                or event_text(original) != event_text(event) \
                or _group_qr_text(original) != qr:
            _group_qr_hint(group_id, '未能核验原始消息，未处理；' + recovery)
            return True
    except Exception:
        _group_qr_hint(group_id, '机器人未能核验原始消息，未处理；' + recovery)
        return True
    recalled = False
    if role in ('admin', 'owner'):
        try:
            onebot_api('delete_msg', {'message_id': message_id}, timeout=10)
            recalled = True
        except Exception:
            pass
    if not recalled:
        _group_qr_hint(group_id, '消息将继续处理，但机器人没有群管理员权限或撤回失败，凭据仍公开可见；建议手动撤回，以后私聊发送。处理完成后，' + recovery)
    text = event_text(event)
    if qr and not text.startswith(('/login ', '/绑定账号 ', '/取分 ')):
        text = '/login ' + qr
    synthetic = dict(event, message_type='private', user_id=int(user_id), raw_message=text,
                     message=[{'type': 'text', 'data': {'text': text}}])
    def report(_user: int, text: str) -> None:
        suffix = ''
        if text.startswith(('取分完成', '取分部分完成', '取分失败')):
            suffix = ' 请在公众号显示的有效期内使用新码。'
        if '会话已保留' in text:
            suffix = ' 登录成功 60 秒后可 /logout；上机前请轮换二维码，从公众号重新获取新码。'
        text = text.replace('私聊发送', '发送').replace('请私聊', '请在群内').replace('可私聊', '可在群内')
        _group_qr_hint(group_id, text + suffix)
    from site_bindings import handle as handle_bindings
    if not handle_bindings(synthetic, report):
        DEFAULT_SYNC.handle(synthetic, report)
    return True


def upload_catalogs(result) -> tuple[list[dict], list[dict]]:
    """Read only the public raw caches; never refresh or discover tokens here."""
    fish = load_json(DATA / "music_data.json", None)
    lxns = load_json(DATA / "songs.json", None)
    if not all(isinstance(rows, list) and rows and all(isinstance(r, dict) for r in rows)
               for rows in (fish, lxns)):
        raise ValueError("upload_catalog_unavailable")
    return fish, lxns

def upload_adapter(module, result, bindings, catalogs):
    fish, lxns = catalogs
    credentials = module.Credentials(**bindings)
    return module.upload_scores(result.scores, fish, lxns, credentials,
                                complete=result.summary.get("status") == "complete")

def configure_private_sync() -> None:
    """Pure wiring: no credentials, catalogs, sockets or game reads."""
    import site_bindings
    from qr_sync import DEFAULT_SYNC
    site_bindings.configure_upload(uploader=upload_adapter, catalog_provider=upload_catalogs)
    DEFAULT_SYNC.before_capture = site_bindings.snapshot
    DEFAULT_SYNC.on_result = site_bindings.upload_callback

def dispatch_frame(frame: str | bytes) -> None:
    """Only events reach the handler; API replies are never treated as events."""
    try:
        payload = json.loads(frame)
        if not isinstance(payload, dict):
            return
        # API replies carry retcode/echo. Meta events (heartbeat/lifecycle) carry
        # post_type plus a "status" object, so "status" alone must not mark a reply.
        if "retcode" in payload or "echo" in payload or ("status" in payload and "post_type" not in payload):
            check_onebot_response(payload)
            return
        if payload.get("post_type") == "message":
            handle(payload)
    except OneBotError as exc:
        print(str(exc), file=sys.stderr, flush=True)
    except Exception:
        # Headers, raw frames and remote error messages may contain secrets.
        print("OneBot event processing failed", file=sys.stderr, flush=True)

def consume_ws() -> None:
    import websocket  # websocket-client; optional when using legacy SSE

    connection = websocket.create_connection(
        OB_WS, header=onebot_headers("WS"), timeout=65,
        http_no_proxy=["127.0.0.1", "localhost", "::1"],
    )
    try:
        while True:
            frame = connection.recv()
            if not frame:
                return
            dispatch_frame(frame)
    finally:
        connection.close()

def handle(event: dict[str, Any]) -> None:
    from qr_sync import DEFAULT_SYNC, group_sensitive, event_text
    from site_bindings import TOKEN_HELP
    text = event_text(event)
    public_help = text in ('/help', '/帮助', '/login', '/绑定账号', '/取分')
    token_help = text in ('/token', '/获取token', '/绑定', '/绑定水鱼', '/绑定落雪')
    help_text = ('舞萌助手\n\n'
                 '【登录取分】\n'
                 '• /login 或 /绑定账号 二维码文本：登录并保留会话，14 分钟后自动尝试登出\n'
                 '• /logout：登录成功 60 秒后可退出自己的游戏会话\n'
                 '• /清理会话：归档可确认失效的记录（登录未成功或游戏侧已失效；不会冒充已登出）\n'
                 '• /取分 二维码文本：取分并保留会话，14 分钟后自动尝试登出\n'
                 '• /购券 2 或 /购券 3：只购买并保留券，不打歌、不消耗券\n'
                 '• /查券：使用保留会话查询券库存（游戏侧观测值，不代表付款或到账）\n\n'
                 '【成绩同步】\n'
                 '• /token：查看水鱼、落雪 Token 获取方式\n'
                 '• /绑定水鱼 TOKEN 或 /绑定落雪 TOKEN：私聊直接绑定，白名单群核验后绑定\n'
                 '• /解绑水鱼 或 /解绑落雪：移除绑定\n'
                 '• /上传状态：查看自己的绑定状态\n\n'
                 '绑定后，完整取分会自动上传到已绑定的站点。\n\n'
                 '【群聊取分】\n'
                 '私聊直接处理；白名单群核验原始消息后处理；能撤回时尝试撤回；'
                 '没有管理权限或撤回失败时提示公开风险后继续。\n\n'
                 '【首次使用】\n'
                 '发送 /同意HTTP取分；二维码接口为明文 HTTP，请确认风险。\n'
                 '二维码公开后请及时轮换。')
    if event.get("post_type") == "message" and event.get("message_type") == "private":
        if public_help:
            send_private(int(event['user_id']), help_text)
            return
        from site_bindings import handle as handle_bindings
        if handle_bindings(event, send_private):
            return
        DEFAULT_SYNC.handle(event, send_private)
        return
    if event.get("post_type") != "message" or event.get("message_type") != "group":
        return
    if int(event.get("group_id", 0)) not in ALLOWED_GROUP_IDS:
        return
    if public_help:
        _group_qr_hint(int(event['group_id']), help_text)
        return
    if token_help:
        _group_qr_hint(int(event['group_id']), TOKEN_HELP.replace('私聊绑定', '群内绑定（须成功撤回）'))
        return
    if text in ('/logout', '/清理会话', '/同意HTTP取分', '/撤销HTTP取分', '/上传状态', '/解绑水鱼', '/解绑落雪', '/查券') or text.startswith('/购券'):
        synthetic = dict(event, message_type='private')
        def report(_user: int, result: str) -> None:
            _group_qr_hint(int(event['group_id']), result.replace('私聊', '群内'))
        from site_bindings import handle as handle_bindings
        if not handle_bindings(synthetic, report):
            DEFAULT_SYNC.handle(synthetic, report)
        return
    if group_sensitive(event):
        qr = _group_qr_text(event)
        if qr or text.startswith(('/绑定水鱼 ', '/绑定落雪 ')):
            _group_qr_capture(event, qr)
        else:
            _group_qr_hint(int(event['group_id']), '命令格式无效，请发送 /help 查看用法；已公开的二维码请轮换，Token 请撤销并重新生成。')
        return

configure_private_sync()

def main():
    import qr_sync
    import websocket
    qr_sync.PRIVATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    qr_sync.private_directory(qr_sync.PRIVATE)
    onebot_headers('HTTP')
    onebot_headers('WS')
    while True:
        try:
            consume_ws()
        except Exception:
            print('OneBot connection failed; retrying', file=sys.stderr, flush=True)
        time.sleep(3)

if __name__ == '__main__':
    main()

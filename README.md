# 舞萌账号登录 OneBot 插件

独立的 OneBot 11 正向 WebSocket 客户端，支持私聊及配置白名单群中的二维码登录、读取成绩、保留会话和实际登出，以及按 QQ 绑定水鱼/落雪并上传完整成绩。未实现 OAuth。

完整分步示例、真实回复文案、systemd 配置与常见问题见 [`USAGE.md`](USAGE.md)。

## 命令

- `/login <二维码>` 或 `/绑定账号 <二维码>`：登录取分并保留游戏会话。
- `/logout`：登出自己的保留会话，服务端确认后才删除记录。
- `/取分 <二维码>`：读取后自动尝试登出。
- `/同意HTTP取分`、`/撤销HTTP取分`：明确接受或撤销二维码明文 HTTP 传输。
- `/绑定水鱼 TOKEN`、`/绑定落雪 TOKEN`、`/解绑水鱼`、`/解绑落雪`、`/上传状态`、`/token`。
- `/help`、`/帮助`。

群中的二维码和 Token 仅在机器人有管理员/群主权限、原消息内容及归属核验通过且成功撤回后处理。反馈留在原会话。默认群白名单为空；私聊可用，但 QQ 可能拒绝向非好友发送消息。二维码形状合法不代表有效，真实有效性由服务端判断。

## 安装与运行

Python 3.10+、Linux（使用 fcntl 和 SIGALRM）：

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
mkdir -m 700 private
export ONEBOT_TOKEN_FILE=/absolute/path/to/onebot-token
export ALLOWED_GROUP_IDS=123456789
python3 plugin/main.py
```

令牌文件由部署者创建并设为 0600。HTTP/WS 默认为本机 3000/3001，可通过 `ONEBOT_HTTP`、`ONEBOT_WS` 设置；支持 `ONEBOT_HTTP_TOKEN_FILE`、`ONEBOT_WS_TOKEN_FILE` 分别设置。`MAIMAI_PRIVATE_DIR` 必须为绝对路径，目录须由运行用户拥有且为 0700；默认使用仓库的 private 目录。

**本发布包不包含游戏生产配置、协议密钥、真实二维码、登录会话、站点 Token、用户成绩或曲库。** 登录后端需要部署者合法取得并配置 `private/config.json`，包含 `config` 对象和 `region_id`、`place_id`。具体字段与校验见 `maimai-minimal-client.py` 的 `Config` 及 `maimai-live-client.py` 的 `load_config`。不提供有效密钥，不会自动发现其他部署的凭据。未提供配置时真实登录不可用。

### private/config.json 结构（全部为占位符，非真实值）

```json
{
  "region_id": 0,
  "place_id": 0,
  "blockers": [],
  "config": {
    "qr_url": "https://qr-endpoint.invalid/lookup",
    "game_base": "https://game-endpoint.invalid/SomeServlet/",
    "chip_id": "A63E01XXXXX",
    "open_game_id": "GAMEID",
    "qr_secret": "<64位十六进制>",
    "salt": "<十六进制>",
    "aes_key": "<64位十六进制>",
    "aes_iv": "<32位十六进制>",
    "mai_encoding": "1.00",
    "game_content_type": "application/json",
    "qr_headers": {"User-Agent": "<客户端标识>", "Content-Type": "application/json"}
  }
}
```

字段格式要求：`chip_id` 需匹配 `A63E01` 加 5 位大写字母或数字；`qr_secret`、`aes_key` 为 32 字节（64 位十六进制），`aes_iv` 为 16 字节（32 位十六进制），`salt` 非空；`region_id`、`place_id` 必须为整数。上述十六进制字段在加载时通过 `bytes.fromhex` 解码，非法长度或形状会被拒绝。

公开仓库请勿填入真实机房域名、区服、机台标识或协议密钥；建议在公开文档中保持占位符，真实值仅保留在本地未跟踪的 private 目录。

可选成绩上传还需要将公开曲库 `music_data.json` 和 `songs.json` 放入 `plugin/data/`；缺少曲库时上传失败会明确反馈，登录核心不依赖这些缓存。仅完整取分触发上传，上传提示“已接受，待核验”不等于已完成远端读回核验。

## 离线验证

```sh
ALLOWED_GROUP_IDS=123456789 python3 -m unittest discover -s plugin -q
python3 maimai-minimal-client.py --self-test
python3 maimai-session-implementation-tests.py
```

测试使用虚构身份、密钥和响应，不执行真实登录或扫描。源码包测试通过不等于真实游戏/站点接口验收。当前仅适用于单实例部署；不提供多进程全局会话锁。

## 发布边界

只发布此目录的白名单源码，不从线上目录直接 git add。`.gitignore` 排除运行数据，但不能清除已经提交的历史。本项目按 MIT 许可证发布，见 `LICENSE`；第三方服务、协议与数据权利仍归其权利人，MIT 不授予这些权利。

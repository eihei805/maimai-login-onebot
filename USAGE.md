# 使用示例

本文档中的回复文本与实际代码一致，可直接对照排查。示例里的 QQ、群号、二维码、Token 均为虚构值。

## 1. 部署

```sh
git clone https://github.com/eihei805/maimai-login-onebot.git
cd maimai-login-onebot
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
mkdir -m 700 private
printf '%s' '你的OneBot令牌' > onebot-token && chmod 600 onebot-token
```

`private/config.json` 按 README 的占位符结构填入自行合法取得的真实值，`private/` 保持 0700。

启动：

```sh
export ONEBOT_TOKEN_FILE=$PWD/onebot-token
export ONEBOT_HTTP=http://127.0.0.1:3000
export ONEBOT_WS=ws://127.0.0.1:3001
export ALLOWED_GROUP_IDS=123456789,987654321
export MAIMAI_PRIVATE_DIR=$PWD/private
python3 plugin/main.py
```

正常情况下启动不打印任何内容；令牌缺失/为空/含换行会直接报错退出。连接失败时打印 `OneBot connection failed; retrying` 并每 3 秒重试，不会退出。

systemd 示例（`/etc/systemd/system/maimai-login.service`）：

```ini
[Unit]
Description=maimai login OneBot plugin
After=network-online.target

[Service]
WorkingDirectory=/opt/maimai-login-onebot
Environment=ONEBOT_TOKEN_FILE=/opt/maimai-login-onebot/onebot-token
Environment=ONEBOT_HTTP=http://127.0.0.1:3000
Environment=ONEBOT_WS=ws://127.0.0.1:3001
Environment=ALLOWED_GROUP_IDS=123456789,987654321
Environment=MAIMAI_PRIVATE_DIR=/opt/maimai-login-onebot/private
ExecStart=/opt/maimai-login-onebot/.venv/bin/python /opt/maimai-login-onebot/plugin/main.py
Restart=always
RestartSec=5
User=maimai

[Install]
WantedBy=multi-user.target
```

定时退出需要额外安装下面的 service/timer，并使用与主服务相同的运行用户、私有目录和 OneBot 凭据。只启动 `plugin/main.py` 不会自动运行定时退出。

`/etc/systemd/system/maimai-login-logout.service`：

```ini
[Unit]
Description=maimai timed logout pass
After=network-online.target

[Service]
Type=oneshot
User=maimai
WorkingDirectory=/opt/maimai-login-onebot
Environment=MAIMAI_PRIVATE_DIR=/opt/maimai-login-onebot/private
Environment=ONEBOT_HTTP=http://127.0.0.1:3000
Environment=ONEBOT_TOKEN_FILE=/opt/maimai-login-onebot/onebot-token
ExecStart=/opt/maimai-login-onebot/.venv/bin/python /opt/maimai-login-onebot/plugin/timed_logout.py
UMask=0077
NoNewPrivileges=true
TimeoutStartSec=300
```

`/etc/systemd/system/maimai-login-logout.timer`：

```ini
[Unit]
Description=Check maimai logout deadlines

[Timer]
OnBootSec=30s
OnUnitInactiveSec=10s
AccuracySec=1s
Unit=maimai-login-logout.service

[Install]
WantedBy=timers.target
```

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now maimai-login-logout.timer
```

登录成功时间和自动尝试标记存储在私有会话记录中。重启后恢复原截止时间；旧的无时间字段记录不会自动退出。满 14 分钟后在下一次扫描尝试退出，失败保留会话并通知，使用 `/logout` 手动重试。接口接受仍不代表已核验游戏侧会话释放。

## 2. 私聊完整流程

依次发送（`→` 后为机器人的实际回复）：

```text
/help
→ 舞萌助手

  【登录取分】
  • /login 或 /绑定账号 二维码文本：登录并保留会话
  • /logout：退出自己的游戏会话
  • /清理会话：归档可确认失效的记录（登录未成功，或游戏侧已不再认识的会话；不会冒充已登出）
  • /取分 二维码文本：取分后保留会话，登录满 60 秒可 /logout，第 14 分钟自动尝试登出

  【成绩同步】
  • /token：查看水鱼、落雪 Token 获取方式
  • /绑定水鱼 TOKEN 或 /绑定落雪 TOKEN：私聊直接绑定，群聊撤回成功后绑定
  • /解绑水鱼 或 /解绑落雪：移除绑定
  • /上传状态：查看自己的绑定状态

  绑定后，完整取分会自动上传到已绑定的站点。

  【群聊取分】
  私聊直接处理；白名单群中，机器人有管理权限且成功撤回敏感消息后才处理；没有权限或撤回失败只提醒，不取分或绑定。

  【首次使用】
  发送 /同意HTTP取分；二维码接口为明文 HTTP，请确认风险。
  二维码公开后请及时轮换。

/login
→ 私聊用法：/login <SGWCMAID二维码文本> 登录取分并保留会话，/logout 实际登出；/取分 <SGWCMAID二维码文本> 或直接发送二维码文本取分后保留会话，登录满 60 秒可 /logout，第 14 分钟自动尝试登出。仅支持文本。

/取分
→ （同上用法文本，并附加 HTTP 授权说明）

/同意HTTP取分
→ 已保存 HTTP 取分授权，请重新发送二维码。

/login SGWCMAIDxxxxxxxxxxxxAAAAAAAA...（完整 84 字符二维码文本）
→ 正在验证二维码并尝试登录；有效性以服务端结果为准。
→ 取分完成：800 条成绩；会话已保留，使用 /logout 登出。
```

此时会话已保留，在游戏侧表现为仍处于登录状态。

登出：

```text
/logout
→ 已确认游戏登出。
```

没有保留会话时：

```text
/logout
→ 没有本机器人保留的会话；未执行游戏登出。
```

### 取分后定时登出

```text
/取分 SGWCMAIDxxxxxxxxxxxxAAAAAAAA...
→ 正在验证二维码并尝试登录；有效性以服务端结果为准。
→ 取分完成：800 条成绩；会话已保留；登录成功 60 秒后可 /logout，14 分钟后自动尝试登出。
```

### 无效 / 过期二维码

```text
/login SGWCMAID加上格式正确但已过期的内容
→ 正在验证二维码并尝试登录；有效性以服务端结果为准。
→ 二维码验证失败：可能为空码、已过期或验证服务异常；未进入游戏登录，无需 /logout，请重新获取二维码。
```

格式不合法的输入不会发往服务端：

```text
/login not-a-qr
→ 私聊用法：/login <SGWCMAID二维码文本> 登录取分并保留会话，/logout 实际登出；……
```

未授权就发二维码：

```text
→ 取分会将二维码发送到游戏的原始 HTTP 接口（未加密，可能被网络中间人读取）……如接受，请私聊发送 /同意HTTP取分，然后重新发送 /login <SGWCMAID二维码文本>。
```

撤销授权：

```text
/撤销HTTP取分
→ 已撤销后续 HTTP 取分授权；已开始的任务仍会尝试完成登出。
```

## 3. 私聊拦截条件

| 情况 | 回复 |
| --- | --- |
| 正在取分或登出 | 取分正在进行，请稍后重试；二维码未加入队列。 |
| 已有保留会话 | 已有保留或状态未确认的会话，请先 /logout；新二维码未使用。 |
| 同一 QQ 60 秒内重复 | 取分请求过于频繁，请等待 60 秒后重试。 |
| 内部失败 | 取分服务暂不可用，请稍后重试。 |

取分在后台线程执行；进程内信号量串行化，超时为 210 秒（登出 75 秒）。超时后登出状态记为未确认，会话记录会保留，需要用 `/logout` 重试。

## 4. 群聊流程

先在环境变量中列入群号，否则该群所有命令直接忽略（无回复）。

```text
（群 123456789，机器人有管理权限）
/login SGWCMAIDxxxxxxxxxxxxAAAAAAAA...
```

执行顺序为：白名单 → 纯文本消息 → 机器人管理/群主权限 → 读取原消息并核验内容、群号、发送者 → 撤回原消息 → 绑定/取分。撤回失败则不处理，仅给出提醒。

二维码或 Token 在群内**成功撤回后**才会被使用，回复发回同一个群，不会私聊。普通控制命令（`/logout`、`/清理会话`、`/同意HTTP取分`、`/撤销HTTP取分`、`/上传状态`、`/解绑水鱼`、`/解绑落雪`）只操作发送者自己的数据，不需要撤回。

## 5. 站点绑定与上传

```text
/token
→ 水鱼成绩导入 Token：https://maimai.diving-fish.com/
  登录后在个人资料中找到「成绩导入 Token」，复制或生成。
  落雪个人 API 密钥：https://maimai.lxns.net/user/profile
  登录后在账号详情生成个人 API 密钥。
  私聊绑定：/绑定水鱼 TOKEN 或 /绑定落雪 PERSONAL_TOKEN。

/绑定水鱼 你从水鱼复制的Token
→ 水鱼凭据已绑定。

/绑定落雪 你从落雪复制的个人密钥
→ 落雪个人凭据已绑定。

/上传状态
→ 上传凭据状态：水鱼已配置，落雪已配置。
  （叠加 /token 的帮助文本）
```

绑定后，只有**完整**取分才自动上传：

```text
→ 上传结果：水鱼：成功，落雪：已接受，待核验。
```

`已接受，待核验` 表示站点已接收但本插件没有读回核对，不等于已确认入库。

未完整取分时不上传：

```text
→ 成绩取分未完整完成，未上传。
```

没有绑定任何站点：

```text
→ 未配置上传凭据；可私聊绑定水鱼或落雪个人令牌。
```

解绑：

```text
/解绑水鱼
→ 水鱼凭据已解绑。
```

### 上传前置条件

1. `plugin/data/music_data.json`（水鱼曲库）与 `plugin/data/songs.json`（落雪曲库）存在且非空，否则上传会失败并回复 `取分已完成，但上传未完成；请检查站点后再重试。`
2. 凭据在取分开始时快照，取分过程中再改绑定不会影响这一次上传。
3. 两个站点各自独立，一个失败不影响另一个。

## 6. 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ONEBOT_HTTP` | `http://127.0.0.1:3000` | OneBot HTTP API，用于发送回复 |
| `ONEBOT_WS` | `ws://127.0.0.1:3001` | OneBot 正向 WebSocket，用于接收事件 |
| `ONEBOT_TOKEN` / `ONEBOT_TOKEN_FILE` | 无 | 两端共用的令牌或令牌文件 |
| `ONEBOT_HTTP_TOKEN` / `ONEBOT_HTTP_TOKEN_FILE` | 无 | 仅 HTTP 端覆盖 |
| `ONEBOT_WS_TOKEN` / `ONEBOT_WS_TOKEN_FILE` | 无 | 仅 WS 端覆盖 |
| `ALLOWED_GROUP_IDS` | 空 | 逗号分隔的群号白名单；为空则所有群都忽略 |
| `MAIMAI_PRIVATE_DIR` | 仓库内 `private/` | 凭据、同意记录、会话记录的存储目录，必须是运行用户所有的 0700 绝对路径 |
| `MAIMAI_CLIENT` | 仓库内 `maimai-live-client.py` | 实际执行登录的客户端脚本 |

令牌缺失、为空或含换行会在启动时直接抛出，不会静默降级。

## 7. 数据落盘位置

均在 `MAIMAI_PRIVATE_DIR` 下，目录 0700：

- `qq-<QQ>.json`：该 QQ 的站点凭据，0600，原子写入
- `qr-consents.json`：HTTP 取分授权记录，0600
- `session-<QQ>.json`：保留会话记录，0600，只含校验过的会话字段，不含二维码
- `capture-<随机>.json`：每次取分的成绩结果，供上传使用

日志不打印令牌、二维码、响应正文或异常内容。

## 8. 常见问题

**机器人不回复任何群消息** — 群号不在 `ALLOWED_GROUP_IDS`，或未设置该变量。

**群内发二维码没反应** — 机器人不是管理员/群主，或撤回失败，或消息不是纯文本（含图片、@ 等会被拒绝）。

**私聊发不出去** — 对方 QQ 未添加机器人好友时会被 QQ 拒收，这是账号侧限制。

**一直提示要 `/logout`** — 上次会话未确认关闭。先重试 `/logout`：若那条记录有真实会话，会恢复并发送游戏登出；若是「登录未成功」留下的 `unknown` 占位（没有会话可退），它会一直报未确认（游戏侧会话约 30 分钟后自行过期，登出行为不变）。这时发一次 `/清理会话` 即可归档 `unknown` 记录并解除阻塞。

若 `/logout` 反复报未确认、且游戏侧会话已自行过期（常见：扫码后超过约 30 分钟），`/清理会话` 会先发一次**只读**探针：只有游戏明确回以空响应（证明它已不认识该会话）才归档并解除阻塞；会话仍然有效时回复「请先 /logout」，网络故障时回复「暂时无法确认」且不动记录。

**短时间内多次取分被拒** — 单个 QQ 冷却 60 秒，全局同时只允许一个取分任务。

**上传一直失败** — 检查两个曲库文件是否存在；确认 Token 未过期、站点侧权限已开启。

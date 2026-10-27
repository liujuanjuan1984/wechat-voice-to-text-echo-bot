# wechat-voice-to-text-echo-bot

极简的微信私聊语音转文字机器人。它直接使用微信 iLink Bot 返回的转写文本，
不使用 LLM、STT 或媒体处理。

- 只接受扫码绑定的单个微信用户
- 语音转写后直接以普通文本回复原文

## 安装

要求 Python 3.10+。

```bash
python3 -m venv .venv
.venv/bin/pip install .
```

## 首次登录

登录绑定只需执行一次：

```bash
.venv/bin/voice-bot login
```

终端会显示二维码。使用要绑定的微信账号扫码并确认后，该微信用户会自动成为
唯一允许使用的用户。登录凭证保存在
`~/.local/state/wechat-voice-to-text-echo-bot/credentials.json`，权限为 `0600`。
可通过 `VOICE_BOT_STATE_DIR` 修改状态目录。

完成本次登录后，后续启动、重启和开机自启都不需要再次扫码。只有凭证失效、
被撤销或需要更换绑定用户时，才需要重新登录。

## 运行

```bash
.venv/bin/voice-bot serve
```

配置常驻服务并开机自启：

```bash
repo_dir="$(pwd)"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$unit_dir"
cat > "$unit_dir/wechat-voice-to-text-echo-bot.service" <<EOF
[Unit]
Description=WeChat voice transcription reply bot
After=network-online.target

[Service]
WorkingDirectory=$repo_dir
ExecStart=$repo_dir/.venv/bin/voice-bot serve
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now wechat-voice-to-text-echo-bot.service
loginctl enable-linger "$USER"
```

`enable-linger` 确保未登录桌面时服务也会在开机后启动。

```bash
journalctl --user -u wechat-voice-to-text-echo-bot.service -f
```

## 导出缓存

机器人会按收到顺序缓存本次服务进程启动后成功回复的语音转写文本。
使用唯一绑定的微信用户在私聊中发送：

```text
导出
```

机器人会将当前缓存中的全部文本合并为一条普通文本消息回复，相邻语音之间保留一个空行。
服务端成功发送后缓存清零，之后收到的语音会进入新缓存。

缓存为空时回复“暂无可导出的文本。”。`导出` 仅对绑定用户生效；缓存只保存在进程内存中，
服务重启后从空缓存开始。

## 隐私与边界

- 转写正文不写入业务文件，日志只记录数量和长度
- 群聊、其他用户、Bot 自身消息和没有微信转写的语音都会被忽略
- 同一 Bot token 不能被多个进程同时轮询
- iLink 是外部服务，协议和限流可能变化

## License

[MIT](LICENSE)

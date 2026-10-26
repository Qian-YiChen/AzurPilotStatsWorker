# B 主动连接 A（SSH 加密传输）

当 A 只有 HTTP/NAT 地址时，复用已有 SSH 入口，无需新增公网映射。

1. 在 B 生成专用 Ed25519 密钥，将公钥放到 A 的专用无 shell 用户。该用户只能公钥认证，`AllowTcpForwarding local`、`PermitOpen 127.0.0.1:8090`、`PermitListen none`、`MaxSessions 0`，禁用代理、TTY 和 X11 转发。修改 sshd 后先执行 `sshd -t`，再 reload，保留原配置以便恢复。
2. 通过可信渠道核验 A 的主机公钥，存入 B 独立的 `known_hosts`，权限 600。私钥同样为 600，不使用管理员密码作为后台凭据。
3. 在 B 用 systemd 维护以下命令，设置 `Restart=always`、`RestartSec=5`，并使用无特权用户运行：

```sh
ssh -NT -p SSH公网端口 -i /path/to/id_ed25519 \
  -o BatchMode=yes -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/path/to/known_hosts \
  -o ExitOnForwardFailure=yes -o ServerAliveInterval=20 -o ServerAliveCountMax=3 \
  -L 127.0.0.1:18092:127.0.0.1:8090 专用用户@A的公网IP
```

4. B `.env` 配置 `UPLOADER_WS_URL=ws://127.0.0.1:18092/internal/v1/worker/connect`。Linux Compose 中 recognizer 设置 `network_mode: host`、移除 `ports`，将 `LISTEN_HOST` 改为 `127.0.0.1`。数据卷、令牌和内存限制保持配置。
5. A 配置 `WORKER_MODE=reverse`、`BATCH_INTERVAL=30m`。B 的 `/healthz` 应显示 `uploader_connected=true`，A 应显示 `worker_connected=true`、`worker_mode=reverse`、`batch_interval_seconds=1800`。

SSH 仅承载 B 发起的 WebSocket 连接；A 仍主动发送批次。无需等待 30 份，每半小时派发，单条消息最多 30 份，较大的时段拆成多条。原图始终经 123 盘下载，隧道中不传原图。

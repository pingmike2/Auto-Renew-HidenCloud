## HidenCloud自动续期
使用Github Actions 自动给HidenCloud服务续期,HidenCloud容易封多账号，隔离好环境,使用独享节点(仅自己一人使用的)

温馨提示： HidenCloud已取消 Remenber_web 长效cookie，也停用了邮箱密码登录，需要 Discord OAuth 授权登录（带 Cloudflare 验证）。请绑定 Discord 账号并配置下方 `DISCORD_EMAIL/DISCORD_PASSWORD`，尽量使用干净点的节点，否则无法过验证。

## 配置

在仓库 `Settings → Secrets and variables → Actions` 中添加以下 Secrets：

| Secret 名称 | 是否必填 | 说明 | 示例 |
|---|---|---|---|
| `DISCORD_EMAIL`      | ✅必填 | Discord 账号（唯一登录方式，HidenCloud 已停用邮箱密码与长效 cookie） | |
| `DISCORD_PASSWORD`   | ✅必填 | Discord 密码 | |
| `NODE_LINK`          | ✅必填 | 代理节点地址,例如:vless:// vmess:// trojan:// hysteria2:// anytls://(不配置会导致 Discord 异地登录验证,收验证邮件) | |
| `DISCORD_TOTP_SECRET` | ❌可选 | Discord 2FA 的 TOTP base32 密钥（开了 2FA 才需要） | |
| `EMAIL`         | ❌可选 | HidenCloud 邮箱（仅通知展示用，配合 `PASSWORD`） | |
| `PASSWORD`      | ❌可选 | HidenCloud 密码（已停用，仅通知展示保留） | |
| `AREA`          | ❌可选 | 地区标签（如 `新加坡`），仅用于 TG 通知里展示服务器地区，帮助区分多台服务器；留空则通知中不显示地区 | |
| `TG_BOT_TOKEN`  | ❌可选 | Telegram Bot Token | 
| `TG_CHAT_ID`    | ❌可选 | Telegram Chat ID |

登录方式：`DISCORD_EMAIL` + `DISCORD_PASSWORD`（Discord OAuth 授权，自动处理 Cloudflare 验证）。


### 代理格式（确认在v2rayN里使用正常的节点,使用注册时使用的代理节点）

`NODE_LINK` 支持以下任意一种代理协议的完整分享链接（必填，Discord 登录必须走代理避免异地验证）：

- **VLESS**：`vless://uuid@server:port?security=reality&sni=...&type=ws&...`
- **VMess**：`vmess://base64encoded...`
- **Trojan**：`trojan://password@server:port?sni=...&type=ws&...`
- **tuic**：`tuic://uuid:password@server:port...`
- **anytls**：`anytls://uuid@server:port...`
- **hysteria2**：`hysteria2://base64@server:port...`
- **SOCKS5**：`socks5://user:pass@server:port` 或 `socks://user:pass@server:port`

## 使用

### GitHub Actions 运行步骤

1. Fork 本仓库  
2. 在仓库 Secrets 中配置必填的环境变量（`DISCORD_EMAIL`、`DISCORD_PASSWORD`、`NODE_LINK`）,（可选）配置 `TG_BOT_TOKEN`、`TG_CHAT_ID`、`AREA`  
3. Actions菜单里手动触发 `workflow_dispatch`  
4. 根据服务到期时间来修改cron运行时间,比如你的服务是25号到期,计算后是星期二,则设置cron为每周一运行


---

**⚠️ 免责声明**：本脚本仅供学习交流使用，使用者需遵守 [HidenCloud](https://hidencloud.com) 的服务条款。因使用本脚本造成的任何问题，作者不承担任何责任。

# cloudflare — 三网优选 IP 自动更新

每天北京时间 **8:00 / 12:00 / 20:00** 自动跑一次,拉多家优选 IP 源聚合,
把三网的 IP 全部写到一个域名上,跑完发 TG 和微信通知。

## 写到哪里

三网(电信/联通/移动)各取 Top 5,去重后一共最多 15 条 A 记录,
全部写到同一个域名上,全部 DNS only(灰云,不代理),TTL 60 秒。

- 默认写到 `CF_DOMAIN` 本身(例如 `example.com`)
- 想写到子域名上(例如 `cf.example.com`),再加一个 Secret `CF_HOSTNAME=cf.example.com`,Zone ID 还是 `example.com` 那个

注意:一个域名挂三网的 IP,客户端拿到的是轮询,电信用户也可能拿到移动优选的 IP,速度不如按运营商分域名精准;换来的是简单、一个域名通用。

## 先配 Secrets

仓库 Settings → Secrets and variables → Actions → New repository secret。

**必填(没有这三个跑不起来):**

| Secret | 内容 |
|---|---|
| `CF_API_TOKEN` | Cloudflare API Token,权限 Zone:DNS:Edit + Zone:Zone:Read,只圈你要改的那个 Zone。Cloudflare 面板 → My Profile → API Tokens → Create Token → Edit zone DNS 模板,Zone Resources 选 Include / Specific zone / 你的域名。**不要写进代码、不要发聊天里。** |
| `CF_ZONE_ID` | Cloudflare 面板进域名首页,右下角 API 部分的 Zone ID |
| `CF_DOMAIN` | 你的域名,例如 `example.com` |

**选填(不配就跳过那条通知,不影响更新):**

| Secret | 内容 |
|---|---|
| `CF_HOSTNAME` | 想写到子域名才填,例如 `cf.example.com`;不填就写到域名本身 |
| `TG_BOT_TOKEN` | Telegram 机器人 Token(你这边的还是空的,填好再跑才有 TG 通知) |
| `TG_CHAT_ID` | Telegram 接收通知的 chat_id(同上,还是空的) |
| `WECHAT_API_URL` | 自建微信推送接口地址:`https://wx.djcf.pp.ua/wxsend` |
| `WECHAT_AUTH_TOKEN` | 自建微信推送接口的鉴权 Token |
| `WECHAT_WEBHOOK` | 备选:企业微信群机器人 webhook 完整 URL(不用企业微信就不填) |

## 通知怎么配

成功和失败都会发一条,内容是时间、域名、三网 IP 列表(失败是错误原因)。

**Telegram:**
1. 在 Telegram 里找 @BotFather,发 `/newbot`,按提示建一个 bot,拿到 Token,填 `TG_BOT_TOKEN`。**Token 别发聊天里,只填 Secret。**
2. 先给这个 bot 发一条消息(不然它发不出去)。
3. 找 @getmyid_bot 这类查 ID 的 bot,发任意消息,它回你的 chat_id,填 `TG_CHAT_ID`。如果是发到群里,先把 bot 拉进群,chat_id 是负数那个群 ID。

**自建微信(你的 wxsend 接口):**
1. `WECHAT_API_URL` 填 `https://wx.djcf.pp.ua/wxsend`,`WECHAT_AUTH_TOKEN` 填你的接口鉴权 Token。**Token 别发聊天里,只填 Secret。**
2. 脚本按常见自建 wxsend 格式发:POST JSON `{"token": token, "title": 标题, "content": 全文, "message": 全文}`,同时带 `Authorization: Bearer <token>` 头。
3. 如果你的接口字段名不一样(比如叫 `msg` / `text` / `to`,或者 Token 放 query 里),把接口格式发我,我改一行就行。先跑一次看微信能不能收到,收不到就把 Actions 日志里那行 `[warn] 微信通知发送失败` 的原因发我。

**备选:企业微信群机器人:**
在企业微信里建一个群(自己一个人也行),群设置 → 群机器人 → 添加机器人,复制 webhook 地址,形如 `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx`,整个 URL 填 `WECHAT_WEBHOOK`。每次跑完机器人会在群里发一条文本消息。

## 怎么跑

- 自动:每天北京时间 8:00 / 12:00 / 20:00(GitHub cron 用 UTC,写的是 `0 0,4,12 * * *`,定时任务可能晚几分钟,正常)
- 手动:仓库 Actions → CF Best IP → Run workflow
- 跑完的结果除了 TG/微信通知,也会写在那次 Actions 运行的 Summary 里

## 数据源(加源只改一个文件)

源地址全在 `sources.json` 里,分 `ct`(电信)、`cu`(联通)、`cmcc`(移动)、`all`(通用,三网都拼进去)四个列表。加一个源就在对应列表里加一行 URL,删源就删掉那行,不用动 `update_cf_dns.py`。改完提交,下次定时跑就用新的源。

- 分网源权重 2,通用源权重 1
- `sources.json` 必须放在 `update_cf_dns.py` 旁边(脚本在 `best ip` 文件夹里,就放 `best ip` 文件夹里);找不到它时脚本会用内置的源顶上,照样能跑
- 现在里面的 13 个源:love-ztm、RikkaSaiko1、svip-s、090227、vipmc838、164746、IPDB、joname1 等

## 文件

- `update_cf_dns.py` — 拉源聚合 + 调 Cloudflare API 更新 DNS + 发通知。本地 dry-run:`CF_DOMAIN=example.com python3 update_cf_dns.py --dry-run`(只拉源,不改 DNS、不发通知)
- `sources.json` — 数据源地址,见上面
- `.github/workflows/cf-best.yml` — GitHub Actions 定时任务

## 注意

GitHub Runner 在境外,测不出三网延迟;三网的"优选"属性来自上游源本身的分网实测,本脚本只做聚合+过滤(只留 Cloudflare 官方网段)+按多源出现次数排序,不做本地测速。通知发送失败只告警,不会让更新本身失败。

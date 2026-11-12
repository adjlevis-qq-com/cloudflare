# cloudflare — 三网优选 IP 自动更新

每天北京时间 **8:00 / 12:00 / 20:00** 自动跑一次,拉多家优选 IP 源聚合,
把 IP 写到指定 Cloudflare 域名的 DNS 记录上。

## 写到哪里

全部 DNS only(灰云,不代理),TTL 60 秒,每网 Top 3:

| 子域名 | 内容 |
|---|---|
| `ct.你的域名` | 电信优选 IP |
| `cu.你的域名` | 联通优选 IP |
| `cmcc.你的域名` | 移动优选 IP |
| `cf.你的域名` | 三网综合优选 IP |

## 先配 3 个 Secrets

仓库 Settings → Secrets and variables → Actions → New repository secret:

| Secret | 内容 |
|---|---|
| `CF_API_TOKEN` | Cloudflare API Token,权限 Zone:DNS:Edit + Zone:Zone:Read,只圈你要改的那个 Zone。Cloudflare 面板 → My Profile → API Tokens → Create Token → Edit zone DNS 模板,Zone Resources 选 Include / Specific zone / 你的域名。**不要写进代码、不要发聊天里。** |
| `CF_ZONE_ID` | Cloudflare 面板进域名首页,右下角 API 部分的 Zone ID |
| `CF_DOMAIN` | 你的域名,例如 `example.com` |

## 怎么跑

- 自动:每天北京时间 8:00 / 12:00 / 20:00(GitHub cron 用 UTC,写的是 `0 0,4,12 * * *`,定时任务可能晚几分钟,正常)
- 手动:仓库 Actions → CF Best IP → Run workflow

## 文件

- `update_cf_dns.py` — 拉源聚合 + 调 Cloudflare API 更新 DNS。本地 dry-run:`CF_DOMAIN=example.com python3 update_cf_dns.py --dry-run`(只拉源,不改 DNS)
- `.github/workflows/cf-best.yml` — GitHub Actions 定时任务

## 注意

GitHub Runner 在境外,测不出三网延迟;三网的"优选"属性来自上游源本身的分网实测,本脚本只做聚合+过滤(只留 Cloudflare 官方网段)+按多源出现次数排序,不做本地测速。

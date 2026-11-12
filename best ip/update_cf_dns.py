#!/usr/bin/env python3
"""三网 Cloudflare 优选 IP -> 更新到指定 Cloudflare 域名.

用法:
  本地 dry-run (只拉源、不改 DNS):
    CF_DOMAIN=example.com python3 update_cf_dns.py --dry-run
  GitHub Actions 里跑 (改 DNS):
    环境变量 CF_API_TOKEN / CF_ZONE_ID / CF_DOMAIN 由仓库 Secrets 提供

设计:
  - 数据源地址放在同目录的 sources.json 里(加源/删源只改那个文件,不动代码);
    找不到 sources.json 时用下面 SOURCES_DEFAULT 里的内置源顶上.
  - 拉多家上游源,按运营商分 ct(电信)/cu(联通)/cmcc(移动),去重,
    只留 Cloudflare 官方网段内的 IP,按多源出现次数排序,每网取前 5 个.
  - 三网的 IP 全部写到一个域名上(默认就是 CF_DOMAIN 本身):
    每网 Top 5,去重后一共最多 15 条 A 记录,全部 DNS only (灰云,不代理).
    想写到子域名上(例如 cf.example.com),就再设 CF_HOSTNAME=cf.example.com.
  - 每次先删该域名旧的 A 记录,再建新 A 记录,TTL 60 秒.
  - 注意:一个域名挂三网的 IP,客户端拿到的是轮询,电信用户也可能拿到
    移动优选的 IP,速度不如按运营商分域名精准;换来的是简单、一个域名通用.
    想按网精准,改回 ct/cu/cmcc 三个子域名就行.
  - GitHub Runner 在境外,测不出三网延迟;三网的"优选"属性来自
    上游源本身的分网实测,本脚本只做聚合+过滤+排序,不做本地测速.
"""
import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from ipaddress import ip_address, ip_network

SOURCES_DEFAULT = {
    "ct": [
        "https://raw.githubusercontent.com/love-ztm/cfip/refs/heads/main/best_ips.txt",
        "https://raw.githubusercontent.com/RikkaSaiko1/CloudFlare-ip/refs/heads/main/ip.txt",
        "https://cf.090227.xyz/ct",
        "https://raw.githubusercontent.com/vipmc838/cf_best_ip/main/cloudflare_bestip.txt",
        "https://bestcf.pages.dev/cfyes/ipv4.txt",
        "https://bestcf.pages.dev/wetest/ipv4.txt",
    ],
    "cu": [
        "https://raw.githubusercontent.com/love-ztm/cfip/refs/heads/main/ubest_ips.txt",
        "https://cf.090227.xyz/cu",
        "https://raw.githubusercontent.com/vipmc838/cf_best_ip/main/cloudflare_bestip.txt",
        "https://bestcf.pages.dev/cfyes/ipv4.txt",
        "https://bestcf.pages.dev/wetest/ipv4.txt",
    ],
    "cmcc": [
        "https://raw.githubusercontent.com/svip-s/cloudflare_ip/refs/heads/main/best_ips.txt",
        "https://cf.090227.xyz/cmcc",
        "https://raw.githubusercontent.com/vipmc838/cf_best_ip/main/cloudflare_bestip.txt",
        "https://bestcf.pages.dev/cfyes/ipv4.txt",
        "https://bestcf.pages.dev/wetest/ipv4.txt",
    ],
    "all": [
        "https://bestcf.pages.dev/cmliu/all.txt",
        "https://addressesapi.090227.xyz/CloudFlareYes",
        "https://ip.164746.xyz/ipTop10.html",
        "https://ipdb.api.030101.xyz/?type=bestcf&country=true",
        "https://raw.githubusercontent.com/joname1/BestCFip/main/ipv4.txt",
        "https://bestcf.tools12345.com/tiancheng/all.txt",
        "https://bestcf.pages.dev/nirevil/ipv4.txt",
    ],
}


def load_sources() -> dict:
    """先找 sources.json:脚本所在目录 -> 当前目录 -> 脚本父目录(仓库根目录);
    找不到或解析失败就用内置源,保证脚本还能跑."""
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, "sources.json"),
                 os.path.join(os.getcwd(), "sources.json"),
                 os.path.join(os.path.dirname(here), "sources.json")):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            src = {k: [u for u in v if isinstance(u, str) and u.startswith("http")]
                   for k, v in data.items()
                   if k in ("ct", "cu", "cmcc", "all") and isinstance(v, list)}
            if src.get("ct") or src.get("cu") or src.get("cmcc") or src.get("all"):
                print(f"数据源:使用 {path}")
                return {k: src.get(k, []) for k in ("ct", "cu", "cmcc", "all")}
            print(f"[warn] {path} 里没有可用源,用内置源", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - 源文件坏了也不让主流程失败
            print(f"[warn] 读取 {path} 失败({e}),用内置源", file=sys.stderr)
    print("数据源:使用内置源")
    return SOURCES_DEFAULT


SOURCES = load_sources()
# 三网全部写到一个域名上,目标域名见 CF_HOSTNAME(默认 CF_DOMAIN 本身)
TAG_FOR = {"ct": "电信", "cu": "联通", "cmcc": "移动"}

CF_CIDRS = [
    "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "104.16.0.0/13",
    "104.24.0.0/14", "108.162.192.0/18", "131.0.72.0/22", "141.101.64.0/18",
    "162.158.0.0/15", "172.64.0.0/13", "173.245.48.0/20", "188.114.96.0/20",
    "190.93.240.0/20", "197.234.240.0/22", "198.41.128.0/17",
]
CF_NETS = [ip_network(c) for c in CF_CIDRS]
TOP_N = int(os.environ.get("TOP_N", "5"))
TTL = int(os.environ.get("CF_TTL", "60"))
API = "https://api.cloudflare.com/client/v4"
# 版本号:每次改代码就加 1,运行时会打印出来,Actions 日志第一行就能看到跑的是哪个版本
SCRIPT_VERSION = "2026-11-12-v10 (单域名/每网Top5/TG+自建微信通知/源地址独立文件+示例/天诚源/CFYes+WeTest分网+NiREvil)"


def in_cf(ip: str) -> bool:
    try:
        addr = ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in CF_NETS)


def fetch_one(args):
    url, carrier = args
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "cf-best/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            text = r.read().decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001 - 单个源失败不影响其他源
        print(f"[warn] 拉取失败 {url}: {e}", file=sys.stderr)
        return []
    out = []
    import re

    for line in re.split(r"[\r\n,]+", text):
        m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", line)
        if not m:
            continue
        tag = line.split("#", 1)[1].strip() if "#" in line else ""
        # 行里带 电信/联通/移动 就只算给对应的网:vipmc838 是整标签 "#电信",
        # CFYes/WeTest 是 "#CFYes优选 | 移动 | ..." 这种夹在中间的,同样能认出来;
        # 其他标签(比如天诚的 #天诚优选1 | HK)是地区/来源备注,不分网,不过滤
        tagged = [name for name, label in TAG_FOR.items() if label in tag]
        if tagged and carrier not in tagged:
            continue
        if in_cf(m.group(1)):
            out.append(m.group(1))
    return list(dict.fromkeys(out))


def build(carrier: str) -> list[str]:
    # 分网源权重 2,通用源权重 1:不然通用源会把三网的差异盖住,
    # 三网 Top 5 容易算成同一批 IP
    score: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        jobs = [(u, carrier, 2) for u in SOURCES[carrier]] + [
            (u, carrier, 1) for u in SOURCES["all"]
        ]
        futs = {pool.submit(fetch_one, (u, c)): w for u, c, w in jobs}
        for fut in as_completed(futs):
            w = futs[fut]
            for ip in fut.result():
                score[ip] = score.get(ip, 0) + w
    ranked = sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))
    return [ip for ip, _ in ranked[:TOP_N]]


def post_json(url: str, data: dict, timeout: int = 15, headers: dict | None = None):
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(
        url, method="POST", data=json.dumps(data).encode(), headers=h,
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def send_notifications(text: str):
    """TG + 自建微信通知,都是可选的:没配对应 Secret 就跳过,通知失败不影响主流程.
    TG:TG_BOT_TOKEN + TG_CHAT_ID (BotFather 建 bot 拿 token,先给 bot 发条消息,
        再用 @getmyid_bot 这类 bot 查自己的 chat_id)
    自建微信:WECHAT_API_URL + WECHAT_AUTH_TOKEN,你的接口
        https://wx.djcf.pp.ua/wxsend,按常见自建 wxsend 格式发:
        body {"token": token, "title": 标题, "content": 全文, "message": 全文},
        同时带 Authorization: Bearer <token> 头.
        如果你的接口字段名不一样(比如叫 msg/text/to),把格式发我,改一行就行.
    备选:WECHAT_WEBHOOK = 企业微信群机器人 webhook 完整 URL,也支持."""
    tg_token = os.environ.get("TG_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("TG_CHAT_ID", "").strip()
    if tg_token and tg_chat:
        try:
            post_json(f"https://api.telegram.org/bot{tg_token}/sendMessage",
                      {"chat_id": tg_chat, "text": text})
            print("TG 通知已发送")
        except Exception as e:  # noqa: BLE001 - 通知失败只告警,不让主流程失败
            print(f"[warn] TG 通知发送失败: {e}", file=sys.stderr)
    wx_url = os.environ.get("WECHAT_API_URL", "").strip()
    wx_token = os.environ.get("WECHAT_AUTH_TOKEN", "").strip()
    if wx_url:
        try:
            title = text.split("\n", 1)[0]
            headers = {"Authorization": f"Bearer {wx_token}"} if wx_token else None
            post_json(wx_url,
                      {"token": wx_token, "title": title, "content": text, "message": text},
                      headers=headers)
            print("微信通知已发送")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 微信通知发送失败: {e}", file=sys.stderr)
        return
    webhook = os.environ.get("WECHAT_WEBHOOK", "").strip()
    if webhook:
        try:
            post_json(webhook, {"msgtype": "text", "text": {"content": text}})
            print("微信通知已发送")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 微信通知发送失败: {e}", file=sys.stderr)


def notify_text(domain: str, target: str, results: dict, merged: list[str],
                ok: bool, err: str = "") -> str:
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    if not ok:
        return (f"❌ CF 三网优选更新失败 ({now} 北京时间)\n"
                f"域名: {target}\n错误: {err or '未知错误'}")
    lines = [f"✅ CF 三网优选更新成功 ({now} 北京时间)",
             f"域名: {target}",
             f"电信: {', '.join(results.get('ct') or ['(无)'])}",
             f"联通: {', '.join(results.get('cu') or ['(无)'])}",
             f"移动: {', '.join(results.get('cmcc') or ['(无)'])}",
             f"合计: {len(merged)} 个 IP"]
    return "\n".join(lines)


def cf_req(token: str, method: str, path: str, data=None):
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.load(r)
    if not body.get("success"):
        raise RuntimeError(f"Cloudflare API 失败 {method} {path}: {body}")
    return body["result"]


def update_dns(token: str, zone_id: str, name: str, ips: list[str]):
    existing = cf_req(token, "GET",
                      f"/zones/{zone_id}/dns_records?type=A&name={name}&per_page=100")
    for rec in existing:
        cf_req(token, "DELETE", f"/zones/{zone_id}/dns_records/{rec['id']}")
        print(f"  删除旧记录 {name} -> {rec['content']}")
    for ip in ips:
        cf_req(token, "POST", f"/zones/{zone_id}/dns_records",
               {"type": "A", "name": name, "content": ip, "ttl": TTL, "proxied": False})
        print(f"  新增记录 {name} -> {ip} (TTL {TTL}, DNS only)")


def main() -> int:
    print(f"cf-best {SCRIPT_VERSION}")
    dry_run = "--dry-run" in sys.argv
    domain = os.environ.get("CF_DOMAIN", "").strip()
    if not domain:
        print("缺少 CF_DOMAIN (你的域名,例如 example.com)", file=sys.stderr)
        return 2
    results = {}
    merged: list[str] = []
    for carrier in ("ct", "cu", "cmcc"):
        ips = build(carrier)
        results[carrier] = ips
        print(f"{carrier}: {', '.join(ips) if ips else '(没有拿到 IP)'}")
        for ip in ips:
            if ip not in merged:
                merged.append(ip)
    print(f"合并去重后写到一个域名: {', '.join(merged) if merged else '(没有拿到 IP)'}")
    target = os.environ.get("CF_HOSTNAME", "").strip() or domain
    if dry_run:
        print("dry-run:不改 DNS,也不发通知")
        return 0
    token = os.environ.get("CF_API_TOKEN", "").strip()
    zone_id = os.environ.get("CF_ZONE_ID", "").strip()
    if not token or not zone_id:
        print("缺少 CF_API_TOKEN 或 CF_ZONE_ID", file=sys.stderr)
        return 2
    if not merged:
        print("[warn] 三网都没有 IP,跳过,不删旧记录")
        return 0
    print(f"更新 {target}:")
    update_dns(token, zone_id, target, merged)
    text = notify_text(domain, target, results, merged, ok=True)
    with open("notify.txt", "w", encoding="utf-8") as f:
        f.write(text + "\n")
    send_notifications(text)
    print("完成")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as e:  # noqa: BLE001 - 失败也要发一条通知再退出
        domain = os.environ.get("CF_DOMAIN", "").strip()
        target = os.environ.get("CF_HOSTNAME", "").strip() or domain or "(未配置域名)"
        text = notify_text(domain, target, {}, [], ok=False, err=str(e))
        try:
            with open("notify.txt", "w", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError:
            pass
        send_notifications(text)
        raise
    if code != 0:
        domain = os.environ.get("CF_DOMAIN", "").strip()
        target = os.environ.get("CF_HOSTNAME", "").strip() or domain or "(未配置域名)"
        send_notifications(notify_text(domain, target, {}, [], ok=False,
                                       err=f"退出码 {code},见 Actions 日志"))
    raise SystemExit(code)

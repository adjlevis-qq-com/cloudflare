#!/usr/bin/env python3
"""三网 Cloudflare 优选 IP -> 更新到指定 Cloudflare 域名.

用法:
  本地 dry-run (只拉源、不改 DNS):
    CF_DOMAIN=example.com python3 update_cf_dns.py --dry-run
  GitHub Actions 里跑 (改 DNS):
    环境变量 CF_API_TOKEN / CF_ZONE_ID / CF_DOMAIN 由仓库 Secrets 提供

设计:
  - 拉多家上游源,按运营商分 ct(电信)/cu(联通)/cmcc(移动),去重,
    只留 Cloudflare 官方网段内的 IP,按多源出现次数排序,每网取前 N 个.
  - 写到指定域名下的 4 个子域名,全部 DNS only (灰云,不代理):
      ct.<domain>   -> 电信 Top N
      cu.<domain>   -> 联通 Top N
      cmcc.<domain> -> 移动 Top N
      cf.<domain>   -> 三网综合 Top N
  - 每次先删旧 A 记录,再建新 A 记录,TTL 60 秒.
  - 注意:GitHub Runner 在境外,测不出三网延迟;三网的"优选"属性来自
    上游源本身的分网实测,本脚本只做聚合+过滤+排序,不做本地测速.
"""
import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from ipaddress import ip_address, ip_network

SOURCES = {
    "ct": [
        "https://raw.githubusercontent.com/love-ztm/cfip/refs/heads/main/best_ips.txt",
        "https://raw.githubusercontent.com/RikkaSaiko1/CloudFlare-ip/refs/heads/main/ip.txt",
        "https://cf.090227.xyz/ct",
        "https://raw.githubusercontent.com/vipmc838/cf_best_ip/main/cloudflare_bestip.txt",
    ],
    "cu": [
        "https://raw.githubusercontent.com/love-ztm/cfip/refs/heads/main/ubest_ips.txt",
        "https://cf.090227.xyz/cu",
        "https://raw.githubusercontent.com/vipmc838/cf_best_ip/main/cloudflare_bestip.txt",
    ],
    "cmcc": [
        "https://raw.githubusercontent.com/svip-s/cloudflare_ip/refs/heads/main/best_ips.txt",
        "https://cf.090227.xyz/cmcc",
        "https://raw.githubusercontent.com/vipmc838/cf_best_ip/main/cloudflare_bestip.txt",
    ],
    "all": [
        "https://bestcf.pages.dev/cmliu/all.txt",
        "https://addressesapi.090227.xyz/CloudFlareYes",
        "https://ip.164746.xyz/ipTop10.html",
        "https://ipdb.api.030101.xyz/?type=bestcf&country=true",
        "https://raw.githubusercontent.com/joname1/BestCFip/main/ipv4.txt",
    ],
}
# 电信/联通/移动 -> 子域名前缀;改这里就能换子域名
SUBDOMAINS = {"ct": "ct", "cu": "cu", "cmcc": "cmcc", "all": "cf"}
TAG_FOR = {"ct": "电信", "cu": "联通", "cmcc": "移动"}

CF_CIDRS = [
    "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "104.16.0.0/13",
    "104.24.0.0/14", "108.162.192.0/18", "131.0.72.0/22", "141.101.64.0/18",
    "162.158.0.0/15", "172.64.0.0/13", "173.245.48.0/20", "188.114.96.0/20",
    "190.93.240.0/20", "197.234.240.0/22", "198.41.128.0/17",
]
CF_NETS = [ip_network(c) for c in CF_CIDRS]
TOP_N = int(os.environ.get("TOP_N", "3"))
TTL = int(os.environ.get("CF_TTL", "60"))
API = "https://api.cloudflare.com/client/v4"


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
        # vipmc838 那份带 #电信/#联通/#移动 标签,标签不对就跳过
        if tag and tag not in ("默认", "IPv6") and carrier in TAG_FOR:
            if tag != TAG_FOR[carrier]:
                continue
        if in_cf(m.group(1)):
            out.append(m.group(1))
    return list(dict.fromkeys(out))


def build(carrier: str) -> list[str]:
    urls = [(u, carrier) for u in SOURCES[carrier] + SOURCES["all"]]
    score: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        for lst in pool.map(fetch_one, urls):
            for ip in lst:
                score[ip] = score.get(ip, 0) + 1
    ranked = sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))
    return [ip for ip, _ in ranked[:TOP_N]]


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


def update_dns(token: str, zone_id: str, domain: str, carrier: str, ips: list[str]):
    name = f"{SUBDOMAINS[carrier]}.{domain}"
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
    dry_run = "--dry-run" in sys.argv
    domain = os.environ.get("CF_DOMAIN", "").strip()
    if not domain:
        print("缺少 CF_DOMAIN (你的域名,例如 example.com)", file=sys.stderr)
        return 2
    results = {}
    for carrier in ("ct", "cu", "cmcc", "all"):
        ips = build(carrier)
        results[carrier] = ips
        print(f"{carrier}: {', '.join(ips) if ips else '(没有拿到 IP)'}")
    if dry_run:
        print("dry-run:不改 DNS")
        return 0
    token = os.environ.get("CF_API_TOKEN", "").strip()
    zone_id = os.environ.get("CF_ZONE_ID", "").strip()
    if not token or not zone_id:
        print("缺少 CF_API_TOKEN 或 CF_ZONE_ID", file=sys.stderr)
        return 2
    for carrier, ips in results.items():
        if not ips:
            print(f"[warn] {carrier} 没有 IP,跳过,不删旧记录")
            continue
        print(f"更新 {SUBDOMAINS[carrier]}.{domain}:")
        update_dns(token, zone_id, domain, carrier, ips)
    print("完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""北师大羽毛球场空位监控：重放浏览器里复制的查询请求，发现新空位就发邮件。

架构思路参考 tennis-monitor（GitHub Actions 长跑轮询 + 缓存去重状态），
代码独立实现，只依赖 Python 3.8+ 标准库。用法见同目录 README.md。

    python monitor.py --test-email     # 发一封测试邮件
    python monitor.py --inspect        # 请求一次，打印返回结构，帮你填 parser
    python monitor.py --check          # 查一轮，只打印不发邮件
    python monitor.py --once           # 查一轮，有新空位就发邮件
    python monitor.py --loop           # 常驻轮询（自己电脑/服务器）
    python monitor.py --ci-loop 19800  # GitHub Actions 长跑模式，最多跑 N 秒
"""

import argparse
import gzip
import json
import os
import random
import re
import shlex
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

BEIJING = timezone(timedelta(hours=8))
HERE = Path(__file__).resolve().parent

DEFAULTS = {
    "request_file": "request.txt",
    "state_file": "state.json",
    "booking_url": "https://tycg.bnu.edu.cn/www/dd/vue/spa/zhcg#/",
    "date_in_request": "",
    "date_format": "%Y-%m-%d",
    "day_offsets": [0, 1, 2, 3],
    "release_time": "",
    "interval_seconds": 60,
    "active_hours": ["06:30", "23:30"],
    "burst_windows": [{"start": "06:58", "end": "07:20", "interval_seconds": 10}],
    "timeout_seconds": 15,
    "include_times": [],
    "include_courts": [],
    "time_range": [],
    "court_regex": "",
    "streak_min": 2,
    "notify_first_run": True,
    "failure_alert_after": 5,
    "daily_report_at": "",
    "parser": {},
    "smtp": {},
}

# 环境变量覆盖（GitHub Actions 用 repo secrets 注入）
ENV_SMTP = {
    "SMTP_HOST": "host", "SMTP_PORT": "port", "SMTP_USER": "user",
    "SMTP_PASSWORD": "password", "MAIL_TO": "to",
}
ENV_REQUEST = "BNU_REQUEST_CURL"

LOGIN_HINTS = ("authserver", "/cas/", "cas/login", "统一身份认证", "login.html", "用户登录")
LOGIN_MSG = re.compile(r"登录|登陆|未认证|token|会话|过期|unauthori[sz]ed|not\s*login", re.I)


def now():
    return datetime.now(BEIJING)


def log(msg):
    print(f"[{now():%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- 配置


def load_config(path):
    path = Path(path)
    if not path.exists():
        sys.exit(f"找不到配置文件 {path}")
    cfg = dict(DEFAULTS)
    cfg.update(json.loads(path.read_text(encoding="utf-8")))
    base = path.resolve().parent
    for key in ("request_file", "state_file"):
        p = Path(cfg[key])
        cfg[key] = p if p.is_absolute() else base / p
    cfg["smtp"] = dict(cfg["smtp"])
    for env, key in ENV_SMTP.items():
        if os.environ.get(env):
            cfg["smtp"][key] = os.environ[env].strip()
    if isinstance(cfg["smtp"].get("to"), str) and "," in cfg["smtp"]["to"]:
        cfg["smtp"]["to"] = [x.strip() for x in cfg["smtp"]["to"].split(",") if x.strip()]
    return cfg


# ---------------------------------------------------------------- cURL 解析

_FLAGS_NO_VALUE = {
    "--compressed", "-L", "--location", "-k", "--insecure", "-s", "--silent",
    "-i", "--include", "-v", "--verbose", "-g", "--globoff", "--http1.1", "--http2",
}


def parse_curl(text):
    """解析浏览器「复制为 cURL (bash)」得到的命令。"""
    text = text.strip()
    text = re.sub(r"\\\r?\n", " ", text)          # 行尾续行符
    text = re.sub(r"(^|\s)\$'", r"\1'", text)      # $'...' 写法
    tokens = shlex.split(text)
    if not tokens or tokens[0] != "curl":
        raise ValueError("请求内容应以 curl 开头（请用「复制为 cURL (bash)」，不要选 cmd/PowerShell）")

    req = {"method": None, "url": None, "headers": {}, "data": None}
    data_parts = []
    i = 1
    while i < len(tokens):
        t = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if t in ("-X", "--request"):
            req["method"] = nxt.upper()
            i += 2
        elif t in ("-H", "--header"):
            name, _, value = nxt.partition(":")
            req["headers"][name.strip()] = value.strip()
            i += 2
        elif t in ("-b", "--cookie"):
            req["headers"]["Cookie"] = nxt
            i += 2
        elif t in ("-A", "--user-agent"):
            req["headers"]["User-Agent"] = nxt
            i += 2
        elif t in ("-e", "--referer"):
            req["headers"]["Referer"] = nxt
            i += 2
        elif t in ("-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--data-urlencode"):
            data_parts.append(nxt)
            i += 2
        elif t == "--url":
            req["url"] = nxt
            i += 2
        elif t in _FLAGS_NO_VALUE or t.startswith("-"):
            i += 1
        else:
            req["url"] = req["url"] or t
            i += 1
    if not req["url"]:
        raise ValueError("cURL 里没有找到 URL")
    if data_parts:
        req["data"] = "&".join(data_parts)
    req["method"] = req["method"] or ("POST" if data_parts else "GET")
    # 压缩自己处理，避免收到标准库解不了的 br/zstd
    for k in list(req["headers"]):
        if k.lower() in ("accept-encoding", "content-length", "host"):
            del req["headers"][k]
    req["headers"]["Accept-Encoding"] = "gzip, deflate"
    return req


def substitute(req, old, new):
    if not old:
        return req
    out = dict(req)
    out["url"] = req["url"].replace(old, new)
    out["data"] = req["data"].replace(old, new) if req["data"] else req["data"]
    out["headers"] = {k: v.replace(old, new) for k, v in req["headers"].items()}
    return out


# ---------------------------------------------------------------- HTTP


class SessionExpired(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # 接口 302 多半是被踢回统一身份认证登录页


_opener = urllib.request.build_opener(_NoRedirect)


def fetch(req, timeout):
    body = req["data"].encode("utf-8") if req["data"] is not None else None
    r = urllib.request.Request(req["url"], data=body, headers=req["headers"], method=req["method"])
    try:
        with _opener.open(r, timeout=timeout) as resp:
            raw = resp.read()
            enc = (resp.headers.get("Content-Encoding") or "").lower()
            charset = resp.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308, 401):
            raise SessionExpired(f"HTTP {e.code} {e.headers.get('Location', '')}") from e
        raise
    if enc == "gzip":
        raw = gzip.decompress(raw)
    elif enc == "deflate":
        raw = zlib.decompress(raw)
    text = raw.decode(charset, errors="replace")
    if text.lstrip().startswith("<") and any(h in text[:5000] for h in LOGIN_HINTS):
        raise SessionExpired("返回了登录页面")
    return text


# ---------------------------------------------------------------- 解析空位


def _walk(node, parts, parents):
    """按 a.b.*.c 路径展开，产出 (元素, 父级链)。* 表示遍历列表/字典的每一项。"""
    if not parts:
        yield node, parents
        return
    head, rest = parts[0], parts[1:]
    if head == "*":
        items = node.values() if isinstance(node, dict) else (node if isinstance(node, list) else [])
        for child in items:
            yield from _walk(child, rest, parents + [node])
    elif isinstance(node, dict) and head in node:
        yield from _walk(node[head], rest, parents + [node])
    elif isinstance(node, list) and head.isdigit() and int(head) < len(node):
        yield from _walk(node[int(head)], rest, parents + [node])


def _get(item, parents, spec):
    """取字段，支持 a.b 嵌套；开头每个 ../ 往上一层对象（例如场地名在外层）。"""
    node = item
    chain = [p for p in parents if isinstance(p, dict)]
    while spec.startswith("../"):
        spec = spec[3:]
        node = chain.pop() if chain else {}
    for part in spec.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node


def _matches(value, rule):
    s = str(value)
    if "in" in rule:
        return s in [str(v) for v in rule["in"]]
    if "not_in" in rule:
        return s not in [str(v) for v in rule["not_in"]]
    if "equals" in rule:
        return s == str(rule["equals"])
    if "gt" in rule:
        try:
            return float(value) > float(rule["gt"])
        except (TypeError, ValueError):
            return False
    if "regex" in rule:
        return re.search(rule["regex"], s) is not None
    raise ValueError(f"规则看不懂: {rule}")


def _check_ok(data, parser):
    """按 parser.ok_when 判断接口是否返回成功；失败时区分「登录失效」和其他错误。"""
    rule = parser.get("ok_when")
    if not rule or _matches(_get(data, [], rule["field"]), rule):
        return
    msg = _get(data, [], parser.get("message_field", "msg")) or _get(data, [], "message") or ""
    detail = f"{rule['field']}={_get(data, [], rule['field'])} {msg}".strip()
    if LOGIN_MSG.search(str(msg)):
        raise SessionExpired(detail)
    raise RuntimeError(f"接口返回失败：{detail}")


def extract_free_slots(text, parser, date_label):
    """返回空位列表 [{date, time, court, ...}]。"""
    if parser.get("mode") == "regex":
        slots = []
        for m in re.finditer(parser["pattern"], text, re.S):
            slot = {k: v for k, v in m.groupdict().items() if v is not None}
            slot.setdefault("date", date_label)
            slots.append(slot)
        return slots

    try:
        data = json.loads(text)
    except ValueError:
        raise RuntimeError(f"返回的不是 JSON：{text[:200]!r}")
    _check_ok(data, parser)
    rule = parser.get("free_when")
    if not parser.get("list_path") or not rule:
        raise ValueError("config.json 里 parser.list_path / parser.free_when 还没填，先运行 --inspect")
    rules = rule if isinstance(rule, list) else [rule]
    slots = []
    for item, parents in _walk(data, parser["list_path"].split("."), []):
        if not isinstance(item, dict):
            continue
        if not all(_matches(_get(item, parents, r["field"]), r) for r in rules):
            continue
        slot = {name: _get(item, parents, spec) for name, spec in parser.get("fields", {}).items()}
        slot = {k: ("" if v is None else str(v)) for k, v in slot.items()}
        if not slot.get("date"):
            slot["date"] = date_label
        slots.append(slot)
    return slots


def slot_key(slot):
    return " | ".join(f"{k}={slot[k]}" for k in sorted(slot))


DAY_TAGS = {0: "今天", 1: "明天", 2: "后天", 3: "大后天"}


def _span(time_str):
    """'08:00-09:00' -> (480, 540)，单位分钟；解析不了返回 None。"""
    m = re.findall(r"(\d{1,2}):(\d{2})", time_str or "")
    if len(m) < 2:
        return None
    (h1, m1), (h2, m2) = m[:2]
    return int(h1) * 60 + int(m1), int(h2) * 60 + int(m2)


def _hm(mins):
    h, m = divmod(mins, 60)
    return f"{h}" if m == 0 else f"{h}:{m:02d}"


def short_time(time_str):
    """'08:00-09:00' -> '8-9点'。"""
    span = _span(time_str)
    return f"{_hm(span[0])}-{_hm(span[1])}点" if span else time_str


def date_label(date_str, t=None):
    """'2026-10-14' -> '2026-10-14（后天）'。"""
    t = t or now()
    try:
        d = datetime.strptime(re.sub(r"\D", "", date_str)[:8], "%Y%m%d").date()
    except ValueError:
        return date_str
    tag = DAY_TAGS.get((d - t.date()).days)
    return f"{date_str}（{tag}）" if tag else date_str


def slot_text(slot):
    main = [date_label(slot.get("date", "")), short_time(slot.get("time", "")), slot.get("court", "")]
    extra = [f"{k}:{v}" for k, v in slot.items() if k not in ("date", "time", "court", "where") and v]
    return " ".join(x for x in main + extra if x)


def _order(slot):
    """排序：日期 → 场地号（羽2 在 羽10 前）→ 开始时间。"""
    court = slot.get("court", "")
    num = re.findall(r"\d+", court)
    return (slot.get("date", ""), court.rstrip("0123456789"), int(num[-1]) if num else 0,
            (_span(slot.get("time")) or (0, 0))[0])


def find_runs(free, min_len):
    """同一天、同一场地、时间首尾相接的空场连成一段，返回长度 >= min_len 的段（每段是 key 列表）。"""
    groups = {}
    for k, slot in free.items():
        span = _span(slot.get("time"))
        if span:
            groups.setdefault((slot.get("date"), slot.get("court")), []).append((span, k))
    runs = []
    for items in groups.values():
        items.sort()
        cur = [items[0]]
        for it in items[1:]:
            if it[0][0] == cur[-1][0][1]:
                cur.append(it)
                continue
            if len(cur) >= min_len:
                runs.append(cur)
            cur = [it]
        if len(cur) >= min_len:
            runs.append(cur)
    return [[k for _, k in r] for r in runs]


def run_text(free, keys):
    first, last = free[keys[0]], free[keys[-1]]
    start, end = _span(first["time"])[0], _span(last["time"])[1]
    hours = (end - start) / 60
    hours = int(hours) if hours == int(hours) else round(hours, 1)
    return f"{date_label(first.get('date', ''))} {_hm(start)}-{_hm(end)}点 {first.get('court', '')}（连续{hours}小时）"


def compose_email(free, new_keys, first, min_run):
    """新空场的邮件 (标题, 正文)。新空场和已有空场连成 >= min_run 场时，作为特别提醒。"""
    new = set(new_keys)
    runs = [r for r in find_runs(free, min_run) if new & set(r)]
    runs.sort(key=lambda r: (-len(r), _order(free[r[0]])))
    in_run = {k for r in runs for k in r}
    singles = sorted((k for k in new if k not in in_run), key=lambda k: _order(free[k]))
    label = "当前空场" if first else "新空场"
    parts = []
    if runs:
        parts.append(f"🔥 连续{min_run}场及以上（{len(runs)} 段）：\n" + "\n".join("  • " + run_text(free, r) for r in runs))
    if singles:
        head = "其他" + label if runs else label
        parts.append(f"{head}（{len(singles)} 个）：\n" + "\n".join("  • " + slot_text(free[k]) for k in singles))
    if runs:
        more = f" 等{len(runs)}段" if len(runs) > 1 else ""
        subject = f"🔥 特别提醒·连续场：{run_text(free, runs[0])}{more}"
    else:
        more = f" 等{len(singles)}个" if len(singles) > 1 else ""
        subject = f"🏸 {label}：{slot_text(free[singles[0]])}{more}"
    return subject, "\n\n".join(parts)


def already_started(slot, t=None):
    """今天、开始时间已过的时段不算（有些系统过去的时段不置灰）。"""
    t = t or now()
    if re.sub(r"\D", "", slot.get("date", ""))[:8] != t.strftime("%Y%m%d"):
        return False
    m = re.search(r"(\d{1,2}):(\d{2})", slot.get("time", ""))
    return bool(m) and int(m.group(1)) * 60 + int(m.group(2)) <= t.hour * 60 + t.minute


def wanted(slot, cfg):
    if already_started(slot):
        return False
    t, c = slot.get("time", ""), slot.get("court", "")
    if cfg["time_range"]:
        span = _span(t)
        lo, hi = (_minutes(x) for x in cfg["time_range"])
        if not span or span[0] < lo or span[1] > hi:
            return False
    if cfg["court_regex"] and not re.search(cfg["court_regex"], c):
        return False
    if cfg["include_times"] and not any(x in t for x in cfg["include_times"]):
        return False
    if cfg["include_courts"] and not any(x in c for x in cfg["include_courts"]):
        return False
    return True


# ---------------------------------------------------------------- 邮件


def send_email(cfg, subject, body):
    smtp = cfg["smtp"]
    if smtp.get("dry_run"):
        log(f"[dry_run，不真正发送] {subject}\n{body}")
        return
    for k in ("host", "user", "password"):
        if not smtp.get(k):
            raise RuntimeError(f"邮箱没配置 {k}（config.json 的 smtp，或环境变量 SMTP_*）")
    to = smtp.get("to") or smtp["user"]
    to_list = [x.strip() for x in (to if isinstance(to, list) else [to]) if x.strip()]
    sender = smtp["user"].strip()
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr(("羽毛球场监控", sender))
    msg["To"] = ", ".join(to_list)
    # 缺 Date / Message-ID 的邮件容易被收件方当成垃圾邮件
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2] or None)
    port = int(smtp.get("port") or 465)
    ctx = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(smtp["host"], port, context=ctx, timeout=20)
    else:
        server = smtplib.SMTP(smtp["host"], port, timeout=20)
        server.starttls(context=ctx)
    with server:
        server.login(sender, smtp["password"])
        code, resp = server.mail(sender)
        if code != 250:
            raise smtplib.SMTPSenderRefused(code, resp, sender)
        for r in to_list:
            code, resp = server.rcpt(r)
            if code not in (250, 251):
                raise smtplib.SMTPRecipientsRefused({_mask(r): (code, resp)})
        code, resp = server.data(msg.as_string())
        if code != 250:
            raise smtplib.SMTPDataError(code, resp)
    reply = resp.decode("utf-8", "replace") if isinstance(resp, bytes) else str(resp)
    log(f"已发邮件：{subject}（{_mask(sender)} → {', '.join(map(_mask, to_list))}，服务器回复 {code} {reply}）")


def _mask(addr):
    """日志是公开的，邮箱只露前两位和域名。"""
    name, _, domain = addr.partition("@")
    return f"{name[:2]}***@{domain}" if domain else "***"


def notify(cfg, subject, body):
    """发信失败不能把监控搞挂，记日志即可。"""
    try:
        send_email(cfg, subject, body)
        return True
    except Exception as e:
        log(f"邮件发送失败：{type(e).__name__}: {e}")
        return False


# ---------------------------------------------------------------- 状态


def load_state(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def save_state(path, state):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- 一轮检查


def target_dates(cfg, t=None):
    """要查的日期。放场时间(release_time)之前不查最远的那天，免得把未放出的场当成空位。"""
    t = t or now()
    last = max(cfg["day_offsets"])
    rel = cfg.get("release_time")
    offsets = [d for d in cfg["day_offsets"] if not (rel and d == last and t.strftime("%H:%M") < rel)]
    return [(t.date() + timedelta(days=d)).strftime(cfg["date_format"]) for d in offsets]


def load_request(cfg):
    text = os.environ.get(ENV_REQUEST)
    if not text:
        path = cfg["request_file"]
        if not Path(path).exists():
            sys.exit(f"找不到 {path}，也没有环境变量 {ENV_REQUEST}。"
                     "在浏览器 F12 → 网络 里右键查询请求 →「复制为 cURL (bash)」")
        text = Path(path).read_text(encoding="utf-8")
    return parse_curl(text)


def query_all(cfg):
    """返回 (空位字典, 查询失败的日期)。只有全部日期都失败才抛异常。"""
    base = load_request(cfg)
    slots, failed, last_err = [], [], None
    dates = target_dates(cfg) if cfg["date_in_request"] else [""]
    for d in dates:
        try:
            text = fetch(substitute(base, cfg["date_in_request"], d), cfg["timeout_seconds"])
            slots += extract_free_slots(text, cfg["parser"], d)
        except SessionExpired:
            raise
        except Exception as e:
            log(f"{d or '查询'} 失败：{type(e).__name__}: {e}")
            failed.append(d)
            last_err = e
    if dates and len(failed) == len(dates):
        raise last_err
    return {slot_key(s): s for s in slots if wanted(s, cfg)}, failed


def run_once(cfg, st, send=True):
    """查一轮并更新状态。返回 True 表示查询成功。"""
    stamp = now().strftime("%Y-%m-%d %H:%M:%S")
    today = now().strftime("%Y-%m-%d")
    counts = st.setdefault("counts", {})
    counts[today] = counts.get(today, 0) + 1
    for d in [d for d in counts if d < today]:
        counts.pop(d)

    try:
        free, failed = query_all(cfg)
    except SessionExpired as e:
        log(f"登录失效：{e}")
        if send and st.get("alerted") != "session":
            if notify(cfg, "⚠️ 羽毛球监控：登录已失效，需要更新请求",
                      "预约系统提示未登录，监控暂时看不到场地。\n\n"
                      "请重新登录预约系统，按 README「更新登录状态」重新复制 cURL，\n"
                      f"更新 GitHub Secret {ENV_REQUEST}（或本地 request.txt）。\n\n"
                      f"详情：{e}\n时间：{stamp}"):
                st["alerted"] = "session"
        return False
    except Exception as e:
        st["failures"] = st.get("failures", 0) + 1
        log(f"查询失败（连续 {st['failures']} 次）：{type(e).__name__}: {e}")
        if send and st["failures"] >= cfg["failure_alert_after"] and st.get("alerted") != "failure":
            if notify(cfg, "⚠️ 羽毛球监控：连续查询失败",
                      f"已经连续失败 {st['failures']} 次，最近一次错误：\n{type(e).__name__}: {e}\n\n"
                      "可能原因：学校网站在维护、接口改了，或者屏蔽了境外 IP（GitHub Actions 在海外）。"):
                st["alerted"] = "failure"
        return False

    if st.get("alerted"):
        log("恢复正常")
        if send:
            notify(cfg, "✅ 羽毛球监控已恢复", f"查询恢复正常。\n时间：{stamp}")
    st["failures"] = 0
    st["alerted"] = None

    old = st.get("free", {})
    first = "free" not in st
    # 这轮没查成功的日期，沿用上一轮的结果，免得下一轮把它们当成「新空位」
    kept = {k: v for k, v in old.items() if any(f"date={d}" in k.split(" | ") for d in failed)}
    new_keys = sorted(k for k in free if k not in old)
    gone = [k for k in old if k not in free and k not in kept]
    log(f"当前空位 {len(free)} 个，新增 {len(new_keys)}，被订走 {len(gone)}")
    for k in new_keys:
        log(f"  + {slot_text(free[k])}")

    to_notify = new_keys if (not first or cfg["notify_first_run"]) else []
    if send and to_notify:
        subject, body = compose_email(free, to_notify, first, cfg["streak_min"])
        if not notify(cfg, subject, body):
            # 没发出去就别记成「已知」，下一轮还会再提醒
            free = {k: v for k, v in free.items() if k in old}

    if send:
        maybe_daily_report(cfg, st, free)
    st["free"] = {k: old.get(k, stamp) for k in free}  # 值 = 首次发现时间
    st["free"].update(kept)
    return True


def maybe_daily_report(cfg, st, free):
    at = cfg.get("daily_report_at")
    today = now().strftime("%Y-%m-%d")
    if not at or now().strftime("%H:%M") < at or st.get("report_date") == today:
        return
    n = st.get("counts", {}).get(today, 0)
    lines = "\n".join("  · " + slot_text(s) for s in free.values()) or "  （无）"
    if notify(cfg, "✅ 羽毛球监控日报", f"今天已检查 {n} 轮，监控正常。\n\n当前空位：\n{lines}"):
        st["report_date"] = today


# ---------------------------------------------------------------- 调度


def _minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_active_hours(cfg, t=None):
    t = t or now()
    start, end = (_minutes(x) for x in cfg["active_hours"])
    return start <= t.hour * 60 + t.minute < end


def current_interval(cfg):
    mins = now().hour * 60 + now().minute
    for w in cfg["burst_windows"]:
        if _minutes(w["start"]) <= mins < _minutes(w["end"]):
            return w["interval_seconds"]
    return cfg["interval_seconds"]


def avoid_burst_handover(cfg, start_ts, max_seconds):
    """别在放场冲刺时交班：本班若会在冲刺开始前 15 分钟到冲刺结束之间结束，就提前到冲刺前 18 分钟收工，
    让下一班在冲刺前就位。返回调整后的 max_seconds。"""
    end = datetime.fromtimestamp(start_ts + max_seconds, BEIJING)
    for w in cfg["burst_windows"]:
        if w["interval_seconds"] >= cfg["interval_seconds"]:
            continue  # 只管加速的窗口（凌晨减速的不算）
        ws = end.replace(hour=_minutes(w["start"]) // 60, minute=_minutes(w["start"]) % 60, second=0, microsecond=0)
        we = end.replace(hour=_minutes(w["end"]) // 60, minute=_minutes(w["end"]) % 60, second=0, microsecond=0)
        if ws - timedelta(minutes=15) <= end < we:
            return max(60, int((ws - timedelta(minutes=18)).timestamp() - start_ts))
    return max_seconds


def loop(cfg, max_seconds=None):
    """持续轮询。max_seconds 用于 GitHub Actions：到点就退出，交给下一班接力。"""
    start = time.time()
    if max_seconds:
        max_seconds = avoid_burst_handover(cfg, start, max_seconds)
    st = load_state(cfg["state_file"])
    if max_seconds:
        until = datetime.fromtimestamp(start + max_seconds, BEIJING)
        log(f"开始监控（本班到 {until:%m-%d %H:%M} 交班）")
    else:
        log("开始监控（Ctrl+C 退出）")
    while True:
        left = (max_seconds - (time.time() - start)) if max_seconds else None
        if left is not None and left <= 0:
            log("本班时长用完，交给下一班")
            break
        if not in_active_hours(cfg):
            mins = now().hour * 60 + now().minute
            to_open = (_minutes(cfg["active_hours"][0]) - mins) % (24 * 60) * 60
            if left is not None and to_open > left:
                log("距监控时段开始太久，本班退出，等下一班")
                break
            nap = min(600, to_open, left or 600)
            log(f"不在监控时段 {cfg['active_hours'][0]}-{cfg['active_hours'][1]}，等 {nap // 60} 分钟")
            time.sleep(max(1, nap))
            continue
        try:
            run_once(cfg, st)
        except Exception as e:  # 兜底，别让单轮异常终止长跑
            log(f"本轮异常：{type(e).__name__}: {e}")
        save_state(cfg["state_file"], st)
        wait = current_interval(cfg) * random.uniform(0.85, 1.15)
        if left is not None:
            wait = min(wait, max(1, max_seconds - (time.time() - start)))
        time.sleep(wait)
    save_state(cfg["state_file"], st)


# ---------------------------------------------------------------- --inspect


def _describe(node, path="", out=None, depth=0):
    """找出 JSON 里所有「由对象组成的数组」，这通常就是场地/时段列表。"""
    out = [] if out is None else out
    if depth > 8:
        return out
    if isinstance(node, list):
        dicts = [x for x in node if isinstance(x, dict)]
        if dicts and all(p != (path or "(根)") for p, _, _ in out):
            out.append((path or "(根)", len(node), dicts[0]))
        for x in node[:3]:
            _describe(x, f"{path}.*" if path else "*", out, depth + 1)
    elif isinstance(node, dict):
        for k, v in node.items():
            _describe(v, f"{path}.{k}" if path else k, out, depth + 1)
    return out


def inspect(cfg):
    base = load_request(cfg)
    d = target_dates(cfg)[-1] if cfg["date_in_request"] else ""
    req = substitute(base, cfg["date_in_request"], d)
    print(f"请求：{req['method']} {req['url']}" + (f"（日期替换为 {d}）" if d else ""))
    try:
        text = fetch(req, cfg["timeout_seconds"])
    except SessionExpired as e:
        sys.exit(f"登录失效：{e}\n请重新登录后重新复制 cURL。")
    try:
        data = json.loads(text)
    except ValueError:
        print("返回的不是 JSON（可能是 HTML），前 1500 字：\n")
        print(text[:1500])
        return
    print("\n返回内容（截断）：")
    print(json.dumps(data, ensure_ascii=False, indent=1)[:2500])
    found = _describe(data)
    if found:
        print("\nlist_path 候选：")
        for path, n, sample in found:
            print(f"  {path!r}  共 {n} 项，第一项：{json.dumps(sample, ensure_ascii=False)[:300]}")
    if cfg["parser"].get("list_path"):
        try:
            slots = extract_free_slots(text, cfg["parser"], d)
            print(f"\n按当前 parser 解析出空位 {len(slots)} 个：")
            for s in slots[:30]:
                print("  • " + slot_text(s))
        except Exception as e:
            print(f"\n按当前 parser 解析出错：{e}")


def sample_alerts(cfg):
    """按真实提醒的格式各发一封：普通空场、连续场。标题加 [测试]，免得当成真空场去抢。"""
    today = now().date()
    d1 = (today + timedelta(days=1)).strftime(cfg["date_format"])
    d3 = (today + timedelta(days=3)).strftime(cfg["date_format"])
    note = "\n\n（测试邮件，不是真实空场）"
    single = {"date": d1, "time": "11:00-12:00", "court": "羽1"}
    subject, body = compose_email({slot_key(single): single}, [slot_key(single)], False, cfg["streak_min"])
    send_email(cfg, "[测试] " + subject, body + note)
    old = {"date": d3, "time": "19:00-20:00", "court": "羽2"}
    new = [{"date": d3, "time": "20:00-21:00", "court": "羽2"}, {"date": d3, "time": "10:00-11:00", "court": "羽5"}]
    free = {slot_key(x): x for x in [old] + new}
    subject, body = compose_email(free, [slot_key(x) for x in new], False, cfg["streak_min"])
    send_email(cfg, "[测试] " + subject, body + note)


def test_email_body(cfg):
    """测试邮件里附上两种提醒的格式示例。"""
    today = now().date()
    d1 = (today + timedelta(days=1)).strftime(cfg["date_format"])
    d3 = (today + timedelta(days=3)).strftime(cfg["date_format"])
    sample = [
        {"date": d1, "time": "11:00-12:00", "court": "羽1"},
        {"date": d3, "time": "19:00-20:00", "court": "羽2"},
        {"date": d3, "time": "20:00-21:00", "court": "羽2"},
        {"date": d3, "time": "10:00-11:00", "court": "羽5"},
    ]
    free = {slot_key(x): x for x in sample}
    k = list(free)
    s1, b1 = compose_email({k[0]: free[k[0]]}, [k[0]], False, cfg["streak_min"])
    s2, b2 = compose_email(free, k[1:], False, cfg["streak_min"])
    m = re.fullmatch(r"\^(\D+)\[(\d)-(\d)\]\$", cfg["court_regex"] or "")
    courts = f"{m[1]}{m[2]}-{m[1]}{m[3]}" if m else cfg["court_regex"]
    rules = [f"只看 {courts}" if courts else "",
             f"只看 {cfg['time_range'][0]}-{cfg['time_range'][1]}" if cfg["time_range"] else "",
             f"同一场地连续 ≥{cfg['streak_min']} 场为特别提醒"]
    return ("收到这封邮件说明邮箱配置正确。\n"
            f"当前规则：{'；'.join(x for x in rules if x)}；同一个空场只提醒一次。\n\n"
            "下面是提醒格式示例（不是真实空场）：\n\n"
            f"━━ 普通提醒 ━━\n标题：{s1}\n{b1}\n\n"
            f"━━ 特别提醒 ━━\n标题：{s2}\n{b2}")


# ---------------------------------------------------------------- 入口


def main():
    ap = argparse.ArgumentParser(description="北师大羽毛球场空位监控")
    ap.add_argument("-c", "--config", default=str(HERE / "config.json"))
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--test-email", action="store_true", help="发送测试邮件")
    g.add_argument("--sample-alerts", action="store_true", help="按真实格式发普通、连续场提醒各一封（标题带 [测试]）")
    g.add_argument("--inspect", action="store_true", help="打印返回结构，帮你填 parser")
    g.add_argument("--check", action="store_true", help="查一轮，只打印不发邮件")
    g.add_argument("--once", action="store_true", help="查一轮，有新空位就发邮件")
    g.add_argument("--loop", action="store_true", help="常驻轮询")
    g.add_argument("--ci-loop", type=int, metavar="SECONDS", help="GitHub Actions 长跑模式")
    args = ap.parse_args()
    cfg = load_config(args.config)

    if args.test_email:
        send_email(cfg, "🏸 羽毛球监控测试邮件", test_email_body(cfg))
    elif args.sample_alerts:
        sample_alerts(cfg)
    elif args.inspect:
        inspect(cfg)
    elif args.check:
        ok = run_once(cfg, {}, send=False)
        sys.exit(0 if ok else 1)
    elif args.once:
        st = load_state(cfg["state_file"])
        ok = run_once(cfg, st)
        save_state(cfg["state_file"], st)
        sys.exit(0 if ok else 1)
    else:
        try:
            loop(cfg, args.ci_loop)
        except KeyboardInterrupt:
            log("已退出")


if __name__ == "__main__":
    main()

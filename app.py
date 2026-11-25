#!/usr/bin/env python3
"""我的个人工作台 · 单文件 Python 工作台（零依赖，只用标准库）
用法: python3 app.py 18080
首次使用：先把下面的 PASSWORD / WORK_PASSWORD 改掉（或设环境变量 WB_PASSWORD / WB_WORK_PASSWORD），
也可以启动后打开 /change-password 修改（会存入 auth.json，下次启动沿用）。
数据: 同目录 data.json {"briefs": [...], "news": [...], "rankings": [...]}
同步: POST /api/push {"password": "...", "brief": {...}}  按 broadcast_date 去重写入 data.json
开源说明：本仓库是作者自用工作台的脱敏版；真实数据文件（data.json、auth.json、*.db 等）不会进仓库，
带 .sample.json 后缀的是示例数据文件，照着格式写自己的即可。
"""
import fcntl
import gzip
import base64, json, os, re, sys, hashlib, time, calendar, urllib.request, uuid, threading, queue, shutil, socket, math
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE, "data.json")
PASSWORD = os.environ.get("WB_PASSWORD", "changeme")  # 主口令：首次使用务必改掉；也可设环境变量 WB_PASSWORD，或在 /change-password 里改（存入 auth.json）
AUTH_SALT = "workbench-v1"
HIRED = None  # 你的入职日，示例：date(2024, 3, 25)；留空则不显示时光卡
CONTRACT_END = None  # 合同到期日，示例：date(2027, 3, 25)；留空则不显示时光卡

# ---- 每日背景图（Bing 每日壁纸，自然风光，每天换一张）----
BG_CACHE_FILE = os.path.join(BASE, "daily_bg.json")
BING_API = "https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=1&mkt=zh-CN"

def get_daily_bg():
    """返回今日背景图 {url, title, copyright}；失败返回缓存或 None。按日期缓存，每天只取一次。"""
    today = date.today().isoformat()
    cached = None
    try:
        with open(BG_CACHE_FILE, encoding="utf-8") as f:
            cached = json.load(f)
    except Exception:
        cached = None
    if cached and cached.get("date") == today and cached.get("url"):
        return cached
    try:
        req = urllib.request.Request(BING_API, headers={"User-Agent": "Mozilla/5.0"})
        d = json.load(urllib.request.urlopen(req, timeout=8))
        img = (d.get("images") or [{}])[0]
        u = img.get("url", "")
        if u:
            if u.startswith("/"):
                u = "https://www.bing.com" + u
            info = {"date": today, "url": u, "title": img.get("title", ""), "copyright": img.get("copyright", "")}
            try:
                with open(BG_CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump(info, f, ensure_ascii=False)
            except Exception:
                pass
            return info
    except Exception:
        pass
    return cached  # 取不到就用旧缓存；都没有则返回 None（无背景图）

# ---- Agent 聊天中枢 v1 ----
CHAT_DIR = os.environ.get("WB_CHAT_DIR", os.path.join(BASE, "chat"))  # Agent 聊天历史目录（不存在会自动创建）
CHAT_HISTORY_DIR = os.path.join(CHAT_DIR, "history")
CHAT_LOCK = os.path.join(CHAT_DIR, "history.lock")
# Agent 名单：(id, 显示名, 首字母, 头像 class, 职责说明)
# 主 Agent（rumi）与平级调度者（atlas）固定；子 Agent 从 agents/ 工位子文件夹动态读，
# 每个子文件夹一个子 Agent，按目录创建时间排（先建的在前，新成员排最后）。
# 新成员到岗只需建 agents/<id>/（status.json + quota.json + heartbeat.json），
# 聊天列表、指挥中心、原理页三处自动出现，不用再改代码。
FIXED_CHAT_AGENTS = [
    ("rumi", "Rumi", "R", "rumi", "主 Agent · 拆单、验收、汇报"),
    ("atlas", "atlas", "A", "m1", "平级调度者"),
]
BUS_DIR = os.environ.get("WB_BUS_DIR", os.path.join(BASE, "agent-bus"))  # 子 Agent 状态目录（工单池状态文件放这里）

_SUB_AGENT_CACHE = {"ts": 0.0, "subs": None}

def scan_sub_agents():
    """读 agent-bus/agents/ 下的子文件夹：每个子文件夹一个子 Agent。
    读 status.json（角色/状态/尾网）与 quota.json（额度）；15 秒缓存。"""
    now = time.time()
    if _SUB_AGENT_CACHE["subs"] is not None and now - _SUB_AGENT_CACHE["ts"] < 15:
        return _SUB_AGENT_CACHE["subs"]
    out = []
    base = os.path.join(BUS_DIR, "agents")
    try:
        names = os.listdir(base)
    except Exception:
        names = []
    fixed_ids = {a[0] for a in FIXED_CHAT_AGENTS}
    dirs = []
    for n in names:
        if n in fixed_ids:
            continue
        p = os.path.join(base, n)
        try:
            if os.path.isdir(p):
                dirs.append((os.stat(p).st_mtime, n))
        except Exception:
            pass
    dirs.sort()
    for _, n in dirs:
        st = {}
        try:
            with open(os.path.join(base, n, "status.json"), encoding="utf-8") as f:
                st = json.load(f) or {}
        except Exception:
            pass
        q = {}
        try:
            with open(os.path.join(base, n, "quota.json"), encoding="utf-8") as f:
                q = json.load(f) or {}
        except Exception:
            pass
        out.append({"id": n, "name": n, "role": st.get("role") or "子 Agent",
                    "status": st.get("status") or "待签到", "tailnet": st.get("tailnet"),
                    "last_seen": st.get("last_seen"), "quota": q})
    _SUB_AGENT_CACHE["ts"] = now
    _SUB_AGENT_CACHE["subs"] = out
    return out

def chat_agent_list():
    """聊天名单：固定主 Agent + 平级调度者 + 工位子文件夹里的子 Agent（新成员排最后）。"""
    out = list(FIXED_CHAT_AGENTS)
    for s in scan_sub_agents():
        out.append((s["id"], s["name"], (s["name"][:1] or "?").upper(), s["id"], "子 Agent · 领单执行"))
    return out

# 18 个板块：(分组, 板块id, 标题, 副标题, 数据来源)
GROUPS = [("world", "天下"), ("career", "程序员职业"), ("family", "家庭")]
SECTIONS = [
    ("world", "lianbo", "国内新闻联播", "从节目单里拣出与你的生活、工作和钱袋子真正相关的信号。", "briefs"),
    ("world", "global_ai", "国际 AI", "全球 AI 大事，只看与国内从业者相关的部分。", "global_ai"),
    ("world", "global_hot", "国际热点新闻", "世界正在发生什么，哪些会传导到你身边。", "global_hot"),
    ("world", "global_top10", "全球热点新闻 Top10", "每天最重要的十条全球新闻。", "global_top10"),
    ("career", "platform_policy", "平台政策与合规", "平台规则变了，先看对你接单、上架有没有影响。", "platform_policy"),
    ("career", "model_api", "大模型 API 价格与能力", "模型涨价降价、能力升级，直接关系调用成本。", "model_api"),
    ("career", "rankings", "最新模型排行榜", "综合与编程两个维度的最新排名。", "rankings"),
    ("career", "ai_dev_ecosystem", "AI 编程工具与技术生态", "新工具、新框架，值不值得你花时间学。", "ai_dev_ecosystem"),
    ("career", "java_ecosystem", "Java 生态版本", "JDK、Spring 这些你天天用的东西的版本动态。", "java_ecosystem"),
    ("career", "xinchuang", "信创国产化", "国产替代的风向，公司项目迟早会碰到。", "xinchuang"),
    ("career", "jobs_trends", "行业就业与技术风向", "招人、裁员、薪资、技术栈，看看大环境。", "jobs_trends"),
    ("career", "junior_ai_impact", "AI 对初级开发岗位冲击", "AI 越来越能写代码，初级岗的出路在哪里。", "junior_ai_impact"),
    ("career", "security_compliance", "安全与合规红线", "数据安全、合规红线，别踩。", "security_compliance"),
    ("family", "local_education", "本地教育政策", "本地的教育政策变化，直接关系孩子。", "local_education"),
    ("family", "child_growth", "孩子成长", "孩子的学习与成长，家长能做什么。", "child_growth"),
    ("family", "child_health", "孩子健康", "孩子生病、长个、视力，日常注意事项。", "child_health"),
    ("family", "local_livelihood", "本地民生", "本地的水电、交通、菜价、办事，身边的小事。", "local_livelihood"),
    ("family", "family_finance", "家庭财务", "钱怎么花、怎么存，家庭账本的参考。", "family_finance"),
]

def auth_token():
    return hashlib.sha256((PASSWORD + AUTH_SALT).encode()).hexdigest()

WORK_PASSWORD = os.environ.get("WB_WORK_PASSWORD", "changeme")  # 「工作」栏第二道口令，首次使用务必改掉
WORK_AUTH_SALT = "workbench-work-v1"

def work_auth_token():
    return hashlib.sha256((WORK_PASSWORD + WORK_AUTH_SALT).encode()).hexdigest()

# ---- 口令可改：auth.json（0600）优先，缺失时沿用上面写死的默认值 ----
AUTH_FILE = os.path.join(BASE, "auth.json")
def _load_auth():
    try:
        with open(AUTH_FILE, encoding="utf-8") as f:
            d = json.load(f)
        pw = str(d.get("password") or "").strip()
        wpw = str(d.get("work_password") or "").strip()
        return (pw or None, wpw or None)
    except Exception:
        return (None, None)
def _save_auth():
    tmp = AUTH_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"password": PASSWORD, "work_password": WORK_PASSWORD}, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, AUTH_FILE)
_apw, _awpw = _load_auth()
if _apw:
    PASSWORD = _apw
if _awpw:
    WORK_PASSWORD = _awpw
if PASSWORD == "changeme" or WORK_PASSWORD == "changeme":
    print("[workbench] 警告：正在使用默认口令，请尽快在 /change-password 里改掉，或设环境变量 WB_PASSWORD / WB_WORK_PASSWORD", flush=True)

def load_data():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            d = json.load(f)
        d.setdefault("briefs", []); d.setdefault("news", []); d.setdefault("rankings", [])
        d["briefs"].sort(key=lambda b: b.get("broadcast_date", ""), reverse=True)
        return d
    except Exception:
        return {"briefs": [], "news": [], "rankings": []}

def save_data(d):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)


# ---- 聊天历史读写（文件锁，防中枢与 agent 并发写坏）----
def _chat_lock():
    os.makedirs(CHAT_DIR, exist_ok=True)
    os.makedirs(CHAT_HISTORY_DIR, exist_ok=True)
    return open(CHAT_LOCK, "w")

def chat_history_path(agent):
    return os.path.join(CHAT_HISTORY_DIR, agent + ".json")

def chat_load(agent):
    try:
        with open(chat_history_path(agent), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def chat_append(agent, sender, text, image=None, file=None):
    lock = _chat_lock()
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        msgs = chat_load(agent)
        nid = (max((m.get("id", 0) for m in msgs), default=0) + 1)
        msg = {"id": nid, "from": sender, "text": text, "ts": int(time.time())}
        if image:
            msg["image"] = image
        if file:
            msg["file"] = file
        msgs.append(msg)
        p = chat_history_path(agent)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(msgs, f, ensure_ascii=False)
        os.replace(tmp, p)
        return nid
    finally:
        lock.close()

# ---- 首页随手记（存 BASE/notes.json，文件锁＋原子替换）----
NOTES_FILE = os.path.join(BASE, "notes.json")
NOTES_LOCK = os.path.join(BASE, "notes.lock")

def _notes_lock():
    return open(NOTES_LOCK, "w")

def notes_load():
    try:
        with open(NOTES_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []

def notes_save(notes):
    tmp = NOTES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(notes, f, ensure_ascii=False)
    os.replace(tmp, NOTES_FILE)

def notes_add(text):
    lock = _notes_lock()
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        notes = notes_load()
        nid = (max((n.get("id", 0) for n in notes), default=0) + 1)
        note = {"id": nid, "text": text, "done": False, "ts": int(time.time())}
        notes.insert(0, note)
        notes_save(notes)
        return note
    finally:
        lock.close()

def notes_toggle(nid):
    lock = _notes_lock()
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        notes = notes_load()
        for n in notes:
            if n.get("id") == nid:
                n["done"] = not n.get("done")
                notes_save(notes)
                return n
        return None
    finally:
        lock.close()

def notes_delete(nid):
    lock = _notes_lock()
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        notes = [n for n in notes_load() if n.get("id") != nid]
        notes_save(notes)
        return True
    finally:
        lock.close()


# ---- 首页系统状态（磁盘/内存/运行时间/服务端口/隧道进程，纯 Python 探测）----
# ---- 下载统计（示例配置）：每项 = {"name", "version", "count_file", "url", "download_file"(可选)}
# count_file 是 {"count": 次数, "last": "最后下载时间"} 的 JSON；没有就填 ""，那一项显示 0。
# DOWNLOADS 为空时，首页不显示下载卡。
DOWNLOADS = [
    # {"name": "我的产品", "version": "v1.0.0", "count_file": "", "url": "https://example.com", "download_file": ""},
]

def _read_count_file(path):
    """读一个下载计数文件；读不到返回 0。"""
    try:
        if not path:
            return {"count": 0, "last": ""}
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return {"count": int(d.get("count", 0)), "last": d.get("last", "")}
    except Exception:
        return {"count": 0, "last": ""}

def downloads_status():
    """返回 DOWNLOADS 每一项的实时计数。"""
    out = []
    for it in DOWNLOADS:
        d = _read_count_file(it.get("count_file", ""))
        out.append({"name": it.get("name", ""), "version": it.get("version", ""),
                    "url": it.get("url", ""), "count": d["count"], "last": d["last"],
                    "has_file": bool(it.get("download_file"))})
    return out

DISKS = [("系统盘", "/")]  # 首页系统状态里要看的盘；想看别的盘，在这里加 ("名字", "挂载点")，例如 ("U盘", "/media/usb")


def system_status():
    out = {}
    disks = []
    for label, path in DISKS:
        try:
            u = shutil.disk_usage(path)
            disks.append({"label": label, "total": u.total, "used": u.used, "free": u.free,
                          "pct": round(u.used / u.total * 100) if u.total else 0})
        except Exception:
            disks.append({"label": label, "total": 0, "used": 0, "free": 0, "pct": 0, "missing": True})
    out["disks"] = disks
    try:
        mem = {}
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                k, _, v = line.partition(":")
                if k in ("MemTotal", "MemAvailable"):
                    mem[k] = int(v.split()[0]) * 1024
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", 0)
        out["mem"] = {"total": total, "used": total - avail,
                      "pct": round((total - avail) / total * 100) if total else 0}
    except Exception:
        out["mem"] = {"total": 0, "used": 0, "pct": 0}
    try:
        with open("/proc/uptime", encoding="utf-8") as f:
            out["uptime_sec"] = int(float(f.read().split()[0]))
    except Exception:
        out["uptime_sec"] = 0
    procs = {}
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % pid, "rb") as f:
                    cmd = f.read().decode("utf-8", "ignore")
            except Exception:
                continue
            if "zeronews" in cmd:
                procs["zeronews"] = True
            if "cloudflared" in cmd and "tunnel" in cmd:
                procs["cloudflared"] = True
    except Exception:
        pass
    out["tunnels"] = {"zeronews": bool(procs.get("zeronews")),
                      "cloudflared": bool(procs.get("cloudflared"))}

    def port_open(port):
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=1)
            s.close()
            return True
        except Exception:
            return False
    out["services"] = {"workbench": port_open(18080), "fans": port_open(8080)}
    return out


# ---- Agent 聊天 v2：SSE 实时推送（替代 15 秒轮询）----
# history JSON 仍是唯一事实来源；agent 经 chat-helper.py 写文件，
# 后台线程每 0.5 秒看文件 mtime，变了就把新增消息推给连着的浏览器。
CHAT_TYPING_DIR = os.path.join(CHAT_DIR, "typing")
CHAT_READ_DIR = os.path.join(CHAT_DIR, "read")
CHAT_STREAM_DIR = os.path.join(CHAT_DIR, "stream")

def _chat_marker_path(kind_dir, agent):
    return os.path.join(kind_dir, agent + ".json")

def _chat_read_json_file(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else default
    except Exception:
        return default

def chat_read_state(agent):
    """返回 (user 已读到第几条, agent 已读到第几条)。"""
    d = _chat_read_json_file(_chat_marker_path(CHAT_READ_DIR, agent), {})
    try:
        return int(d.get("user") or 0), int(d.get("agent") or 0)
    except Exception:
        return 0, 0

def chat_set_read(agent, who, up_to):
    """写已读标记（who=user|agent），只进不退；原子替换。"""
    os.makedirs(CHAT_READ_DIR, exist_ok=True)
    lock = _chat_lock()
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        d = _chat_read_json_file(_chat_marker_path(CHAT_READ_DIR, agent), {})
        try:
            cur = int(d.get(who) or 0)
        except Exception:
            cur = 0
        if int(up_to) > cur:
            d[who] = int(up_to)
            p = _chat_marker_path(CHAT_READ_DIR, agent)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            os.replace(tmp, p)
    finally:
        lock.close()

def chat_typing_state(agent):
    """返回 (正在输入?, ts)。"""
    d = _chat_read_json_file(_chat_marker_path(CHAT_TYPING_DIR, agent), {})
    return bool(d.get("typing")), int(d.get("ts") or 0)

def _chat_stream_path(agent):
    return os.path.join(CHAT_STREAM_DIR, agent + ".json")

def chat_stream_state(agent):
    """返回进行中流 (stream_id, text, ts)；无流返回 (None, '', 0)。"""
    d = _chat_read_json_file(_chat_stream_path(agent), {})
    try:
        sid = d.get("stream_id") or None
        txt = d.get("text") or ""
        ts = int(d.get("ts") or 0)
    except Exception:
        return None, "", 0
    if not sid or not isinstance(txt, str):
        return None, "", 0
    return sid, txt, ts

def chat_stream_append(agent, stream_id, chunk):
    """把 chunk 追加到该 agent 的进行中流（stream_id 不同则重开）。原子替换，返回累计全文。"""
    os.makedirs(CHAT_STREAM_DIR, exist_ok=True)
    lock = _chat_lock()
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        d = _chat_read_json_file(_chat_stream_path(agent), {})
        if d.get("stream_id") != stream_id:
            d = {"stream_id": stream_id, "text": "", "ts": int(time.time())}
        d["text"] = (d.get("text") or "") + (chunk or "")
        d["ts"] = int(time.time())
        p = _chat_stream_path(agent)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, p)
        return d["text"]
    finally:
        lock.close()

def chat_stream_finish(agent, stream_id):
    """收尾：把流的全文作为一条消息写进历史，删流文件。返回新消息 id；流不存在/不匹配返回 None。"""
    lock = _chat_lock()
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        d = _chat_read_json_file(_chat_stream_path(agent), {})
        if d.get("stream_id") != stream_id:
            return None
        text = d.get("text") or ""
        try:
            os.remove(_chat_stream_path(agent))
        except Exception:
            pass
        if not text:
            return None
        msgs = chat_load(agent)
        nid = (max((m.get("id", 0) for m in msgs), default=0) + 1)
        msgs.append({"id": nid, "from": agent, "text": text, "ts": int(time.time())})
        p = chat_history_path(agent)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(msgs, f, ensure_ascii=False)
        os.replace(tmp, p)
        return nid
    finally:
        lock.close()

def chat_stream_cancel(agent, stream_id):
    """丢弃进行中流（写一半失败时清理，避免页面留残缺气泡）。"""
    lock = _chat_lock()
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        d = _chat_read_json_file(_chat_stream_path(agent), {})
        if d.get("stream_id") == stream_id:
            try:
                os.remove(_chat_stream_path(agent))
            except Exception:
                pass
            return True
        return False
    finally:
        lock.close()

class _ChatHub:
    """SSE 订阅中心：每连接一个有界队列；队列满了视为掉线，踢掉。"""
    def __init__(self):
        self._lock = threading.Lock()
        self._clients = set()
    def subscribe(self):
        q = queue.Queue(maxsize=200)
        with self._lock:
            self._clients.add(q)
        return q
    def unsubscribe(self, q):
        with self._lock:
            self._clients.discard(q)
    def broadcast(self, event, data):
        line = "event: %s\ndata: %s\n\n" % (event, json.dumps(data, ensure_ascii=False))
        with self._lock:
            dead = []
            for q in list(self._clients):
                try:
                    q.put_nowait(line)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._clients.discard(q)

CHAT_HUB = _ChatHub()

def _chat_watch_loop():
    """后台线程：盯 history/typing/read 文件，变了就推 SSE。永不抛错。"""
    last_hist = {}    # agent -> (mtime, size, max_id)
    last_typing = {}  # agent -> (typing, ts)
    last_read = {}    # agent -> (user_up_to, agent_up_to)
    last_stream = {}  # agent -> (stream_id, text)
    while True:
        try:
            for aid, _n, _i, _c, _r in chat_agent_list():
                try:
                    st = os.stat(chat_history_path(aid))
                    key = (st.st_mtime, st.st_size)
                except Exception:
                    continue
                prev = last_hist.get(aid)
                if prev is None:
                    msgs = chat_load(aid)
                    last_hist[aid] = (key, max((m.get("id", 0) for m in msgs), default=0))
                elif key != prev[0]:
                    msgs = chat_load(aid)
                    mx = prev[1]
                    for m in msgs:
                        mid = m.get("id", 0)
                        if mid > prev[1]:
                            CHAT_HUB.broadcast("message", {"agent": aid, "message": m})
                            if mid > mx:
                                mx = mid
                    last_hist[aid] = (key, mx)
                t = chat_typing_state(aid)
                if aid not in last_typing:
                    last_typing[aid] = t
                elif last_typing[aid] != t:
                    last_typing[aid] = t
                    CHAT_HUB.broadcast("typing", {"agent": aid, "typing": t[0], "ts": t[1]})
                r = chat_read_state(aid)
                if aid not in last_read:
                    last_read[aid] = r
                elif last_read[aid] != r:
                    last_read[aid] = r
                    CHAT_HUB.broadcast("read", {"agent": aid, "user": r[0], "agent_read": r[1]})
                sid, txt, _sts = chat_stream_state(aid)
                cur = (sid, txt)
                prev = last_stream.get(aid)
                if prev is None:
                    last_stream[aid] = cur
                elif cur != prev:
                    last_stream[aid] = cur
                    if sid is None:
                        CHAT_HUB.broadcast("stream", {"agent": aid, "stream_id": prev[0], "text": "", "done": True})
                    else:
                        CHAT_HUB.broadcast("stream", {"agent": aid, "stream_id": sid, "text": txt})
        except Exception:
            pass
        time.sleep(0.5)

def chat_watch_start():
    t = threading.Thread(target=_chat_watch_loop, daemon=True, name="chat-watch")
    t.start()

def chat_agent_status():
    out = []
    now = time.time()
    quotas = {}
    try:
        with open(os.path.join(CHAT_DIR, "quota.json"), encoding="utf-8") as f:
            quotas = json.load(f) or {}
    except Exception:
        quotas = {}
    # per-agent quota files: each sub-agent writes its own workspace file
    # /opt/shared/agent-bus/agents/<aid>/quota.json ; present + valid wins
    bus_agents = os.path.join(BUS_DIR, "agents")
    for _aid in ["atlas"] + [s["id"] for s in scan_sub_agents()]:
        try:
            with open(os.path.join(bus_agents, _aid, "quota.json"), encoding="utf-8") as f:
                _q = json.load(f)
            if isinstance(_q, dict) and isinstance(_q.get("pct"), (int, float)):
                _base = dict(quotas.get(_aid) or {})
                _base.update(_q)
                quotas[_aid] = _base
        except Exception:
            pass
    ws_map = {s["id"]: s for s in scan_sub_agents()}
    for aid, name, initial, cls, role in chat_agent_list():
        status, status_text = "poll", "轮询模式 · 约 1 分钟取一次"
        if aid == "Lei":
            status, status_text = "relay", "中继模式 · 约 1 分钟回一次"
        elif aid == "atlas":
            # atlas 是平级调度者，不跑轮询心跳；以它是否在收件箱回过消息判断是否已接入
            # （回过 = 已按邀请建了自己的轮询；最近一条用户消息若超过 30 分钟没回，认为可能离线）
            msgs_m1 = chat_load(aid)
            replied = any(m.get("from") == "atlas" for m in msgs_m1)
            unreplied_old = bool(
                msgs_m1
                and msgs_m1[-1].get("from") == "user"
                and now - msgs_m1[-1].get("ts", 0) > 30 * 60
            )
            if replied and not unreplied_old:
                status, status_text = "poll", "轮询模式 · 约 1 分钟取一次"
            else:
                status, status_text = "offline", "未接入 · 先在它那边说一声"
        else:
            _ws = ws_map.get(aid) or {}
            if _ws.get("status") == "待签到":
                status, status_text = "offline", "待签到 · 未到岗"
            else:
                try:
                    with open(os.path.join(BUS_DIR, aid, "status.json"), encoding="utf-8") as f:
                        st = json.load(f)
                    _ls = st.get("last_seen", "").replace("Z", "+00:00")
                    _ls = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", _ls)
                    dt = datetime.fromisoformat(_ls)
                    if now - dt.timestamp() > 45 * 60:
                        status = "offline"
                        status_text = "离线 · 上次在线 " + dt.strftime("%m-%d %H:%M")
                except Exception:
                    status = "offline"
                    status_text = "离线 · 未见心跳"
        msgs = chat_load(aid)
        last_time, last_text = "", ""
        if msgs:
            last = msgs[-1]
            last_time = datetime.fromtimestamp(last.get("ts", 0)).strftime("%H:%M")
            last_text = (last.get("text", "") or "")[:40]
            if not last_text and last.get("image"):
                last_text = "[图片]"
        qinfo = quotas.get(aid) or {}
        qp = qinfo.get("pct")
        qa = qinfo.get("as_of", "")
        qr = qinfo.get("reset") or ''
        # 附加词元取自 note 里的“附加词元剩 Y”，Y 可能是 0 / 1B 这类字符串，不自创字段
        qt = None
        _tm = re.search(r"附加词元剩\s*([^\s，；,;]+)", qinfo.get("note") or "")
        if _tm:
            qt = _tm.group(1)
        # 免费额度按周重置：存的重置时间过期后自动顺延 7 天，直到未来
        if qr:
            try:
                _rm = re.match(r"^(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})$", qr.strip())
                if _rm:
                    _mo, _da, _hh, _mi = (int(x) for x in _rm.groups())
                    _now = datetime.now()
                    _rd = datetime(_now.year, _mo, _da, _hh, _mi)
                    while _rd > _now + timedelta(days=7):
                        _rd -= timedelta(days=7)
                    while _rd <= _now:
                        _rd += timedelta(days=7)
                    qr = _rd.strftime("%m-%d %H:%M")
            except Exception:
                pass

        out.append({"id": aid, "name": name, "initial": initial, "cls": cls,
                    "img": "/avatar-" + aid + ".png?v=5",
                    "video": "/avatar-" + aid + ".mp4?v=4" if aid in ("muse", "atlas", "muse-2", "muse-3", "muse-4") else "",
                    "quota": qp, "quotaAsOf": qa, "quotaReset": qr, "quotaTokens": qt,
                    "hasAvatar": os.path.exists(os.path.join(BASE, "avatar-" + aid + ".png")),
                    "role": role, "status": status, "statusText": status_text,
                    "tailnet": (ws_map.get(aid) or {}).get("tailnet"),
                    "time": last_time, "lastText": last_text})
    return out

# ---- imgloc 图床（匿名，无需 key；单张 ≤10MB，每 IP 每小时 ≤60 张，保存 1 年）----
IMGLOC_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
IMGLOC_BASE = "https://imgloc.com/upload.php"
IMGLOC_MAX = 10 * 1024 * 1024


def _imgloc_token():
    req = urllib.request.Request(
        IMGLOC_BASE + "?action=token",
        headers={"User-Agent": IMGLOC_UA, "Referer": "https://imgloc.com/"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    token = data.get("token", "")
    if not token:
        raise ValueError("imgloc token 获取失败")
    return token


def _imgloc_post(image_bytes, filename, mime, token):
    boundary = "----" + uuid.uuid4().hex
    body = b""
    body += ("--" + boundary + "\r\n").encode()
    body += (
        'Content-Disposition: form-data; name="image"; filename="%s"\r\n' % filename
    ).encode()
    body += ("Content-Type: %s\r\n\r\n" % mime).encode()
    body += image_bytes + b"\r\n"
    body += ("--" + boundary + "\r\n").encode()
    body += b'Content-Disposition: form-data; name="token"\r\n\r\n'
    body += token.encode() + b"\r\n"
    body += ("--" + boundary + "--\r\n").encode()
    req = urllib.request.Request(
        IMGLOC_BASE,
        data=body,
        headers={
            "User-Agent": IMGLOC_UA,
            "Referer": "https://imgloc.com/",
            "Origin": "https://imgloc.com",
            "Content-Type": "multipart/form-data; boundary=" + boundary,
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def imgloc_upload(image_bytes, filename, mime):
    """传到 imgloc，返回图片 URL；失败抛 ValueError。token 失效时自动刷新重试一次。"""
    res = _imgloc_post(image_bytes, filename, mime, _imgloc_token())
    if not res.get("ok") and "凭证" in str(res.get("message", "")):
        res = _imgloc_post(image_bytes, filename, mime, _imgloc_token())
    if not res.get("ok") or not res.get("url"):
        raise ValueError("imgloc 上传失败：" + str(res.get("message", "未知错误")))
    return res["url"]


def _check_image(data, mime):
    """校验图片格式：png/jpeg/gif/webp 按魔数核对，其余 image/* 信任声明的 mime。"""
    sigs = {
        "image/png": data[:8] == b"\x89PNG\r\n\x1a\n",
        "image/jpeg": data[:3] == b"\xff\xd8\xff",
        "image/gif": data[:6] in (b"GIF87a", b"GIF89a"),
        "image/webp": data[:4] == b"RIFF" and data[8:12] == b"WEBP",
    }
    if mime in sigs:
        return sigs[mime]
    return mime.startswith("image/")

# ---- 聊天文件本地存储（V2 第2条：替代 imgloc 公开图床；需登录鉴权）----
CHAT_UPLOAD_DIR = os.path.join(CHAT_DIR, "uploads")
CHAT_UPLOAD_MAX = 10 * 1024 * 1024          # 单文件 ≤10MB
CHAT_UPLOAD_TOTAL_MAX = 500 * 1024 * 1024   # 上传目录总量 ≤500MB（系统盘紧张）
CHAT_UPLOAD_EXTS = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
    "heic": "image/heic", "heif": "image/heif", "avif": "image/avif",
    "tif": "image/tiff", "tiff": "image/tiff",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "ppt": "application/vnd.ms-powerpoint",
    "txt": "text/plain", "md": "text/markdown", "csv": "text/csv",
    "py": "text/x-python", "js": "text/javascript", "ts": "text/typescript",
    "jsx": "text/javascript", "tsx": "text/typescript",
    "java": "text/x-java-source", "c": "text/x-csrc", "h": "text/x-chdr",
    "cpp": "text/x-c++src", "hpp": "text/x-c++hdr",
    "go": "text/x-go", "rs": "text/x-rustsrc", "rb": "text/x-ruby",
    "php": "text/x-php", "html": "text/html", "css": "text/css",
    "json": "application/json", "xml": "text/xml",
    "yaml": "text/yaml", "yml": "text/yaml", "toml": "text/plain",
    "sh": "text/x-sh", "sql": "text/x-sql", "log": "text/plain",
    "ini": "text/plain", "cfg": "text/plain",
    "zip": "application/zip",
}
CHAT_UPLOAD_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "heic", "heif", "avif", "tif", "tiff"}
CHAT_UPLOAD_INLINE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "application/pdf"}

def _chat_upload_total_size():
    total = 0
    try:
        for name in os.listdir(CHAT_UPLOAD_DIR):
            fp = os.path.join(CHAT_UPLOAD_DIR, name)
            if os.path.isfile(fp):
                total += os.path.getsize(fp)
    except Exception:
        pass
    return total

def chat_upload_save(name, mime, data):
    """保存一个上传文件。返回 (stored, url, size, kind, mime)。失败抛 ValueError。"""
    ext = ""
    if "." in name:
        ext = name.rsplit(".", 1)[-1].lower()
    if ext not in CHAT_UPLOAD_EXTS:
        raise ValueError("不支持的文件类型")
    mime = CHAT_UPLOAD_EXTS[ext]
    if len(data) == 0 or len(data) > CHAT_UPLOAD_MAX:
        raise ValueError("文件过大（单文件上限 10MB）")
    if mime.startswith("image/") and not _check_image(data, mime):
        raise ValueError("图片内容与格式不符")
    os.makedirs(CHAT_UPLOAD_DIR, exist_ok=True)
    if _chat_upload_total_size() + len(data) > CHAT_UPLOAD_TOTAL_MAX:
        raise ValueError("上传空间已满（上限 500MB），请联系管理员清理")
    stored = uuid.uuid4().hex + "." + ext
    tmp = os.path.join(CHAT_UPLOAD_DIR, stored + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, os.path.join(CHAT_UPLOAD_DIR, stored))
    kind = "image" if ext in CHAT_UPLOAD_IMAGE_EXTS else "file"
    return stored, "/api/chat/file/" + stored, len(data), kind, mime

# ---- 天气（和风优先，open-meteo 其次，wttr.in 兜底；10 分钟缓存，失败显示 —）----
# 城市在下面三行改：CITY_NAME 显示用，CITY_LAT / CITY_LON 是经纬度（和风与 open-meteo 共用）
CITY_NAME = "北京"
CITY_LAT = 39.90
CITY_LON = 116.40
WX_CACHE = {"at": 0, "data": None}
WX_CODE = {0: "晴", 1: "多云", 2: "多云", 3: "阴", 45: "雾", 48: "雾",
           51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨", 56: "冻雨", 57: "冻雨",
           61: "小雨", 63: "雨", 65: "大雨", 66: "冻雨", 67: "冻雨",
           71: "小雪", 73: "雪", 75: "大雪", 77: "雪粒",
           80: "阵雨", 81: "阵雨", 82: "暴雨", 85: "阵雪", 86: "阵雪", 95: "雷阵雨"}

WX_CACHE_FILE = os.path.join(BASE, "weather.json")

def _wx_load_disk():
    try:
        with open(WX_CACHE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("data") and time.time() - d.get("at", 0) < 10800:
            WX_CACHE["at"] = d["at"]
            WX_CACHE["data"] = d["data"]
    except Exception:
        pass

def _wx_save_disk():
    try:
        with open(WX_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"at": WX_CACHE["at"], "data": WX_CACHE["data"]}, f, ensure_ascii=False)
    except Exception:
        pass

# wttr.in 备用天气源：open-meteo 失败时用；天气状况英文->中文，WWO 天气码->WMO 码
WTTR_COND = {"Clear": "晴", "Sunny": "晴", "Partly cloudy": "多云", "Cloudy": "多云",
             "Overcast": "阴", "Mist": "雾", "Fog": "雾", "Light rain": "小雨",
             "Light rain shower": "阵雨", "Moderate rain": "雨", "Moderate rain at times": "雨",
             "Heavy rain": "大雨", "Heavy rain at times": "大雨", "Light snow": "小雪",
             "Moderate snow": "雪", "Heavy snow": "大雪", "Patchy snow possible": "阵雪",
             "Thundery outbreaks possible": "雷阵雨", "Blizzard": "大雪", "Freezing fog": "雾",
             "Patchy light rain": "小雨", "Patchy rain possible": "阵雨"}
WTTR_CODE = {113: 0, 116: 1, 119: 2, 122: 3, 143: 45, 248: 45, 260: 45, 176: 61,
             263: 61, 266: 61, 293: 61, 296: 61, 299: 63, 302: 63, 305: 65, 308: 65,
             359: 65, 179: 71, 182: 71, 185: 71, 227: 71, 230: 75, 317: 71, 320: 71,
             323: 71, 326: 71, 329: 75, 332: 75, 335: 75, 338: 75, 200: 95, 386: 95, 389: 95}

QW_KEY_FILE = os.path.join(BASE, "qweather_key")

def _qweather_key():
    try:
        with open(QW_KEY_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""

QW_HOST_FILE = os.path.join(BASE, "qweather_host")

def _qweather_host():
    try:
        with open(QW_HOST_FILE, encoding="utf-8") as f:
            h = f.read().strip()
        h = h.replace("https://", "").replace("http://", "").strip("/").strip()
        return h
    except Exception:
        return ""

# 和风天气图标码 -> WMO 码（只为取 WX_ICON 的图标；状况文字直接用和风的 text）
QW_CODE = {100: 0, 101: 1, 102: 1, 103: 2, 104: 3, 150: 45, 151: 45, 153: 45, 154: 45,
           300: 80, 301: 80, 302: 63, 303: 63, 304: 63, 305: 61, 306: 63, 307: 65, 308: 65,
           309: 65, 310: 65, 311: 65, 312: 65, 313: 65, 314: 65, 315: 65, 316: 65, 317: 65,
           318: 65, 350: 61, 351: 61, 399: 61, 400: 71, 401: 73, 402: 75, 403: 75, 404: 75,
           405: 75, 406: 75, 407: 75, 408: 75, 409: 75, 410: 75, 456: 71, 457: 71, 499: 71,
           500: 45, 501: 45, 502: 45, 503: 45, 504: 45, 507: 45, 508: 45, 509: 45, 510: 45,
           511: 45, 512: 45, 513: 45, 514: 45, 515: 45, 900: 95, 901: 95}

def _json_from_response(r):
    raw = r.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))

def _fetch_qweather():
    key = _qweather_key()
    host = _qweather_host()
    if not key or not host:
        return None
    try:
        headers = {"X-QW-Api-Key": key}
        req = urllib.request.Request(
            f"https://{host}/weather/v1/current/{CITY_LAT}/{CITY_LON}?lang=zh", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            now = _json_from_response(r)
        req = urllib.request.Request(
            f"https://{host}/weather/v1/daily/{CITY_LAT}/{CITY_LON}?days=7&lang=zh", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            fc = _json_from_response(r)
        cond = now.get("condition") or {}
        temp = now.get("temperature") or {}
        wind = now.get("wind") or {}
        speed = (wind.get("speed") or {})
        days = []
        for item in (fc.get("days") or [])[:7]:
            dt = item.get("daytime") or {}
            dcond = dt.get("condition") or {}
            try:
                icon = int(dcond.get("code") or 100)
            except Exception:
                icon = 100
            mx = (item.get("temperatureMax") or {}).get("value")
            mn = (item.get("temperatureMin") or {}).get("value")
            days.append({"date": (item.get("forecastStartTime") or "")[:10],
                         "max": round(float(mx)) if mx is not None else 0,
                         "min": round(float(mn)) if mn is not None else 0,
                         "code": QW_CODE.get(icon, 0),
                         "cond": dcond.get("text") or "未知"})
        try:
            t = round(float(temp.get("value")))
        except Exception:
            return None
        try:
            hum = round(float(now.get("humidity")) * 100)
        except Exception:
            hum = None
        try:
            ws = round(float(speed.get("value") or 0))
        except Exception:
            ws = 0
        try:
            icon = int(cond.get("code") or 100)
        except Exception:
            icon = 100
        print("[weather] qweather ok", flush=True)
        return {"temp": t, "cond": cond.get("text") or "未知", "hum": hum, "wind": ws,
                "code": QW_CODE.get(icon, 0), "days": days}
    except Exception as e:
        print(f"[weather] qweather fetch failed: {e!r}", flush=True)
        return None

def _fetch_openmeteo():
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={CITY_LAT}&longitude={CITY_LON}"
           "&current=temperature_2m,weather_code,relative_humidity_2m,wind_speed_10m"
           "&daily=temperature_2m_max,temperature_2m_min,weather_code&timezone=Asia%2FShanghai&forecast_days=7")
    w = None
    last_err = None
    for _ in range(2):
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                d = _json_from_response(r)
            cur = d["current"]
            daily = d.get("daily", {})
            days = []
            for i in range(min(7, len(daily.get("time", [])))):
                days.append({"date": daily["time"][i],
                             "max": round(daily["temperature_2m_max"][i]),
                             "min": round(daily["temperature_2m_min"][i]),
                             "code": daily["weather_code"][i],
                             "cond": WX_CODE.get(daily["weather_code"][i], "未知")})
            w = {"temp": round(cur["temperature_2m"]), "cond": WX_CODE.get(cur["weather_code"], "未知"),
                 "hum": cur.get("relative_humidity_2m"), "wind": cur.get("wind_speed_10m"),
                 "code": cur["weather_code"], "days": days}
            break
        except Exception as e:
            last_err = e
            time.sleep(1)
    if w is None and last_err is not None:
        print(f"[weather] open-meteo fetch failed: {last_err!r}", flush=True)
    return w

def _fetch_wttr():
    try:
        req = urllib.request.Request("https://wttr.in/Beijing?format=j1",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = _json_from_response(r)
        cur = (d.get("current_condition") or [{}])[0]
        desc = (cur.get("weatherDesc") or [{"value": ""}])[0].get("value", "")
        cond = WTTR_COND.get(desc, desc or "未知")
        try:
            wind = round(float(cur.get("windspeedKmph") or 0))
        except Exception:
            wind = 0
        days = []
        for item in (d.get("weather") or [])[:3]:
            hrs = item.get("hourly") or []
            mid = hrs[4] if len(hrs) > 4 else (hrs[0] if hrs else {})
            ddesc = (mid.get("weatherDesc") or [{"value": ""}])[0].get("value", "")
            try:
                code = int(mid.get("weatherCode") or 113)
            except Exception:
                code = 113
            days.append({"date": item.get("date", ""),
                         "max": round(float(item.get("maxtempC") or 0)),
                         "min": round(float(item.get("mintempC") or 0)),
                         "code": WTTR_CODE.get(code, 0),
                         "cond": WTTR_COND.get(ddesc, ddesc or "未知")})
        try:
            temp = round(float(cur.get("temp_C")))
        except Exception:
            return None
        try:
            hum = int(cur.get("humidity"))
        except Exception:
            hum = None
        try:
            wcode = int(cur.get("weatherCode") or 113)
        except Exception:
            wcode = 113
        return {"temp": temp, "cond": cond, "hum": hum, "wind": wind,
                "code": WTTR_CODE.get(wcode, 0), "days": days}
    except Exception as e:
        print(f"[weather] wttr.in fetch failed: {e!r}", flush=True)
        return None

def get_weather(force=False):
    now = time.time()
    if WX_CACHE["at"] == 0:
        _wx_load_disk()
    if not force and now - WX_CACHE["at"] < 600:
        return WX_CACHE["data"]
    w = _fetch_qweather()
    if w is None:
        w = _fetch_openmeteo()
    if w is None:
        w = _fetch_wttr()
    if w is None:
        if WX_CACHE["data"] is not None:
            WX_CACHE["at"] = now
            return WX_CACHE["data"]
        WX_CACHE["at"] = now
        WX_CACHE["data"] = None
        return None
    WX_CACHE["at"] = now
    WX_CACHE["data"] = w
    _wx_save_disk()
    return w


def _wx_refresh_loop():
    while True:
        try:
            get_weather(force=True)
        except Exception:
            pass
        time.sleep(600)

def wx_watch_start():
    threading.Thread(target=_wx_refresh_loop, daemon=True).start()

# ---- 国内金价（上期所沪金连续 nf_AU0，新浪行情接口，无 key）----
GOLD_CACHE_FILE = os.path.join(BASE, "gold.json")
GOLD_CACHE = {"at": 0, "data": None}
GOLD_HOLD_GRAMS = 0.0  # 你的纸黄金持仓克数；填 0 则只显示行情，不显示持仓
GOLD_AVG_COST = 0.0  # 你的持仓均价（元/克）
GOLD_ALERTS = [780, 900, 1000]  # 提醒价位（示例）：金价到这些位置时卡片上提示
GOLD_SPREAD = 5.0


def _gold_load_disk():
    try:
        with open(GOLD_CACHE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("data") and time.time() - d.get("at", 0) < 10800:
            GOLD_CACHE["at"] = d["at"]
            GOLD_CACHE["data"] = d["data"]
    except Exception:
        pass


def _gold_save_disk():
    try:
        with open(GOLD_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"at": GOLD_CACHE["at"], "data": GOLD_CACHE["data"]}, f, ensure_ascii=False)
    except Exception:
        pass


def _fetch_gold_sina():
    try:
        req = urllib.request.Request(
            "https://hq.sinajs.cn/list=nf_AU0",
            headers={"Referer": "https://finance.sina.com.cn",
                     "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read().decode("gbk", "ignore")
        m = re.search(r'"([^"]*)"', raw)
        if not m:
            return None
        f = m.group(1).split(",")
        last = float(f[8])
        prev = float(f[10])
        if last <= 0 or prev <= 0:
            return None
        chg = last - prev
        return {"price": last, "open": float(f[2]), "high": float(f[3]),
                "low": float(f[4]), "bid": float(f[6]), "ask": float(f[7]),
                "prev": prev, "chg": round(chg, 2),
                "pct": round(chg / prev * 100, 2),
                "date": f[17] if len(f) > 17 else ""}
    except Exception:
        return None


def get_gold_price(force=False):
    now = time.time()
    if GOLD_CACHE["at"] == 0:
        _gold_load_disk()
    if not force and GOLD_CACHE["data"] is not None and now - GOLD_CACHE["at"] < 60:
        d = dict(GOLD_CACHE["data"])
        d["cached"] = True
        return d
    g = _fetch_gold_sina()
    if g is None:
        if GOLD_CACHE["data"] is not None:
            GOLD_CACHE["at"] = now
            d = dict(GOLD_CACHE["data"])
            d["cached"] = True
            d["stale"] = True
            return d
        return {"ok": False, "msg": "行情源暂时不可用"}
    sell = round(g["price"] - GOLD_SPREAD, 2)
    value = round(GOLD_HOLD_GRAMS * sell, 2)
    cost = round(GOLD_HOLD_GRAMS * GOLD_AVG_COST, 2)
    g.update({"ok": True, "sell": sell, "grams": GOLD_HOLD_GRAMS,
              "avg": GOLD_AVG_COST, "value": value,
              "pnl": round(value - cost, 2), "alerts": GOLD_ALERTS,
              "at": time.strftime("%H:%M:%S", time.localtime(now))})
    GOLD_CACHE["at"] = now
    GOLD_CACHE["data"] = g
    _gold_save_disk()
    return g



# ---- 农历与节日倒计时 ----
_LUNAR_MONTH = ["正月", "二月", "三月", "四月", "五月", "六月", "七月", "八月", "九月", "十月", "冬月", "腊月"]
_LUNAR_DAY = ["初一", "初二", "初三", "初四", "初五", "初六", "初七", "初八", "初九", "初十",
              "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
              "廿一", "廿二", "廿三", "廿四", "廿五", "廿六", "廿七", "廿八", "廿九", "三十"]


def lunar_str(d):
    """阳历 date → 农历中文，如 九月廿七。lunardate 模块在同目录，未装则返回空。"""
    try:
        import lunardate
        l = lunardate.LunarDate.from_solar_date(d.year, d.month, d.day)
        s = _LUNAR_MONTH[l.month - 1] + _LUNAR_DAY[l.day - 1]
        return ("闰" + s) if l.isLeapMonth else s
    except Exception:
        return ""


def lunar_day_str(d):
    """阳历 date → 当天农历日字：初一显示月份名（十月/冬月/腊月/正月…），其余显示初二/十五/廿三…；未装 lunardate 返回空。"""
    try:
        import lunardate
        l = lunardate.LunarDate.from_solar_date(d.year, d.month, d.day)
        if l.day == 1:
            nm = _LUNAR_MONTH[l.month - 1]
            return ("闰" + nm) if l.isLeapMonth else nm
        return _LUNAR_DAY[l.day - 1]
    except Exception:
        return ""


# ---- 节假日（7 大法定节假日）----
# 固定阳历：元旦 1/1、劳动节 5/1、国庆 10/1；清明 4/5（2026/2027 已核实，余年默认 4/5）
# 农历（经 lunardate 算阳历）：春节正月初一、端午五月初五、中秋八月十五
_FIXED_HOLIDAYS = {(1, 1): "元旦", (5, 1): "劳动节", (10, 1): "国庆节"}
_QINGMING = {2026: (4, 5), 2027: (4, 5)}

def holiday_name(d):
    """阳历 date → 节假日名（元旦/春节/清明/劳动节/端午/中秋/国庆），不是返回 ''。"""
    if (d.month, d.day) in _FIXED_HOLIDAYS:
        return _FIXED_HOLIDAYS[(d.month, d.day)]
    qm, qd = _QINGMING.get(d.year, (4, 5))
    if (d.month, d.day) == (qm, qd):
        return "清明节"
    try:
        import lunardate
        l = lunardate.LunarDate.from_solar_date(d.year, d.month, d.day)
        if (l.month, l.day) == (1, 1):
            return "春节"
        if (l.month, l.day) == (5, 5):
            return "端午节"
        if (l.month, l.day) == (8, 15):
            return "中秋节"
    except Exception:
        pass
    return ""


# ---- 二十四节气（天文算法：太阳视黄经每 15° 一节气，NOAA/Meeus 低精度公式）----
# 已用 2026 全年 24 个节气核实（香港天文台＋农民历多源一致），全部命中。
_SOLAR_TERMS = [(315, "立春"), (330, "雨水"), (345, "惊蛰"), (0, "春分"), (15, "清明"), (30, "谷雨"),
                (45, "立夏"), (60, "小满"), (75, "芒种"), (90, "夏至"), (105, "小暑"), (120, "大暑"),
                (135, "立秋"), (150, "处暑"), (165, "白露"), (180, "秋分"), (195, "寒露"), (210, "霜降"),
                (225, "立冬"), (240, "小雪"), (255, "大雪"), (270, "冬至"), (285, "小寒"), (300, "大寒")]

def _julian_day(y, m, d, ut=0.0):
    """儒略日（UT 小时）。"""
    if m <= 2:
        y -= 1
        m += 12
    A = math.floor(y / 100)
    B = 2 - A + math.floor(A / 4)
    return math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + d + B - 1524.5 + ut / 24.0

def _sun_longitude(jd):
    """太阳视黄经（度），0–360。"""
    T = (jd - 2451545.0) / 36525.0
    L0 = (280.46646 + T * (36000.76983 + T * 0.0003032)) % 360
    M = (357.52911 + T * (35999.05029 - T * 0.0001537)) % 360
    Mr = math.radians(M)
    C = (1.914602 - T * (0.004817 + T * 0.000014)) * math.sin(Mr) \
        + (0.019993 - T * 0.000101) * math.sin(2 * Mr) \
        + 0.000289 * math.sin(3 * Mr)
    omega = 125.04 - 1934.136 * T
    return (L0 + C - 0.00569 - 0.00478 * math.sin(math.radians(omega))) % 360

def solar_term_on(d):
    """阳历 date → 当天节气名（如 立冬），没有返回 ''。
    原理：取当天 00:00 CST（= 前一日 16:00 UTC）与次日 00:00 CST 的太阳黄经，
    若其间跨过某个 15° 倍数，则该节气落在当天。"""
    jd0 = _julian_day(d.year, d.month, d.day, ut=-8.0)
    jd1 = jd0 + 1.0
    l0 = _sun_longitude(jd0)
    l1 = _sun_longitude(jd1)
    if l1 < l0:
        l1 += 360
    for deg, name in _SOLAR_TERMS:
        t = deg if deg >= l0 else deg + 360
        if l0 <= t < l1:
            return name
    return ""


def next_festivals(today):
    """返回 [(名, 日期, 剩余天数)]：元旦与春节（取下一个）。"""
    out = []
    ny = date(today.year, 1, 1)
    if ny < today:
        ny = date(today.year + 1, 1, 1)
    out.append(("元旦", ny, (ny - today).days))
    try:
        import lunardate
        for y in (today.year, today.year + 1):
            sf = lunardate.LunarDate(y, 1, 1).to_solar_date()
            if sf >= today:
                out.append(("春节", sf, (sf - today).days))
                break
    except Exception:
        pass
    return out


# ---- 每日电影：30 部高分片，按一年中的天数轮换；海报为豆瓣图床直链 ----
MOVIES = [
    {"t": "毒品网络", "o": "Traffic", "y": 2000, "r": 7.6, "g": "剧情 / 惊悚 / 犯罪", "c": "美国 墨西哥 德国", "d": "史蒂文·索德伯格",
     "p": "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p817029842.jpg",
     "q": "这一路下来有进步、有失败。但是在失败中，我看到的不是问题，而是契机。",
     "s": "在崇尚娱乐的现代社会，毒品已成为美国政府甚为头痛的难题。为应对日益严重的毒品犯罪和买卖，美国总统发起反毒品战争计划，正直的俄亥俄州最高法院大法官罗伯特·刘易斯由此调任为国家药品政策管理处的官员，他不仅要面对各个利益团体的攻讦和拉拢，还要应对来自家庭内部的毒品侵蚀……"},
    {"t": "肖申克的救赎", "o": "The Shawshank Redemption", "y": 1994, "r": 9.7, "g": "剧情 / 犯罪", "c": "美国", "d": "弗兰克·德拉邦特",
     "p": "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p2936346968.jpg",
     "q": "希望是件美丽的东西，也许是最好的东西，而美好的东西是永远不会消逝的。",
     "s": "银行家安迪被冤枉杀害妻子及其情人，被判终身监禁，来到肖申克监狱。在狱中他结识了瑞德，凭借智慧与坚韧，他不仅赢得了狱友的尊重，更用二十年时间完成了一场不可思议的越狱，奔向自由。"},
    {"t": "霸王别姬", "o": "Farewell My Concubine", "y": 1993, "r": 9.6, "g": "剧情 / 爱情 / 同性", "c": "中国大陆 中国香港", "d": "陈凯歌",
     "p": "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p2911205318.jpg",
     "q": "说的是一辈子！差一年，一个月，一天，一个时辰，都不算一辈子！",
     "s": "段小楼与程蝶衣自幼在京剧班学艺，两人合演《霸王别姬》名动京城。半个世纪的梨园生涯里，他们的命运随着时代跌宕起伏，戏里戏外难分真假，一段爱恨纠缠终成绝唱。"},
    {"t": "阿甘正传", "o": "Forrest Gump", "y": 1994, "r": 9.5, "g": "剧情 / 爱情", "c": "美国", "d": "罗伯特·泽米吉斯",
     "p": "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p510876253.jpg",
     "q": "生活就像一盒巧克力，你永远不知道下一颗是什么味道。",
     "s": "智商只有 75 的阿甘心地单纯善良，凭借一双飞毛腿和一颗赤子之心，阴差阳错地参与了美国数十年间的重大历史事件，从越战英雄到捕虾船长，他用最简单的方式活出了最不平凡的人生。"},
    {"t": "泰坦尼克号", "o": "Titanic", "y": 1997, "r": 9.5, "g": "剧情 / 爱情 / 灾难", "c": "美国 墨西哥", "d": "詹姆斯·卡梅隆",
     "p": "https://img9.doubanio.com/view/photo/s_ratio_poster/public/p457760035.jpg",
     "q": "You jump, I jump. 你跳，我就跟着跳。",
     "s": "1912 年，豪华邮轮泰坦尼克号首航，穷画家杰克与贵族少女露丝相遇相爱。然而冰山撞击让这艘巨轮走向沉没，杰克把生的机会留给了露丝，自己沉入冰冷的大西洋。"},
    {"t": "千与千寻", "o": "千と千尋の神隠し", "y": 2001, "r": 9.4, "g": "剧情 / 动画 / 奇幻", "c": "日本", "d": "宫崎骏",
     "p": "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p2557573348.jpg",
     "q": "不管前方的路有多苦，只要走的方向正确，不管多么崎岖不平，都比站在原地更接近幸福。",
     "s": "十岁少女千寻跟随父母搬家途中误入神明世界，父母被变成猪。为了救回父母，她在汤婆婆的汤屋打工，结识了白龙与无脸男，在一次次冒险中学会勇敢与责任。"},
    {"t": "这个杀手不太冷", "o": "Léon", "y": 1994, "r": 9.4, "g": "剧情 / 动作 / 犯罪", "c": "法国 美国", "d": "吕克·贝松",
     "p": "https://img9.doubanio.com/view/photo/s_ratio_poster/public/p2913554676.jpg",
     "q": "生活总是这么辛苦吗，还是只有小时候是这样？总是如此。",
     "s": "职业杀手里昂独来独往，邻家小女孩玛蒂尔达全家被缉毒警察灭门后敲开了他的门。冷酷的杀手与孤独的女孩在朝夕相处中彼此救赎，一段动人的忘年之情在枪林弹雨中绽放。"},
    {"t": "美丽人生", "o": "La vita è bella", "y": 1997, "r": 9.5, "g": "剧情 / 喜剧 / 爱情", "c": "意大利", "d": "罗伯托·贝尼尼",
     "p": "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p2578474613.jpg",
     "q": "早安，公主！",
     "s": "二战期间，犹太青年圭多与心爱的朵拉结婚生子，却因犹太血统与儿子一同被送进集中营。为了不让儿子在恐惧中长大，圭多用善意的谎言把集中营变成一场赢坦克的游戏。"},
    {"t": "辛德勒的名单", "o": "Schindler's List", "y": 1993, "r": 9.5, "g": "剧情 / 历史 / 战争", "c": "美国", "d": "史蒂文·斯皮尔伯格",
     "p": "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p492406163.jpg",
     "q": "当你拯救了一个人，你就拯救了整个世界。",
     "s": "二战期间，德国商人辛德勒在波兰开设工厂，起初只为发战争财。但目睹纳粹对犹太人的屠杀后，他倾尽家产行贿，列出了一份名单，拯救了 1200 多名犹太工人的生命。"},
    {"t": "盗梦空间", "o": "Inception", "y": 2010, "r": 9.4, "g": "剧情 / 科幻 / 悬疑", "c": "美国 英国", "d": "克里斯托弗·诺兰",
     "p": "https://img9.doubanio.com/view/photo/s_ratio_poster/public/p513344864.jpg",
     "q": "你等车的时候，车是不会来的。",
     "s": "造梦师柯布擅长在梦境中窃取机密，一次任务要求他反向操作——在目标脑中植入一个想法。他召集了一支梦境特工队，潜入层层梦境，在现实与梦境的边缘完成这场不可能的任务。"},
    {"t": "星际穿越", "o": "Interstellar", "y": 2014, "r": 9.4, "g": "剧情 / 科幻 / 冒险", "c": "美国 英国 加拿大", "d": "克里斯托弗·诺兰",
     "p": "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p2614988097.jpg",
     "q": "不要温和地走进那个良夜，老年应当在日暮时燃烧咆哮。",
     "s": "地球濒临毁灭，宇航员库珀穿越虫洞，前往遥远星系寻找人类新家园。穿越时空的他与留在地球的女儿墨菲之间，一场跨越数十年的父女之情成为拯救人类的关键。"},
    {"t": "海上钢琴师", "o": "The Legend of 1900", "y": 1998, "r": 9.3, "g": "剧情 / 音乐", "c": "意大利", "d": "朱塞佩·托纳多雷",
     "p": "https://img9.doubanio.com/view/photo/s_ratio_poster/public/p2914698334.jpg",
     "q": "陆上的人喜欢寻根问底，虚度很多光阴。冬天忧虑夏天的迟来，夏天担心冬天的将至。",
     "s": "1900 年，一个弃婴在远洋客轮弗吉尼亚号上被发现，取名 1900。他一生没有下过船，却用钢琴弹遍了整个世界。城市的琴键有尽头，而他的琴键只在 88 个键之间。"},
    {"t": "楚门的世界", "o": "The Truman Show", "y": 1998, "r": 9.3, "g": "剧情 / 科幻", "c": "美国", "d": "彼得·威尔",
     "p": "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p479682972.jpg",
     "q": "如果再也见不到你，祝你早安、午安、晚安。",
     "s": "楚门自出生起就生活在一档全球直播的真人秀里，整个小镇都是摄影棚，所有人都是演员。三十年后他开始察觉真相，毅然走向那扇通往真实世界的大门。"},
    {"t": "忠犬八公的故事", "o": "Hachi: A Dog's Tale", "y": 2009, "r": 9.4, "g": "剧情", "c": "美国 英国", "d": "拉斯·霍尔斯道姆",
     "p": "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p2587099240.jpg",
     "q": "它只是在等它的主人。",
     "s": "大学教授帕克在车站捡到一只秋田犬，取名八公。此后十年，八公每天准时到车站等主人下班。主人去世后，它仍风雨无阻地守在原地，直到生命尽头。"},
    {"t": "当幸福来敲门", "o": "The Pursuit of Happyness", "y": 2006, "r": 9.1, "g": "剧情 / 传记 / 家庭", "c": "美国", "d": "加布里尔·穆奇诺",
     "p": "https://img9.doubanio.com/view/photo/s_ratio_poster/public/p2220721286.jpg",
     "q": "如果你有梦想的话，就要去捍卫它。",
     "s": "推销员克里斯破产、妻离子散，带着儿子露宿街头。他一边打零工一边争取证券公司的实习机会，在最艰难的日子里也没有放下对儿子的承诺与对未来的信念。"},
    {"t": "触不可及", "o": "Intouchables", "y": 2011, "r": 9.3, "g": "剧情 / 喜剧", "c": "法国", "d": "奥利维埃·纳卡什 / 埃里克·托莱达诺",
     "p": "https://img9.doubanio.com/view/photo/s_ratio_poster/public/p1454261925.jpg",
     "q": "你不能因为失去了双腿，就放弃生活。",
     "s": "富翁菲利普因跳伞事故全身瘫痪，雇佣了刚出狱的黑人青年德瑞斯做护工。两个来自不同世界的人在朝夕相处中成为挚友，彼此的生活都因对方而重新点亮。"},
    {"t": "摔跤吧！爸爸", "o": "Dangal", "y": 2016, "r": 9.0, "g": "剧情 / 传记 / 运动", "c": "印度", "d": "涅提·蒂瓦里",
     "p": "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p2401676338.jpg",
     "q": "金牌不是在摔跤场上赢来的，是在训练场上赢来的。",
     "s": "前摔跤手马哈维亚渴望儿子继承衣钵，却连生四个女儿。他发现两个女儿极具摔跤天赋，便不顾世俗眼光亲自训练她们，一路打进英联邦运动会，为印度赢得荣誉。"},
    {"t": "我不是药神", "o": "Dying to Survive", "y": 2018, "r": 9.0, "g": "剧情 / 喜剧", "c": "中国大陆", "d": "文牧野",
     "p": "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p2527119568.jpg",
     "q": "我只是想活着，犯了什么罪？",
     "s": "神油店主程勇为救患白血病的老父亲，铤而走险从印度走私仿制药，意外成为病友心中的药神。从唯利是图到不惜代价救人，他在金钱与良知之间走完了一场救赎。"},
    {"t": "活着", "o": "To Live", "y": 1994, "r": 9.3, "g": "剧情 / 家庭", "c": "中国大陆 中国香港", "d": "张艺谋",
     "p": "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p2597919477.jpg",
     "q": "活着，好好活着。",
     "s": "富贵从纨绔子弟到一无所有，历经时代变迁、家破人亡，唯有妻子家珍不离不弃。几十年风雨里，活着本身就是最大的倔强与温柔。"},
    {"t": "大话西游之大圣娶亲", "o": "A Chinese Odyssey Part Two", "y": 1995, "r": 9.2, "g": "喜剧 / 爱情 / 奇幻", "c": "中国香港 中国大陆", "d": "刘镇伟",
     "p": "https://img9.doubanio.com/view/photo/s_ratio_poster/public/p2455050536.jpg",
     "q": "曾经有一份真诚的爱情放在我面前，我没有珍惜，等我失去的时候我才后悔莫及。",
     "s": "至尊宝被月光宝盒带回五百年前，遇见白晶晶与紫霞。戴上金箍才能救紫霞，戴上金箍却要断绝情欲。他最终戴上金箍成为孙悟空，望着紫霞在夕阳武士怀里远去。"},
    {"t": "让子弹飞", "o": "Let the Bullets Fly", "y": 2010, "r": 9.0, "g": "剧情 / 喜剧 / 动作", "c": "中国大陆 中国香港", "d": "姜文",
     "p": "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p1512562287.jpg",
     "q": "让子弹飞一会儿。",
     "s": "民国年间，土匪张牧之摇身变为县长，来到鹅城上任，与当地恶霸黄四郎斗智斗勇。一场真假县长、鸿门宴与子弹横飞的博弈背后，是站着把钱挣了的豪气。"},
    {"t": "无间道", "o": "Infernal Affairs", "y": 2002, "r": 9.3, "g": "剧情 / 悬疑 / 犯罪", "c": "中国香港", "d": "刘伟强 / 麦兆辉",
     "p": "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p2597979873.jpg",
     "q": "对不起，我是警察。",
     "s": "警校学员陈永仁被派进黑帮做卧底十年，黑帮成员刘建明则在警队步步高升。两个身份互换的男人在无间地狱中彼此追寻，真相揭开时已无人能全身而退。"},
    {"t": "春光乍泄", "o": "Happy Together", "y": 1997, "r": 8.9, "g": "剧情 / 爱情 / 同性", "c": "中国香港 日本 韩国", "d": "王家卫",
     "p": "https://img2.doubanio.com/view/photo/s_ratio_poster/public/p465939041.jpg",
     "q": "不如我们从头来过。",
     "s": "黎耀辉与何宝荣在阿根廷分分合合，瀑布、探戈与布宜诺斯艾利斯的灯火里，一段感情在爱与伤害之间反复。直到黎耀辉终于明白，有些人一旦错过，就不在了。"},
    {"t": "花样年华", "o": "In the Mood for Love", "y": 2000, "r": 9.2, "g": "剧情 / 爱情", "c": "中国香港", "d": "王家卫",
     "p": "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p2918312477.jpg",
     "q": "如果有多一张船票，你会不会跟我一起走？",
     "s": "六十年代的香港，周慕云与苏丽珍同住一栋楼，两个人的配偶各自出轨。他们在克制与试探中靠近，旗袍、云吞面与 2046 房号里，一段无疾而终的感情成为时代缩影。"},
    {"t": "教父", "o": "The Godfather", "y": 1972, "r": 9.3, "g": "剧情 / 犯罪", "c": "美国", "d": "弗朗西斯·福特·科波拉",
     "p": "https://img9.doubanio.com/view/photo/s_ratio_poster/public/p616779645.jpg",
     "q": "我会给他一个无法拒绝的条件。",
     "s": "黑手党教父维托·柯里昂年迈之际，最小的儿子迈克本想远离家族生意，却在一次刺杀后被迫接手。在权力与亲情的漩涡中，他完成了从局外人到新一代教父的转变。"},
    {"t": "阿飞正传", "o": "Days of Being Wild", "y": 1990, "r": 8.5, "g": "剧情 / 爱情", "c": "中国香港", "d": "王家卫",
     "p": "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p2525770523.jpg",
     "q": "我听别人说这世界上有一种鸟是没有脚的，它只能够一直的飞呀飞呀，飞累了就在风里面睡觉。",
     "s": "六十年代香港，阿飞旭仔与养母决裂，在舞女苏丽珍与咪咪之间流连。他一生都在寻找生母，如无脚之鸟般不停飞翔，直到在菲律宾的雨夜里戛然而止。"},
    {"t": "一一", "o": "Yi Yi", "y": 2000, "r": 9.0, "g": "剧情 / 家庭", "c": "中国台湾 日本", "d": "杨德昌",
     "p": "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p2923629857.jpg",
     "q": "你不在了，留下来的人要怎么办？",
     "s": "台北一个中产家庭的一年：父亲 NJ 在事业与初恋间徘徊，母亲敏敏陷入信仰迷茫，儿子洋洋用相机记录看不见的世界，女儿婷婷经历初恋。一部关于生活本身的电影。"},
    {"t": "饮食男女", "o": "Eat Drink Man Woman", "y": 1994, "r": 9.1, "g": "剧情 / 家庭 / 喜剧", "c": "中国台湾 美国", "d": "李安",
     "p": "https://img2.doubanio.com/view/photo/s_ratio_poster/public/p1910899751.jpg",
     "q": "人生不能像做菜，把所有的料都准备好了才下锅。",
     "s": "台北名厨朱老先生丧妻后独自抚养三个女儿，每周日的家宴是全家唯一聚齐的时刻。大女儿想出嫁，二女儿情路坎坷，小女儿渴望自由，一桌家宴道尽两代人的心事。"},
    {"t": "喜宴", "o": "The Wedding Banquet", "y": 1993, "r": 9.0, "g": "剧情 / 喜剧 / 爱情", "c": "中国台湾 美国", "d": "李安",
     "p": "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p2249048907.jpg",
     "q": "你正看着我呢。",
     "s": "旅美青年高伟同与同性恋人赛门同居多年，为安抚台北父母的催婚，决定与来自大陆的薇薇假结婚。一场中式喜宴上，谎言与真情交错，所有人的心事都被端上台面。"},
    {"t": "卧虎藏龙", "o": "Crouching Tiger, Hidden Dragon", "y": 2000, "r": 8.4, "g": "剧情 / 动作 / 爱情", "c": "中国大陆 中国香港 美国 中国台湾", "d": "李安",
     "p": "https://img3.doubanio.com/view/photo/s_ratio_poster/public/p2908175252.jpg",
     "q": "把手握紧，里面什么也没有；把手放开，你得到的是一切。",
     "s": "清朝末年，大侠李慕白欲归隐山林，将青冥剑托付给俞秀莲。不料宝剑被玉娇龙盗走，三人在江湖恩怨与儿女情长之间，展开一场竹林间的绝世对决。"},
]


def daily_movie(today):
    """按一年中的天数轮换，每日一部。"""
    return MOVIES[today.timetuple().tm_yday % len(MOVIES)]


# 每日一句（经典心灵语录，附作者；按一年中的天数轮换）
DAILY_QUOTES = [
    ("我孤身一人难以改变世界，但是我能抛出一块石头，以激起阵阵涟漪。", "特蕾莎修女"),
    ("生活不是等待风暴过去，而是学会在雨中起舞。", "薇薇安·格林"),
    ("山重水复疑无路，柳暗花明又一村。", "陆游"),
    ("不要为明天忧虑，因为明天自有明天的忧虑。", "《圣经·马太福音》"),
    ("路漫漫其修远兮，吾将上下而求索。", "屈原"),
    ("最困难的时候，就是最接近成功的时候。", "拿破仑"),
    ("海内存知己，天涯若比邻。", "王勃"),
    ("不积跬步，无以至千里；不积小流，无以成江海。", "荀子"),
    ("只有把抱怨环境的心情，化为上进的力量，才是成功的保证。", "罗曼·罗兰"),
    ("纸上得来终觉浅，绝知此事要躬行。", "陆游"),
    ("人生没有彩排，每一天都是现场直播。", "佚名"),
    ("心若没有栖息的地方，到哪里都是流浪。", "三毛"),
    ("天行健，君子以自强不息。", "《周易》"),
    ("你所浪费的今天，是昨天死去的人奢望的明天。", "佚名"),
    ("成功的秘诀，在于对目标坚忍不拔。", "本杰明·迪斯雷利"),
    ("山不在高，有仙则名；水不在深，有龙则灵。", "刘禹锡"),
    ("不要等待机会，而要创造机会。", "萧伯纳"),
    ("千里之行，始于足下。", "老子"),
    ("生活就是一面镜子，你笑，它也笑；你哭，它也哭。", "佚名"),
    ("宝剑锋从磨砺出，梅花香自苦寒来。", "佚名"),
    ("不要因为走得太远，而忘记了为什么出发。", "纪伯伦"),
    ("志不强者智不达。", "墨子"),
    ("最美的风景，是回家的路。", "佚名"),
    ("业精于勤，荒于嬉；行成于思，毁于随。", "韩愈"),
    ("不要害怕失败，害怕失败才是最大的失败。", "佚名"),
    ("黑夜无论怎样悠长，白昼总会到来。", "莎士比亚"),
    ("知之者不如好之者，好之者不如乐之者。", "孔子"),
    ("把每一个平凡的日子，过成值得纪念的日子。", "佚名"),
    ("宠辱不惊，闲看庭前花开花落。", "陈眉公"),
    ("所有的失去，都会以另一种方式归来。", "佚名"),
]


def daily_quote(today):
    """按一年中的天数轮换，每日一句。"""
    return DAILY_QUOTES[today.timetuple().tm_yday % len(DAILY_QUOTES)]


# 天气代码 → 表情（七天预报用）
WX_ICON = {0: "☀️", 1: "🌤️", 2: "⛅", 3: "☁️", 45: "🌫️", 48: "🌫️",
           51: "🌦️", 53: "🌦️", 55: "🌦️", 56: "🌧️", 57: "🌧️",
           61: "🌧️", 63: "🌧️", 65: "⛈️", 66: "🌧️", 67: "🌧️",
           71: "🌨️", 73: "❄️", 75: "❄️", 77: "🌨️", 80: "🌧️", 81: "🌧️",
           82: "⛈️", 85: "🌨️", 86: "❄️", 95: "⛈️", 96: "⛈️", 99: "⛈️"}

def esc(s):
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

CSS = """
/* v5 A/B/C/D 分区布局，Glance 风格延续：中性干净、高密度、小部件化。分区可独立调整。 */
:root{
  --bg:#f4f4f5; --card:#fff; --border:#e4e4e7; --border-soft:#ececee;
  --ink:#18181b; --muted:#71717a; --faint:#a1a1aa;
  --accent:#0f766e; --accent-soft:#f0fdfa; --accent-border:#99f6e4;
  --red:#d43d2a; --radius:10px; --top-h:52px; --side-w:236px;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif}
a{color:inherit;text-decoration:none}
a:hover{color:var(--accent)}

/* ---- 顶栏：A区品牌 + C区导航 ---- */
.app-top{position:sticky;top:0;z-index:30;background:rgba(244,244,245,.92);backdrop-filter:blur(10px);border-bottom:1px solid var(--border)}
.app-top-in{max-width:1600px;margin:0 auto;padding:0 20px;height:var(--top-h);display:flex;align-items:center;gap:18px}
.brand{font-size:17px;font-weight:800;letter-spacing:.02em;white-space:nowrap}
.brand span{color:var(--accent)}
.cnav{display:flex;gap:2px}
.cnav a{padding:6px 12px;border-radius:8px;font-size:14px;color:var(--muted);white-space:nowrap;font-weight:600}
.cnav a:hover{color:var(--ink);background:#ececee}
.cnav a.on{color:var(--accent);background:var(--accent-soft)}
.wx-chip{margin-left:auto;font-size:12.5px;color:var(--muted);background:var(--card);border:1px solid var(--border);border-radius:999px;padding:4px 10px;white-space:nowrap}

/* ---- 壳：B区左栏 + D区主内容 ---- */
.shell{max-width:1600px;margin:0 auto;display:flex;align-items:flex-start}
.sidebar{width:var(--side-w);flex:none;position:sticky;top:var(--top-h);height:calc(100dvh - var(--top-h));overflow-y:auto;padding:16px 12px 110px;border-right:1px solid var(--border);background:rgba(255,255,255,.72);backdrop-filter:blur(8px)}
.sidebar::-webkit-scrollbar{width:0}
.main{flex:1;min-width:0;padding:18px 22px 110px}
.shell.no-side .main{padding:18px 22px 110px}
.shell.no-side .sidebar{display:none}

/* 粉丝 B区资料卡（头像/名称/切换账号） */
.fans-profile{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:16px 12px 12px;text-align:center;margin-bottom:12px}
.fp-avatar{width:64px;height:64px;border-radius:50%;object-fit:cover;border:2px solid var(--border-soft);background:#eef2f7}
.fp-name{margin-top:10px;font-size:15px;font-weight:800;line-height:1.4}
.fp-sub{margin-top:2px;font-size:12.5px;color:var(--muted)}
.fp-switch{display:block;margin-top:10px;padding:7px 10px;border:1px solid var(--accent-border);border-radius:8px;background:var(--accent-soft);color:var(--accent);font-size:13px;font-weight:700}
.fp-switch:hover{color:var(--accent)}
.fans-item{padding:9px 10px;font-size:14px}
.fans-item span:first-child{font-size:16px}

/* B区菜单 */
.side-sec{font-size:11px;font-weight:700;letter-spacing:.08em;color:var(--faint);text-transform:uppercase;padding:10px 10px 5px}
.side-sec:first-child{padding-top:2px}
.side-group{margin-bottom:6px}
.bg-set-btn{margin-left:8px;border:1px solid var(--border);background:var(--card);border-radius:999px;width:32px;height:32px;font-size:16px;cursor:pointer;line-height:1;flex:none}
.bg-set-btn:hover{background:#ececee}
.bg-set-panel{position:fixed;top:calc(var(--top-h) + 8px);right:16px;z-index:60;width:260px;background:var(--card);border:1px solid var(--border);border-radius:12px;box-shadow:0 8px 28px rgba(0,0,0,.14);padding:14px 16px;display:none}
.bg-set-panel.open{display:block}
.bg-set-t{font-size:14px;font-weight:700;margin-bottom:10px}
.bg-set-panel input[type=range]{width:100%;accent-color:var(--accent)}
.bg-set-v{display:flex;justify-content:space-between;align-items:baseline;gap:8px;margin-top:6px;font-size:12px;color:var(--muted)}
.bg-set-v span:first-child{font-size:14px;font-weight:700;color:var(--ink)}
.bg-credit{margin-top:18px;padding:10px 4px 2px;border-top:1px solid var(--border);font-size:11px;line-height:1.5;color:var(--muted)}
.review-nav{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0 14px}
.review-date{padding:4px 10px;border:1px solid var(--border);border-radius:999px;font-size:12.5px;color:var(--muted);background:var(--card)}
.review-date.on{color:var(--accent);border-color:var(--accent-border);background:var(--accent-soft);font-weight:700}
.review-body{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:18px 20px}
.review-body h1{font-size:19px;margin:0 0 10px}
.review-body h2{font-size:16.5px;margin:18px 0 8px}
.review-body h3{font-size:15px;margin:14px 0 6px}
.review-body p{margin:6px 0;font-size:14px;line-height:1.7}
.review-body ul,.review-body ol{margin:6px 0;padding-left:22px;font-size:14px;line-height:1.7}
.review-body li{margin:4px 0}
.review-body blockquote{margin:10px 0;padding:10px 14px;background:var(--accent-soft);border-left:3px solid var(--accent-border);border-radius:0 8px 8px 0;font-size:13px;color:var(--muted);line-height:1.7}
.review-body strong{color:var(--ink)}
.side-group-h{display:flex;align-items:center;gap:8px;width:100%;padding:9px 10px;border:0;background:none;font:inherit;font-size:14px;font-weight:700;color:var(--ink);cursor:pointer;border-radius:8px;text-align:left}
.side-group-h:hover{background:#ececee}
.side-group-h .chev{margin-left:auto;color:var(--faint);transition:transform .18s ease;font-size:12px;line-height:1}
.side-group.open .side-group-h{color:var(--accent)}
.side-group.open .side-group-h .chev{transform:rotate(180deg)}
.side-items{display:none}
.side-group.open .side-items{display:block}
.side-item{display:flex;align-items:center;gap:8px;padding:7px 10px;border-radius:8px;font-size:13.5px;color:#3f3f46}
.side-item.sub{padding-left:26px;font-size:13px}
.review-sub{display:none}
.review-sub.open{display:block}
button.review-parent{width:100%;text-align:left;font:inherit;cursor:pointer;border:0;background:none}
.review-parent .chev{margin-left:auto;font-size:11px;color:var(--muted)}
.side-item:hover{background:#ececee;color:var(--ink)}
.side-item.on{color:var(--accent);background:var(--accent-soft);font-weight:700}
.side-item .n{margin-left:auto;font-size:11px;color:var(--faint);font-variant-numeric:tabular-nums}
.side-note{padding:10px;font-size:12px;color:var(--muted);line-height:1.6}

/* D区首页仪表盘 */
.dateline{color:var(--muted);font-size:13px;margin-bottom:12px;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.dateline b{color:var(--ink);font-size:15px}
.home-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:14px;align-items:stretch}
.home-grid .wide{grid-column:1 / -1}
.home-card[data-card="weather"],.home-card[data-card="time"]{grid-column:span 2}
.home-card[data-card="countdown"],.home-card[data-card="fans"]{grid-column:span 1}
.home-card[data-card="movie"]{grid-column:1 / -1}
.home-card[data-card="notes"]{grid-column:span 2}
.notes-add{display:flex;gap:6px;margin-bottom:6px}
.notes-add input{flex:1;min-width:0;background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.14);border-radius:8px;color:#f1f5f9;padding:7px 10px;font-size:13.5px;outline:none}
.notes-add input::placeholder{color:#64748b}
.notes-add button{flex:none;background:var(--accent);color:#fff;border:none;border-radius:8px;padding:7px 12px;font-size:13px;cursor:pointer;white-space:nowrap}
.notes-status{font-size:12px;color:#f87171;min-height:15px;margin-bottom:2px}
.notes-list{display:flex;flex-direction:column;overflow:auto;max-height:220px}
.note-row{display:flex;align-items:center;gap:8px;padding:6px 2px;border-bottom:1px solid rgba(255,255,255,.06);font-size:13.5px;color:#e2e8f0}
.note-row.done .note-text{color:#64748b;text-decoration:line-through}
.note-check{flex:none;width:22px;height:22px;border-radius:50%;border:1px solid rgba(255,255,255,.25);background:transparent;color:#94a3b8;font-size:12px;cursor:pointer;line-height:1;padding:0}
.note-row.done .note-check{background:#22c55e;border-color:#22c55e;color:#fff}
.note-text{flex:1;word-break:break-word}
.note-del{flex:none;background:none;border:none;color:#64748b;font-size:13px;cursor:pointer;padding:2px 4px}
.note-del:hover{color:#f87171}
.home-card[data-card="status"]{grid-column:span 2}
.home-card[data-card="calendar"]{grid-column:span 2}
.home-card[data-card="downloads"]{grid-column:span 2}
.status-list{display:flex;flex-direction:column}
.st-row{display:flex;align-items:center;gap:8px;padding:5px 2px;border-bottom:1px solid rgba(255,255,255,.06);font-size:13px;color:#e2e8f0}
.st-row:last-child{border-bottom:none}
.st-nm{flex:none;width:112px;color:#94a3b8;white-space:nowrap}
.st-bar{flex:1;height:6px;background:rgba(255,255,255,.1);border-radius:3px;overflow:hidden}
.st-bar i{display:block;height:100%;border-radius:3px}
.st-bar i.ok{background:#22c55e}
.st-bar i.warn{background:#f59e0b}
.st-bar i.bad{background:#ef4444}
.st-vl{flex:none;font-variant-numeric:tabular-nums;white-space:nowrap}
.dot-ok{color:#22c55e;margin-right:4px}
.dot-bad{color:#ef4444;margin-right:4px}
@media (max-width:900px){
  .home-grid{grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
  .home-card[data-card="weather"],.home-card[data-card="time"],.home-card[data-card="countdown"],.home-card[data-card="fans"],.home-card[data-card="notes"],.home-card[data-card="status"],.home-card[data-card="calendar"]{grid-column:auto}
}
.home-card{display:flex;flex-direction:column}
.home-card .widget-b{flex:1;display:flex;flex-direction:column}

/* 大时钟（无背景、居中；时间由 JS 每秒刷新） */
.clock-hero{text-align:center;color:var(--ink);padding:6px 0 18px;margin:0}
.clock-time{font-size:64px;font-weight:200;letter-spacing:.02em;line-height:1;font-variant-numeric:tabular-nums}
.clock-date{margin-top:8px;font-size:15px;color:var(--muted);letter-spacing:.04em}

/* 深色卡（天气 / 电影，仿 iTab） */
.widget.dark{background:linear-gradient(160deg,#1e293b,#111827);color:#e2e8f0;border-color:#1e293b}
.widget.dark .widget-h{border-bottom-color:rgba(255,255,255,.08)}
.widget.dark .widget-h .k{color:#64748b}
.widget.dark .widget-h h2{color:#f1f5f9}
.widget.dark .widget-h .more{color:#94a3b8}
.widget.dark a{color:#7dd3fc}
.home-card{cursor:grab}
.home-card.dragging{opacity:.45;cursor:grabbing}
.home-card.drag-over{outline:2px dashed var(--accent);outline-offset:2px}

/* 天气：当前＋七天 */
.wx-now{display:flex;align-items:center;gap:16px;padding:4px 2px 10px}
.wx-temp{font-size:52px;font-weight:200;line-height:1;color:#fff}
.wx-cond{font-size:14px;line-height:1.6;color:#e2e8f0}
.wx-cond small{color:#94a3b8}
.wx-week{display:grid;grid-template-columns:repeat(7,1fr);gap:4px;border-top:1px solid rgba(255,255,255,.08);padding-top:10px}
.wx-day{text-align:center}
.wd-nm{font-size:11.5px;color:#94a3b8}
.wd-ic{font-size:19px;margin:3px 0 2px}
.wd-t{font-size:11.5px;color:#e2e8f0;font-variant-numeric:tabular-nums;white-space:nowrap}

/* 每日一句（底部居中） */
.home-wrap{display:flex;flex-direction:column;min-height:calc(100dvh - var(--top-h) - 18px);margin-bottom:-110px}
.home-grid{margin-bottom:60px}
.daily-quote{margin:auto auto 28px;text-align:center;max-width:820px;padding:20px 16px 14px;background:radial-gradient(ellipse at center,rgba(0,0,0,.38) 0%,rgba(0,0,0,.18) 55%,rgba(0,0,0,0) 78%);border-radius:12px}
/* 时光卡背景：森林公路图（/time-bg.jpg），深罩保白字可读 */
.widget.dark.home-card[data-card="time"]{background-color:#111827;background-image:linear-gradient(rgba(8,18,14,.72),rgba(8,18,14,.78)),url("/time-bg.jpg?v=2");background-size:cover;background-position:center}
.daily-quote .q-text{font-family:"Noto Serif SC","Songti SC","STSong",SimSun,serif;font-size:19px;font-weight:500;color:#fff;line-height:1.8;letter-spacing:.02em;text-shadow:0 1px 3px rgba(0,0,0,.65),0 2px 14px rgba(0,0,0,.5)}
.daily-quote .q-author{margin-top:8px;font-family:"Noto Serif SC","Songti SC","STSong",SimSun,serif;font-size:13.5px;color:rgba(255,255,255,.88);text-shadow:0 1px 3px rgba(0,0,0,.65),0 2px 10px rgba(0,0,0,.5)}
/* 倒计时（深色月夜卡，仿 iTab） */
.cd-card{position:relative;overflow:hidden;background:radial-gradient(circle at 24% 12%,rgba(255,255,255,.85) 0%,rgba(226,232,240,.45) 14%,rgba(226,232,240,.12) 32%,rgba(226,232,240,0) 55%),linear-gradient(160deg,#0b1220 0%,#1e293b 55%,#0f172a 100%);color:#f1f5f9;border-color:rgba(255,255,255,.08)}
.cd-card::before{content:"";position:absolute;left:50%;top:56%;width:92px;height:92px;transform:translate(-50%,-50%);border-radius:50%;background:radial-gradient(circle at 38% 35%,#ffffff 0%,#e2e8f0 58%,#94a3b8 100%);box-shadow:0 0 22px 8px rgba(226,232,240,.5),0 0 60px 22px rgba(148,163,184,.25);opacity:.6;pointer-events:none}
.cd-card::after{content:"";position:absolute;inset:0;background:radial-gradient(circle at 24% 12%,rgba(255,255,255,.3) 0%,rgba(255,255,255,0) 42%);pointer-events:none}
.cd-card .widget-h,.cd-card .widget-b,.cd-card .widget-f{position:relative;z-index:1}
.cd-card .widget-h h2,.cd-card .cd-nm,.cd-card .cd-num{text-shadow:0 1px 6px rgba(0,0,0,.55)}
.cd-card .widget-f{border-top-color:rgba(255,255,255,.08);color:#94a3b8}
/* 粉丝卡：换背景——深青绿晕，不用月亮圆盘（窄卡上月亮会压住号名/“粉”字） */
/* 粉丝卡背景：牵手剪影图（/fans-bg.jpg），深罩保白字可读 */
.widget.dark.home-card.fans-card{position:relative;overflow:hidden;background-color:#1e1b2e;background-image:linear-gradient(rgba(10,8,24,.45),rgba(10,8,24,.62)),url("/fans-bg.jpg?v=1");background-size:cover;background-position:center 30%;color:#f1f5f9;border-color:rgba(255,255,255,.08)}
.fans-card .widget-h{border-bottom-color:rgba(255,255,255,.08)}
.fans-card .widget-f{border-top-color:rgba(255,255,255,.08);color:#94a3b8}
.fans-card .cd-nm{font-size:12px;white-space:nowrap;flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis}
.fans-card .cd-num{font-size:16px;white-space:nowrap;flex:none;text-shadow:0 1px 6px rgba(0,0,0,.55)}
.fans-card .cd-num small{font-size:12px}
.fans-card .cd-row{gap:4px;overflow:hidden}
.fans-card .widget-b{padding:12px 10px}
/* 下载合一卡 B版：深色渐变，上下两行＋错开排布 */
.home-card[data-card="downloads"]{background:linear-gradient(135deg,#16213f 0%,#123a4d 48%,#0b3d3a 100%);color:#eef6f3;border-color:rgba(255,255,255,.14);border-radius:20px;overflow:hidden;position:relative;box-shadow:0 18px 40px rgba(0,0,0,.45),inset 0 1px 0 rgba(255,255,255,.14)}
.home-card[data-card="downloads"]::after{content:"";position:absolute;width:220px;height:220px;border-radius:50%;background:radial-gradient(circle,rgba(94,234,212,.28),transparent 68%);top:-90px;right:-60px;pointer-events:none}
.home-card[data-card="downloads"] .widget-h{display:flex;flex-direction:row-reverse;justify-content:space-between;align-items:baseline;gap:10px;padding:12px 24px 0;border-bottom:none;position:relative;z-index:1}
.home-card[data-card="downloads"] .widget-h h2{font-size:16px;letter-spacing:.02em;color:#eef6f3}
.home-card[data-card="downloads"] .widget-h .k{font-size:12px;color:#9fb8b2;letter-spacing:.14em;text-transform:uppercase}
.home-card[data-card="downloads"] .widget-b{padding:0 24px;position:relative;z-index:1}
.home-card[data-card="downloads"] .b-row{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;padding:10px 0 8px;border-bottom:1px solid rgba(255,255,255,.13)}
.home-card[data-card="downloads"] .b-name{font-size:16px;font-weight:800;color:#eef6f3;white-space:nowrap}
.home-card[data-card="downloads"] .b-name span{font-size:12px;font-weight:500;color:#9fb8b2;margin-left:6px}
.home-card[data-card="downloads"] .b-time{font-size:13px;color:#a8c2bb;margin-top:6px;line-height:1.5}
.home-card[data-card="downloads"] .b-num{font-size:36px;line-height:.95;font-weight:900;letter-spacing:-.03em;white-space:nowrap;color:#5eead4;text-shadow:0 0 24px rgba(94,234,212,.45);font-variant-numeric:tabular-nums}
.home-card[data-card="downloads"] .b-row.alt .b-num{color:#facc15;text-shadow:0 0 24px rgba(250,204,21,.35)}
.home-card[data-card="downloads"] .b-num small{font-size:14px;font-weight:700;margin-left:3px}
.home-card[data-card="downloads"] .b-stagger{position:relative;z-index:1;padding-top:10px;display:grid;gap:8px}
.home-card[data-card="downloads"] .b-views{justify-self:start;display:flex;align-items:center;min-height:34px;font-size:13px;color:#d7ece7;background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.14);padding:8px 12px;border-radius:999px}
.home-card[data-card="downloads"] .b-views strong{font-size:16px;color:#fff;margin:0 3px}
.home-card[data-card="downloads"] .b-dl{justify-self:end;display:inline-flex;align-items:center;min-height:38px;background:#5eead4;color:#062a26;font-size:14px;font-weight:900;padding:10px 16px;border-radius:999px;text-decoration:none;box-shadow:0 8px 22px rgba(94,234,212,.28)}
.home-card[data-card="downloads"] .widget-f{margin-top:8px;padding:0 24px 12px;border-top:none;display:flex;justify-content:flex-start;gap:16px;flex-wrap:wrap;font-size:13px;color:#9fb8b2;position:relative;z-index:1}
.home-card[data-card="downloads"] .widget-f a{color:#c9e8e1;text-decoration:none;border-bottom:1px dotted rgba(255,255,255,.4);padding:4px 0;min-height:26px;display:inline-flex;align-items:center}
@media (max-width:520px){.home-card[data-card="downloads"] .b-num{font-size:30px}.home-card[data-card="downloads"] .b-row{padding:8px 0 6px}.home-card[data-card="downloads"] .widget-h{padding:10px 18px 0}.home-card[data-card="downloads"] .widget-b{padding:0 18px}.home-card[data-card="downloads"] .widget-f{padding:0 18px 12px}}
.home-card[data-card="gold"]{grid-column:span 2;background:linear-gradient(135deg,#1a1405 0%,#3d2c07 48%,#241a04 100%);color:#f7ecd0;border-color:rgba(255,255,255,.14);border-radius:20px;overflow:hidden;position:relative;box-shadow:0 18px 40px rgba(0,0,0,.45),inset 0 1px 0 rgba(255,255,255,.14)}
.home-card[data-card="gold"]::after{content:"";position:absolute;width:220px;height:220px;border-radius:50%;background:radial-gradient(circle,rgba(250,204,21,.28),transparent 68%);top:-90px;right:-60px;pointer-events:none}
.home-card[data-card="gold"] .widget-h{display:flex;flex-direction:row-reverse;justify-content:space-between;align-items:baseline;gap:10px;padding:12px 24px 0;border-bottom:none;position:relative;z-index:1}
.home-card[data-card="gold"] .widget-h h2{font-size:16px;letter-spacing:.02em;color:#f7ecd0}
.home-card[data-card="gold"] .widget-h .k{font-size:12px;color:#cbb37a;letter-spacing:.14em;text-transform:uppercase}
.home-card[data-card="gold"] .widget-b{padding:0 24px;position:relative;z-index:1}
.home-card[data-card="gold"] .g-price{font-size:36px;line-height:1;font-weight:900;letter-spacing:-.03em;white-space:nowrap;color:#facc15;text-shadow:0 0 24px rgba(250,204,21,.45);font-variant-numeric:tabular-nums}
.home-card[data-card="gold"] .g-price small{font-size:14px;font-weight:700;margin-left:3px}
.home-card[data-card="gold"] .g-chg{font-size:15px;font-weight:800;white-space:nowrap}
.home-card[data-card="gold"] .g-chg.up{color:#f87171}
.home-card[data-card="gold"] .g-chg.down{color:#4ade80}
.home-card[data-card="gold"] .g-rows{display:flex;flex-direction:column;gap:8px;padding:10px 0 4px}
.home-card[data-card="gold"] .g-row{display:flex;justify-content:space-between;align-items:baseline;gap:12px;font-size:13px;color:#e8d9ac;border-bottom:1px solid rgba(255,255,255,.10);padding-bottom:8px}
.home-card[data-card="gold"] .g-row:last-child{border-bottom:none}
.home-card[data-card="gold"] .g-row b{color:#fff;font-variant-numeric:tabular-nums}
.home-card[data-card="gold"] .g-row .neg{color:#4ade80;font-weight:800}
.home-card[data-card="gold"] .g-foot{padding:0 24px 12px;font-size:12px;color:#cbb37a;position:relative;z-index:1}
@media (max-width:520px){.home-card[data-card="gold"] .g-price{font-size:30px}.home-card[data-card="gold"] .widget-h{padding:10px 18px 0}.home-card[data-card="gold"] .widget-b{padding:0 18px}.home-card[data-card="gold"] .g-foot{padding:0 18px 12px}}

.cd-card .widget-h{border-bottom-color:rgba(255,255,255,.08)}
.cd-card .widget-h .k{color:#64748b}
.cd-card .widget-h h2{color:#f1f5f9}
.cd-rows{flex:1;display:flex;flex-direction:column;gap:14px;justify-content:space-evenly}
.cd-row{display:flex;align-items:baseline;gap:8px}
.cd-nm{font-size:14px;font-weight:600;color:#e2e8f0}
.cd-num{margin-left:auto;font-size:28px;font-weight:800;color:#fff;font-variant-numeric:tabular-nums;line-height:1}
.cd-num small{font-size:14px;font-weight:600;color:#cbd5e1}
.cd-card{cursor:pointer}
.cd-set-hint{margin-left:auto;font-size:12px;color:#94a3b8}
/* 倒计时设置弹窗 */
.cd-modal{position:fixed;inset:0;z-index:100;display:none;align-items:center;justify-content:center;background:rgba(0,0,0,.45)}
.cd-modal.open{display:flex}
.cd-modal-box{width:min(440px,92vw);max-height:82vh;overflow:auto;background:var(--card);border-radius:14px;box-shadow:0 16px 48px rgba(0,0,0,.25);padding:18px 20px}
.cd-modal-box h3{margin:0 0 4px;font-size:17px}
.cd-modal-box .sub{font-size:12.5px;color:var(--muted);margin-bottom:12px}
.cd-list{display:flex;flex-direction:column;gap:8px;margin-bottom:14px}
.cd-item{display:flex;align-items:center;gap:8px;font-size:13.5px;border:1px solid var(--border);border-radius:8px;padding:8px 10px}
.cd-item .nm{font-weight:700}
.cd-item .dt{color:var(--muted);font-size:12.5px}
.cd-item .days{margin-left:auto;font-weight:800;color:var(--red)}
.cd-item .del{border:0;background:none;color:var(--muted);cursor:pointer;font-size:15px;padding:0 2px}
.cd-item .del:hover{color:var(--red)}
.cd-item.builtin{opacity:.75}
.cd-add{display:flex;gap:8px}
.cd-add input{flex:1;min-width:0;border:1px solid var(--border);border-radius:8px;padding:8px 10px;font:inherit;font-size:13.5px}
.cd-add input[type=date]{flex:0 0 150px}
.cd-add button,.cd-close{border:0;border-radius:8px;padding:8px 14px;font:inherit;font-size:13.5px;cursor:pointer;background:var(--accent);color:#fff;font-weight:700}
.cd-close{width:100%;margin-top:14px;background:#e4e4e7;color:#18181b}

/* 每日电影（深色卡） */
.mv{display:flex;gap:16px;align-items:flex-start}
.mv-info{flex:1;min-width:0}
.mv-title{font-size:22px;font-weight:800;color:#fff;line-height:1.3}
.mv-rate{margin:6px 0;font-size:13px;color:#cbd5e1}
.mv-stars{color:#f59e0b;letter-spacing:1px;margin-right:6px}
.mv-rate b{color:#fff;font-size:15px}
.mv-tags{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0;font-size:12px;color:#cbd5e1}
.mv-tags span{background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.14);border-radius:999px;padding:2px 9px}
.mv-dir{font-size:13px;color:#94a3b8;margin:4px 0 8px}
.mv-quote{background:rgba(255,255,255,.07);border-radius:10px;padding:10px 12px;font-size:14px;line-height:1.7;color:#f1f5f9;margin:0 0 10px}
.mv-plot{font-size:13.5px;line-height:1.75;color:#cbd5e1;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
.mv-poster{width:150px;flex:none;border-radius:10px;box-shadow:0 4px 16px rgba(0,0,0,.4)}

/* 首页日历卡（深色；月历：阳历＋农历＋节假日＋节气） */
.home-card[data-card="calendar"] table.cal{width:100%;border-collapse:collapse;font-size:12px;text-align:center}
.home-card[data-card="calendar"] table.cal th{color:#94a3b8;font-weight:600;padding:2px 0;font-size:11px}
.home-card[data-card="calendar"] table.cal td{padding:3px 0 4px;position:relative;color:#e2e8f0}
.home-card[data-card="calendar"] table.cal td.today{color:#f87171;font-weight:800}
.home-card[data-card="calendar"] table.cal td .cal-day{font-size:14px;line-height:1.25}
.home-card[data-card="calendar"] table.cal td .cal-sub{font-size:10px;line-height:1.3;color:#94a3b8}
.home-card[data-card="calendar"] table.cal td .cal-sub.hol{color:#f87171;font-weight:700}
.home-card[data-card="calendar"] table.cal td .cal-sub.term{color:#34d399;font-weight:700}
.home-card[data-card="calendar"] table.cal td .dot{position:absolute;top:1px;right:2px;width:4px;height:4px;border-radius:50%;background:#f87171}
.home-card[data-card="calendar"] .meta{font-size:11px;color:#94a3b8;margin-top:6px}

/* 小部件通用 */
.widget{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden}
.home-card{transition:box-shadow .15s,opacity .15s;user-select:none;-webkit-user-select:none}
.home-card.dragging{opacity:.45}
.home-card.drop-before{box-shadow:0 -3px 0 0 var(--accent)}
.home-card.drop-after{box-shadow:0 3px 0 0 var(--accent)}
.drag-handle{cursor:grab;font-size:13px;color:var(--faint);letter-spacing:1px;flex:none;user-select:none}
.drag-handle:active{cursor:grabbing}
.widget-h{padding:11px 14px 9px;border-bottom:1px solid var(--border-soft);display:flex;align-items:baseline;gap:8px}
.widget-h .k{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--faint)}
.widget-h h2{margin:0;font-size:15px;font-weight:700;letter-spacing:.01em}
.widget-h .more{margin-left:auto;font-size:12px;color:var(--muted);white-space:nowrap}
.widget-h .more:hover{color:var(--accent)}
.widget-b{padding:12px 14px}

/* 新闻（D区） */
#news .sec-head{padding:2px 2px 8px}
#news .sec-head .k{font-size:11px;font-weight:700;letter-spacing:.08em;color:var(--faint);text-transform:uppercase}
#news .sec-head h2{margin:2px 0 2px;font-size:20px;font-weight:800;letter-spacing:.01em}
#news .sec-head p{margin:0;color:var(--muted);font-size:13px}
#news .card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:14px;margin:12px 0}
#news .card h3{margin:0 0 3px;font-size:15.5px;font-weight:700;line-height:1.5}
#news .card p{margin:0 0 6px;font-size:14px;color:#3f3f46}
#news .card .meta{font-size:12px;color:var(--faint);margin-bottom:6px}
#news .card .rel{font-size:13px;color:#52525b;background:#fafafa;border:1px solid var(--border-soft);border-radius:8px;padding:7px 9px;margin:6px 0}
#news .card .act{font-size:13px;color:#0f766e;background:var(--accent-soft);border-radius:8px;padding:7px 9px}
#news .empty{padding:18px 4px;color:var(--muted);font-size:13.5px}
#news.switching{opacity:0;transition:opacity .16s ease}
#news:not(.switching){transition:opacity .22s ease}
a.brief-item{display:flex;gap:10px;align-items:baseline;padding:9px 4px;border-top:1px solid var(--border-soft);font-size:14px}
a.brief-item:first-of-type{border-top:none}
a.brief-item .d{color:#52525b;font-size:12.5px;font-weight:600;white-space:nowrap;font-variant-numeric:tabular-nums}
a.brief-item .t{font-weight:600;line-height:1.5}
a.brief-item:hover{color:var(--accent)}
.rank-table{width:100%;border-collapse:collapse;font-size:13.5px}
.rank-table td{padding:6px 0;border-top:1px solid var(--border-soft)}
.rank-table th{padding:6px 0;text-align:left;font-weight:600}
.rank-table tr:first-child td{border-top:none}
.rank-table td.r{width:26px;color:var(--faint);font-variant-numeric:tabular-nums}
.rank-table td.s{text-align:right;color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap}
#news .rank-table td{padding:7px 0}

/* 简报 / 排行小部件（首页用） */
.brief-list a{display:flex;gap:9px;align-items:baseline;padding:7px 0;border-top:1px solid var(--border-soft);font-size:13.5px;line-height:1.5}
.brief-list a:first-child{border-top:none;padding-top:0}
.brief-list .d{color:var(--faint);font-size:12px;white-space:nowrap;font-variant-numeric:tabular-nums}
.brief-list .t{font-weight:600}
.mini-rank h4{margin:10px 0 2px;font-size:12px;color:var(--muted);font-weight:700;letter-spacing:.04em}
.mini-rank h4:first-child{margin-top:0}

/* 时光 */
.time-rows{flex:1;display:flex;flex-direction:column;gap:10px;justify-content:space-evenly}
.time-row{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.time-row .lb{font-size:13px;color:var(--muted);display:flex;align-items:center;gap:6px}
.time-row .lb .dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.time-row .num{font-size:24px;font-weight:800;letter-spacing:-.01em;font-variant-numeric:tabular-nums}
.time-row .num small{font-size:12px;font-weight:600;color:var(--muted)}
.time-row .sub{margin-left:auto;font-size:11.5px;color:var(--faint)}
.time-bar{height:6px;border-radius:999px;background:#e4e4e7;overflow:hidden;margin-top:12px}
.time-bar i{display:block;height:100%;background:linear-gradient(90deg,#14b8a6,#0f766e);border-radius:999px}
.time-note{margin-top:8px;font-size:12.5px;color:var(--muted)}
/* 时光卡深色版（同天气卡） */
.widget.dark .time-row .lb{color:#94a3b8}
.widget.dark .time-row .num{color:#fff}
.widget.dark .time-row .num small{color:#94a3b8}
.widget.dark .time-row .sub{color:#64748b}
.widget.dark .time-bar{background:rgba(255,255,255,.12)}
.widget.dark .time-note{color:#94a3b8}

/* 天气 */
.wx{display:flex;align-items:center;gap:12px}
.wx .city{font-size:12.5px;color:var(--muted)}
.wx .city b{display:block;font-size:14px;color:var(--ink)}
.wx .temp{font-size:34px;font-weight:800;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.wx .cond{font-size:12.5px;color:var(--muted);line-height:1.5}

/* 月历 */
table.cal{width:100%;border-collapse:collapse;text-align:center;font-size:13px}
table.cal th{color:var(--faint);font-weight:600;font-size:11.5px;padding:4px 0}
table.cal td{padding:5px 0;color:#3f3f46;position:relative}
table.cal td.empty{color:transparent}
table.cal td.today{font-weight:800;color:var(--red)}
table.cal td .dot{position:absolute;left:50%;bottom:1px;transform:translateX(-50%);width:5px;height:5px;border-radius:50%;background:var(--red)}
.meta{font-size:12px;color:var(--faint);margin-top:8px;line-height:1.5}

/* 粉丝 / 聊天小部件 */
.stat-rows{display:flex;flex-direction:column;gap:8px}
.stat-row{display:flex;align-items:baseline;justify-content:space-between;gap:8px;font-size:13.5px}
.stat-row .nm{color:var(--muted)}
.stat-row .vl{font-size:18px;font-weight:800;font-variant-numeric:tabular-nums}
.stat-row .vl small{font-size:11.5px;font-weight:600;color:var(--muted)}
/* 窄卡（倒计时/粉丝）卡头收紧：藏英文栏名，防标题换行 */
.home-card[data-card="countdown"] .widget-h .k,.home-card[data-card="fans"] .widget-h .k{display:none}
.home-card[data-card="countdown"] .widget-h,.home-card[data-card="fans"] .widget-h{gap:6px}
.home-card[data-card="countdown"] .widget-h h2,.home-card[data-card="fans"] .widget-h h2{white-space:nowrap}
.home-card[data-card="fans"] .stat-row{flex-wrap:wrap;gap:2px}
.home-card[data-card="fans"] .stat-row .nm{font-size:12.5px}
.home-card[data-card="fans"] .stat-row .vl{font-size:16px}
.home-card[data-card="fans"] .widget-f{flex-wrap:wrap;gap:4px}
.home-card[data-card="fans"] .widget-f span{display:none}
.chat-prev{font-size:13.5px;color:#3f3f46;line-height:1.6;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.widget-f{padding:10px 14px;border-top:1px solid var(--border-soft);font-size:12.5px;color:var(--muted);display:flex;justify-content:space-between;align-items:center}
.widget-f a{color:var(--accent);font-weight:700}

/* D区内嵌页（粉丝 / 聊天） */
.embed-frame{width:100%;height:calc(100dvh - var(--top-h) - 40px);min-height:560px;border:1px solid var(--border);border-radius:var(--radius);background:#fff}
.embed-note{margin-top:10px;font-size:12.5px;color:var(--muted)}
/* 聊天模式：D区定高一屏，iframe 弹性填满，外层不再滚动 */
body.mode-chat{overflow:hidden}
.shell.chat-mode .main{display:flex;flex-direction:column;height:calc(100dvh - var(--top-h));padding:18px 22px 12px}
.shell.chat-mode .d-head{flex:none}
.shell.chat-mode .embed-frame{flex:1;min-height:0;height:auto}
.shell.chat-mode .embed-note{flex:none}
.embed-note a{color:var(--accent);font-weight:700}
.d-head{display:flex;align-items:baseline;gap:10px;margin-bottom:12px;flex-wrap:wrap}
.d-head h2{margin:0;font-size:19px;font-weight:800}
.d-head .sub{color:var(--muted);font-size:13px}

/* 底部栏（手机） */
.bottombar{position:fixed;left:50%;transform:translateX(-50%);bottom:14px;z-index:20;display:flex;gap:2px;background:rgba(255,255,255,.94);backdrop-filter:blur(10px);border:1px solid var(--border);border-radius:999px;padding:5px;box-shadow:0 6px 24px rgba(0,0,0,.10)}
.bottombar a{display:flex;flex-direction:column;align-items:center;gap:1px;padding:5px 13px;border-radius:999px;font-size:11px;color:var(--muted);line-height:1.3}
.bottombar a .ic{font-size:16px;line-height:1}
.bottombar a.on{color:var(--accent);background:var(--accent-soft);font-weight:700}

/* ---- 子页通用：粉丝独立页 / 简报详情 / 登录 ---- */
.wrap{max-width:1100px;margin:0 auto;padding:0 20px 110px}
.topbar{display:flex;align-items:center;gap:12px;padding:14px 0 10px}
.topbar h1{margin:0;font-size:19px;font-weight:800}
.topbar a{font-size:20px;color:var(--muted)}
.topbar a:hover{color:var(--ink)}
.topbar .btn{margin-left:auto}
.btn{display:inline-block;padding:7px 12px;border:1px solid var(--border);border-radius:8px;background:var(--card);font-size:13px;font-weight:700;color:var(--ink)}
.btn:hover{border-color:var(--accent);color:var(--accent)}
.fans-frame{margin-top:4px}
.fans-frame iframe{width:100%;height:calc(100dvh - 180px);min-height:560px;border:1px solid var(--border);border-radius:var(--radius);background:#fff}
.banner{margin-top:12px;font-size:12.5px;color:var(--muted);background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:10px 12px;line-height:1.6}
.back{display:inline-block;font-size:13.5px;color:var(--muted);padding:12px 0 4px}
.back:hover{color:var(--accent)}
.sec-head{padding:12px 0 4px}
.sec-head .k{font-size:11px;font-weight:700;letter-spacing:.08em;color:var(--faint);text-transform:uppercase}
.sec-head h2{margin:2px 0 2px;font-size:20px;font-weight:800;letter-spacing:.01em}
.sec-head p{margin:0;color:var(--muted);font-size:13px}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:14px;margin:12px 0}
.card h2{font-size:16.5px;margin:0 0 8px}
.act{font-size:13px;color:#0f766e;background:var(--accent-soft);border-radius:8px;padding:7px 9px}
.overview{background:var(--accent-soft);border-left:3px solid var(--accent);border-radius:0 8px 8px 0;padding:10px 12px;margin:12px 0;font-size:14.5px;line-height:1.6}
ol.prog{margin:6px 0 6px 20px;font-size:14.5px;line-height:1.6}
ol.prog li{margin-bottom:4px}
.impact{border-top:1px dashed var(--border);padding:10px 0}
.impact .lb{display:inline-block;background:var(--accent-soft);color:var(--accent);font-size:12px;border-radius:6px;padding:1px 8px;margin-bottom:4px;font-weight:700}
.impact .hd{font-weight:700;font-size:14.5px;margin-bottom:2px}
.impact p{margin:0 0 6px;font-size:14px;color:#3f3f46}
.bl{margin:8px 0 8px 18px;font-size:14.5px;line-height:1.6}
.bl li{margin-bottom:4px}
.login{max-width:340px;margin:80px auto;background:var(--card);border:1px solid var(--border);border-radius:12px;padding:28px 24px}
.login h2{font-size:18px;margin:0 0 6px}
.login p{color:var(--muted);font-size:13px;margin:0 0 16px}
.login input{width:100%;padding:11px;border:1px solid var(--border);border-radius:8px;font-size:15px;margin-bottom:12px}
.login button{width:100%;padding:11px;background:var(--accent);color:#fff;border:none;border-radius:8px;font-size:15px;font-weight:700}
.err{color:var(--red);font-size:13px;margin-bottom:10px}

/* 响应式 */
@media (min-width:901px){
  .bottombar{display:none}
}
@media (max-width:900px){
  .app-top-in{padding:0 12px;gap:10px}
  .cnav a{padding:6px 8px;font-size:13px}
  .wx-chip{display:none}
  .sidebar{position:fixed;left:0;top:var(--top-h);bottom:0;z-index:25;background:var(--bg);transform:translateX(-100%);transition:transform .2s ease;width:260px;box-shadow:4px 0 24px rgba(0,0,0,.08)}
  .sidebar.open{transform:none}
  .main{padding:14px 12px 108px}
  .side-toggle{display:inline-flex}
  .bottombar{bottom:10px}
  .bottombar a{padding:5px 11px}
}
.side-toggle{display:none;align-items:center;justify-content:center;width:34px;height:34px;border:1px solid var(--border);border-radius:8px;background:var(--card);font-size:16px;cursor:pointer}
@media (min-width:901px){
  .side-toggle{display:none}
}
"""

LOGIN_TMPL = ("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
'<meta name="viewport" content="width=device-width,initial-scale=1"><title>登录 · 我的个人工作台</title>'
"<style>" + CSS + "</style></head><body>"
'<div class="login"><h2>我的个人工作台</h2><p>请输入访问口令</p>'
"__ERR__" + '<form method="post" action="/login">' + '<input type="hidden" name="next" value="__NEXT__">'
'<input type="password" name="password" placeholder="口令" autofocus>'
'<button type="submit">进入</button></form></div></body></html>')

def login_html(err="", next_url="/"):
    return LOGIN_TMPL.replace("__ERR__", err).replace("__NEXT__", esc(next_url))

def work_login_html(err="", next_url="/?mode=work"):
    return (LOGIN_TMPL.replace("__ERR__", err).replace("__NEXT__", esc(next_url))
            .replace("我的个人工作台</h2><p>请输入访问口令</p>", "💼 工作</h2><p>请输入工作口令</p>")
            .replace('action="/login"', 'action="/work-login"')
            .replace("<title>登录 · 我的个人工作台</title>", "<title>工作口令 · 我的个人工作台</title>"))

def change_password_html():
    js = """<script>function changePw(kind){var pre=kind==='main'?'Main':'Work';var o=document.getElementById('old'+pre).value;var n=document.getElementById('new'+pre).value;var n2=document.getElementById('new'+pre+'2').value;var m=document.getElementById('pwMsg');if(!o||!n){m.textContent='当前口令和新口令都要填';return;}if(n!==n2){m.textContent='两次输入的新口令不一致';return;}fetch('/api/change-password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:kind,old:o,new:n})}).then(function(r){return r.json();}).then(function(d){if(d.ok){m.textContent='\u2705 已修改，立即生效';document.querySelectorAll('.pw-wrap input').forEach(function(i){i.value='';});}else{m.textContent='\u274c '+(d.msg||'修改失败');}}).catch(function(){m.textContent='网络抖了一下，没改成，再点一次';});}</script>"""
    return (page_head("修改口令")
        + '<style>.pw-wrap{max-width:520px;margin:24px auto;background:var(--card);border:1px solid var(--border);border-radius:14px;padding:22px}.pw-wrap h2{margin:0 0 6px;font-size:19px}.pw-wrap h3{margin:22px 0 4px;font-size:16px}.pw-wrap label{display:block;margin:10px 0;font-size:14px}.pw-wrap input{width:100%;padding:10px;margin-top:6px;border:1px solid var(--border);border-radius:9px;font-size:15px;box-sizing:border-box;background:var(--card)}.pw-btn{width:100%;padding:11px;margin-top:14px;border:none;border-radius:9px;background:#111827;color:#fff;font-size:15px;cursor:pointer}.pw-msg{min-height:20px;color:#b45309;font-size:14px}</style>'
        + '<div class="pw-wrap"><h2>\U0001f511 修改口令</h2>'
        + '<p class="meta">改完立即生效，当前浏览器会自动换成新口令登录。工作台口令改后，粉丝系统口令同步更新；每天简报推送不受影响。</p>'
        + '<div id="pwMsg" class="pw-msg"></div>'
        + '<h3>工作台口令</h3>'
        + '<label>当前口令<input type="password" id="oldMain" autocomplete="current-password"></label>'
        + '<label>新口令（4-64 个字符）<input type="password" id="newMain" autocomplete="new-password"></label>'
        + '<label>确认新口令<input type="password" id="newMain2" autocomplete="new-password"></label>'
        + '<button class="pw-btn" onclick="changePw(\'main\')">修改工作台口令</button>'
        + '<h3>工作口令</h3>'
        + '<label>当前工作口令<input type="password" id="oldWork" autocomplete="current-password"></label>'
        + '<label>新工作口令（4-64 个字符）<input type="password" id="newWork" autocomplete="new-password"></label>'
        + '<label>确认新工作口令<input type="password" id="newWork2" autocomplete="new-password"></label>'
        + '<button class="pw-btn" onclick="changePw(\'work\')">修改工作口令</button>'
        + '<p class="meta" style="margin-top:18px"><a href="/">\u2190 回工作台</a></p>'
        + '</div>' + js + page_foot())

def page_head(title):
    return (f'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>'
            f'<style>{CSS}</style></head><body><div class="wrap">')

def page_foot():
    return ('<div class="bottombar">'
            '<a href="/" class="on">🏠<span>工作台</span></a>'
            '<a href="/?mode=news">📰<span>新闻</span></a>'
            '<a href="/?mode=fans">👥<span>粉丝</span></a>'
            '<a href="/?mode=chat">💬<span>聊天</span></a>'
            '<a href="/?mode=command">🎛️<span>指挥</span></a>'
            '<a href="/?mode=principle">📖<span>原理</span></a>'
            '<a href="/?mode=work">💼<span>工作</span></a>'
            '</div></div></body></html>')

# ---- 粉丝管理系统（独立应用，端口 8080；经工作台 /fansapp/ 反向代理，与工作台共用一个零讯隧道）----
FANS_URL = "/fansapp/"
# 工作台免登 token：与粉丝系统同口令（跟随主口令）、同 HMAC 口令（wechat-fans），iframe 带上即免输口令
import hmac as _hmac, hashlib as _hashlib
def fans_token():
    return _hmac.new(b"wechat-fans", PASSWORD.encode(), _hashlib.sha256).hexdigest()
FANS_TOKEN = fans_token()
# 公众号账号：示例一个；有几个号就写几项。db 是粉丝库 sqlite 路径，avatar_file 是头像图路径（可留空）。
# 粉丝库不存在时，粉丝数显示为空，不会报错；粉丝系统本身是独立应用，这里只读数。
FANS_ACCOUNTS = {
    "main": {"name": "我的公众号", "db": os.path.join(BASE, "fans.db"), "avatar_file": ""},
}


def fans_live_count(db_path):
    """读粉丝库当前关注数（subscribe='1'）；读不到返回 None。"""
    try:
        import sqlite3

        con = sqlite3.connect(db_path)
        try:
            return con.execute(
                "SELECT COUNT(*) FROM wx_user_info WHERE subscribe='1'"
            ).fetchone()[0]
        finally:
            con.close()
    except Exception:
        return None


def latest_chat_preview():
    """Lei 收件箱最新一条的预览（用于工作台聊天入口卡片）。"""
    msgs = chat_load("Lei")
    if not msgs:
        return "还没有消息，来聊一句吧"
    last = msgs[-1]
    who = "你" if last.get("from") == "user" else "Lei"
    text = (last.get("text") or "").strip()
    if not text and last.get("image"):
        text = "[图片]"
    if len(text) > 34:
        text = text[:34] + "…"
    return "%s：%s" % (who, text)


def render_fans():
    return (
        page_head("粉丝管理")
        + '<div class="topbar"><a href="/">‹</a><h1>粉丝管理</h1>'
        + f'<a class="btn" href="{FANS_URL}" target="_blank" rel="noopener">在新窗口打开 ↗</a></div>'
        + f'<div class="fans-frame"><iframe src="{FANS_URL}" title="粉丝管理系统"></iframe></div>'
        + '<div class="banner">💡 粉丝系统是独立应用，这里内嵌显示；如果框里打不开，点右上角“在新窗口打开”。口令与工作台相同。</div>'
        + page_foot()
    )


def news_content_html(data, group, section_id):
    """新闻板块内容（仅 sec-head + 正文，不含分组标签与板块列表；B区左栏即导航）。供 D区与无刷新片段共用。"""
    secs = [s for s in SECTIONS if s[0] == group]
    sec = next((s for s in secs if s[1] == section_id), secs[0])
    gname = dict(GROUPS)[group]
    idx = SECTIONS.index(sec) + 1
    body = ""
    if sec[4] == "briefs":
        items = []
        for b in data["briefs"]:
            d = b.get("broadcast_date", "")
            items.append(f'<a class="brief-item" href="/?mode=news&group=world&section=lianbo&brief={d}"><div class="d">{d}</div><div class="t">{esc(b.get("headline") or d)}</div></a>')
        body = "\n".join(items) if items else '<div class="empty">暂无简报</div>'
    elif sec[4] == "rankings":
        def table(dim, title):
            rows = [r for r in data["rankings"] if r.get("dimension") == dim]
            rows.sort(key=lambda r: r.get("rank", 99))
            trs = "".join(f'<tr><td class="r">{r["rank"]}</td><td>{esc(r["model_name"])}</td><td class="s">{esc(r.get("score_label",""))}</td></tr>' for r in rows)
            return f'<div class="card"><h3>{title}</h3><table class="rank-table">{trs}</table><div class="meta">来源：{esc(rows[0].get("source_name","")) if rows else ""}</div></div>'
        body = table("overall", "综合 Top 5") + table("coding", "编程 Top 5")
    else:
        items = [n for n in data["news"] if n.get("category") == sec[4]]
        if not items:
            body = '<div class="empty">本板块暂无内容</div>'
        else:
            cards = []
            for n in items:
                cards.append(f'''<div class="card"><h3>{esc(n.get("title",""))}</h3>
<div class="meta">{esc(n.get("source_name",""))} · {esc(n.get("published_at",""))}</div>
<p>{esc(n.get("summary",""))}</p>
{f'<p class="rel">📌 {esc(n["why_it_matters"])}</p>' if n.get("why_it_matters") else ""}
{f'<div class="act">💡 {esc(n["action"])}</div>' if n.get("action") else ""}
</div>''')
            body = "".join(cards)
    return (f'<div class="sec-head"><div class="k">{gname} · 板块 {idx:02d}</div><h2>{esc(sec[2])}</h2><p>{esc(sec[3])}</p></div>'
            f'{body}')


SIDEBAR_ACCORDION_JS = """
<script>
(function(){
  document.querySelectorAll('.side-group-h').forEach(function(btn){
    btn.addEventListener('click', function(){
      var g = btn.closest('.side-group');
      var wasOpen = g.classList.contains('open');
      document.querySelectorAll('.side-group').forEach(function(x){ x.classList.remove('open'); });
      if(!wasOpen) g.classList.add('open');
    });
  });
})();
</script>
"""


NEWS_SWITCH_JS_V5 = """
<script>
(function(){
  var box = document.getElementById('news');
  function swap(clean, link){
    if(!box){ window.location = clean; return; }
    var fetchUrl = clean + (clean.indexOf('?') >= 0 ? '&' : '?') + 'frag=1';
    box.classList.add('switching');
    fetch(fetchUrl, {headers:{'X-Requested-With':'fetch'}})
      .then(function(r){ if(!r.ok) throw new Error('http '+r.status); return r.text(); })
      .then(function(html){
        box.innerHTML = html;
        box.classList.remove('switching');
        try{ history.pushState(null, '', clean); }catch(e){}
        document.querySelectorAll('.side-item').forEach(function(a){ a.classList.remove('on'); });
        if(link) link.classList.add('on');
        var y = box.getBoundingClientRect().top + window.scrollY - 60;
        window.scrollTo({top: y, behavior:'smooth'});
      })
      .catch(function(){ window.location = clean; });
  }
  document.addEventListener('click', function(e){
    var a = e.target.closest('a[data-sec]');
    if(!a) return;
    e.preventDefault();
    swap(a.getAttribute('href'), a);
  });
  window.addEventListener('popstate', function(){ window.location.reload(); });
  var tg = document.getElementById('sideToggle');
  if(tg){ tg.addEventListener('click', function(){ var s=document.querySelector('.sidebar'); if(s) s.classList.toggle('open'); }); }
  document.addEventListener('click', function(e){
    var s=document.querySelector('.sidebar');
    if(s && s.classList.contains('open') && !e.target.closest('.sidebar') && !e.target.closest('#sideToggle')){ s.classList.remove('open'); }
  });
})();
</script>
"""


def sidebar_html(mode, group, section_id, view, acct="main", period="daily"):
    """B区左栏。mode=home/news → 新闻分组+板块+日历；mode=fans → 账号资料卡+粉丝四项；mode=chat → 不用（B区隐藏）。"""
    if mode == "work":
        items = [
            ("overview", "🎯", "总览", "/?mode=work&view=overview"),
            ("weekly", "📅", "每周计划", "/?mode=work&view=weekly"),
            ("interview", "🎤", "面试准备", "/?mode=work&view=interview"),
            ("tracking", "📝", "投递跟踪", "/?mode=work&view=tracking"),
            ("rights", "🛡️", "权益与材料", "/?mode=work&view=rights"),
        ]
        lis = "".join(
            f'<a class="side-item{" on" if view == k else ""}" href="{u}"><span>{ic}</span>{nm}</a>'
            for k, ic, nm, u in items)
        return (f'<div class="side-sec">找工作 · 计划</div>{lis}'
                f'<div class="side-note">' + ('合同 ' + str(CONTRACT_END) + ' 到期，' if CONTRACT_END else '') + '目标到期前拿到 offer。打勾状态只存在本机浏览器里，换设备要重打。</div>')
    if mode == "fans":
        acct = acct if acct in FANS_ACCOUNTS else "main"
        _keys = list(FANS_ACCOUNTS)
        other = _keys[(_keys.index(acct) + 1) % len(_keys)] if len(_keys) > 1 else acct
        cfg = FANS_ACCOUNTS[acct]
        cnt = fans_live_count(cfg["db"])
        cnt_txt = f"{cnt} 粉" if cnt is not None else ""
        items = [
            ("trend", "📈", "粉丝趋势", f"/?mode=fans&view=trend&acct={acct}", ""),
            ("fans", "👥", "粉丝列表", f"/?mode=fans&view=fans&acct={acct}", ""),
            ("unfollows", "📉", "取关列表", f"/?mode=fans&view=unfollows&acct={acct}", ""),
            ("cohorts", "📊", "数据分析", f"/?mode=fans&view=cohorts&acct={acct}", ""),
        ]
        lis = "".join(
            f'<a class="side-item fans-item{" on" if view == k else ""}{cls}" href="{u}"><span>{ic}</span>{nm}</a>'
            for k, ic, nm, u, cls in items)
        # 数据复盘：父项（点它展开/收起子菜单）＋三个子菜单（日/周/月），默认收起，在复盘页时展开
        ritems = [
            ("daily", "📅", "日复盘", f"/?mode=fans&view=review&period=daily&acct={acct}"),
            ("weekly", "📆", "周复盘", f"/?mode=fans&view=review&period=weekly&acct={acct}"),
            ("monthly", "🗓️", "月复盘", f"/?mode=fans&view=review&period=monthly&acct={acct}"),
        ]
        rlis = "".join(
            f'<a class="side-item fans-item sub{" on" if (view == "review" and period == k) else ""}" href="{u}"><span>{ic}</span>{nm}</a>'
            for k, ic, nm, u in ritems)
        _ropen = " open" if view == "review" else ""
        _rchev = "▾" if view == "review" else "▸"
        rparent = (f'<button class="side-item fans-item review-parent{" on" if view == "review" else ""}" id="reviewParent" type="button">'
                   f'<span>📝</span>数据复盘<span class="chev">{_rchev}</span></button>')
        rsub = f'<div class="review-sub{_ropen}" id="reviewSub">{rlis}</div>'
        _switch = (f'<a class="fp-switch" href="/?mode=fans&view={view}&acct={other}">⇄ 切换到{FANS_ACCOUNTS[other]["name"]}</a>' if len(FANS_ACCOUNTS) > 1 else "")
        profile = (
            f'<div class="fans-profile">'
            f'<img class="fp-avatar" src="/fans-avatar/{acct}" alt="公众号头像" onerror="this.style.display=\'none\'">'
            f'<div class="fp-name">{esc(cfg["name"])}</div>'
            f'<div class="fp-sub">{cnt_txt}</div>'
            f'{_switch}'
            f'</div>')
        return (f'{profile}<div class="side-sec">粉丝管理</div>{lis}{rparent}{rsub}'
                f'<div class="side-note">数据分析即原分群分析。点 C 区“粉丝管理”回到总览（当前粉丝 / 累计取关 / 今日新增 / 今日取关 / 每日明细）。</div>')
    # home / news：新闻分组 + 板块（手风琴：默认只显示三个组名，点组名展开子菜单）
    GROUP_ICONS = {"world": "🌏", "career": "💻", "family": "🏠"}
    parts = ['<div class="side-sec">新闻解读</div>']
    for g, gname in GROUPS:
        secs = [s for s in SECTIONS if s[0] == g]
        is_open = " open" if (mode == "news" and g == group and section_id) else ""
        parts.append(
            f'<div class="side-group{is_open}" data-group="{g}">'
            f'<button class="side-group-h" type="button"><span>{GROUP_ICONS.get(g, "")}</span><span>{gname}</span><span class="chev">▾</span></button>'
            f'<div class="side-items">')
        for s in secs:
            on = " on" if (mode == "news" and s[0] == group and s[1] == section_id) else ""
            parts.append(
                f'<a class="side-item{on}" data-sec href="/?mode=news&group={s[0]}&section={s[1]}"><span>{s[2]}</span></a>')
        parts.append('</div></div>')
    return "".join(parts)


def home_dashboard_html(data, today, seniority, countdown, pct, weekday, m_d, w, fans_counts):
    """D区首页仪表盘（iTab 卡片风）：大时钟＋七天天气＋倒计时＋每日电影＋时光＋粉丝。卡片可拖拽换位。"""
    lunar = lunar_str(today)
    date_line = f"{m_d} · 星期{weekday}" + (f" · {lunar}" if lunar else "")
    # 大时钟（深色卡，整幅；时间由 JS 每秒刷新）
    clock = (f'<div class="clock-hero" data-card="clock"><div class="clock-time" id="clockTime">--:--</div>'
             f'<div class="clock-date">{date_line}</div></div>')
    # 天气（深色卡，七天预报）
    if w and w.get("days"):
        cur_ic = WX_ICON.get(w.get("code", 0), "☀️")
        day_cells = ""
        for i, d in enumerate(w["days"]):
            dt = date.fromisoformat(d["date"]) if d.get("date") else today
            if i == 0:
                nm = "今天"
            elif i == 1:
                nm = "明天"
            else:
                nm = "周" + "日一二三四五六"[dt.weekday()]
            day_cells += (f'<div class="wx-day"><div class="wd-nm">{nm}</div>'
                          f'<div class="wd-ic">{WX_ICON.get(d["code"], "☀️")}</div>'
                          f'<div class="wd-t">{d["min"]}°~{d["max"]}°</div></div>')
        wx_card = (f'<div class="widget dark home-card" draggable="true" data-card="weather">'
                   f'<div class="widget-h dark-h"><span class="k">Weather</span><h2>天气</h2><span class="more">{CITY_NAME}</span></div>'
                   f'<div class="widget-b"><div class="wx-now"><span class="wx-temp">{w["temp"]}°</span>'
                   f'<span class="wx-cond">{cur_ic} {esc(w["cond"])}<br><small>最低 {w["days"][0]["min"]}° 最高 {w["days"][0]["max"]}° · 湿度 {w["hum"]}% · 风 {w["wind"]} km/h</small></span></div>'
                   f'<div class="wx-week">{day_cells}</div></div></div>')
    elif w:
        wx_card = (f'<div class="widget dark home-card" draggable="true" data-card="weather">'
                   f'<div class="widget-h dark-h"><span class="k">Weather</span><h2>天气</h2><span class="more">{CITY_NAME}</span></div>'
                   f'<div class="widget-b"><div class="wx-now"><span class="wx-temp">{w["temp"]}°</span>'
                   f'<span class="wx-cond">{esc(w["cond"])}</span></div></div></div>')
    else:
        wx_card = (f'<div class="widget dark home-card" draggable="true" data-card="weather">'
                   f'<div class="widget-h dark-h"><span class="k">Weather</span><h2>天气</h2><span class="more">{CITY_NAME}</span></div>'
                   f'<div class="widget-b"><div class="wx-now"><span class="wx-temp">—°</span>'
                   f'<span class="wx-cond">未获取到<br><a href="/?wx=1">点此重试</a></span></div></div></div>')
    # 倒计时（元旦 / 春节，深色月夜卡，只显示名称＋天数）
    fests = next_festivals(today)
    cd_rows = "".join(
        f'<div class="cd-row"><span class="cd-nm">{esc(nm)}</span>'
        f'<span class="cd-num">{days}<small> 天</small></span></div>'
        for nm, dt, days in fests)
    cd_card = (f'<div class="widget dark home-card cd-card" draggable="true" data-card="countdown" id="cdCard" title="点我设置倒计时">'
               f'<div class="widget-h dark-h"><span class="k">Countdown</span><h2>⏳ 倒计时</h2><span class="cd-set-hint">⚙️ 设置</span></div>'
               f'<div class="widget-b"><div class="cd-rows" id="cdRows">{cd_rows}</div></div></div>')
    cd_modal = ('<div class="cd-modal" id="cdModal"><div class="cd-modal-box"><h3>⏳ 倒计时设置</h3>'
                '<div class="sub">点卡片上的倒计时可以打开这里。内置节日（元旦、春节）不用管；下面可以加你自己的倒计时，比如生日、纪念日。</div>'
                '<div class="cd-list" id="cdList"></div>'
                '<div class="cd-add"><input id="cdName" type="text" placeholder="名称，比如 妈妈的生日" maxlength="20">'
                '<input id="cdDate" type="date"><button id="cdAddBtn" type="button">＋ 添加</button></div>'
                '<button class="cd-close" id="cdCloseBtn" type="button">完成</button></div></div>')
    # 每日电影（深色卡）
    mv = daily_movie(today)
    stars = "★" * max(0, min(5, round(mv["r"] / 2)))
    mv_card = (f'<div class="widget dark home-card wide" draggable="true" data-card="movie">'
               f'<div class="widget-h dark-h"><span class="k">Daily Movie</span><h2>🎬 每日电影</h2><span class="more">{m_d} · 星期{weekday}</span></div>'
               f'<div class="widget-b"><div class="mv">'
               f'<div class="mv-info"><div class="mv-title">{esc(mv["t"])}</div>'
               f'<div class="mv-rate"><span class="mv-stars">{stars}</span><b>{mv["r"]}</b><span> 豆瓣评分</span></div>'
               f'<div class="mv-tags"><span>{esc(mv["g"])}</span><span>{mv["y"]}</span><span>{esc(mv["c"])}</span></div>'
               f'<div class="mv-dir">导演：{esc(mv["d"])}</div>'
               f'<div class="mv-quote">❝ {esc(mv["q"])} ❞</div>'
               f'<div class="mv-plot">{esc(mv["s"])}</div></div>'
               f'<img class="mv-poster" src="{esc(mv["p"])}" alt="{esc(mv["t"])}海报" loading="lazy" '
               f'onerror="this.style.display=\'none\'"></div></div></div>')
    # 时光（深色卡，同天气卡）
    # 时光（深色卡）：入职日 → 目标日之间走了多远；HIRED / CONTRACT_END 留空则不显示这张卡
    if HIRED and CONTRACT_END:
        _hired_s = f"{HIRED.year}年{HIRED.month}月{HIRED.day}日"
        _end_s = f"{CONTRACT_END.year}年{CONTRACT_END.month}月{CONTRACT_END.day}日"
        _span_s = f"{HIRED:%Y.%m.%d} → {CONTRACT_END:%Y.%m.%d}"
        time_card = (f'<div class="widget dark home-card" draggable="true" data-card="time">'
                     f'<div class="widget-h dark-h"><span class="k">Time</span><h2>⏳ 时光</h2><span class="more">{_span_s}</span></div>'
                     f'<div class="widget-b"><div class="time-rows">'
                     f'<div class="time-row"><div class="lb"><span class="dot" style="background:#14b8a6"></span>来到这里</div>'
                     f'<div class="num">{seniority}<small> 天</small></div><div class="sub">自 {_hired_s}</div></div>'
                     f'<div class="time-row"><div class="lb"><span class="dot" style="background:#d43d2a"></span>目标日</div>'
                     f'<div class="num">{countdown}<small> 天</small></div><div class="sub">至 {_end_s}</div></div>'
                     f'</div><div class="time-bar"><i style="width:{pct}%"></i></div>'
                     f'<div class="time-note">这段旅程已经走了 {pct}%，还剩 {countdown} 天。</div></div></div>')
    else:
        time_card = ""
    # 粉丝（沿用现有）
    fans_rows = ""
    for _ak, _av in FANS_ACCOUNTS.items():
        _c = (fans_counts or {}).get(_ak)
        if _c is not None:
            fans_rows += (f'<div class="cd-row"><span class="cd-nm">{esc(_av["name"])}</span>'
                          f'<span class="cd-num">{_c}<small> 粉</small></span></div>')
    if not fans_rows:
        fans_rows = '<div class="meta">公众号粉丝与流量数据</div>'
    fans_card = (f'<div class="widget dark home-card fans-card" draggable="true" data-card="fans">'
                 f'<div class="widget-h dark-h"><span class="k">Fans</span><h2>粉丝数量</h2>'
                 f'<a class="more" href="/?mode=fans">管理 →</a></div>'
                 f'<div class="widget-b"><div class="cd-rows">{fans_rows}</div></div>'
                 f'<div class="widget-f"><span>公众号粉丝与流量</span><a href="/?mode=fans">进入粉丝管理 →</a></div></div>')
    # 随手记（存盒子 notes.json，手机电脑同步；卡片可拖拽换位）
    notes_card = ('<div class="widget dark home-card" draggable="true" data-card="notes">'
                  '<div class="widget-h dark-h"><span class="k">Notes</span><h2>📝 随手记</h2></div>'
                  '<div class="widget-b"><div class="notes-add">'
                  '<input id="notesInput" type="text" placeholder="写一句，点添加…" maxlength="200">'
                  '<button id="notesAddBtn" type="button">＋ 添加</button></div>'
                  '<div class="notes-status" id="notesStatus"></div>'
                  '<div class="notes-list" id="notesList"><div class="meta">加载中…</div></div></div></div>')
    # 下载合一卡（60 秒刷新；不换行、超长省略）
    # 下载合一卡：按 DOWNLOADS 配置渲染；DOWNLOADS 为空时不显示这张卡（60 秒刷新）
    if DOWNLOADS:
        _dl_links = "".join(f'<a href="{esc(it.get("url", ""))}" target="_blank">{esc(it.get("name", ""))} ↗</a>' for it in DOWNLOADS if it.get("url"))
        dl_card = ('<div class="widget home-card" draggable="true" data-card="downloads">'
                   '<div class="widget-h"><span class="k">Downloads</span><h2>⬇️ 下载统计</h2></div>'
                   '<div class="widget-b"><div id="dlRows"><div class="meta">加载中…</div></div></div>'
                   f'<div class="widget-f">{_dl_links}</div></div>')
        dl_js = """<script>(function(){var rows=document.getElementById("dlRows");if(!rows){return;}function st(s){if(!s){return "—";}return s.length>10?s.slice(5):s;}function render(items){var html="";for(var i=0;i<items.length;i++){var d=items[i];html+='<div class="b-row"><div><div class="b-name">'+d.name+' <span>'+(d.version||"")+'</span></div><div class="b-time">最近下载 '+st(d.last)+'</div></div><div class="b-num">'+(d.count||0)+'<small> 次</small></div></div>';if(d.has_file){html+='<div class="b-stagger"><a class="b-dl" href="/download?i='+i+'">直接下载 ↓</a></div>';}}rows.innerHTML=html||"<div>暂无下载统计。</div>";}function load(){fetch("/api/downloads").then(function(r){return r.json();}).then(function(d){if(d.ok){render(d.items);}else{rows.innerHTML="<div>加载失败，刷新试试。</div>";}}).catch(function(){rows.innerHTML="<div>加载失败，刷新试试。</div>";});}load();setInterval(load,60000);})();</script>"""
    else:
        dl_card = ""
        dl_js = ""
    # 金价卡（B 方案深色墨金；上期所沪金连续，60 秒刷新）
    gold_card = ('<div class="widget home-card" draggable="true" data-card="gold">'
                 '<div class="widget-h"><span class="k">Gold</span><h2>国内金价</h2><span class="more">上期所沪金连续</span></div>'
                 '<div class="widget-b"><div id="goldRows"><div class="meta">加载中…</div></div></div>'
                 '<div class="g-foot" id="goldFoot">数据来源：上期所沪金连续 · 每 60 秒刷新</div></div>')
    gold_js = """<script>(function(){var rows=document.getElementById("goldRows");var foot=document.getElementById("goldFoot");if(!rows){return;}function fmt(n){return Number(n).toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});}function render(d){var up=d.chg>=0;var cls=up?"up":"down";var arrow=up?"▲":"▼";var html="";html+='<div style="display:flex;align-items:baseline;gap:12px;padding:8px 0 2px"><span class="g-price">'+fmt(d.price)+'<small> 元/克</small></span><span class="g-chg '+cls+'">'+arrow+' '+fmt(d.chg)+' ('+d.pct+'%)</span></div>';html+='<div class="g-rows">';html+='<div class="g-row"><span>今开 '+fmt(d.open)+' · 最高 '+fmt(d.high)+' · 最低 '+fmt(d.low)+'</span><span>昨结算 '+fmt(d.prev)+'</span></div>';html+='<div class="g-row"><span>持仓 '+d.grams+' 克 · 均价 '+fmt(d.avg)+' 元/克</span><span>卖出约 <b>'+fmt(d.sell)+'</b></span></div>';html+='<div class="g-row"><span>持仓浮亏（按卖出价估算）</span><span class="neg">'+fmt(d.pnl)+' 元</span></div>';html+='<div class="g-row"><span>提醒位：780 / 900 / 1,000（见 GOLD_ALERTS）</span></div>';html+='</div>';rows.innerHTML=html;if(foot){foot.textContent="数据来源：上期所沪金连续 · 更新 "+(d.at||"")+" · 每 60 秒刷新";}}function load(){fetch("/api/gold-price").then(function(r){return r.json();}).then(function(d){if(d.ok){render(d);}else{rows.innerHTML="<div>行情暂不可用，稍后自动重试。</div>";}}).catch(function(){rows.innerHTML="<div>行情暂不可用，稍后自动重试。</div>";});}load();setInterval(load,60000);})();</script>"""
    # 系统状态（磁盘/内存/运行时间/服务/隧道，60 秒刷新）
    status_card = ('<div class="widget dark home-card" draggable="true" data-card="status">'
                   '<div class="widget-h dark-h"><span class="k">System</span><h2>🖥️ 系统状态</h2></div>'
                   '<div class="widget-b"><div class="status-list" id="statusList"><div class="meta">加载中…</div></div></div></div>')
    # 日历卡（月历：阳历＋农历＋节假日＋二十四节气；从左栏移到首页）
    # 小字优先级：节假日（红）＞ 节气（绿）＞ 农历（灰）
    cal = calendar.Calendar(firstweekday=6)
    month_days = cal.monthdayscalendar(today.year, today.month)
    brief_dates = {b.get("broadcast_date") for b in data["briefs"]}
    rows = []
    for week in month_days:
        tds = []
        for d in week:
            if d == 0:
                tds.append('<td class="empty"></td>')
            else:
                ds = f"{today.year:04d}-{today.month:02d}-{d:02d}"
                dd = date(today.year, today.month, d)
                lun = lunar_day_str(dd)
                hol = holiday_name(dd)
                term = solar_term_on(dd)
                cls = "today" if d == today.day else ""
                if hol:
                    sub, sub_cls = hol, "cal-sub hol"
                elif term:
                    sub, sub_cls = term, "cal-sub term"
                else:
                    sub, sub_cls = lun, "cal-sub"
                dot = '<span class="dot"></span>' if ds in brief_dates else ""
                tds.append(f'<td class="{cls}"><div class="cal-day">{d}</div><div class="{sub_cls}">{sub}</div>{dot}</td>')
        rows.append("<tr>" + "".join(tds) + "</tr>")
    cal_table = ('<table class="cal"><tr><th>日</th><th>一</th><th>二</th><th>三</th><th>四</th><th>五</th><th>六</th></tr>'
                 + "".join(rows) + '</table>')
    calendar_card = (f'<div class="widget dark home-card" draggable="true" data-card="calendar">'
                     f'<div class="widget-h dark-h"><span class="k">Calendar</span><h2>📅 日历 · {today.month}月</h2></div>'
                     f'<div class="widget-b">{cal_table}<div class="meta">红字 = 节假日 · 绿字 = 节气 · 红点 = 当天有联播简报</div></div></div>')
    drag_js = ('<script>(function(){var grid=document.getElementById("homeGrid");if(!grid){return;}'
               'var K="wb_home_order_v2";'
               'function save(){try{localStorage.setItem(K,JSON.stringify(Array.prototype.map.call(grid.querySelectorAll(".home-card"),function(c){return c.getAttribute("data-card");})));}catch(e){}}'
               'try{var order=JSON.parse(localStorage.getItem(K)||"null");'
               'if(order&&order.length){var map={};Array.prototype.forEach.call(grid.querySelectorAll(".home-card"),function(c){map[c.getAttribute("data-card")]=c;});'
               'order.forEach(function(k){if(map[k]){grid.appendChild(map[k]);}});}}catch(e){}'
               'Array.prototype.forEach.call(grid.querySelectorAll("img"),function(im){im.setAttribute("draggable","false");});'
               'Array.prototype.forEach.call(grid.querySelectorAll(".home-card .widget-h"),function(h){'
               'var s=document.createElement("span");s.className="drag-handle";s.textContent="⋮⋮";s.title="拖我换位置";h.insertBefore(s,h.firstChild);});'
               'var drag=null;'
               'function clearDrop(){Array.prototype.forEach.call(grid.querySelectorAll(".home-card"),function(c){c.classList.remove("drop-before");c.classList.remove("drop-after");});}'
               'Array.prototype.forEach.call(grid.querySelectorAll(".home-card"),function(card){'
               'card.addEventListener("dragstart",function(e){drag=card;card.classList.add("dragging");e.dataTransfer.effectAllowed="move";try{e.dataTransfer.setData("text/plain",card.getAttribute("data-card"));}catch(err){}});'
               'function liveMove(target,e){if(!drag||drag===target){return;}var r=target.getBoundingClientRect();var wide=target.classList.contains("wide");var after=wide?(e.clientY-r.top)>r.height/2:(e.clientX-r.left)>r.width/2;if(after){if(target.nextSibling!==drag){grid.insertBefore(drag,target.nextSibling);}}else{if(drag.nextSibling!==target){grid.insertBefore(drag,target);}}clearDrop();target.classList.add(after?"drop-after":"drop-before");}'
               'card.addEventListener("dragend",function(){card.classList.remove("dragging");drag=null;clearDrop();save();});'
               'card.addEventListener("dragenter",function(e){if(!drag||drag===card){return;}e.preventDefault();liveMove(card,e);});'
               'card.addEventListener("dragover",function(e){if(!drag||drag===card){return;}e.preventDefault();e.dataTransfer.dropEffect="move";liveMove(card,e);});'
               'card.addEventListener("dragleave",function(){card.classList.remove("drop-before");card.classList.remove("drop-after");});'
               'card.addEventListener("drop",function(e){e.preventDefault();if(!drag||drag===card){return;}clearDrop();save();});'
               '});'
               'var CK="wb_custom_countdowns";'
               'function loadCustom(){try{return JSON.parse(localStorage.getItem(CK)||"[]");}catch(e){return [];}}'
               'function saveCustom(l){try{localStorage.setItem(CK,JSON.stringify(l));}catch(e){}}'
               'function daysLeft(ds){var d=new Date(ds+"T00:00:00");var t=new Date();t.setHours(0,0,0,0);'
               'var diff=Math.round((d-t)/86400000);return diff;}'
               'function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");}'
               'function renderCustom(){var rows=document.getElementById("cdRows");if(!rows){return;}'
               'Array.prototype.forEach.call(rows.querySelectorAll(".cd-row.custom"),function(r){r.remove();});'
               'loadCustom().forEach(function(c){var d=daysLeft(c.date);if(isNaN(d)){return;}'
               'var label=d<0?"已过 "+(-d)+" 天":(d===0?"就是今天":d+"");'
               'var div=document.createElement("div");div.className="cd-row custom";'
               'div.innerHTML="<span class=\\"cd-nm\\">"+esc(c.name)+"</span><span class=\\"cd-num\\">"+label+(d>0?"<small> 天</small>":"")+"</span>";'
               'rows.appendChild(div);});}'
               'function renderList(){var list=document.getElementById("cdList");if(!list){return;}'
               'var html="";'
               'Array.prototype.forEach.call(document.querySelectorAll("#cdRows .cd-row:not(.custom)"),function(r){'
               'var nm=r.querySelector(".cd-nm");var num=r.querySelector(".cd-num");'
               'if(nm){html+="<div class=\\"cd-item builtin\\"><span class=\\"nm\\">"+nm.textContent+"</span><span class=\\"dt\\">内置</span><span class=\\"days\\">"+(num?num.textContent:"")+"</span></div>";}});'
               'loadCustom().forEach(function(c,i){var d=daysLeft(c.date);'
               'html+="<div class=\\"cd-item\\"><span class=\\"nm\\">"+esc(c.name)+"</span><span class=\\"dt\\">"+c.date+"</span><span class=\\"days\\">"+(d<0?"已过"+(-d)+"天":(d===0?"今天":d+"天"))+"</span><button class=\\"del\\" data-i=\\""+i+"\\" title=\\"删除\\">✕</button></div>";});'
               'list.innerHTML=html||"<div class=\\"sub\\">还没有自定义倒计时。</div>";'
               'Array.prototype.forEach.call(list.querySelectorAll(".del"),function(b){b.addEventListener("click",function(){'
               'var l=loadCustom();l.splice(parseInt(b.getAttribute("data-i"),10),1);saveCustom(l);renderCustom();renderList();});});}'
               'var modal=document.getElementById("cdModal"),cdCard=document.getElementById("cdCard");'
               'function openModal(){renderCustom();renderList();if(modal){modal.classList.add("open");}}'
               'function closeModal(){if(modal){modal.classList.remove("open");}}'
               'if(cdCard){cdCard.addEventListener("click",function(e){if(e.target.closest("a")){return;}openModal();});}'
               'var addBtn=document.getElementById("cdAddBtn");'
               'if(addBtn){addBtn.addEventListener("click",function(){'
               'var nm=document.getElementById("cdName"),dt=document.getElementById("cdDate");'
               'if(!nm||!dt||!nm.value.trim()||!dt.value){return;}'
               'var l=loadCustom();l.push({name:nm.value.trim(),date:dt.value});saveCustom(l);'
               'nm.value="";dt.value="";renderCustom();renderList();});}'
               'var closeBtn=document.getElementById("cdCloseBtn");'
               'if(closeBtn){closeBtn.addEventListener("click",closeModal);}'
               'if(modal){modal.addEventListener("click",function(e){if(e.target===modal){closeModal();}});}'
               'renderCustom();'
               'var t=document.getElementById("clockTime");'
               'function tick(){var n=new Date();var p=function(x){return (x<10?"0":"")+x;};'
               'if(t){t.textContent=p(n.getHours())+":"+p(n.getMinutes())+":"+p(n.getSeconds());}}'
               'tick();setInterval(tick,1000);})();</script>')
    # 每日一句（底部居中）
    q_text, q_author = daily_quote(today)
    quote_html = (f'<div class="daily-quote"><div class="q-text">「{esc(q_text)}」</div>'
                  f'<div class="q-author">- {esc(q_author)} -</div></div>')
    notes_js = """<script>(function(){var list=document.getElementById("notesList");var input=document.getElementById("notesInput");var addBtn=document.getElementById("notesAddBtn");var statusEl=document.getElementById("notesStatus");if(!list){return;}function showStatus(m){if(!statusEl){return;}statusEl.textContent=m;setTimeout(function(){if(statusEl){statusEl.textContent="";}},4000);}function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");}function render(notes){if(!notes.length){list.innerHTML="<div class='meta'>还没有随手记，写一句吧。</div>";return;}var html="";notes.forEach(function(n){html+="<div class='note-row"+(n.done?" done":"")+"'><button class='note-check' data-id='"+n.id+"' title='完成/取消'>✓</button><span class='note-text'>"+esc(n.text)+"</span><button class='note-del' data-id='"+n.id+"' title='删除'>✕</button></div>";});list.innerHTML=html;Array.prototype.forEach.call(list.querySelectorAll(".note-check"),function(b){b.addEventListener("click",function(){toggle(parseInt(b.getAttribute("data-id"),10));});});Array.prototype.forEach.call(list.querySelectorAll(".note-del"),function(b){b.addEventListener("click",function(){del(parseInt(b.getAttribute("data-id"),10));});});}function load(){fetch("/api/notes").then(function(r){return r.json();}).then(function(d){if(d.ok){render(d.notes||[]);}}).catch(function(){list.innerHTML="<div class='meta'>加载失败，刷新试试。</div>";});}function post(url,body){return fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}).then(function(r){return r.json();});}function add(){var t=input.value.trim();if(!t){return;}post("/api/notes/add",{text:t}).then(function(d){if(d.ok){input.value="";load();}else{showStatus("没保存上，再点一次");}}).catch(function(){showStatus("网络抖了一下，没保存上，再点一次");});}function toggle(id){post("/api/notes/toggle",{id:id}).then(function(d){if(d.ok){load();}else{showStatus("没保存上，再点一次");}}).catch(function(){showStatus("网络抖了一下，没保存上，再点一次");});}function del(id){post("/api/notes/delete",{id:id}).then(function(d){if(d.ok){load();}else{showStatus("没保存上，再点一次");}}).catch(function(){showStatus("网络抖了一下，没保存上，再点一次");});}if(addBtn){addBtn.addEventListener("click",add);}if(input){input.addEventListener("keydown",function(e){if(e.key==="Enter"){add();}});}load();})();</script>"""
    status_js = """<script>(function(){var list=document.getElementById("statusList");if(!list){return;}function fmtGB(b){return (b/1073741824).toFixed(1)+"G";}function dot(ok){return ok?"<span class='dot-ok'>●</span>":"<span class='dot-bad'>●</span>";}function barCls(p){return p>=90?"bad":(p>=80?"warn":"ok");}function render(d){var h="";(d.disks||[]).forEach(function(x){h+="<div class='st-row'><span class='st-nm'>"+x.label+"</span><span class='st-bar'><i class='"+barCls(x.pct)+"' style='width:"+x.pct+"%'></i></span><span class='st-vl'>"+fmtGB(x.used)+" / "+fmtGB(x.total)+" · "+x.pct+"%</span></div>";});if(d.mem){h+="<div class='st-row'><span class='st-nm'>内存</span><span class='st-bar'><i class='"+barCls(d.mem.pct)+"' style='width:"+d.mem.pct+"%'></i></span><span class='st-vl'>"+fmtGB(d.mem.used)+" / "+fmtGB(d.mem.total)+" · "+d.mem.pct+"%</span></div>";}var sv=d.services||{},tn=d.tunnels||{};h+="<div class='st-row'><span class='st-nm'>工作台服务</span><span class='st-vl'>"+dot(sv.workbench)+(sv.workbench?"运行中":"已停止")+"</span></div>";h+="<div class='st-row'><span class='st-nm'>粉丝系统</span><span class='st-vl'>"+dot(sv.fans)+(sv.fans?"运行中":"已停止")+"</span></div>";h+="<div class='st-row'><span class='st-nm'>零讯隧道</span><span class='st-vl'>"+dot(tn.zeronews)+(tn.zeronews?"运行中":"已停止")+"</span></div>";h+="<div class='st-row'><span class='st-nm'>Cloudflare 隧道</span><span class='st-vl'>"+dot(tn.cloudflared)+(tn.cloudflared?"运行中":"已停止")+"</span></div>";var up=d.uptime_sec||0,dd=Math.floor(up/86400),hh=Math.floor(up%86400/3600);h+="<div class='st-row'><span class='st-nm'>盒子已运行</span><span class='st-vl'>"+dd+" 天 "+hh+" 小时</span></div>";list.innerHTML=h;}function load(){fetch("/api/system-status").then(function(r){return r.json();}).then(function(d){if(d.ok){render(d.status);}}).catch(function(){list.innerHTML="<div class='meta'>加载失败，刷新试试。</div>";});}load();setInterval(load,60000);})();</script>"""
    return (
        f'<div class="home-wrap">{clock}'
        f'<div class="home-grid" id="homeGrid">'
        f'{wx_card}{time_card}{cd_card}{fans_card}{dl_card}{gold_card}{notes_card}{status_card}{calendar_card}{mv_card}'
        f'</div>{quote_html}{cd_modal}{drag_js}{notes_js}{status_js}{dl_js}{gold_js}</div>')


FANS_VIEWS = {
    "overview": ("", "总览"),
    "trend": ("#trend", "粉丝趋势"),
    "fans": ("#fans", "粉丝列表"),
    "unfollows": ("#unfollows", "取关列表"),
    "cohorts": ("#cohorts", "数据分析"),
    "review": ("", "数据复盘"),
}


# ---- 找工作网页版计划（C 区「工作」模式，数据与 Excel 版计划表一致）----
# ---- 「工作」栏数据：默认用下面的示例（几行样子货）；想用自己的，写一个 job-plan.json 放同目录，格式见 job-plan.sample.json ----
WORK_PHASES_SAMPLE = [
    ("一 · 试水启动", "第 1–2 周", "简历定稿，小批量试投摸行情", "投出第一批，拿到首轮面试反馈"),
    ("二 · 密集面试", "第 3–6 周", "密集投递，把面试量跑起来", "累计面试 8 场以上"),
]
WORK_WEEKLY_SAMPLE = [
    ("w1", "第1周", "示例日期", "试水", "简历最终定稿", "核对简历：公司名、时间、电话", "定稿 PDF 存好", "高"),
    ("w2", "第2周", "示例日期", "试水", "第一批试投", "按优先方向投 5–10 家，记到投递跟踪", "投出 5 家以上", "高"),
]
WORK_INTERVIEW_SAMPLE = [
    ("i1", "主项目", "STAR 故事：背景、任务、动作、结果", "你的项目规模与你的实际负责部分，分清团队成果和个人贡献", "写成稿背熟", "高"),
    ("i2", "高并发", "慢 SQL 优化案例", "一个真实慢 SQL：执行计划前后对比", "能讲清原因、做法、结果", "中"),
]

def _load_job_plan():
    """读 job-plan.json（{phases, weekly, interview}），没有就用示例。"""
    try:
        with open(os.path.join(BASE, "job-plan.json"), encoding="utf-8") as f:
            d = json.load(f)
        return (d.get("phases") or WORK_PHASES_SAMPLE,
                d.get("weekly") or WORK_WEEKLY_SAMPLE,
                d.get("interview") or WORK_INTERVIEW_SAMPLE)
    except Exception:
        return WORK_PHASES_SAMPLE, WORK_WEEKLY_SAMPLE, WORK_INTERVIEW_SAMPLE

WORK_PHASES, WORK_WEEKLY, WORK_INTERVIEW = _load_job_plan()

WORK_JS = (
    '<script>(function(){'
    'var CK="wb_work_checks_v1",TK="wb_work_track_v1",saved={};'
    'try{saved=JSON.parse(localStorage.getItem(CK)||"{}");}catch(e){saved={};}'
    'function upd(){var w=document.querySelectorAll(".work-check[data-kind=\\"w\\"]"),'
    'iw=document.querySelectorAll(".work-check[data-kind=\\"i\\"]"),cw=0,ci=0,i;'
    'for(i=0;i<w.length;i++){if(w[i].checked){cw++;}}'
    'for(i=0;i<iw.length;i++){if(iw[i].checked){ci++;}}'
    'var a=document.getElementById("workProgW");if(a){a.textContent=cw+"/"+w.length;}'
    'var b=document.getElementById("workProgI");if(b){b.textContent=ci+"/"+iw.length;}}'
    'var boxes=document.querySelectorAll(".work-check");'
    'for(var k=0;k<boxes.length;k++){var b2=boxes[k];'
    'if(saved[b2.getAttribute("data-id")]){b2.checked=true;}'
    'b2.addEventListener("change",function(){var s={};'
    'try{s=JSON.parse(localStorage.getItem(CK)||"{}");}catch(e){s={};}'
    's[this.getAttribute("data-id")]=this.checked;'
    'try{localStorage.setItem(CK,JSON.stringify(s));}catch(e){}upd();});}'
    'var cells=document.querySelectorAll(".work-track-cell"),tsaved={};'
    'try{tsaved=JSON.parse(localStorage.getItem(TK)||"{}");}catch(e){tsaved={};}'
    'for(var j=0;j<cells.length;j++){var c=cells[j],key=c.getAttribute("data-cell");'
    'if(tsaved[key]){c.textContent=tsaved[key];}'
    'c.addEventListener("input",function(){var s2={};'
    'try{s2=JSON.parse(localStorage.getItem(TK)||"{}");}catch(e){s2={};}'
    'var all=document.querySelectorAll(".work-track-cell");'
    'for(var m=0;m<all.length;m++){s2[all[m].getAttribute("data-cell")]=all[m].textContent;}'
    'try{localStorage.setItem(TK,JSON.stringify(s2));}catch(e){}});}'
    'upd();})();</script>'
)


def work_html(view):
    if view == "weekly":
        rows = []
        for it in WORK_WEEKLY:
            rows.append(
                '<tr><td>' + it[1] + '<br>' + it[2] + '</td><td><b>' + esc(it[4]) + '</b><br>' + esc(it[5]) + '</td>'
                '<td>' + esc(it[6]) + '</td><td>' + it[7] + '</td>'
                '<td><input type="checkbox" class="work-check" data-kind="w" data-id="' + it[0] + '"></td></tr>')
        return (f'<div class="card"><h3>每周计划 · {len(WORK_WEEKLY)} 项</h3>'
                '<p class="meta">做完一项就打勾，状态只存在本机浏览器里，换设备要重打。总览页能看到进度。</p>'
                '<table class="rank-table"><tr><td><b>周次</b></td><td><b>任务</b></td><td><b>完成标准</b></td><td><b>优先级</b></td><td><b>完成</b></td></tr>'
                + "".join(rows) + '</table></div>')
    if view == "interview":
        rows = []
        for it in WORK_INTERVIEW:
            rows.append(
                '<tr><td><b>' + esc(it[1]) + '</b><br>' + esc(it[2]) + '</td><td>' + esc(it[3]) + '</td>'
                '<td>' + esc(it[4]) + '</td><td>' + it[5] + '</td>'
                '<td><input type="checkbox" class="work-check" data-kind="i" data-id="' + it[0] + '"></td></tr>')
        return (f'<div class="card"><h3>面试准备 · {len(WORK_INTERVIEW)} 项</h3>'
                '<p class="meta">只讲自己真做过的。团队成果和个人贡献分清，反而更可信。</p>'
                '<table class="rank-table"><tr><td><b>准备点</b></td><td><b>你的素材（简历里已有的，别编新的）</b></td><td><b>准备方法</b></td><td><b>优先级</b></td><td><b>完成</b></td></tr>'
                + "".join(rows) + '</table></div>')
    if view == "tracking":
        cols = ["投递日期", "公司", "岗位", "方向", "薪资范围", "渠道", "状态", "下一步"]
        head = "".join('<td><b>' + c + '</b></td>' for c in cols)
        body = []
        for r in range(10):
            cells = "".join('<td class="work-track-cell" contenteditable="true" data-cell="t' + str(r) + '_' + str(ci) + '"></td>' for ci in range(len(cols)))
            body.append('<tr>' + cells + '</tr>')
        return ('<div class="card"><h3>投递跟踪</h3>'
                '<p class="meta">每投一家记一行，直接点格子就能填，填完自动存在本机浏览器里。状态填：已投递 / 已回复 / 面试中 / 终面 / 已发 offer / 已拒绝 / 已放弃。</p>'
                '<table class="rank-table"><tr>' + head + '</tr>' + "".join(body) + '</table></div>')
    if view == "rights":
        return ('<div class="card"><h3>权益与材料 · 如果公司不续签，别吃亏</h3>'
                '<p class="meta">以下是一般口径的自查清单，不代替专业意见；谈不拢先打 12333 咨询，再考虑劳动仲裁，证据先留好。</p>'
                '<p><b>材料留存（现在就办，别等离职后进不去系统）：</b></p>'
                '<p>劳动合同原件 / 扫描件 —— 证明入职日与合同期限；'
                '工资流水 —— 银行 App 导出近 12 个月工资入账记录，算经济补偿的月工资基数；'
                '社保缴费记录 —— 当地人社渠道导出，核年限、办失业金、社保衔接；'
                '考勤 / 绩效 / 奖金记录 —— 尤其是年终奖发放记录；'
                '不续签书面通知 —— 如公司决定不续签，要求出书面通知，别只听口头；'
                '终止劳动合同证明 —— 公司出具，写明合同期满不续签；别签成离职申请；'
                'offer 书面条款（含入职时间） —— 入职时间与 offer 保留期，写进 offer 或邮件。</p>'
                '<p><b>关键原则：</b></p>'
                '<p>1. 千万别主动提离职：你主动走，一般没有经济补偿；公司到期不续签，按劳动合同法一般要给经济补偿。</p>'
                '<p>2. 不续签要书面：让公司出书面不续签通知，口头说法留不住。</p>'
                '<p>3. 在职身份更好谈薪：尽量在合同到期前把新 offer 谈定，再处理去留。</p>'
                '<p>4. 失业金：非本人意愿中断就业、社保缴满 1 年，一般可申领失业保险金，到期后去当地人社部门办理。</p>'
                '<p>5. 入职时间谈在前：拿到 offer 先把入职时间与 offer 保留期谈定、写进书面条款再签，别签了才发现要立即到岗。</p>'
                '<p>6. 社保别断月：上家交到离职月，下家从入职月起交，入职别拖过月中（有些公司过 15 号就下月才交社保）。</p>'
                '<p><b>经济补偿粗算：</b>干满 3 年一般按 3 个月工资计，基数是离职前 12 个月平均月工资。精确数字以劳动合同法与当地规定为准。</p></div>')
    # overview
    prows = []
    for ph in WORK_PHASES:
        prows.append('<tr><td><b>' + esc(ph[0]) + '</b><br>' + esc(ph[1]) + '</td><td>' + esc(ph[2]) + '</td><td>' + esc(ph[3]) + '</td></tr>')
    return ('<div class="card"><h3>找工作 · 总览</h3>'
            '<p class="meta">理想状态：拿到满意的 offer，衔接好入职时间 ｜ offer 先谈定，入职时间写进书面条款</p>'
            '<p>当前状态：在职，X 年 Java 经验（示例，改成你自己的）</p>'
            '<p>目标岗位：高级 Java 工程师 / Java 技术专家（示例）</p>'
            '<p>目标薪资参考：按岗位 JD 核对（示例）</p>'
            '<p>优先方向：① 金融科技 ② 银行科技子公司 ③ Java+AI 应用公司（示例）</p>'
            '<p>核心打法：三张牌 —— 主项目实战 ＋ 高并发案例 ＋ 自己的 AI 落地产品（示例）</p>'
            f'<p>进度：每周计划 <b id="workProgW">0/{len(WORK_WEEKLY)}</b> ｜ 面试准备 <b id="workProgI">0/{len(WORK_INTERVIEW)}</b>（去对应页打勾，这里自动变）</p></div>'
            '<div class="card"><h3>五阶段时间线</h3>'
            '<table class="rank-table"><tr><td><b>阶段</b></td><td><b>核心目标</b></td><td><b>里程碑</b></td></tr>'
            + "".join(prows) + '</table></div>')



def render_shell(data, mode, group, section_id, view, force_wx=False, acct="main", brief_date="", review_date="", period="daily"):
    """v5 A/B/C/D 分区壳。mode=home/news/fans/chat/work/command/principle。"""
    today = date.today()
    seniority = (today - HIRED).days if HIRED else 0
    countdown = (CONTRACT_END - today).days if CONTRACT_END else 0
    total = seniority + countdown
    pct = round(seniority / total * 100) if total > 0 else 0
    weekday = "一二三四五六日"[today.weekday()]
    m_d = f"{today.month}月{today.day}日"
    w = get_weather(force_wx)
    if w:
        wx_html = (f'<div class="wx"><div class="city">实时天气<b>{CITY_NAME}</b></div>'
                   f'<div class="temp">{w["temp"]}°</div>'
                   f'<div class="cond">{esc(w["cond"])}<br>湿度 {w["hum"]}% · 风 {w["wind"]} km/h</div></div>')
        wx_chip = f"{CITY_NAME} {w['temp']}° {esc(w['cond'])}"
    else:
        wx_html = (f'<div class="wx"><div class="city">实时天气<b>{CITY_NAME}</b></div>'
                   '<div class="temp">—°</div><div class="cond">未获取到<br><a href="/?wx=1">点此重试</a></div></div>')
        wx_chip = f"{CITY_NAME} 天气未获取"
    fans_counts = {k: fans_live_count(v["db"]) for k, v in FANS_ACCOUNTS.items()}

    # C区 active
    c_news = " on" if mode in ("home", "news") else ""
    c_fans = " on" if mode == "fans" else ""
    c_chat = " on" if mode == "chat" else ""
    c_work = " on" if mode == "work" else ""
    c_command = " on" if mode == "command" else ""
    c_principle = " on" if mode == "principle" else ""
    top = (f'<header class="app-top"><div class="app-top-in">'
           f'<button class="side-toggle" id="sideToggle" aria-label="菜单">☰</button>'
           f'<a class="brand" href="/">我的<span>个人工作台</span></a>'
           f'<nav class="cnav"><a href="/" class="{c_news.strip()}">📰 新闻解读</a>'
           f'<a href="/?mode=fans&acct={acct}" class="{c_fans.strip()}">👥 粉丝管理</a>'
           f'<a href="/?mode=chat" class="{c_chat.strip()}">💬 Agent聊天</a>'
           f'<a href="/?mode=command" class="{c_command.strip()}">🎛️ 指挥中心</a>'
           f'<a href="/?mode=principle" class="{c_principle.strip()}">📖 工作台原理</a>'
           f'<a href="/?mode=work" class="{c_work.strip()}">💼 工作</a></nav>'
           f'<span class="wx-chip">{wx_chip}</span>'
           f'<button class="bg-set-btn" id="bgSetBtn" title="背景设置">🎨</button><a class="bg-set-btn" href="/change-password" title="修改口令" style="display:inline-flex;align-items:center;justify-content:center;text-decoration:none">\U0001f511</a></div></header>')

    if mode == "chat":
        # B区与D区都展示 web 聊天：整幅内嵌聊天页，不显示左栏
        main = (f'<div class="d-head"><h2>💬 Agent 聊天</h2><span class="sub">web 聊天，与 Lei / atlas / muse-2 / muse-3 / muse-4 对话</span></div>'
                f'<iframe class="embed-frame" src="/chat" title="Agent 聊天"></iframe>'
                f'<div class="embed-note">聊天页打不开时，<a href="/chat" target="_blank" rel="noopener">在新窗口打开 →</a></div>')
        shell_cls = "shell no-side chat-mode"
        side = ""
        extra_js = ""
    elif mode == "command":
        # C区指挥中心：D区整幅内嵌指挥中心页，不显示左栏；状态由页内 JS 调 /api/chat/agents 每 30 秒刷新
        main = (f'<div class="d-head"><h2>🎛️ Agent 指挥中心</h2><span class="sub">Agent 状态 · 工单 · 聊天一屏看全，状态每 30 秒自动刷新</span></div>'
                f'<iframe class="embed-frame" src="/command-center/" title="Agent 指挥中心"></iframe>'
                f'<div class="embed-note">指挥中心打不开时，<a href="/command-center/" target="_blank" rel="noopener">在新窗口打开 →</a></div>')
        shell_cls = "shell no-side chat-mode"
        side = ""
        extra_js = ""
    elif mode == "principle":
        # C区工作台原理：D区整幅内嵌一屏版原理页，不显示左栏；纯静态讲解页
        main = (f'<div class="d-head"><h2>📖 工作台原理</h2><span class="sub">盒子 · Tailscale · 零讯 · muse 一家小公司，一屏看懂</span></div>'
                f'<iframe class="embed-frame" src="/principle/" title="工作台原理"></iframe>'
                f'<div class="embed-note">原理页打不开时，<a href="/principle/" target="_blank" rel="noopener">在新窗口打开 →</a></div>')
        shell_cls = "shell no-side chat-mode"
        side = ""
        extra_js = ""
    elif mode == "work":
        v = view if view in ("overview", "weekly", "interview", "tracking", "rights") else "overview"
        _vt = {"overview": "🎯 总览", "weekly": "📅 每周计划", "interview": "🎤 面试准备",
               "tracking": "📝 投递跟踪", "rights": "🛡️ 权益与材料"}[v]
        main = (f'<div class="d-head"><h2>💼 工作 · {_vt}</h2>'
                f'<span class="sub">找工作准备计划' + (f' · 合同 {CONTRACT_END} 到期' if CONTRACT_END else '') + '</span></div>'
                f'<div id="news">{work_html(v)}</div>')
        shell_cls = "shell"
        side = sidebar_html(mode, group, section_id, v)
        extra_js = WORK_JS
    elif mode == "fans":
        acct = acct if acct in FANS_ACCOUNTS else "main"
        view = view if view in FANS_VIEWS else "overview"
        frag, vname = FANS_VIEWS[view]
        if view == "review":
            if period not in ("daily", "weekly", "monthly"):
                period = "daily"
            pname = {"daily": "日复盘", "weekly": "周复盘", "monthly": "月复盘"}[period]
            if period == "daily":
                rbody = review_html(acct, review_date)
            elif period == "weekly":
                rbody = period_summary_html(acct, 7, "周")
            else:
                rbody = period_summary_html(acct, 30, "月")
            main = (f'<div class="d-head"><h2>📝 数据复盘</h2><span class="sub">{pname} · {esc(FANS_ACCOUNTS[acct]["name"])} · AI 复盘信息</span></div>'
                    f'<div id="news">{rbody}</div>')
        else:
            fans_src = f"{FANS_URL}?embed=1&acct={acct}&auth={fans_token()}{frag}"
            _vicon = {"overview": "👥", "trend": "📈", "fans": "👥", "unfollows": "📉", "cohorts": "📊"}[view]
            _vtitle = "粉丝管理" if view == "overview" else vname
            main = (f'<div class="d-head"><h2>{_vicon} {_vtitle}</h2><span class="sub">{esc(FANS_ACCOUNTS[acct]["name"])} · 两号数据严格隔离</span></div>'
                    f'<iframe class="embed-frame" src="{fans_src}" title="粉丝管理"></iframe>'
                    f'<div class="embed-note">总览默认展示当前粉丝 / 累计取关 / 今日新增 / 今日取关 / 每日明细。框里打不开时，<a href="{FANS_URL}" target="_blank" rel="noopener">在新窗口打开粉丝系统 →</a></div>')
        shell_cls = "shell"
        side = sidebar_html(mode, group, section_id, view, acct, period)
        extra_js = ('<script>(function(){var b=document.getElementById("reviewParent"),s=document.getElementById("reviewSub");'
                    'if(b&&s){b.addEventListener("click",function(){var o=s.classList.toggle("open");'
                    'var c=b.querySelector(".chev");if(c){c.textContent=o?"▾":"▸";}});}})();</script>')
    elif mode == "news":
        if brief_date:
            b = next((x for x in data["briefs"] if x.get("broadcast_date") == brief_date), None)
            if b:
                main = f'<div id="news">{brief_content_html(b)}</div>'
            else:
                main = f'<div id="news"><div class="empty">未找到 {esc(brief_date)} 的简报</div><a class="back" href="/?mode=news&group=world&section=lianbo">← 返回国内新闻联播</a></div>'
        else:
            news = news_content_html(data, group, section_id)
            main = f'<div id="news">{news}</div>'
        shell_cls = "shell"
        side = sidebar_html(mode, group, section_id, view)
        extra_js = NEWS_SWITCH_JS_V5 + SIDEBAR_ACCORDION_JS
    else:  # home
        main = home_dashboard_html(data, today, seniority, countdown, pct, weekday, m_d, w, fans_counts)
        shell_cls = "shell"
        side = sidebar_html("home", group, section_id, view)
        extra_js = SIDEBAR_ACCORDION_JS

    foot = ('<div class="bottombar">'
            '<a href="/" class="on">🏠<span>工作台</span></a>'
            '<a href="/?mode=news">📰<span>新闻</span></a>'
            '<a href="/?mode=fans">👥<span>粉丝</span></a>'
            '<a href="/?mode=chat">💬<span>聊天</span></a>'
            '<a href="/?mode=command">🎛️<span>指挥</span></a>'
            '<a href="/?mode=principle">📖<span>原理</span></a>'
            '<a href="/?mode=work">💼<span>工作</span></a>'
            '</div></body></html>')
    # 每日背景图（Bing 每日壁纸）＋浅色遮罩，保证浅色调与可读性
    bg = get_daily_bg()
    bg_style = ""
    bg_credit = ""
    bg_js = ""
    if bg and bg.get("url"):
        # 注意：<style> 是 raw text，& 不用转成 &amp;，否则 URL 会被浏览器原样请求而 404；只防双引号
        u = bg["url"].replace('"', "%22")
        bg_style = ('<style>body{background-image:linear-gradient(rgba(255,255,255,.60),rgba(255,255,255,.60)),'
                    'url("' + u + '");background-size:cover;background-position:center;background-attachment:fixed}</style>')
        cp = esc(bg.get("copyright") or bg.get("title") or "")
        if cp:
            bg_credit = '<div class="bg-credit">🖼 今日壁纸 · ' + cp + '</div>'
        # 背景遮白调节：滑块改 localStorage，刷新后仍生效；默认 60%
        bg_js = ('<div class="bg-set-panel" id="bgSetPanel"><div class="bg-set-t">🎨 背景遮白</div>'
                 '<input type="range" id="bgWhiteRange" min="0" max="90" step="5" value="60">'
                 '<div class="bg-set-v"><span id="bgWhiteVal">60%</span><span>越白字越清楚，越低图越清楚</span></div></div>'
                 '<script>(function(){var U=' + json.dumps(bg["url"]) + ';var K="wb_bg_white";'
                 'function apply(v){document.body.style.backgroundImage="linear-gradient(rgba(255,255,255,"+(v/100)+"),rgba(255,255,255,"+(v/100)+")),url(\\""+U+"\\")";'
                 'document.body.style.backgroundSize="cover";document.body.style.backgroundPosition="center";document.body.style.backgroundAttachment="fixed";}'
                 'var saved=null;try{saved=localStorage.getItem(K);}catch(e){}'
                 'if(saved!==null&&saved!==""){var v=parseInt(saved,10);if(!isNaN(v)){apply(v);'
                 'var r=document.getElementById("bgWhiteRange");if(r){r.value=v;}'
                 'var t=document.getElementById("bgWhiteVal");if(t){t.textContent=v+"%";}}}'
                 'var b=document.getElementById("bgSetBtn"),p=document.getElementById("bgSetPanel");'
                 'if(b&&p){b.addEventListener("click",function(e){e.stopPropagation();p.classList.toggle("open");});'
                 'document.addEventListener("click",function(e){if(!p.contains(e.target)&&e.target!==b){p.classList.remove("open");}});}'
                 'var r=document.getElementById("bgWhiteRange");'
                 'if(r){r.addEventListener("input",function(){var v=parseInt(r.value,10);apply(v);'
                 'var t=document.getElementById("bgWhiteVal");if(t){t.textContent=v+"%";}'
                 'try{localStorage.setItem(K,String(v));}catch(e){}});}})();</script>')
    return (f'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>我的个人工作台</title>'
            f'<style>{CSS}</style>{bg_style}</head><body' + (' class="mode-chat"' if mode == "chat" else '') + '>'
            f'{top}<div class="{shell_cls}">'
            + (f'<aside class="sidebar">{side}{bg_credit}</aside>' if side else '')
            + f'<main class="main">{main}</main></div>'
            + foot + extra_js + bg_js)


def md_to_html(text):
    """极简 Markdown 转 HTML（只覆盖复盘稿用到的元素：#/##/###、> 引用、- 列表、数字列表、**加粗**、段落）。"""
    import re
    def inline(s):
        s = esc(s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        return s
    lines = text.split("\n")
    out = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
            continue
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            lv = len(m.group(1))
            out.append(f"<h{lv}>{inline(m.group(2))}</h{lv}>")
            i += 1
            continue
        if line.startswith("> "):
            qs = []
            while i < n and lines[i].startswith("> "):
                qs.append(inline(lines[i][2:].strip()))
                i += 1
            out.append("<blockquote>" + "<br>".join(qs) + "</blockquote>")
            continue
        if line.startswith("- "):
            items = []
            while i < n and lines[i].startswith("- "):
                items.append(f"<li>{inline(lines[i][2:].strip())}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        m = re.match(r"^\d+\.\s+(.*)", line)
        if m:
            items = []
            while i < n and re.match(r"^\d+\.\s+", lines[i]):
                txt = re.sub(r"^\d+\.\s+", "", lines[i])
                items.append(f"<li>{inline(txt)}</li>")
                i += 1
            out.append("<ol>" + "".join(items) + "</ol>")
            continue
        # 连续普通行并成一段
        paras = []
        while i < n and lines[i].strip() and not re.match(r"^(#{1,3}\s|>\s|-\s|\d+\.\s)", lines[i]):
            paras.append(inline(lines[i].strip()))
            i += 1
        out.append("<p>" + "<br>".join(paras) + "</p>")
    return "".join(out)


REVIEWS_DIR = os.path.join(BASE, "reviews")

def review_dates(acct):
    """该账号已有的复盘日期（新→旧）。"""
    d = os.path.join(REVIEWS_DIR, acct)
    if not os.path.isdir(d):
        return []
    dates = []
    for fn in os.listdir(d):
        if fn.endswith(".md") and len(fn) == 13 and fn[:4].isdigit():
            dates.append(fn[:-3])
    return sorted(dates, reverse=True)

def review_html(acct, review_date=""):
    """AI 数据复盘 D区内容：最新一篇复盘正文＋往期日期导航。"""
    dates = review_dates(acct)
    if not dates:
        return ('<div class="sec-head"><div class="k">数据复盘</div><h2>AI 复盘信息</h2>'
                '<p>该账号暂无 AI 复盘记录。</p></div>')
    if not review_date or review_date not in dates:
        review_date = dates[0]
    try:
        with open(os.path.join(REVIEWS_DIR, acct, review_date + ".md"), encoding="utf-8") as f:
            body = md_to_html(f.read())
    except Exception:
        body = "<p>复盘文件读取失败。</p>"
    nav_dates = dates[:13]
    if review_date not in nav_dates:
        nav_dates = nav_dates + [review_date]
    nav = "".join(
        f'<a class="review-date{" on" if d == review_date else ""}" href="/?mode=fans&view=review&acct={acct}&period=daily&review={d}">{d}</a>'
        for d in nav_dates)
    return (f'<div class="sec-head"><div class="k">数据复盘 · 日复盘 · AI 复盘信息</div>'
            f'<h2>每日复盘 · {esc(review_date)}</h2><p>每天 09:50 由 AI 根据昨日采集数据生成；只认真实文件，缺失会标注，不编造。</p></div>'
            f'<div class="review-nav">{nav}</div>'
            f'<div class="review-body">{body}</div>')


# ---- 复盘实时数据（周/月：直接读盒子最新 stats，请求时现算）----
STATS_DIR = {}  # 复盘数据源：{账号: stats 目录}，示例 {"main": "/path/to/stats"}；留空则复盘显示为空

def _read_csv(path):
    import csv
    rows = []
    try:
        with open(path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                rows.append(r)
    except Exception:
        pass
    return rows

def _to_int(s, default=0):
    try:
        return int(float(s))
    except Exception:
        return default

def _to_float(s, default=0.0):
    try:
        return float(s)
    except Exception:
        return default

def period_summary_html(acct, days, label):
    """周/月复盘：实时读最新 stats，按近 N 天现算并渲染。"""
    sdir = STATS_DIR.get(acct, "")
    daily = [r for r in _read_csv(os.path.join(sdir, "daily.csv")) if r.get("date")]
    daily.sort(key=lambda r: r.get("date", ""))
    if not daily:
        return ('<div class="sec-head"><div class="k">数据复盘</div><h2>AI 复盘信息</h2>'
                '<p>暂无采集数据，无法复盘。</p></div>')
    window = daily[-days:]
    d0, d1 = window[0].get("date", ""), window[-1].get("date", "")
    n = len(window)
    fans0 = _to_int(window[0].get("total_fans"))
    fans1 = _to_int(window[-1].get("total_fans"))
    reads = sum(_to_int(r.get("y_read")) for r in window)
    shares = sum(_to_int(r.get("y_share")) for r in window)
    newfans = sum(_to_int(r.get("y_newfans")) for r in window)
    avg_read = round(reads / n, 1) if n else 0
    income_rows = _read_csv(os.path.join(sdir, "income.csv"))
    inc_map = {r.get("date", ""): _to_float(r.get("income_yuan")) for r in income_rows}
    income = round(sum(inc_map.get(r.get("date", ""), 0.0) for r in window), 2)
    inc_missing = sum(1 for r in window if r.get("date", "") not in inc_map)
    # 区间内新文（按 articles.csv 的 date 落在窗口内，去重 appmsgid 取最新快照）
    arts = _read_csv(os.path.join(sdir, "articles.csv"))
    in_window = {}
    for a in arts:
        if d0 <= (a.get("date") or "") <= d1:
            in_window[a.get("appmsgid") or a.get("title")] = a
    art_list = sorted(in_window.values(), key=lambda a: _to_int(a.get("read_num")), reverse=True)
    # 按篇收入（article_income.csv，当前账号目录）：先按 appmsgid，再按（标题＋发布日期）
    art_inc_rows = _read_csv(os.path.join(sdir, "article_income.csv"))
    inc_by_id = {}
    inc_by_td = {}
    for _ir in art_inc_rows:
        _raw = (_ir.get("income_yuan") or "").strip()
        if not _raw:
            continue
        try:
            _v = float(_raw)
        except ValueError:
            continue
        if _ir.get("appmsgid"):
            inc_by_id[_ir.get("appmsgid")] = _v
        inc_by_td[((_ir.get("title") or ""), (_ir.get("date") or ""))] = _v
    def _art_inc(a):
        _v = inc_by_id.get(a.get("appmsgid") or "") if a.get("appmsgid") else None
        if _v is None:
            _v = inc_by_td.get(((a.get("title") or ""), (a.get("date") or "")))
        return f"{_v:.2f}" if _v is not None else "—"
    # 每日明细
    drows = "".join(
        f"<tr><td>{esc(r.get('date',''))}</td><td>{_to_int(r.get('total_fans'))}</td>"
        f"<td>{_to_int(r.get('y_read'))}</td><td>{_to_int(r.get('y_share'))}</td>"
        f"<td>{_to_int(r.get('y_newfans'))}</td></tr>" for r in window)
    if art_list:
        arows = "".join(
            f"<tr><td>{esc((a.get('title') or '')[:28])}</td><td>{esc(a.get('date',''))}</td>"
            f"<td>{_to_int(a.get('read_num'))}</td><td>{_to_int(a.get('like_num'))}</td>"
            f"<td>{_to_int(a.get('comment_num'))}</td><td>{_art_inc(a)}</td></tr>" for a in art_list[:10])
        art_html = (f'<h2>文章复盘（区间内 {len(art_list)} 篇，按阅读排序）</h2>'
                    f'<table class="rank-table"><tr><th>标题</th><th>日期</th><th>阅读</th><th>点赞</th><th>评论</th><th>累计收入（元）</th></tr>{arows}</table>')
        best = art_list[0]
        best_line = (f"<li>阅读最高：<strong>{esc(best.get('title') or '')}</strong> "
                     f"（{esc(best.get('date',''))}，{_to_int(best.get('read_num'))} 读 / "
                     f"{_to_int(best.get('like_num'))} 赞 / {_to_int(best.get('comment_num'))} 评论）。</li>")
    else:
        art_html = "<h2>文章复盘</h2><p>区间内无新发表文章。</p>"
        best_line = ""
    net = fans1 - fans0
    net_txt = f"+{net}" if net >= 0 else str(net)
    inc_txt = f"¥{income}" if inc_missing == 0 else f"¥{income}（{inc_missing} 天收入未出数）"
    return (f'<div class="sec-head"><div class="k">数据复盘 · {label} · AI 复盘信息（实时数据）</div>'
            f'<h2>{label}复盘 · {esc(d0)} ～ {esc(d1)}（共 {n} 天）</h2>'
            f'<p>实时读取盒子最新采集数据现算；只认真实文件，缺失会标注，不编造。</p></div>'
            f'<div class="review-body">'
            f'<h2>数据概览</h2><ul>'
            f'<li>总粉丝：{fans0} → <strong>{fans1}</strong>（净 {net_txt}）</li>'
            f'<li>全号阅读：<strong>{reads}</strong> 人（日均 {avg_read}）</li>'
            f'<li>新增关注：<strong>{newfans}</strong> 人；分享：<strong>{shares}</strong> 次</li>'
            f'<li>流量主收入：<strong>{inc_txt}</strong></li>'
            f'{best_line}</ul>'
            f'{art_html}'
            f'<h2>每日明细</h2>'
            f'<table class="rank-table"><tr><th>日期</th><th>总粉丝</th><th>阅读</th><th>分享</th><th>新增关注</th></tr>{drows}</table>'
            f'</div>')


def brief_content_html(b):
    """简报详情正文（不含 page_head/page_foot），供 D区内嵌与独立页共用。"""
    parts = [f'<a class="back" href="/?mode=news&group=world&section=lianbo">← 返回国内新闻联播</a>']
    parts.append(f'<div class="sec-head"><div class="k">国内新闻联播 · {esc(b.get("broadcast_date",""))}</div><h2 style="font-size:22px">{esc(b.get("headline") or b.get("broadcast_date"))}</h2><p>19:00 播出 · 来源：{esc(b.get("source_name",""))}</p></div>')
    if b.get("overview"):
        parts.append(f'<div class="overview">{esc(b["overview"])}</div>')
    pis = b.get("program_items") or []
    if pis:
        lis = []
        for pi in pis:
            t = esc(pi.get("title", ""))
            ch = pi.get("children") or []
            sub = ""
            if ch:
                sub = "<ul class='sub'>" + "".join(f"<li>{esc(c if isinstance(c, str) else c.get('title',''))}</li>" for c in ch) + "</ul>"
            lis.append(f"<li>{t}{sub}</li>")
        parts.append(f'<div class="card"><h2 style="font-size:16.5px;margin-bottom:8px">一、今天联播讲了什么</h2><ol class="prog">{"".join(lis)}</ol></div>')
    imps = b.get("impacts") or []
    if imps:
        ih = []
        for im in imps:
            ih.append(f'''<div class="impact"><span class="lb">{esc(im.get("label",""))}</span>
<div class="hd">{esc(im.get("headline",""))}</div>
<p>{esc(im.get("relation",""))}</p>
<div class="act">💡 {esc(im.get("action",""))}</div></div>''')
        parts.append(f'<div class="card"><h2 style="font-size:16.5px;margin-bottom:8px">二、对老百姓的影响 + 该怎么做</h2>{"".join(ih)}</div>')
    bl = b.get("bottom_line") or []
    if bl:
        parts.append(f'<div class="card"><h2 style="font-size:16.5px;margin-bottom:8px">三句话总结</h2><ol class="bl">{"".join(f"<li>{esc(x)}</li>" for x in bl)}</ol></div>')
    if b.get("source_url"):
        parts.append(f'<div class="meta" style="font-size:12px;color:#9ca3af">来源链接：<a href="{esc(b["source_url"])}">{esc(b["source_url"])}</a></div>')
    return "".join(parts)


def render_brief(b):
    return page_head(b.get("headline", "简报")) + brief_content_html(b) + page_foot()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def authed(self):
        ck = self.headers.get("Cookie", "")
        return f"auth={auth_token()}" in ck

    def work_authed(self):
        ck = self.headers.get("Cookie", "")
        return f"work_auth={work_auth_token()}" in ck

    def _gzip_body(self, body):
        ae = self.headers.get("Accept-Encoding", "")
        if "gzip" in ae and len(body) >= 1024:
            try:
                return gzip.compress(body, compresslevel=6), True
            except Exception:
                pass
        return body, False

    def send_html(self, html, code=200, cookie=None):
        body, zipped = self._gzip_body(html.encode("utf-8"))
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if zipped:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, code=200):
        body, zipped = self._gzip_body(json.dumps(obj, ensure_ascii=False).encode("utf-8"))
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if zipped:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy_fans(self, u, body):
        # 粉丝系统反向代理：/fansapp 或 /fansapp/... -> 127.0.0.1:8080 的对应路径
        # 让工作台与粉丝系统共用一个零讯隧道；粉丝自身的口令/token 鉴权不变
        # 用 http.client 直连（不跟随重定向），以便把粉丝系统的 302 与 Location 原样透传给浏览器
        import http.client as _httpclient
        p = u.path
        if p == "/fansapp":
            tp = "/"
        elif p.startswith("/fansapp/"):
            tp = p[len("/fansapp"):]
        else:
            return False
        path = tp
        if u.query:
            path = path + "?" + u.query
        fwd = {}
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in ("host", "connection", "content-length"):
                continue
            fwd[k] = v
        try:
            conn = _httpclient.HTTPConnection("127.0.0.1", 8080, timeout=15)
            conn.request(self.command, path, body=body, headers=fwd)
            resp = conn.getresponse()
            status = resp.status
            headers = resp.getheaders()
            data = resp.read()
            conn.close()
        except Exception:
            self.send_json({"ok": False, "msg": "fans unavailable"}, 502)
            return True
        self.send_response(status)
        for k, v in headers:
            lk = k.lower()
            if lk in ("connection", "transfer-encoding", "content-length"):
                continue
            if lk == "location" and v.startswith("/") and not v.startswith("/fansapp"):
                v = "/fansapp" + v
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)
        return True

    def do_GET(self):
        u = urlparse(self.path)
        if self._proxy_fans(u, None):
            return
        if u.path == "/command-center" or u.path == "/command-center/":
            if not self.authed():
                self.send_html(login_html(next_url="/command-center/")); return
            try:
                with open(os.path.join(BASE, "command-center", "index.html"), encoding="utf-8") as f:
                    self.send_html(f.read())
            except Exception:
                self.send_html("<h3>指挥中心页面未部署</h3>", 500)
            return
        if u.path == "/principle" or u.path == "/principle/":
            if not self.authed():
                self.send_html(login_html(next_url="/principle/")); return
            try:
                with open(os.path.join(BASE, "principle", "index.html"), encoding="utf-8") as f:
                    self.send_html(f.read())
            except Exception:
                self.send_html("<h3>工作台原理页面未部署</h3>", 500)
            return
        if u.path == "/change-password":
            if not self.authed():
                self.send_html(login_html(next_url="/change-password")); return
            self.send_html(change_password_html()); return
        if u.path == "/chat" or u.path.startswith("/api/chat/"):
            if not self.authed():
                if u.path == "/chat":
                    self.send_html(login_html(next_url="/chat")); return
                self.send_json({"ok": False, "msg": "unauthorized"}, 401); return
            if u.path == "/chat":
                try:
                    with open(os.path.join(BASE, "chat.html"), encoding="utf-8") as f:
                        self.send_html(f.read())
                except Exception:
                    self.send_html("<h3>聊天页面未部署</h3>", 500)
                return
            if u.path == "/api/chat/agents":
                self.send_json({"ok": True, "agents": chat_agent_status()}); return
            if u.path == "/api/chat/messages":
                q = parse_qs(u.query)
                agent = (q.get("agent") or [""])[0]
                if agent not in [a[0] for a in chat_agent_list()]:
                    self.send_json({"ok": False, "msg": "unknown agent"}, 400); return
                ru, ra = chat_read_state(agent)
                self.send_json({"ok": True, "messages": chat_load(agent), "read": {"user": ru, "agent": ra}}); return
            if u.path == "/api/chat/stream":
                # SSE 推送：长连接；ThreadingHTTPServer 下每连接一个线程
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                q = CHAT_HUB.subscribe()
                try:
                    hello = {"typing": {}, "read": {}, "stream": {}}
                    for aid, _n, _i, _c, _r in chat_agent_list():
                        hello["typing"][aid] = chat_typing_state(aid)[0]
                        ru, ra = chat_read_state(aid)
                        hello["read"][aid] = {"user": ru, "agent": ra}
                        sid, txt, _sts = chat_stream_state(aid)
                        if sid:
                            hello["stream"][aid] = {"stream_id": sid, "text": txt}
                    self.wfile.write(("event: hello\ndata: %s\n\n" % json.dumps(hello, ensure_ascii=False)).encode("utf-8"))
                    self.wfile.flush()
                    last_ping = time.time()
                    while True:
                        try:
                            line = q.get(timeout=5)
                        except queue.Empty:
                            if time.time() - last_ping >= 25:
                                self.wfile.write(b": ping\n\n")
                                self.wfile.flush()
                                last_ping = time.time()
                            continue
                        self.wfile.write(line.encode("utf-8"))
                        self.wfile.flush()
                        last_ping = time.time()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception:
                    pass
                finally:
                    CHAT_HUB.unsubscribe(q)
                return
            if u.path.startswith("/api/chat/file/"):
                name = u.path[len("/api/chat/file/"):]
                if not re.fullmatch(r"[0-9a-f]{32}\.[a-z0-9]+", name or ""):
                    self.send_json({"ok": False, "msg": "not found"}, 404); return
                fp = os.path.join(CHAT_UPLOAD_DIR, name)
                if not os.path.isfile(fp):
                    self.send_json({"ok": False, "msg": "not found"}, 404); return
                ext = name.rsplit(".", 1)[-1].lower()
                mime = CHAT_UPLOAD_EXTS.get(ext, "application/octet-stream")
                try:
                    with open(fp, "rb") as f:
                        data = f.read()
                except Exception:
                    self.send_json({"ok": False, "msg": "read failed"}, 500); return
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(data)))
                disp = "inline" if mime in CHAT_UPLOAD_INLINE_MIMES else "attachment"
                self.send_header("Content-Disposition", '%s; filename="%s"' % (disp, name))
                self.send_header("Cache-Control", "private, max-age=86400")
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_json({"ok": False, "msg": "not found"}, 404); return
        if u.path == "/download":
            # 公开下载入口：按 DOWNLOADS 里的 download_file 直传（支持断点续传）；没配就 404
            try:
                idx = int((parse_qs(u.query).get("i") or ["0"])[0])
                fpath = (DOWNLOADS[idx] or {}).get("download_file") or ""
            except Exception:
                fpath = ""
            if not fpath:
                self.send_json({"ok": False, "msg": "download not configured"}, 404); return
            try:
                size = os.path.getsize(fpath)
            except Exception:
                self.send_json({"ok": False, "msg": "file not found"}, 404); return
            rng = self.headers.get("Range", "") or ""
            start, end, status = 0, size - 1, 200
            m = re.match(r"bytes=(\d*)-(\d*)\s*$", rng.strip()) if rng else None
            if m:
                a, b = m.group(1), m.group(2)
                try:
                    if a == "" and b != "":
                        start = max(0, size - int(b))
                    elif b == "":
                        start = int(a)
                    elif a != "":
                        start = int(a)
                        end = min(int(b), size - 1)
                    else:
                        raise ValueError
                    if start >= size or start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", "bytes */%d" % size)
                        self.send_header("Accept-Ranges", "bytes")
                        self.end_headers(); return
                    status = 206
                except ValueError:
                    start, end, status = 0, size - 1, 200
            try:
                f = open(fpath, "rb")
            except Exception:
                self.send_json({"ok": False, "msg": "read failed"}, 500); return
            try:
                f.seek(start)
                self.send_response(status)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", "attachment; filename=\"%s\"" % os.path.basename(fpath))
                self.send_header("Accept-Ranges", "bytes")
                if status == 206:
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
                self.send_header("Content-Length", str(end - start + 1))
                self.end_headers()
                left = end - start + 1
                try:
                    while left > 0:
                        chunk = f.read(min(65536, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            finally:
                f.close()
            return
        if u.path == "/api/push":
            self.send_json({"ok": False, "msg": "use POST"}, 405); return
        if not self.authed():
            self.send_html(login_html()); return
        if u.path == "/api/notes":
            self.send_json({"ok": True, "notes": notes_load()}); return
        if u.path == "/api/system-status":
            self.send_json({"ok": True, "status": system_status()}); return
        if u.path == "/api/downloads":
            self.send_json({"ok": True, "items": downloads_status()}); return
        if u.path == "/api/gold-price":
            self.send_json(get_gold_price()); return
        q = parse_qs(u.query)
        data = load_data()
        if u.path == "/fans-bg.jpg":
            try:
                with open(os.path.join(BASE, "fans_bg.jpg"), "rb") as f:
                    img = f.read()
            except Exception:
                self.send_json({"ok": False, "msg": "image not found"}, 404); return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(img)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(img)
            return
        if u.path == "/time-bg.jpg":
            try:
                with open(os.path.join(BASE, "time_bg.jpg"), "rb") as f:
                    img = f.read()
            except Exception:
                self.send_json({"ok": False, "msg": "image not found"}, 404); return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(img)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(img)
            return
        if u.path == "/logo.png":
            try:
                with open(os.path.join(BASE, "logo.png"), "rb") as f:
                    img = f.read()
            except Exception:
                self.send_json({"ok": False, "msg": "image not found"}, 404); return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(img)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(img)
            return
        if u.path.startswith("/avatar-") and u.path.endswith(".png"):
            allowed = ("avatar-rumi.png", "avatar-Lei.png", "avatar-muse.png", "avatar-atlas.png", "avatar-muse-2.png", "avatar-muse-3.png", "avatar-muse-4.png")
            fn = u.path[1:]
            if fn not in allowed:
                self.send_json({"ok": False, "msg": "not found"}, 404); return
            try:
                with open(os.path.join(BASE, fn), "rb") as f:
                    img = f.read()
            except Exception:
                self.send_json({"ok": False, "msg": "avatar not found"}, 404); return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(img)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(img)
            return
        if u.path.startswith("/avatar-") and u.path.endswith(".mp4"):
            allowed = ("avatar-Lei.mp4", "avatar-muse.mp4", "avatar-atlas.mp4", "avatar-muse-2.mp4", "avatar-muse-3.mp4", "avatar-muse-4.mp4")
            fn = u.path[1:]
            if fn not in allowed:
                self.send_json({"ok": False, "msg": "not found"}, 404); return
            try:
                with open(os.path.join(BASE, fn), "rb") as f:
                    vid = f.read()
            except Exception:
                self.send_json({"ok": False, "msg": "avatar not found"}, 404); return
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(vid)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(vid)
            return
        if u.path.startswith("/fans-avatar/"):
            acct = u.path[len("/fans-avatar/"):]
            cfg = FANS_ACCOUNTS.get(acct)
            if not cfg:
                self.send_json({"ok": False, "msg": "unknown account"}, 404); return
            try:
                with open(cfg["avatar_file"], "rb") as f:
                    img = f.read()
            except Exception:
                self.send_json({"ok": False, "msg": "avatar not found"}, 404); return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(img)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(img)
            return
        if u.path == "/fans":
            self.send_html(render_fans()); return
        if u.path == "/brief":
            d = (q.get("date") or [""])[0]
            self.send_response(302)
            self.send_header("Location", f"/?mode=news&group=world&section=lianbo&brief={d}")
            self.end_headers(); return
        mode = (q.get("mode") or [""])[0]
        group = (q.get("group") or ["world"])[0]
        if group not in ("world", "career", "family"):
            group = "world"
        section = (q.get("section") or [""])[0]
        view = (q.get("view") or ["overview"])[0]
        acct = (q.get("acct") or ["main"])[0]
        if acct not in FANS_ACCOUNTS:
            acct = "main"
        brief_date = (q.get("brief") or [""])[0]
        review_date = (q.get("review") or [""])[0]
        period = (q.get("period") or ["daily"])[0]
        if period not in ("daily", "weekly", "monthly"):
            period = "daily"
        if q.get("frag"):
            self.send_html(news_content_html(data, group, section)); return
        if mode not in ("home", "news", "fans", "chat", "work", "command", "principle"):
            mode = "news" if (section or brief_date) else "home"
        if brief_date:
            mode = "news"; group = "world"; section = "lianbo"
        if mode == "work" and not self.work_authed():
            self.send_html(work_login_html(next_url=self.path)); return
        self.send_html(render_shell(data, mode, group, section, view, force_wx=bool(q.get("wx")), acct=acct, brief_date=brief_date, review_date=review_date, period=period))

    def do_POST(self):
        u = urlparse(self.path)
        ln = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(ln) if ln else b""
        if self._proxy_fans(u, raw or None):
            return
        if u.path == "/api/chat/send":
            if not self.authed():
                self.send_json({"ok": False, "msg": "unauthorized"}, 401); return
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                self.send_json({"ok": False, "msg": "bad json"}, 400); return
            agent = payload.get("agent", "")
            text = (payload.get("text", "") or "").strip()
            if agent not in [a[0] for a in chat_agent_list()]:
                self.send_json({"ok": False, "msg": "unknown agent"}, 400); return
            if not text or len(text) > 2000:
                self.send_json({"ok": False, "msg": "empty or too long"}, 400); return
            nid = chat_append(agent, "user", text)
            self.send_json({"ok": True, "id": nid}); return
        if u.path == "/api/chat/read":
            if not self.authed():
                self.send_json({"ok": False, "msg": "unauthorized"}, 401); return
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                self.send_json({"ok": False, "msg": "bad json"}, 400); return
            agent = payload.get("agent", "")
            if agent not in [a[0] for a in chat_agent_list()]:
                self.send_json({"ok": False, "msg": "unknown agent"}, 400); return
            try:
                up_to = int(payload.get("up_to") or 0)
            except Exception:
                self.send_json({"ok": False, "msg": "bad up_to"}, 400); return
            chat_set_read(agent, "user", up_to)
            self.send_json({"ok": True}); return
        if u.path == "/api/chat/upload":
            if not self.authed():
                self.send_json({"ok": False, "msg": "unauthorized"}, 401); return
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                self.send_json({"ok": False, "msg": "bad json"}, 400); return
            agent = payload.get("agent", "")
            if agent not in [a[0] for a in chat_agent_list()]:
                self.send_json({"ok": False, "msg": "unknown agent"}, 400); return
            filename = os.path.basename(str(payload.get("filename") or payload.get("name") or "file"))[:80] or "file"
            mime = (payload.get("mime") or "").strip()
            data = payload.get("data", "") or ""
            if data.startswith("data:"):
                try:
                    header, b64 = data.split(",", 1)
                    mime = header.split(";")[0][5:] or mime
                    data_bytes = base64.b64decode(b64, validate=True)
                except Exception:
                    self.send_json({"ok": False, "msg": "bad file data"}, 400); return
            else:
                try:
                    data_bytes = base64.b64decode(data, validate=True)
                except Exception:
                    self.send_json({"ok": False, "msg": "bad file data"}, 400); return
            try:
                stored, url, size, kind, mime = chat_upload_save(filename, mime, data_bytes)
            except ValueError as e:
                self.send_json({"ok": False, "msg": str(e)}, 400); return
            except Exception as e:
                self.send_json({"ok": False, "msg": "保存失败：%s" % e}, 500); return
            if kind == "image":
                nid = chat_append(agent, "user", "", image=url)
            else:
                nid = chat_append(agent, "user", "", file={"url": url, "name": filename, "size": size, "mime": mime})
            self.send_json({"ok": True, "id": nid, "url": url, "name": filename, "size": size, "mime": mime, "kind": kind}); return
        if u.path == "/api/notes/add":
            if not self.authed():
                self.send_json({"ok": False, "msg": "unauthorized"}, 401); return
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                self.send_json({"ok": False, "msg": "bad json"}, 400); return
            text = (payload.get("text", "") or "").strip()
            if not text or len(text) > 200:
                self.send_json({"ok": False, "msg": "empty or too long"}, 400); return
            note = notes_add(text)
            self.send_json({"ok": True, "note": note}); return
        if u.path == "/api/notes/toggle":
            if not self.authed():
                self.send_json({"ok": False, "msg": "unauthorized"}, 401); return
            try:
                payload = json.loads(raw.decode("utf-8"))
                nid = int(payload.get("id") or 0)
            except Exception:
                self.send_json({"ok": False, "msg": "bad id"}, 400); return
            note = notes_toggle(nid)
            if note is None:
                self.send_json({"ok": False, "msg": "not found"}, 404); return
            self.send_json({"ok": True, "note": note}); return
        if u.path == "/api/notes/delete":
            if not self.authed():
                self.send_json({"ok": False, "msg": "unauthorized"}, 401); return
            try:
                payload = json.loads(raw.decode("utf-8"))
                nid = int(payload.get("id") or 0)
            except Exception:
                self.send_json({"ok": False, "msg": "bad id"}, 400); return
            notes_delete(nid)
            self.send_json({"ok": True}); return
        if u.path == "/work-login":
            q = parse_qs(raw.decode("utf-8", "ignore"))
            pw = (q.get("password") or [""])[0]
            nxt = (q.get("next") or ["/?mode=work"])[0]
            if not nxt.startswith("/?mode=work"):
                nxt = "/?mode=work"
            if pw == WORK_PASSWORD:
                self.send_response(302)
                self.send_header("Set-Cookie", f"work_auth={work_auth_token()}; Path=/; Max-Age=2592000; HttpOnly")
                self.send_header("Location", nxt)
                self.end_headers(); return
            self.send_html(work_login_html('<div class="err">口令不对，重试</div>', next_url=nxt)); return
        if u.path == "/login":
            q = parse_qs(raw.decode("utf-8", "ignore"))
            pw = (q.get("password") or [""])[0]
            nxt = (q.get("next") or ["/"])[0]
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = "/"
            if pw == PASSWORD:
                self.send_response(302)
                self.send_header("Set-Cookie", f"auth={auth_token()}; Path=/; Max-Age=2592000; HttpOnly")
                self.send_header("Location", nxt)
                self.end_headers(); return
            self.send_html(login_html('<div class="err">口令不对，重试</div>', next_url=nxt)); return
        if u.path == "/api/change-password":
            if not self.authed():
                self.send_json({"ok": False, "msg": "unauthorized"}, 401); return
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                self.send_json({"ok": False, "msg": "bad json"}, 400); return
            kind = payload.get("kind") or "main"
            if kind not in ("main", "work"):
                self.send_json({"ok": False, "msg": "bad kind"}, 400); return
            old = payload.get("old") or ""
            new = (payload.get("new") or "").strip()
            cur = PASSWORD if kind == "main" else WORK_PASSWORD
            if old != cur:
                self.send_json({"ok": False, "msg": "旧口令不对"}, 400); return
            if len(new) < 4 or len(new) > 64:
                self.send_json({"ok": False, "msg": "新口令须为 4-64 个字符"}, 400); return
            if new == cur:
                self.send_json({"ok": False, "msg": "新口令与旧口令相同"}, 400); return
            globals()["PASSWORD" if kind == "main" else "WORK_PASSWORD"] = new
            _save_auth()
            if kind == "main":
                globals()["FANS_TOKEN"] = fans_token()
            body = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if kind == "main":
                self.send_header("Set-Cookie", "auth=" + auth_token() + "; Path=/; Max-Age=2592000; HttpOnly")
            else:
                self.send_header("Set-Cookie", "work_auth=" + work_auth_token() + "; Path=/; Max-Age=2592000; HttpOnly")
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/push":
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                self.send_json({"ok": False, "msg": "bad json"}, 400); return
            if payload.get("password") != PASSWORD:
                self.send_json({"ok": False, "msg": "auth failed"}, 403); return
            brief = payload.get("brief") or {}
            if not brief.get("broadcast_date"):
                self.send_json({"ok": False, "msg": "missing broadcast_date"}, 400); return
            d = load_data()
            bd = brief["broadcast_date"]
            d["briefs"] = [b for b in d["briefs"] if b.get("broadcast_date") != bd]
            d["briefs"].append(brief)
            d["briefs"].sort(key=lambda b: b.get("broadcast_date", ""), reverse=True)
            save_data(d)
            self.send_json({"ok": True, "count": len(d["briefs"]), "date": bd}); return
        self.send_json({"ok": False, "msg": "not found"}, 404)

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18080
    chat_watch_start()
    wx_watch_start()
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()

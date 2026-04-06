"""
蒸馏看门狗：监控 _progress.json，超时未更新则杀进程+重启 Ollama+重新运行。
用法：python distill_watchdog.py
"""
import subprocess, sys, time, json, os, re, http.server, traceback
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key and value and key not in os.environ:
                os.environ[key] = value


load_env_file(Path(__file__).resolve().parent / ".env")

STALL_TIMEOUT = 300  # 5 分钟无更新视为卡死
STALL_REPORT_TIMEOUT = int(os.environ.get("DISTILL_STALL_REPORT_TIMEOUT", "600"))  # 同一进度卡住满 10 分钟单独汇报
CHECK_INTERVAL = 30   # 每 30 秒检查一次
MAX_RESTARTS = 5
REPORT_INTERVAL = int(os.environ.get("DISTILL_REPORT_INTERVAL", "3600"))  # 默认每 1 小时飞书汇总一次
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
BOOK_FILTER = os.environ.get("DISTILL_BOOK", "")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5-9b-distill")
OLLAMA_RESTART_INTERVAL = int(os.environ.get("DISTILL_OLLAMA_RESTART", "0"))

BASE_DIR = Path(__file__).resolve().parent
SCRIPT = BASE_DIR / "distill_orchestrator.py"
WORK_DIR = BASE_DIR
# 通过环境变量 THREE_TIER_SOURCE_BASE 和 THREE_TIER_OUTPUT_BASE 配置
DEFAULT_SOURCE_BASE = str(Path(__file__).resolve().parent.parent / "books")
DEFAULT_OUTPUT_BASE = str(Path(__file__).resolve().parent.parent / "output")
LLM_BACKEND = os.environ.get("LLM_BACKEND", "ollama").lower()
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
PROGRESS_DIR = Path(os.environ.get("THREE_TIER_OUTPUT_BASE", DEFAULT_OUTPUT_BASE))
ACTIVE_MODEL = (
    OLLAMA_MODEL
    if LLM_BACKEND == "ollama"
    else os.environ.get("GLM_MODEL")
    or os.environ.get("ANTHROPIC_MODEL")
    or os.environ.get("MODEL")
    or ""
)
LOG_DIR = WORK_DIR / "watchdog_logs"
LOG_DIR.mkdir(exist_ok=True)

# =============================================================================
# 可视化面板配置
# =============================================================================
DASHBOARD_PORT = 8089  # 面板 HTTP 端口
USE_DASHBOARD = os.environ.get("DISTILL_DASHBOARD", "1").lower() in ("1", "true", "yes")
SKIP_BOOKS_FILE = WORK_DIR / "skip_books.txt"


def _normalize_skip_rule(value: str) -> str:
    return value.strip().replace("\\", "/").lower()


def load_skip_rules() -> set[str]:
    if not SKIP_BOOKS_FILE.exists():
        return set()
    rules: set[str] = set()
    try:
        with open(SKIP_BOOKS_FILE, "r", encoding="utf-8") as handle:
            for line in handle:
                raw = line.strip()
                if not raw or raw.startswith("#"):
                    continue
                rules.add(_normalize_skip_rule(raw))
    except Exception as exc:
        print_log(f"[WARN] 读取跳过名单失败: {exc}")
    return rules


def is_skipped_book(genre: str, book_file: str, skip_rules: set[str] | None = None) -> bool:
    rules = skip_rules if skip_rules is not None else load_skip_rules()
    stem = Path(book_file).stem
    candidates = {
        _normalize_skip_rule(book_file),
        _normalize_skip_rule(stem),
        _normalize_skip_rule(f"{genre}/{book_file}"),
        _normalize_skip_rule(f"{genre}/{stem}"),
    }
    return any(candidate in rules for candidate in candidates)


def using_ollama(env: Dict[str, str] | None = None) -> bool:
    current_env = env or os.environ
    return current_env.get("LLM_BACKEND", LLM_BACKEND).lower() == "ollama"


def check_ollama_available() -> bool:
    if not using_ollama():
        return True
    try:
        import requests
        response = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
        return response.status_code == 200
    except Exception:
        return False

def find_book_output_dir(book_filter: str = ""):
    """找到当前书的输出目录（genre/book_name 层级）

    当 BOOK_FILTER 指定时，可能有多个目录匹配（如历史遗留目录）。
    此时优先选择：有 tier1_output/tier2_output 且修改时间最近的目录。
    """
    root = Path(PROGRESS_DIR)
    if not root.exists():
        return None
    filter_value = book_filter or BOOK_FILTER
    if filter_value:
        # 有过滤条件：精确匹配 genre/book_name
        # 优先选择有实际输出的目录（tier1/tier2 子目录），而非空目录或归档目录
        filter_path = Path(filter_value)
        filter_variants = {
            filter_value,
            filter_path.name,
            filter_path.stem,
        }
        best_dir = None
        best_dir_mtime = None
        for genre_dir in root.iterdir():
            if not genre_dir.is_dir():
                continue
            # 跳过归档目录
            if genre_dir.name == "归档":
                continue
            for book_dir in genre_dir.iterdir():
                if book_dir.is_dir() and any(part and part in book_dir.name for part in filter_variants):
                    # 检查是否有 tier 输出子目录（代表真正在处理的目录）
                    has_tier = (book_dir / "tier1_output").exists() or (book_dir / "tier2_output").exists()
                    try:
                        dir_mtime = datetime.fromtimestamp(book_dir.stat().st_mtime)
                    except OSError:
                        dir_mtime = datetime.min
                    # 优先选有 tier 输出的；其次选修改时间最近的
                    if has_tier:
                        return book_dir  # 有输出的立即返回
                    if best_dir is None or dir_mtime > best_dir_mtime:
                        best_dir = book_dir
                        best_dir_mtime = dir_mtime
        return best_dir
    # 无 BOOK_FILTER：找最近修改的 genre/book_name 二级目录
    latest_dir = None
    latest_time = None
    for genre_dir in root.iterdir():
        if not genre_dir.is_dir():
            continue
        for book_dir in genre_dir.iterdir():
            if not book_dir.is_dir():
                continue
            try:
                mtime = datetime.fromtimestamp(book_dir.stat().st_mtime)
            except OSError:
                continue
            if latest_time is None or mtime > latest_time:
                latest_dir = book_dir
                latest_time = mtime
    return latest_dir

def find_progress_file(book_dir, min_mtime=None):
    """在指定书的输出目录下查找最新进度（直接路径，避免 rglob 遍历数千文件）

    Args:
        book_dir: 书输出目录
        min_mtime: 忽略早于此时间的进度文件（防止遗留的旧进度触发误判）
    """
    if not book_dir or not book_dir.exists():
        return None, None
    latest = None
    latest_time = None
    # 直接检查已知路径，不做递归遍历
    candidates = []
    tier2_progress = book_dir / "tier2_output" / "_progress.json"
    tier3_progress = book_dir / "tier3_output" / "_progress.json"
    if tier2_progress.exists():
        candidates.append(tier2_progress)
    if tier3_progress.exists():
        candidates.append(tier3_progress)
    # Tier3 完成后会有 knowledge_items.json
    tier3_kb = book_dir / "tier3_output" / "knowledge_items.json"
    if tier3_kb.exists():
        candidates.append(tier3_kb)
    for f in candidates:
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            # 忽略早于启动时间的旧进度文件（防止遗留文件触发误判）
            if min_mtime is not None and mtime < min_mtime:
                continue
            if latest_time is None or mtime > latest_time:
                latest = f
                latest_time = mtime
        except OSError:
            continue
    return latest, latest_time

def kill_stale_processes():
    """杀掉 distill_orchestrator.py 的进程，精确匹配脚本路径避免误杀其他实例"""
    killed = []
    # 精确匹配：命令行中必须包含完整脚本路径，防止误杀其他 watchdog 实例
    # 使用安全的方式：分别检查两个条件，避免 PowerShell 注入
    try:
        result = subprocess.run(
            ["powershell", "-Command",
             "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
             "Where-Object { $_.CommandLine -match 'distill_orchestrator' } | "
             "Where-Object { $_.CommandLine -match [regex]::Escape('" + str(SCRIPT).replace("'", "''") + "') } | "
             "Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=15
        )
        for line in result.stdout.strip().split("\n"):
            pid = line.strip()
            if pid.isdigit():
                try:
                    subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=5)
                    killed.append(pid)
                except Exception:
                    pass
    except Exception:
        pass
    return killed

def restart_ollama():
    """重启 Ollama 服务并验证模型已加载"""
    if not using_ollama():
        return True
    subprocess.run(["taskkill", "/F", "/IM", "ollama.exe"], capture_output=True, timeout=5)
    time.sleep(3)
    subprocess.Popen(["ollama", "serve"], creationflags=subprocess.CREATE_NO_WINDOW)
    time.sleep(8)
    # Verify service
    try:
        import requests
        r = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
        if r.status_code != 200:
            return False
        # Verify model is loaded/available
        models = [m.get("name", "") for m in r.json().get("models", [])]
        ollama_model = os.environ.get("OLLAMA_MODEL", OLLAMA_MODEL)
        model_ok = ollama_model in models or any(ollama_model.split(":")[0] in m for m in models)
        if not model_ok:
            print_log(f"[WARN] Ollama 在线但模型 '{ollama_model}' 未加载，可用: {models}")
            return False
        return True
    except Exception as e:
        print_log(f"[WARN] Ollama 健康检查异常: {e}")
        return False

# BOOK_FILTER 已在上方模块级定义（第 18 行）
SCAN_MODE = os.environ.get("DISTILL_SCAN", os.environ.get("THREE_TIER_SCAN", "")).lower() in ("1", "true", "yes")
BATCH_MODE = os.environ.get("DISTILL_BATCH", "").lower() in ("1", "true", "yes")
OLLAMA_RESTART_INTERVAL = int(os.environ.get("DISTILL_OLLAMA_RESTART", "0"))  # 0=不定期重启, >0=每N本重启一次

def run_distillation(skip_tier1=False, skip_tier2=False, resume=False, scan=False, first_run=False, rerun_failed_only=False, book_filter=None):
    """启动蒸馏脚本

    Args:
        first_run: True 表示首次运行（用 --force 清理旧输出）；False 表示重启续跑（不用 --force 保留已有进度）
        book_filter: 覆盖模块级 BOOK_FILTER 的书籍过滤条件（用于队列模式）
    """
    env = os.environ.copy()
    env["NO_PROXY"] = "*"
    env.setdefault("LLM_BACKEND", LLM_BACKEND)
    if using_ollama(env):
        env.setdefault("OLLAMA_BASE_URL", OLLAMA_BASE_URL)
        env.setdefault("OLLAMA_MODEL", OLLAMA_MODEL)
    env["OLLAMA_MODELS"] = r"D:\03-AI相关\05-本地大模型\models"
    env.setdefault("OLLAMA_NUM_CTX", "8192")
    if not using_ollama(env):
        env.pop("OLLAMA_BASE_URL", None)
        env.pop("OLLAMA_MODEL", None)
        env.pop("OLLAMA_MODELS", None)
        env.pop("OLLAMA_NUM_CTX", None)
    env["CHAPTER_WORKERS"] = "1"
    env["TIER2_CONCURRENCY"] = os.environ.get("TIER2_CONCURRENCY", "1")
    env["THREE_TIER_SOURCE_BASE"] = os.environ.get("THREE_TIER_SOURCE_BASE", r"D:\03-AI相关\03-books(new)\99-知识库\02-小说书库-分类")
    env["THREE_TIER_OUTPUT_BASE"] = os.environ.get("THREE_TIER_OUTPUT_BASE", r"D:\03-AI相关\03-books(new)\99-知识库\04-蒸馏结果")

    # 重定向日志到文件，避免 PIPE 死锁
    env["THREE_TIER_SOURCE_BASE"] = os.environ.get("THREE_TIER_SOURCE_BASE", DEFAULT_SOURCE_BASE)
    env["THREE_TIER_OUTPUT_BASE"] = os.environ.get("THREE_TIER_OUTPUT_BASE", DEFAULT_OUTPUT_BASE)
    log_file = LOG_DIR / f"orchestrator_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    log_fd = open(log_file, "w", encoding="utf-8", buffering=1)

    cmd = [sys.executable, SCRIPT, "--max-books", "1"]
    # --force 只在首次运行（清理旧输出）时使用；重启续跑不用 --force
    _book_filter = book_filter or BOOK_FILTER
    effective_scan = scan or SCAN_MODE or bool(_book_filter)
    if first_run and _book_filter:
        cmd.append("--force")
    if _book_filter:
        cmd.extend(["--book", _book_filter])
    # 指定书籍时也走扫描模式，否则 orchestrator 只会从内置测试书单里筛选
    if effective_scan:
        cmd.append("--scan")
    if resume:
        cmd.append("--resume")
    if rerun_failed_only:
        cmd.append("--rerun-failed-only")
    if skip_tier1:
        cmd.append("--skip-tier1")
    if skip_tier2:
        cmd.append("--skip-tier2")

    print_log(f"日志文件: {log_file}")
    proc = subprocess.Popen(
        cmd,
        cwd=str(WORK_DIR), env=env,
        stdout=log_fd, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        bufsize=1  # 行缓冲
    )
    return proc, log_fd

def print_log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    try:
        print(f"[{ts}] {msg}", flush=True)
    except UnicodeEncodeError:
        safe = msg.encode("gbk", errors="replace").decode("gbk")
        print(f"[{ts}] {safe}", flush=True)

def feishu_notify(title, content):
    """通过飞书 webhook 发送进度通知"""
    if not FEISHU_WEBHOOK:
        return
    try:
        import requests
        body = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": title}},
                "elements": [{"tag": "markdown", "content": content}]
            }
        }
        requests.post(FEISHU_WEBHOOK, json=body, timeout=5)
    except Exception as e:
        print_log(f"[WARN] 飞书通知失败: {e}")
        return False
    return True

# =============================================================================
# 可视化面板（嵌入式 HTTP 服务器，线程安全）
# =============================================================================
import threading

# 全局共享状态（由 main loop 更新，由 dashboard thread 读取）
_dashboard_state = {
    "book_name": "",
    "genre": "",
    "stage": "",
    "index": "?",
    "total": "?",
    "items": "?",
    "last_update": None,
    "restarts": 0,
    "books_completed": 0,
    "books_failed": 0,
    "books_total": 0,
    "elapsed_total": 0.0,
    "stall_warn": False,
    "status": "启动中",
    "mode": "",
    "ollama_model": "",
    "log_lines": [],      # 最新日志行
    "processed_books": [],  # 从 three_tier_progress.json 读取
    "queue_preview": [],  # 接下来要处理的书（扫描源目录）
}

# 线程锁，保护 _dashboard_state 的并发访问
_dashboard_lock = threading.Lock()


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    """HTTP 请求处理：返回 JSON 状态 / HTML 页面"""

    def log_message(self, format, *args):
        pass  # 禁用默认请求日志

    def do_GET(self):
        if self.path == "/api/state":
            self._send_json(_dashboard_state)
        elif self.path == "/api/progress":
            # 返回 three_tier_progress.json 的数据
            pf = PROGRESS_DIR / "three_tier_progress.json"
            if pf.exists():
                try:
                    data = json.loads(pf.read_text(encoding="utf-8"))
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
                    return
                except Exception:
                    pass
            self._send_json({})
        elif self.path == "/" or self.path == "/index.html":
            self._send_html()
        else:
            self.send_response(404)
            self.end_headers()

    def _send_json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        except Exception:
            self.wfile.write(b"{}")

    def _send_html(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        html = _build_dashboard_html()
        self.wfile.write(html.encode("utf-8"))


def _build_dashboard_html() -> str:
    """从模板文件加载 dashboard HTML"""
    template_path = BASE_DIR / "templates" / "dashboard.html"
    try:
        return template_path.read_text(encoding="utf-8")
    except Exception as e:
        print_log(f"[WARN] 加载 dashboard 模板失败: {e}，使用内联模板")
        # 回退到内联模板
        return _build_dashboard_html_fallback()


def _build_dashboard_html_fallback() -> str:
    """内联模板（当模板文件不可用时使用）"""
    return """<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><title>小说蒸馏监控面板</title></head>
<body><h1>Dashboard unavailable</h1></body>
</html>"""


class DashboardServer(threading.Thread):
    """嵌入式 HTTP 面板服务器（后台线程）"""

    def __init__(self, port=DASHBOARD_PORT):
        super().__init__(daemon=True)
        self.port = port
        self._stop_event = threading.Event()

    def run(self):
        import http.server
        try:
            srv = http.server.HTTPServer(("0.0.0.0", self.port), DashboardHandler)
            print_log(f"[Dashboard] 面板已启动 http://localhost:{self.port}")
            while not self._stop_event.wait(1):
                srv.handle_request()
        except OSError as e:
            print_log(f"[Dashboard] 端口 {self.port} 被占用或无法启动: {e}")
        except Exception as e:
            print_log(f"[Dashboard] 异常退出: {e}")

    def stop(self):
        self._stop_event.set()


def _refresh_dashboard_state(
    *, book_name, genre, stage, idx, total, items, last_update,
    restarts, books_completed, elapsed_total, stall_warn, status
):
    """由 main loop 每次检测后调用，更新全局状态"""
    _dashboard_state["book_name"] = book_name
    _dashboard_state["genre"] = genre
    _dashboard_state["stage"] = stage
    _dashboard_state["index"] = idx
    _dashboard_state["total"] = total
    _dashboard_state["items"] = items
    _dashboard_state["last_update"] = last_update
    _dashboard_state["restarts"] = restarts
    _dashboard_state["books_completed"] = books_completed
    _dashboard_state["elapsed_total"] = elapsed_total
    _dashboard_state["stall_warn"] = stall_warn
    _dashboard_state["status"] = status


def _refresh_dashboard_processed(processed_books):
    """更新已处理书籍列表（由 main loop 在必要时调用）"""
    _dashboard_state["processed_books"] = processed_books


def _refresh_dashboard_queue(queue_preview):
    """更新队列预览"""
    _dashboard_state["queue_preview"] = queue_preview


def _refresh_dashboard_log(log_line):
    """追加一条日志行到面板"""
    from datetime import datetime
    _dashboard_state["log_lines"].append({"t": datetime.now().isoformat(), "m": log_line})
    if len(_dashboard_state["log_lines"]) > 100:
        _dashboard_state["log_lines"] = _dashboard_state["log_lines"][-100:]


def _read_quality_summary():
    """读取最新质量报告，生成飞书摘要"""
    book_dir = find_book_output_dir()
    if not book_dir:
        return ""
    lines = []
    for tier_name in ["tier2_output", "tier3_output"]:
        report_file = book_dir / tier_name / "quality_report.json" if book_dir != Path(PROGRESS_DIR) else None
        if not report_file or not report_file.exists():
            for f in (book_dir if book_dir else Path(PROGRESS_DIR)).rglob("quality_report.json"):
                if tier_name.replace("_output", "") in str(f):
                    report_file = f
                    break
        if report_file and report_file.exists():
            try:
                data = json.loads(report_file.read_text(encoding="utf-8"))
                total = data.get("total_items", "?")
                accepted = data.get("accepted_count", "?")
                repairable = data.get("repairable_count", "?")
                rejected = data.get("rejected_count", "?")
                tier_label = tier_name.split("_")[0].upper()
                lines.append(f"**{tier_label}**: {total} 条（✅{accepted} / 🔧{repairable} / ❌{rejected}）")
            except Exception:
                pass
    return "\n".join(lines) if lines else ""

def archive_results(book_dir):
    """将蒸馏结果归档到专门的结果文件夹"""
    import shutil
    if not book_dir or not book_dir.exists():
        print_log("[WARN] 无法归档：输出目录不存在")
        return None
    # 提取书名（取目录名最后一段）
    book_name = book_dir.name
    # 清理书名：去掉日期后缀、书名号、多余分隔符
    clean_name = re.sub(r'_\d{8}(_\d{6})?$', '', book_name)
    clean_name = clean_name.split("--")[0].strip().strip("\u300a\u300b《》")
    date_str = datetime.now().strftime("%Y%m%d")
    archive_dir = Path(RESULTS_ARCHIVE) / f"{clean_name}_{date_str}"
    archive_dir.mkdir(parents=True, exist_ok=True)
    # 要归档的关键文件
    key_files = {
        "tier2": [
            "quality_report.json", "knowledge_items.json", "tier2_summary.json",
            "validation_results.json", "dedupe_report.json",
            "repairable_items.json", "rejected_items.json",
        ],
        "tier3": [
            "quality_report.json", "knowledge_base.json", "tier3_summary.json",
            "validation_results.json", "repairable_items.json", "rejected_items.json",
        ],
        "tier1": [
            "overview.json", "chapter_stats.json", "high_value_chapters.json",
            "plot_keywords.json", "character_cooccurrence.json",
        ],
    }
    copied = 0
    for tier, files in key_files.items():
        src_dir = book_dir / f"{tier}_output"
        if not src_dir.exists():
            continue
        dest_tier = archive_dir / tier
        dest_tier.mkdir(parents=True, exist_ok=True)
        for fname in files:
            src = src_dir / fname
            if src.exists():
                shutil.copy2(src, dest_tier / fname)
                copied += 1
    # 复制 Tier3 的仓库文件
    tier3_dir = book_dir / "tier3_output"
    if tier3_dir.exists():
        for f in tier3_dir.iterdir():
            if f.suffix == ".json" and "仓" in f.name:
                shutil.copy2(f, archive_dir / "tier3" / f.name)
                copied += 1
    print_log(f"归档完成: {archive_dir} ({copied} 个文件)")
    return archive_dir


def _fmt_duration(seconds: float) -> str:
    """将秒数格式化为 HH:MM:SS"""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    if m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def _verify_and_record_tier3_completion(book_dir, genre, book_file, book_name):
    """检查 tier3 是否实际完成（knowledge_items.json 存在），若 progress 文件未记录则补写。

    防止 orchestrator 在 tier3 写完 knowledge_items.json 后、调用 update_progress() 前崩溃，
    导致 progress 文件无记录， watchdog 重启后误判本书未完成而重新跑。
    """
    if not book_dir or not book_dir.exists():
        return False
    tier3_kb = book_dir / "tier3_output" / "knowledge_items.json"
    if not tier3_kb.exists():
        return False
    # tier3 实际已完成，检查 progress 文件是否已记录
    progress_file = PROGRESS_DIR / "three_tier_progress.json"
    if not progress_file.exists():
        return False
    try:
        data = json.loads(progress_file.read_text(encoding="utf-8"))
        key = (genre, book_file)
        existing = [i for i in data.get("processed_books", [])
                    if (i.get("genre", ""), i.get("book_file", "")) == key]
        if existing and existing[-1].get("success"):
            return True  # 已有成功记录
        # 未记录或失败，补写成功记录
        from datetime import datetime as _dt
        entry = {
            "genre": genre,
            "book_file": book_file,
            "book_name": book_name,
            "success": True,
            "error_message": "",
            "processed_at": _dt.now().isoformat(),
        }
        # 原子写入
        entries = data.get("processed_books", [])
        entries.append(entry)
        data["processed_books"] = entries
        data["last_update"] = _dt.now().isoformat()
        tmp = progress_file.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(progress_file)
        print_log(f"[INFO] tier3 实际已完成，补写 progress 记录: {book_name}")
        return True
    except Exception as e:
        print_log(f"[WARN] 补写 progress 记录失败: {e}")
        return False


def _print_status_board(
    *,
    book_name: str,
    stage: str,
    idx: str,
    total: str,
    items: str,
    age: float,
    restarts: int,
    max_restarts: int,
    books_completed: int,
    elapsed_total: float,
    stall_warn: bool = False,
) -> None:
    """打印一行状态板，固定位置覆盖刷新"""
    bar = "━" * 50
    now = datetime.now().strftime("%H:%M:%S")
    elapsed_str = _fmt_duration(elapsed_total)
    age_int = int(age)
    status_icon = "⚠️ STALL" if stall_warn else "✅ OK"
    stage_label = f"{stage} {idx}/{total}" if stage != "?" else "初始化中..."
    items_str = f"{items} 条" if items != "?" else ""

    lines = [
        "",
        f"  {bar}",
        f"  [{now}] 小说蒸馏监控面板",
        f"  {bar}",
        f"  📖 书名   : {book_name or '（未检测到）'}",
        f"  ⚙️  阶段   : {stage_label}   {items_str}",
        f"  🕐 最后更新: {age_int}s 前",
        f"  ⏱️  总耗时 : {elapsed_str}",
        f"  🔁 重启   : {restarts} / {max_restarts}",
        f"  📚 已完成  : {books_completed} 本",
        f"  状态     : {status_icon}",
        f"  {bar}",
        "",
    ]
    for line in lines:
        print_log(line)


def _send_batch_summary():
    """发送批处理汇总通知"""
    failed_list = "\n".join([f"- {b}" for b in books_failed]) if books_failed else "无"
    content = f"**完成: {books_completed} 本 | 失败: {len(books_failed)} 本**\n失败列表:\n{failed_list}"
    feishu_notify("📊 批处理汇总", content)


def _send_periodic_summary(
    *,
    book_name: str,
    stage: str,
    idx: str,
    total: str,
    items: str,
    age: float,
    restarts: int,
    elapsed_total: float,
    books_completed: int,
    books_failed: List[str],
):
    """发送小时级进度汇总，避免频繁骚扰。"""
    elapsed_str = _fmt_duration(elapsed_total)
    lines = [
        f"**已运行: {elapsed_str}**",
        f"完成: {books_completed} 本 | 失败: {len(books_failed)} 本",
    ]
    if book_name:
        lines.append(f"当前书籍: **{book_name}**")
    if stage == "tier1":
        lines.append("当前阶段: Tier1 章节分析中")
    elif stage:
        stage_text = stage
        if idx not in ("", "?") and total not in ("", "?"):
            stage_text = f"{stage} {idx}/{total}"
        lines.append(f"当前阶段: {stage_text}")
        if items not in ("", "?", None):
            lines.append(f"当前累计条目: {items}")
        lines.append(f"距上次更新: {int(age)}s")
    lines.append(f"本书已重启: {restarts} 次")
    if books_failed:
        recent_failed = "、".join(books_failed[-5:])
        lines.append(f"最近失败: {recent_failed}")
    feishu_notify("📊 每小时蒸馏汇总", "\n".join(lines))


def _send_stuck_progress_summary(
    *,
    book_name: str,
    stage: str,
    idx: str,
    total: str,
    stuck_seconds: float,
    restarts: int,
):
    """同一进度长期未推进时，发送单独提醒。"""
    stage_text = stage or "?"
    if idx not in ("", "?") and total not in ("", "?"):
        stage_text = f"{stage_text} {idx}/{total}"
    lines = [
        f"**{book_name or '当前书籍'}**",
        f"进度已连续卡住: {_fmt_duration(stuck_seconds)}",
        f"当前位置: {stage_text}",
        f"本书已重启: {restarts} 次",
        "watchdog 会继续自动自救重试。",
    ]
    feishu_notify("⏳ 进度卡住 10 分钟", "\n".join(lines))


def main():
    # 清理旧日志（保留最近7天）
    LOG_RETENTION_DAYS = 7
    MAX_LOG_FILES = 200  # 最多保留最近 200 个日志文件
    try:
        cutoff = datetime.now() - timedelta(days=LOG_RETENTION_DAYS)
        log_files = []
        for log_file in LOG_DIR.glob("orchestrator_*.log"):
            try:
                if datetime.fromtimestamp(log_file.stat().st_mtime) < cutoff:
                    log_file.unlink()
                    continue
                log_files.append((log_file.stat().st_mtime, log_file))
            except OSError:
                pass
        # 如果日志文件过多，删除最旧的直到只剩 MAX_LOG_FILES 个
        if len(log_files) > MAX_LOG_FILES:
            log_files.sort(key=lambda x: x[0])
            for _, lf in log_files[:-MAX_LOG_FILES]:
                try:
                    lf.unlink()
                except OSError:
                    pass
        # 清理 orchestrator 子目录日志
        for sub in LOG_DIR.glob("orchestrator_*"):
            if sub.is_dir():
                import shutil
                shutil.rmtree(sub, ignore_errors=True)
    except Exception:
        pass

    print_log("")
    print_log("=== 蒸馏看门狗启动 ===")
    mode_str = "批量扫描" if SCAN_MODE else ("指定书籍" if BOOK_FILTER else "书单模式")
    print_log(f"  模式: {mode_str}")
    if BOOK_FILTER:
        print_log(f"  书籍过滤: {BOOK_FILTER}")
    print_log(f"  LLM backend: {LLM_BACKEND} ({ACTIVE_MODEL or 'default'})")
    print_log(f"  卡死超时: {STALL_TIMEOUT}s | 检查间隔: {CHECK_INTERVAL}s | 最大重启: {MAX_RESTARTS}/本")

    # 磁盘空间检查（防止2000本书占用数TB空间）
    try:
        import shutil as _shutil
        total, used = 0, 0
        for path in [PROGRESS_DIR, Path(os.environ.get("THREE_TIER_SOURCE_BASE", ""))]:
            if path.exists():
                usage = _shutil.disk_usage(path)
                total += usage.total
                used += usage.used
        if total > 0:
            free_gb = (total - used) / (1024**3)
            used_gb = used / (1024**3)
            print_log(f"  磁盘空间: 已用 {used_gb:.1f}GB / 剩余 {free_gb:.1f}GB")
            if free_gb < 50:
                print_log("[WARN] 磁盘剩余空间 < 50GB，请确保有足够空间")
                feishu_notify("⚠️ 磁盘空间不足", f"剩余空间仅 {free_gb:.1f}GB，建议清理后继续")
    except Exception as e:
        print_log(f"  磁盘空间检查失败: {e}")

    # 启动可视化面板
    if USE_DASHBOARD:
        dashboard = DashboardServer(port=DASHBOARD_PORT)
        dashboard.start()
        _dashboard_state["mode"] = mode_str
        _dashboard_state["ollama_model"] = ACTIVE_MODEL or LLM_BACKEND

    restarts = 0
    proc = None
    log_fd = None
    last_report_time = datetime.now()
    stall_notify_sent = False
    last_stage = ""
    books_completed = 0
    first_run = True  # 首次运行标记，用于 --force 逻辑
    run_start_time = datetime.now()  # 总计时
    current_book_filter = BOOK_FILTER or ""  # 当前处理的书的过滤条件
    current_book_name = Path(current_book_filter).stem if current_book_filter else ""
    progress_signature = None
    progress_signature_since = datetime.now()
    stuck_progress_report_sent = False
    session_started = False  # 是否已经在本轮监控循环中启动过 orchestrator
    proc_stale_killed = False  # True 表示进程是被 watchdog 主动杀死的，不是正常退出
    book_restarts: Dict[str, int] = {}  # 每本书的独立重启计数 {book_filter: count}
    books_failed: List[str] = []  # 失败书籍列表（用于汇总通知）
    last_queue_refresh = datetime.now() - timedelta(seconds=300)  # 初始立即刷新
    last_processed_refresh = datetime.now()  # 初始立即刷新

    def _strip_date_suffix(name: str) -> str:
        """去掉书名末尾的日期后缀，如 网游之倒行逆施_20260330 -> 网游之倒行逆施"""
        return re.sub(r'_\d{8}(_\d{6})?$', '', name)

    def _find_next_book_from_scan():
        """扫描源目录，返回下一本未处理的书的 (genre, book_filename)。

        在 SCAN 模式下：跳过已成功和已失败的书（失败的书由 rerun 模式单独重跑）。
        在 rerun 模式下：优先返回历史失败的书。
        """
        skip_rules = load_skip_rules()
        progress_file = PROGRESS_DIR / "three_tier_progress.json"
        processed: set = set()  # {(genre, book_file)}
        failed_books: set = set()  # {(genre, book_file)}
        if progress_file.exists():
            try:
                data = json.loads(progress_file.read_text(encoding="utf-8"))
                for b in data.get("processed_books", []):
                    key = (b.get("genre", ""), b.get("book_file", ""))
                    if b.get("success"):
                        processed.add(key)
                    else:
                        failed_books.add(key)
            except Exception:
                pass

        source_base = Path(os.environ.get("THREE_TIER_SOURCE_BASE",
                                r"D:\03-AI相关\03-books(new)\99-知识库\02-小说书库-分类"))
        if not source_base.exists():
            return None

        is_rerun = os.environ.get("DISTILL_RERUN_FAILED", "").lower() in ("1", "true", "yes")

        for genre_dir in sorted(source_base.iterdir()):
            if not genre_dir.is_dir():
                continue
            genre = genre_dir.name
            # 验证 genre 目录可访问性（跳过名称编码异常的目录）
            try:
                genre_files = sorted(genre_dir.iterdir())
            except Exception as e:
                print_log(f"[WARN] 跳过无法访问的genre目录: {genre_dir.name!r} ({e})")
                continue
            for f in genre_files:
                if f.suffix.lower() not in (".txt", ".epub"):
                    continue
                # 跳过文件名含有 e69cac 的文件（这些文件有无效UTF-8字节序列，
                # 会导致与进度文件的键不匹配，无限重试）
                fn_hex = f.name.encode('utf-8', errors='replace').hex()
                if 'e69cac' in fn_hex:
                    print_log(f"[WARN] 跳过含无效UTF-8字节序列的文件: {f.name!r}")
                    continue
                key = (genre, f.name)
                if is_skipped_book(genre, f.name, skip_rules):
                    continue
                if key in processed:
                    continue
                if key in failed_books:
                    # rerun 模式：优先返回失败的书
                    if is_rerun:
                        return (genre, f.name)
                    # scan 模式：跳过失败的书（不重试，避免无限循环）
                    continue
                # 返回下一本未处理的书
                return (genre, f.name)
        # rerun 模式找不到更多失败的书时，也返回 None（正常结束）
        return None

    def _build_queue_preview(max_items=20):
        """构建接下来要处理的书队列（用于面板展示，最多返回 max_items 本）"""
        skip_rules = load_skip_rules()
        progress_file = PROGRESS_DIR / "three_tier_progress.json"
        processed: set = set()
        failed_books: set = set()
        if progress_file.exists():
            try:
                data = json.loads(progress_file.read_text(encoding="utf-8"))
                for b in data.get("processed_books", []):
                    key = (b.get("genre", ""), b.get("book_file", ""))
                    if b.get("success"):
                        processed.add(key)
                    else:
                        failed_books.add(key)
            except Exception:
                pass
        source_base = Path(os.environ.get("THREE_TIER_SOURCE_BASE",
                                r"D:\03-AI相关\03-books(new)\99-知识库\02-小说书库-分类"))
        if not source_base.exists():
            return []
        result = []
        cur_filter = current_book_filter or current_book_name or ""
        is_rerun = os.environ.get("DISTILL_RERUN_FAILED", "").lower() in ("1", "true", "yes")
        for genre_dir in sorted(source_base.iterdir()):
            if not genre_dir.is_dir():
                continue
            genre = genre_dir.name
            try:
                genre_files = sorted(genre_dir.iterdir())
            except Exception:
                continue
            for f in genre_files:
                if f.suffix.lower() not in (".txt", ".epub"):
                    continue
                if len(result) >= max_items:
                    return result
                key = (genre, f.name)
                if is_skipped_book(genre, f.name, skip_rules):
                    continue
                if key in processed:
                    continue
                if key in failed_books and not is_rerun:
                    continue  # scan 模式跳过历史失败的书
                # 跳过当前正在处理的
                if cur_filter and cur_filter in f.name:
                    continue
                result.append((genre, f.name))
        return result

    # 初始化全局面板的 queue_preview 和 books_total（嵌套函数定义之后执行）
    try:
        _find_next_book_from_scan()  # just to populate initial queue
        source_base = Path(os.environ.get("THREE_TIER_SOURCE_BASE",
                                r"D:\03-AI相关\03-books(new)\99-知识库\02-小说书库-分类"))
        total_books = 0
        if source_base.exists():
            for gd in source_base.iterdir():
                if gd.is_dir():
                    for f in gd.iterdir():
                        if f.suffix.lower() in (".txt", ".epub"):
                            total_books += 1
        _dashboard_state["books_total"] = total_books
    except Exception:
        pass

    # 启动时清理进度文件中含无效UTF-8字节序列的条目（防止已损坏的条目导致无限重试）
    try:
        pf = PROGRESS_DIR / "three_tier_progress.json"
        if pf.exists():
            data = json.loads(pf.read_text(encoding="utf-8"))
            original_count = len(data.get("processed_books", []))
            cleaned = []
            for b in data.get("processed_books", []):
                g = b.get("genre", "")
                bf = b.get("book_file", "")
                g_hex = g.encode("utf-8", errors="replace").hex()
                bf_hex = bf.encode("utf-8", errors="replace").hex()
                # e8af95 是明确无效的UTF-8字节序列
                if "e8af95" in g_hex or "e8af95" in bf_hex:
                    print_log(f"[WARN] 启动清理: 移除损坏条目 genre={g!r} book={bf[:30]!r}")
                    continue
                cleaned.append(b)
            if len(cleaned) < original_count:
                data["processed_books"] = cleaned
                pf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                print_log(f"[WARN] 启动清理: 移除 {original_count - len(cleaned)} 个损坏条目，剩余 {len(cleaned)} 个")
    except Exception as e:
        print_log(f"[WARN] 启动清理失败: {e}")

    while restarts < MAX_RESTARTS:
        # 每 30s 刷新已处理书籍列表（从 progress 文件）
        now = datetime.now()
        if (now - last_processed_refresh).total_seconds() >= 30:
            last_processed_refresh = now
            try:
                pf = PROGRESS_DIR / "three_tier_progress.json"
                if pf.exists():
                    data = json.loads(pf.read_text(encoding="utf-8"))
                    _refresh_dashboard_processed(data.get("processed_books", []))
                    total = _dashboard_state.get("books_total", 0)
                    if not total:
                        _dashboard_state["books_total"] = data.get("total_books", 0) or total
            except Exception:
                pass
            # 队列预览每 5 分钟刷新一次（扫描源目录，避免太频繁）
            if (now - last_queue_refresh).total_seconds() >= 300:
                last_queue_refresh = now
                try:
                    _refresh_dashboard_queue(_build_queue_preview(max_items=30))
                except Exception:
                    pass

        # 崩溃重启后重新定位当前处理的书的目录
        book_dir = find_book_output_dir(current_book_filter or current_book_name)
        if book_dir:
            current_book_name = _strip_date_suffix(book_dir.name)

        # ============================================================
        # 重启检测：session_started==True 表示之前已经启动过进程，
        # proc.poll() != None 表示进程已退出（崩溃或正常完成）
        # proc_stale_killed=True 表示是被 watchdog 主动杀死的（STALL 处理），
        # 此时 exit_code==0 不能视为"正常完成"
        # ============================================================
        if session_started and proc is not None and proc.poll() is not None:
            # 关闭上次的日志文件句柄
            if log_fd:
                try:
                    log_fd.flush()
                    log_fd.close()
                except Exception:
                    pass
                log_fd = None

            exit_code = -1
            if proc and proc.poll() is not None:
                exit_code = proc.poll()
                print_log(f"进程退出 (code={exit_code})")

            if exit_code == 0 and not proc_stale_killed:
                print_log("=== 蒸馏正常完成 ===")
                books_completed += 1
                # 防止 tier3 写完后崩溃导致 progress 未记录：验证并补写
                try:
                    genre_from_dir = book_dir.parent.name if book_dir and book_dir.parent.name != "04-蒸馏结果" else ""
                    _verify_and_record_tier3_completion(
                        book_dir,
                        genre_from_dir,
                        current_book_filter or current_book_name or "",
                        current_book_name,
                    )
                except Exception:
                    pass
                archive_dir = archive_results(book_dir)
                summary = _read_quality_summary()
                archive_line = f"\n📁 归档: `{archive_dir}`" if archive_dir else ""
                if summary:
                    feishu_notify("✅ 蒸馏完成", summary + archive_line)
                else:
                    feishu_notify("✅ 蒸馏完成", f"第 {books_completed} 本完成" + archive_line)
                # Ollama 定期重启（防止内存泄漏）
                if using_ollama() and OLLAMA_RESTART_INTERVAL > 0 and books_completed % OLLAMA_RESTART_INTERVAL == 0:
                    print_log(f"已处理 {books_completed} 本书，重启 Ollama 防止内存泄漏...")
                    kill_stale_processes()
                    if restart_ollama():
                        feishu_notify("🔄 Ollama 重启", f"已处理 {books_completed} 本书，定期重启完成")
                    last_stage = ""

                # ============================================================
                # 队列处理：SCAN_MODE 下自动取下一本
                # ============================================================
                if SCAN_MODE:
                    next_book = _find_next_book_from_scan()
                    if next_book:
                        print_log(f"自动取下一本: {next_book[1]}")
                        current_book_filter = next_book[1]
                        current_book_name = Path(next_book[1]).stem
                        # 新书，重置重启计数
                        book_restarts[current_book_filter] = 0
                        # 立即刷新队列预览
                        try:
                            _refresh_dashboard_queue(_build_queue_preview(max_items=30))
                        except Exception:
                            pass
                        # 关闭旧日志
                        if log_fd:
                            try:
                                log_fd.flush()
                                log_fd.close()
                            except Exception:
                                pass
                            log_fd = None
                        proc, log_fd = run_distillation(
                            resume=False, scan=True,
                            first_run=False,
                            rerun_failed_only=False,
                            book_filter=next_book[1]
                        )
                        session_started = True
                        restarts = 0  # 新书，重置重启计数
                        last_stage = ""
                        time.sleep(CHECK_INTERVAL)
                        continue
                    else:
                        print_log("队列处理完成，所有书籍均已处理")
                        _send_batch_summary()
                        return
                # 非扫描模式（单书或书单模式），完成后退出
                return
            else:
                # 异常退出（非零退出码或被强制杀死）
                book_key = current_book_filter or current_book_name or "unknown"
                book_restarts[book_key] = book_restarts.get(book_key, 0) + 1
                print_log(f"异常退出，准备重启 ({book_restarts[book_key]}/{MAX_RESTARTS}/本)")
                proc_stale_killed = False  # 重置标记
                # 崩溃重启后刷新 book_dir，防止监控旧书目录
                book_dir = find_book_output_dir(current_book_filter or current_book_name)
                if book_dir:
                    current_book_name = _strip_date_suffix(book_dir.name)

                # 检查是否超过每本书最大重启次数
                if book_restarts.get(book_key, 0) >= MAX_RESTARTS:
                    print_log(f"[WARN] 书籍 '{book_key}' 已失败 {MAX_RESTARTS} 次，跳过并记录")
                    books_failed.append(book_key)
                    feishu_notify("❌ 书籍蒸馏失败", f"**{book_key}** 已失败 {MAX_RESTARTS} 次，跳过")
                    if log_fd:
                        try:
                            log_fd.flush()
                            log_fd.close()
                        except Exception:
                            pass
                        log_fd = None
                    # 批量模式下自动取下一本
                    if SCAN_MODE:
                        next_book = _find_next_book_from_scan()
                        if next_book:
                            print_log(f"自动取下一本: {next_book[1]}")
                            current_book_filter = next_book[1]
                            current_book_name = Path(next_book[1]).stem
                            book_dir = None
                            session_started = False
                            restarts = 0  # 重置全局重启计数，保证下一本书有完整的 MAX_RESTARTS 次机会
                            book_restarts[current_book_filter] = 0
                            last_stage = ""  # 重置 stage，防止旧书的卡死状态影响新书
                            time.sleep(3)
                            continue
                        else:
                            _send_batch_summary()
                            return
                    else:
                        _send_batch_summary()
                        return

            # Ollama 定期重启也应该在崩溃重启时生效
            if using_ollama() and OLLAMA_RESTART_INTERVAL > 0 and books_completed > 0 and (books_completed + 1) % OLLAMA_RESTART_INTERVAL == 0:
                print_log(f"重启 Ollama（定期维护，每 {OLLAMA_RESTART_INTERVAL} 本）...")
                kill_stale_processes()
                restart_ollama()

            # 崩溃重启后短暂退避，避免 Ollama 资源未释放就立即重跑
            book_key = current_book_filter or current_book_name or "unknown"
            if using_ollama() and exit_code != 0 and book_restarts.get(book_key, 0) > 1:
                print_log("退避 20s，等待 Ollama 资源释放...")
                time.sleep(20)

            if using_ollama() and book_restarts.get(book_key, 0) > 1:
                print_log("杀掉残留进程...")
                kill_stale_processes()
                print_log("重启 Ollama...")
                if not restart_ollama():
                    print_log("[FATAL] Ollama 重启失败，等待 30s 后重试")
                    time.sleep(30)
                    if not restart_ollama():
                        print_log("[FATAL] Ollama 无法启动，退出")
                        _send_batch_summary()
                        return
            elif using_ollama():
                # 首次启动，检查 Ollama 是否在线
                try:
                    import requests
                    r = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
                    if r.status_code != 200:
                        print_log("[WARN] Ollama 不在线，尝试重启...")
                        restart_ollama()
                except Exception:
                    print_log("[WARN] Ollama 不在线，尝试重启...")
                    restart_ollama()

            br = book_restarts.get(book_key, 0)
            print_log(f"启动蒸馏 ({br}/{MAX_RESTARTS}/本)...")
            skip1 = last_stage in ("tier2", "tier3")
            skip2 = last_stage == "tier3"
            use_scan = SCAN_MODE or BATCH_MODE
            if use_scan and not current_book_filter:
                next_book = _find_next_book_from_scan()
                if next_book:
                    current_book_filter = next_book[1]
                    current_book_name = Path(next_book[1]).stem
                    print_log(f"  Initial batch target: {current_book_filter}")
            use_resume = books_completed > 0 and not skip1
            use_rerun_failed = os.environ.get("DISTILL_RERUN_FAILED", "").lower() in ("1", "true", "yes")
            if skip1:
                print_log(f"  跳过已完成的 Tier1/Tier2（上次卡在 {last_stage}）")
            if use_resume:
                print_log(f"  使用 --resume 跳过已处理的书")
            if use_rerun_failed:
                print_log(f"  使用 --rerun-failed-only 只重跑历史失败的书")
            mode_desc = "批量扫描" if use_scan else ("指定书籍" if BOOK_FILTER else "书单模式")
            feishu_notify("🔄 蒸馏重启", f"**{book_key}** 第 **{br}** 次启动\n模式: {mode_desc}")
            proc, log_fd = run_distillation(
                skip_tier1=skip1, skip_tier2=skip2,
                resume=use_resume, scan=use_scan,
                first_run=first_run,
                rerun_failed_only=use_rerun_failed,
                book_filter=current_book_filter or None
            )
            first_run = False  # 之后都是重启，不是首次
            session_started = True  # 标记已启动，后续 proc.poll()!=None 才是崩溃

        # ============================================================
        # 首次启动：session_started=False 表示本轮监控循环还未启动过 orchestrator
        # ============================================================
        if not session_started:
            # 首次启动：检查 Ollama，然后启动蒸馏
            try:
                if using_ollama(): import requests
                r = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5) if using_ollama() else None
                if r is not None and r.status_code != 200:
                    print_log("[WARN] Ollama 不在线，尝试重启...")
                    restart_ollama()
            except Exception:
                print_log("[WARN] Ollama 不在线，尝试重启...")
                restart_ollama()

            print_log(f"启动蒸馏 (第 {restarts + 1} 次)...")
            skip1 = last_stage in ("tier2", "tier3")
            skip2 = last_stage == "tier3"
            use_scan = SCAN_MODE or BATCH_MODE
            if use_scan and not current_book_filter:
                next_book = _find_next_book_from_scan()
                if next_book:
                    current_book_filter = next_book[1]
                    current_book_name = Path(next_book[1]).stem
                    print_log(f"  Initial batch target: {current_book_filter}")
            use_resume = False  # 首次启动不用 --resume
            use_rerun_failed = os.environ.get("DISTILL_RERUN_FAILED", "").lower() in ("1", "true", "yes")
            if skip1:
                print_log(f"  跳过已完成的 Tier1/Tier2（上次卡在 {last_stage}）")
            if use_rerun_failed:
                print_log(f"  使用 --rerun-failed-only 只重跑历史失败的书")
            mode_desc = "批量扫描" if use_scan else ("指定书籍" if BOOK_FILTER else "书单模式")
            feishu_notify("🔄 蒸馏启动", f"第 **{restarts + 1}** 次启动\n模式: {mode_desc}\n{'跳过 Tier1+Tier2' if skip1 else '从头开始'}")
            proc, log_fd = run_distillation(
                skip_tier1=skip1, skip_tier2=skip2,
                resume=use_resume, scan=use_scan,
                first_run=first_run,
                rerun_failed_only=use_rerun_failed,
                book_filter=current_book_filter or None
            )
            _refresh_dashboard_state(
                book_name=current_book_name,
                genre="",
                stage="?",
                idx="?",
                total="?",
                items="0",
                last_update=datetime.now().isoformat(),
                restarts=restarts,
                books_completed=books_completed,
                elapsed_total=(datetime.now() - run_start_time).total_seconds(),
                stall_warn=False,
                status="启动中",
            )
            first_run = False
            session_started = True
            time.sleep(CHECK_INTERVAL)  # 启动后等待一段时间再检查
            continue

        time.sleep(CHECK_INTERVAL)

        progress_file, progress_time = find_progress_file(book_dir, min_mtime=run_start_time)
        if progress_file and progress_time:
            age = (datetime.now() - progress_time).total_seconds()
            if progress_file.name == "_progress.json":
                try:
                    progress_data = json.loads(progress_file.read_text(encoding="utf-8")) if progress_file.exists() else {}
                except json.JSONDecodeError as exc:
                    print_log(f"[WARN] 进度文件正在写入，跳过本轮读取: {progress_file} ({exc})")
                    continue
                except Exception as exc:
                    print_log(f"[WARN] 读取进度文件失败，跳过本轮读取: {progress_file} ({exc})")
                    continue
                stage = progress_data.get("stage", "?")
                idx = progress_data.get("index", "?")
                total = progress_data.get("total", "?")
                items = progress_data.get("items_so_far", "?")
            else:
                stage = "tier3"
                idx = "?"
                total = "?"
                items = "?"

            if age < STALL_TIMEOUT:
                stall_notify_sent = False

            current_signature = (current_book_name, str(stage), str(idx), str(total))
            if current_signature != progress_signature:
                progress_signature = current_signature
                progress_signature_since = datetime.now()
                stuck_progress_report_sent = False
            stuck_progress_age = (datetime.now() - progress_signature_since).total_seconds()
            if (
                STALL_REPORT_TIMEOUT > 0
                and stuck_progress_age >= STALL_REPORT_TIMEOUT
                and not stuck_progress_report_sent
            ):
                _send_stuck_progress_summary(
                    book_name=current_book_name,
                    stage=str(stage),
                    idx=str(idx),
                    total=str(total),
                    stuck_seconds=stuck_progress_age,
                    restarts=restarts,
                )
                stuck_progress_report_sent = True

            if age > STALL_TIMEOUT:
                if not stall_notify_sent:
                    print_log(f"[STALL] {stage} {idx}/{total} 已 {int(age)}s 无更新，杀进程重启")
                    feishu_notify("⚠️ 蒸馏卡死", f"**{stage}** {idx}/{total} 已 {int(age)}s 无更新\n正在杀进程 + 重启 Ollama...")
                    stall_notify_sent = True
                    last_stage = stage
                kill_stale_processes()
                proc = None
                session_started = False  # 进程已杀，下次循环走启动流程
                proc_stale_killed = True  # 标记是被杀死的，exit_code==0 不代表正常完成
                time.sleep(5)
                # 不 continue：让循环自然回到 crash 检测，会发现 proc 已死并进入重启分支
            else:
                elapsed_total = (datetime.now() - run_start_time).total_seconds()
                _print_status_board(
                    book_name=current_book_name,
                    stage=stage,
                    idx=str(idx),
                    total=str(total),
                    items=str(items),
                    age=age,
                    restarts=restarts,
                    max_restarts=MAX_RESTARTS,
                    books_completed=books_completed,
                    elapsed_total=elapsed_total,
                    stall_warn=False,
                )
                # 更新可视化面板
                _refresh_dashboard_state(
                    book_name=current_book_name,
                    genre=book_dir.parent.name if book_dir and book_dir.parent.name != "04-蒸馏结果" else "",
                    stage=stage,
                    idx=str(idx),
                    total=str(total),
                    items=str(items),
                    last_update=datetime.now().isoformat(),
                    restarts=restarts,
                    books_completed=books_completed,
                    elapsed_total=elapsed_total,
                    stall_warn=False,
                    status="运行中",
                )
                _refresh_dashboard_log(f"阶段: {stage} {idx}/{total} | {items}条 | {int(age)}s前")

                since_last = (datetime.now() - last_report_time).total_seconds()
                if since_last >= REPORT_INTERVAL:
                    last_report_time = datetime.now()
                    _send_periodic_summary(
                        book_name=current_book_name,
                        stage=stage,
                        idx=idx,
                        total=total,
                        items=items,
                        age=age,
                        restarts=restarts,
                        elapsed_total=elapsed_total,
                        books_completed=books_completed,
                        books_failed=books_failed,
                    )
        else:
            elapsed_total = (datetime.now() - run_start_time).total_seconds()
            if proc and proc.poll() is None:
                current_signature = (current_book_name, "tier1", "?", "?")
                if current_signature != progress_signature:
                    progress_signature = current_signature
                    progress_signature_since = datetime.now()
                    stuck_progress_report_sent = False
                stuck_progress_age = (datetime.now() - progress_signature_since).total_seconds()
                if (
                    STALL_REPORT_TIMEOUT > 0
                    and stuck_progress_age >= STALL_REPORT_TIMEOUT
                    and not stuck_progress_report_sent
                ):
                    _send_stuck_progress_summary(
                        book_name=current_book_name,
                        stage="tier1",
                        idx="?",
                        total="?",
                        stuck_seconds=stuck_progress_age,
                        restarts=restarts,
                    )
                    stuck_progress_report_sent = True
                # Tier1 阶段还没写出进度文件，显示等待状态
                _print_status_board(
                    book_name=current_book_name,
                    stage="tier1",
                    idx="?",
                    total="?",
                    items="0",
                    age=elapsed_total,
                    restarts=restarts,
                    max_restarts=MAX_RESTARTS,
                    books_completed=books_completed,
                    elapsed_total=elapsed_total,
                    stall_warn=False,
                )
                _refresh_dashboard_state(
                    book_name=current_book_name,
                    genre=book_dir.parent.name if book_dir and book_dir.parent.name != "04-蒸馏结果" else "",
                    stage="tier1",
                    idx="?", total="?", items="0",
                    last_update=datetime.now().isoformat(),
                    restarts=restarts,
                    books_completed=books_completed,
                    elapsed_total=elapsed_total,
                    stall_warn=False,
                    status="Tier1 分析中...",
                )
                if (datetime.now() - last_report_time).total_seconds() >= REPORT_INTERVAL:
                    last_report_time = datetime.now()
                    _send_periodic_summary(
                        book_name=current_book_name,
                        stage="tier1",
                        idx="?",
                        total="?",
                        items="0",
                        age=elapsed_total,
                        restarts=restarts,
                        elapsed_total=elapsed_total,
                        books_completed=books_completed,
                        books_failed=books_failed,
                    )
            else:
                _print_status_board(
                    book_name=current_book_name,
                    stage="?",
                    idx="?",
                    total="?",
                    items="0",
                    age=elapsed_total,
                    restarts=restarts,
                    max_restarts=MAX_RESTARTS,
                    books_completed=books_completed,
                    elapsed_total=elapsed_total,
                    stall_warn=True,
                )
                _refresh_dashboard_state(
                    book_name=current_book_name,
                    genre="",
                    stage="?", idx="?", total="?", items="0",
                    last_update=datetime.now().isoformat(),
                    restarts=restarts,
                    books_completed=books_completed,
                    elapsed_total=elapsed_total,
                    stall_warn=True,
                    status="⚠️ 进程异常",
                )
                print_log("[WARN] 进程已退出但无进度文件")

    print_log(f"[FATAL] 超过 {MAX_RESTARTS} 次重启，放弃")
    _send_batch_summary()

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print_log(f"[FATAL] watchdog 未捕获异常: {exc}")
        print_log(traceback.format_exc())
        raise

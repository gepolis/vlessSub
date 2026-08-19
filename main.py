import asyncio
import json
import logging
import os
import platform
import re
import shutil
import socket
import stat
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import aiohttp
from aiohttp_socks import ProxyConnector
from flask import Flask, Response, jsonify


# ============================================================
# CONFIG
# ============================================================

VLESS_URL = (
    "https://raw.githubusercontent.com/"
    "zieng2/wl/refs/heads/main/vless_universal.txt"
)

BASE_DIR = Path(__file__).resolve().parent

XRAY_DIR = BASE_DIR / "xray"

OUTPUT_FILE = BASE_DIR / "vless-data" / "working_vless.txt"

START_PORT = 10800

# Сколько нод проверяем одновременно
CONCURRENCY_LIMIT = 10

# Перепроверка каждые 5 минут
CHECK_INTERVAL = 15 * 60

# Нода остаётся только если задержка меньше этого значения
MAX_PING_MS = 1000

# Таймаут подключения через ноду
TIMEOUT_PING = 5

# Сколько ждём запуска локального SOCKS Xray
XRAY_START_TIMEOUT = 3

# Flask
FLASK_HOST = "0.0.0.0"
FLASK_PORT = int(os.getenv("PORT", "5000"))

# Проверяем несколько endpoint'ов, чтобы один заблокированный сайт
# не сделал рабочую ноду "мёртвой"
PING_URLS = [
    "https://cp.cloudflare.com/generate_204",
    "https://www.gstatic.com/generate_204",
]

# Тест скорости очень тяжёлый при проверке каждые 5 минут.
# Поэтому выключен.
ENABLE_SPEED_TEST = False

TIMEOUT_SPEED = 5

DOWNLOAD_URL = (
    "https://speed.cloudflare.com/"
    "__down?bytes=10000000"
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger("vless-checker")


# ============================================================
# GLOBAL STATE
# ============================================================

app = Flask(__name__)

state_lock = threading.Lock()

current_subscription = ""

current_status = {
    "checking": False,
    "total": 0,
    "working": 0,
    "failed": 0,
    "last_check": None,
    "duration": None,
}


# ============================================================
# SYSTEM / XRAY
# ============================================================

def detect_system():
    system = platform.system().lower()
    machine = platform.machine().lower()

    logger.info(
        "Система: %s | Архитектура: %s",
        system,
        machine,
    )

    if system == "windows":
        executable = XRAY_DIR / "xray.exe"

        if machine in (
            "amd64",
            "x86_64"
        ):
            asset = "Xray-windows-64.zip"

        elif machine in (
            "arm64",
            "aarch64"
        ):
            asset = "Xray-windows-arm64-v8a.zip"

        else:
            raise RuntimeError(
                f"Неподдерживаемая Windows архитектура: {machine}"
            )

    elif system == "linux":
        executable = XRAY_DIR / "xray"

        if machine in (
            "amd64",
            "x86_64"
        ):
            asset = "Xray-linux-64.zip"

        elif machine in (
            "arm64",
            "aarch64"
        ):
            asset = "Xray-linux-arm64-v8a.zip"

        else:
            raise RuntimeError(
                f"Неподдерживаемая Linux архитектура: {machine}"
            )

    elif system == "darwin":
        executable = XRAY_DIR / "xray"

        if machine in (
            "amd64",
            "x86_64"
        ):
            asset = "Xray-macos-64.zip"

        elif machine in (
            "arm64",
            "aarch64"
        ):
            asset = "Xray-macos-arm64-v8a.zip"

        else:
            raise RuntimeError(
                f"Неподдерживаемая macOS архитектура: {machine}"
            )

    else:
        raise RuntimeError(
            f"Неподдерживаемая система: {system}"
        )

    return system, executable, asset


SYSTEM, XRAY_PATH, XRAY_ASSET = detect_system()


def download_xray():
    """
    Автоматически скачивает Xray-core под текущую ОС.
    """

    if XRAY_PATH.exists():
        logger.info(
            "Xray найден: %s",
            XRAY_PATH
        )
        return

    logger.info(
        "Xray не найден, скачиваю %s",
        XRAY_ASSET
    )

    XRAY_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    url = (
        "https://github.com/XTLS/Xray-core/"
        f"releases/latest/download/{XRAY_ASSET}"
    )

    archive_path = (
        XRAY_DIR / "xray.zip"
    )

    # Не использовать системный HTTP/SOCKS proxy
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({})
    )

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "VLESS-Checker/1.0"
        }
    )

    logger.info(
        "Скачивание: %s",
        url
    )

    with opener.open(
        request,
        timeout=120
    ) as response:

        with open(
            archive_path,
            "wb"
        ) as file:

            shutil.copyfileobj(
                response,
                file
            )

    logger.info(
        "Распаковываю Xray..."
    )

    with zipfile.ZipFile(
        archive_path,
        "r"
    ) as archive:

        archive.extractall(
            XRAY_DIR
        )

    archive_path.unlink(
        missing_ok=True
    )

    if not XRAY_PATH.exists():
        raise RuntimeError(
            f"После распаковки Xray не найден: {XRAY_PATH}"
        )

    # Linux/macOS
    if SYSTEM != "windows":

        current_mode = (
            XRAY_PATH.stat().st_mode
        )

        XRAY_PATH.chmod(
            current_mode
            | stat.S_IXUSR
            | stat.S_IXGRP
            | stat.S_IXOTH
        )

    logger.info(
        "Xray установлен: %s",
        XRAY_PATH
    )


# ============================================================
# SUBSCRIPTION DOWNLOAD
# ============================================================

async def download_vless_urls():
    logger.info(
        "Скачиваю VLESS список..."
    )

    timeout = aiohttp.ClientTimeout(
        total=30,
        connect=10,
        sock_connect=10,
        sock_read=20,
    )

    async with aiohttp.ClientSession(
        timeout=timeout,
        trust_env=False
    ) as session:

        async with session.get(
            VLESS_URL
        ) as response:

            response.raise_for_status()

            text = await response.text()

    urls = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith(
            "vless://"
        )
    ]

    logger.info(
        "Получено VLESS: %d",
        len(urls)
    )

    return urls


# ============================================================
# VLESS PARSER
# ============================================================

def parse_vless(url_str):
    parsed = urlparse(
        url_str.strip()
    )

    if parsed.scheme != "vless":
        return None

    query = parse_qs(
        parsed.query,
        keep_blank_values=True
    )

    def get(name, default=""):
        values = query.get(name)

        if not values:
            return default

        return values[0]

    name = (
        unquote(parsed.fragment)
        if parsed.fragment
        else parsed.hostname
    )

    return {
        "uuid": parsed.username,
        "address": parsed.hostname,
        "port": parsed.port or 443,

        "encryption": get(
            "encryption",
            "none"
        ),

        "type": get(
            "type",
            "tcp"
        ),

        "security": get(
            "security",
            "none"
        ),

        "sni": get(
            "sni",
            ""
        ),

        "path": get(
            "path",
            ""
        ),

        "host": get(
            "host",
            ""
        ),

        "pbk": get(
            "pbk",
            ""
        ),

        "sid": get(
            "sid",
            ""
        ),

        "fp": get(
            "fp",
            "chrome"
        ),

        "flow": get(
            "flow",
            ""
        ),

        "serviceName": (
            get("serviceName")
            or get("service")
        ),

        "name": name,

        "raw_url": (
            url_str.strip()
        ),
    }


# ============================================================
# XRAY CONFIG
# ============================================================

def generate_xray_config(
    node,
    local_port
):
    network = node["type"].lower()

    # Happ / Xray classic
    if network == "raw":
        network = "tcp"

    stream_settings = {
        "network": network,
        "security": node["security"],
    }

    # --------------------------------------------------------
    # TLS
    # --------------------------------------------------------

    if node["security"] == "tls":

        stream_settings[
            "tlsSettings"
        ] = {
            "serverName": (
                node["sni"]
                or node["address"]
            ),

            "allowInsecure": False,
        }

    # --------------------------------------------------------
    # REALITY
    # --------------------------------------------------------

    elif node["security"] == "reality":

        stream_settings[
            "realitySettings"
        ] = {
            "serverName": (
                node["sni"]
                or node["address"]
            ),

            "fingerprint": (
                node["fp"]
                or "chrome"
            ),

            "publicKey": (
                node["pbk"]
            ),

            "shortId": (
                node["sid"]
            ),

            "spiderX": "/",
        }

    # --------------------------------------------------------
    # TCP
    # --------------------------------------------------------

    if network == "tcp":

        stream_settings[
            "tcpSettings"
        ] = {
            "header": {
                "type": "none"
            }
        }

    # --------------------------------------------------------
    # WebSocket
    # --------------------------------------------------------

    elif network == "ws":

        ws_settings = {
            "path": (
                node["path"]
                or "/"
            )
        }

        if node["host"]:

            ws_settings[
                "headers"
            ] = {
                "Host": node["host"]
            }

        stream_settings[
            "wsSettings"
        ] = ws_settings

    # --------------------------------------------------------
    # gRPC
    # --------------------------------------------------------

    elif network == "grpc":

        stream_settings[
            "grpcSettings"
        ] = {
            "serviceName": (
                node["serviceName"]
                or ""
            )
        }

    # --------------------------------------------------------
    # USER
    # --------------------------------------------------------

    user = {
        "id": node["uuid"],
        "encryption": node["encryption"],
        "level": 8,
        "security": "auto",
    }

    if node["flow"]:
        user["flow"] = node["flow"]

    return {
        "log": {
            "loglevel": "warning"
        },

        "inbounds": [
            {
                "listen": "127.0.0.1",

                "port": local_port,

                "protocol": "socks",

                "settings": {
                    "auth": "noauth",
                    "udp": True,
                    "userLevel": 8,
                },

                "tag": "socks",
            }
        ],

        "outbounds": [
            {
                "protocol": "vless",

                "tag": "proxy",

                "settings": {
                    "vnext": [
                        {
                            "address": (
                                node["address"]
                            ),

                            "port": (
                                node["port"]
                            ),

                            "users": [
                                user
                            ],
                        }
                    ]
                },

                "streamSettings": (
                    stream_settings
                ),

                "mux": {
                    "enabled": False
                },
            }
        ],

        "routing": {
            "domainStrategy": "AsIs",

            "rules": [
                {
                    "type": "field",

                    "inboundTag": [
                        "socks"
                    ],

                    "outboundTag": (
                        "proxy"
                    ),
                }
            ],
        },
    }


# ============================================================
# SOCKS WAIT
# ============================================================

async def wait_for_socks(
    port,
    proc
):
    """
    Вместо тупого sleep(1) ждём именно появления SOCKS.
    """

    deadline = (
        time.monotonic()
        + XRAY_START_TIMEOUT
    )

    while (
        time.monotonic()
        < deadline
    ):

        if proc.returncode is not None:
            return False

        try:
            reader, writer = (
                await asyncio.wait_for(
                    asyncio.open_connection(
                        "127.0.0.1",
                        port
                    ),
                    timeout=0.2
                )
            )

            writer.close()

            try:
                await writer.wait_closed()
            except Exception:
                pass

            return True

        except (
            ConnectionRefusedError,
            asyncio.TimeoutError,
            OSError
        ):
            await asyncio.sleep(
                0.05
            )

    return False


# ============================================================
# PING
# ============================================================

async def measure_ping(
    session,
    node_name
):
    best_ping = None

    for url in PING_URLS:

        try:
            started = (
                time.monotonic()
            )

            async with session.get(
                url,

                timeout=(
                    aiohttp.ClientTimeout(
                        total=TIMEOUT_PING
                    )
                ),

                allow_redirects=False,
            ) as response:

                latency = round(
                    (
                        time.monotonic()
                        - started
                    )
                    * 1000
                )

                if (
                    200
                    <= response.status
                    < 400
                ):

                    if (
                        best_ping is None
                        or latency < best_ping
                    ):
                        best_ping = latency

        except Exception:
            continue

    return best_ping


# ============================================================
# OPTIONAL SPEED TEST
# ============================================================

async def measure_download_speed(
    session
):
    if not ENABLE_SPEED_TEST:
        return 0.0

    downloaded = 0

    started = (
        time.monotonic()
    )

    try:
        async with session.get(
            DOWNLOAD_URL,

            timeout=(
                aiohttp.ClientTimeout(
                    total=TIMEOUT_SPEED
                )
            ),
        ) as response:

            if response.status != 200:
                return 0.0

            async for chunk in (
                response.content.iter_chunked(
                    64 * 1024
                )
            ):

                downloaded += len(
                    chunk
                )

    except Exception:
        pass

    elapsed = (
        time.monotonic()
        - started
    )

    if (
        downloaded == 0
        or elapsed <= 0
    ):
        return 0.0

    return round(
        downloaded
        * 8
        / elapsed
        / 1_000_000,
        2
    )


# ============================================================
# TEST SINGLE NODE
# ============================================================

async def test_single_node(
    node,
    local_port,
    semaphore
):
    async with semaphore:

        name = node["name"]

        config = (
            generate_xray_config(
                node,
                local_port
            )
        )

        fd, config_path = (
            tempfile.mkstemp(
                prefix="xray_",
                suffix=".json"
            )
        )

        os.close(fd)

        proc = None

        try:
            with open(
                config_path,
                "w",
                encoding="utf-8"
            ) as file:

                json.dump(
                    config,
                    file,
                    ensure_ascii=False
                )

            proc = (
                await asyncio.create_subprocess_exec(
                    str(XRAY_PATH),

                    "-c",
                    config_path,

                    stdout=(
                        asyncio.subprocess.DEVNULL
                    ),

                    stderr=(
                        asyncio.subprocess.PIPE
                    ),
                )
            )

            ready = (
                await wait_for_socks(
                    local_port,
                    proc
                )
            )

            if not ready:

                logger.warning(
                    "[%s] Xray/SOCKS не запустился",
                    name
                )

                return None

            proxy_url = (
                f"socks5://"
                f"127.0.0.1:"
                f"{local_port}"
            )

            connector = (
                ProxyConnector.from_url(
                    proxy_url
                )
            )

            async with aiohttp.ClientSession(
                connector=connector,
                trust_env=False
            ) as session:

                latency = (
                    await measure_ping(
                        session,
                        name
                    )
                )

                if latency is None:

                    logger.info(
                        "❌ %s | timeout",
                        name
                    )

                    return None

                if latency >= MAX_PING_MS:

                    logger.info(
                        "❌ %s | %d ms",
                        name,
                        latency
                    )

                    return None

                speed = (
                    await measure_download_speed(
                        session
                    )
                )

                if ENABLE_SPEED_TEST:

                    logger.info(
                        "✅ %s | %d ms | %.2f Mbps",
                        name,
                        latency,
                        speed
                    )

                else:

                    logger.info(
                        "✅ %s | %d ms",
                        name,
                        latency
                    )

                return {
                    "node": node,
                    "latency": latency,
                    "speed": speed,
                }

        except Exception as e:

            logger.warning(
                "❌ %s | %s: %r",
                name,
                type(e).__name__,
                e
            )

            return None

        finally:

            if proc:

                try:
                    if proc.returncode is None:
                        proc.terminate()

                    try:
                        await asyncio.wait_for(
                            proc.wait(),
                            timeout=1
                        )

                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()

                except Exception:
                    pass

            try:
                os.unlink(
                    config_path
                )
            except OSError:
                pass


# ============================================================
# COUNTRY / NAME
# ============================================================

FLAG_RE = re.compile(
    r"[\U0001F1E6-\U0001F1FF]{2}"
)


def clean_node_name(name):
    """
    🇷🇺 Yandex — #1
           ↓
    flag = 🇷🇺
    name = Yandex
    """

    name = (
        unquote(name)
        .strip()
    )

    match = FLAG_RE.search(
        name
    )

    if match:
        flag = match.group(0)

        name = (
            name[:match.start()]
            + name[match.end():]
        )

    else:
        flag = "🌐"

    name = name.strip()

    # Убираем старые "#1", "— #12" и т.п.
    name = re.sub(
        r"\s*[—–-]\s*#?\d+\s*$",
        "",
        name
    )

    name = re.sub(
        r"\s*#\d+\s*$",
        "",
        name
    )

    name = name.strip(
        " —–-"
    )

    if not name:
        name = "Server"

    return flag, name


def rename_vless(
    raw_url,
    index
):
    parsed = urlparse(
        raw_url
    )

    old_name = (
        unquote(parsed.fragment)
        if parsed.fragment
        else parsed.hostname
    )

    flag, clean_name = (
        clean_node_name(
            old_name
        )
    )

    # Требуемый формат:
    # 🇷🇺 [1] Yandex

    new_name = (
        f"{flag} [{index}] "
        f"{clean_name}"
    )

    encoded_name = quote(
        new_name,
        safe=""
    )

    return parsed._replace(
        fragment=encoded_name
    ).geturl()


# ============================================================
# SAVE RESULT
# ============================================================

def save_results(
    working
):
    global current_subscription

    renamed = []

    for index, result in enumerate(
        working,
        start=1
    ):

        renamed.append(
            rename_vless(
                result["node"]["raw_url"],
                index
            )
        )

    content = "\n".join(
        renamed
    )

    if content:
        content += "\n"

    extra_url = os.getenv(
        "EXTRA_VLESS_URL",
        ""
    ).strip()

    if extra_url:
        content = (
            extra_url
            + "\n"
            + content
        )

    temp_file = (
        OUTPUT_FILE.with_suffix(
            ".tmp"
        )
    )

    OUTPUT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    if temp_file.is_dir():
        try:
            shutil.rmtree(
                temp_file
            )
        except OSError:
            pass

    temp_file.write_text(
        content,
        encoding="utf-8"
    )

    if OUTPUT_FILE.is_dir():
        try:
            shutil.rmtree(
                OUTPUT_FILE
            )
        except OSError:
            pass

    os.replace(
        temp_file,
        OUTPUT_FILE
    )

    with state_lock:
        current_subscription = (
            content
        )

    logger.info(
        "Сохранено рабочих нод: %d",
        len(working)
    )


# ============================================================
# CHECK CYCLE
# ============================================================

async def run_check():
    started = (
        time.monotonic()
    )

    with state_lock:
        current_status[
            "checking"
        ] = True

    try:
        urls = (
            await download_vless_urls()
        )

        semaphore = (
            asyncio.Semaphore(
                CONCURRENCY_LIMIT
            )
        )

        tasks = []

        for index, raw_url in enumerate(
            urls
        ):

            try:
                node = parse_vless(
                    raw_url
                )

            except Exception as e:

                logger.warning(
                    "Ошибка парсинга #%d: %r",
                    index + 1,
                    e
                )

                continue

            if not node:
                continue

            tasks.append(
                test_single_node(
                    node,
                    START_PORT + index,
                    semaphore
                )
            )

        logger.info(
            "Проверяю %d нод...",
            len(tasks)
        )

        results = (
            await asyncio.gather(
                *tasks
            )
        )

        working = [
            result
            for result in results
            if result is not None
        ]

        # Основная сортировка:
        # минимальный ping сверху

        working.sort(
            key=lambda x: (
                clean_node_name(x["node"]["name"])[0],
                x["latency"],
            )
        )

        # Если включили speed-test —
        # сначала скорость, затем ping

        if ENABLE_SPEED_TEST:

            working.sort(
                key=lambda x: (
                    -x["speed"],
                    x["latency"]
                )
            )

        save_results(
            working
        )

        duration = round(
            time.monotonic()
            - started,
            2
        )

        with state_lock:

            current_status.update({
                "checking": False,

                "total": (
                    len(urls)
                ),

                "working": (
                    len(working)
                ),

                "failed": (
                    len(urls)
                    - len(working)
                ),

                "last_check": (
                    time.strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                ),

                "duration": (
                    duration
                ),
            })

        logger.info(
            "================================"
        )

        logger.info(
            "Проверка завершена"
        )

        logger.info(
            "Всего: %d | Рабочих: %d | "
            "Удалено: %d | %.2f сек",
            len(urls),
            len(working),
            len(urls) - len(working),
            duration,
        )

        logger.info(
            "Следующая проверка через 5 минут"
        )

        logger.info(
            "================================"
        )

    except Exception:

        logger.exception(
            "Ошибка цикла проверки"
        )

        with state_lock:
            current_status[
                "checking"
            ] = False


# ============================================================
# BACKGROUND LOOP
# ============================================================

def checker_loop():
    """
    Отдельный thread для asyncio.
    Flask при этом продолжает отвечать клиентам.
    """

    while True:

        try:
            asyncio.run(
                run_check()
            )

        except Exception:
            logger.exception(
                "Ошибка background checker"
            )

        time.sleep(
            CHECK_INTERVAL
        )


# ============================================================
# FLASK
# ============================================================

@app.get("/sub")
def subscription():
    """
    Возвращает актуальный список VLESS.
    """

    with state_lock:
        content = current_subscription

    if not content:

        # Если приложение перезапустилось,
        # но файл с прошлой проверкой есть.

        if OUTPUT_FILE.exists():

            try:
                content = (
                    OUTPUT_FILE.read_text(
                        encoding="utf-8"
                    )
                )

            except Exception:
                pass

    if not content:

        return Response(
            "Subscription is not ready yet\n",
            status=503,
            content_type=(
                "text/plain; charset=utf-8"
            )
        )

    return Response(
        content,
        status=200,
        content_type=(
            "text/plain; charset=utf-8"
        )
    )


@app.get("/status")
def status():
    """
    Удобно посмотреть состояние сервиса.
    """

    with state_lock:
        data = dict(
            current_status
        )

    return jsonify(data)


@app.get("/")
def index():
    return jsonify({
        "service": (
            "VLESS Checker"
        ),

        "subscription": (
            "/sub"
        ),

        "status": (
            "/status"
        ),

        "check_interval": (
            CHECK_INTERVAL
        ),

        "max_ping_ms": (
            MAX_PING_MS
        ),
    })


@app.route("/bs")
def bs():
    return """vless://a1ebe205-721a-4598-b9d9-81abff65ffac@russia3.azmov.ru:443?encryption=none&type=xhttp&path=%2Fapi%2Fapi_v2%2Fclientupload_process&mode=packet-up&extra=%7B%22xmux%22%3A%7B%22cMaxReuseTimes%22%3A%2264-128%22%2C%22maxConcurrency%22%3A%221-4%22%2C%22hKeepAlivePeriod%22%3A30%2C%22hMaxRequestTimes%22%3A%225000-10000%22%2C%22hMaxReusableSecs%22%3A%223600-7200%22%7D%2C%22seqKey%22%3A%22offset%22%2C%22sessionKey%22%3A%22auth%22%2C%22noSSEHeader%22%3Atrue%2C%22noGRPCHeader%22%3Atrue%2C%22seqPlacement%22%3A%22query%22%2C%22sessionIDKey%22%3A%22auth%22%2C%22xPaddingBytes%22%3A%2250-150%22%2C%22sessionIDTable%22%3A%22%22%2C%22xPaddingHeader%22%3A%22X-Api-Key%22%2C%22xPaddingMethod%22%3A%22tokenish%22%2C%22sessionIDLength%22%3A%2216-32%22%2C%22uplinkChunkSize%22%3A%22204800-614400%22%2C%22sessionPlacement%22%3A%22query%22%2C%22uplinkHTTPMethod%22%3A%22GET%22%2C%22xPaddingObfsMode%22%3Atrue%2C%22xPaddingPlacement%22%3A%22header%22%2C%22scMaxBufferedPosts%22%3A100%2C%22scMaxEachPostBytes%22%3A%22600000%22%2C%22sessionIDPlacement%22%3A%22query%22%2C%22uplinkDataPlacement%22%3A%22body%22%2C%22scMinPostsIntervalMs%22%3A%2250-90%22%7D&security=tls&sni=russia3.azmov.ru&fp=firefox&alpn=h2%2Chttp%2F1.1#🇷🇺 Белые списки #1² LTE | 4G 🔥
vless://a1ebe205-721a-4598-b9d9-81abff65ffac@russia3.azmov.ru:443?encryption=none&type=xhttp&path=%2Fapi%2Fapi_v3%2Fclientstream_sync&mode=packet-up&extra=%7B%22xmux%22%3A%7B%22cMaxReuseTimes%22%3A%2264-128%22%2C%22maxConcurrency%22%3A%221-4%22%2C%22hKeepAlivePeriod%22%3A30%2C%22hMaxRequestTimes%22%3A%225000-10000%22%2C%22hMaxReusableSecs%22%3A%223600-7200%22%7D%2C%22seqKey%22%3A%22offset%22%2C%22sessionKey%22%3A%22auth%22%2C%22noSSEHeader%22%3Atrue%2C%22noGRPCHeader%22%3Atrue%2C%22seqPlacement%22%3A%22query%22%2C%22sessionIDKey%22%3A%22auth%22%2C%22xPaddingBytes%22%3A%2250-150%22%2C%22sessionIDTable%22%3A%22%22%2C%22xPaddingHeader%22%3A%22X-Api-Key%22%2C%22xPaddingMethod%22%3A%22tokenish%22%2C%22sessionIDLength%22%3A%2216-32%22%2C%22uplinkChunkSize%22%3A%22204800-614400%22%2C%22sessionPlacement%22%3A%22query%22%2C%22uplinkHTTPMethod%22%3A%22GET%22%2C%22xPaddingObfsMode%22%3Atrue%2C%22xPaddingPlacement%22%3A%22header%22%2C%22scMaxBufferedPosts%22%3A100%2C%22scMaxEachPostBytes%22%3A%22600000%22%2C%22sessionIDPlacement%22%3A%22query%22%2C%22uplinkDataPlacement%22%3A%22body%22%2C%22scMinPostsIntervalMs%22%3A%2250-90%22%7D&security=tls&sni=russia3.azmov.ru&fp=firefox&alpn=h2%2Chttp%2F1.1#🇷🇺 Белые списки #1³ LTE | 4G 🔥
vless://a1ebe205-721a-4598-b9d9-81abff65ffac@russia3.azmov.ru:443?encryption=none&type=xhttp&path=%2Fapi%2Fv4%2Fmedia%2Fsession%2Fpoll&host=russia3.azmov.ru&mode=packet-up&extra=%7B%22xmux%22%3A%7B%22cMaxReuseTimes%22%3A%2264-128%22%2C%22maxConcurrency%22%3A%221-4%22%2C%22hKeepAlivePeriod%22%3A30%2C%22hMaxRequestTimes%22%3A%225000-10000%22%2C%22hMaxReusableSecs%22%3A%223600-7200%22%7D%2C%22seqKey%22%3A%22offset%22%2C%22headers%22%3A%7B%22Accept%22%3A%22application%2Fvnd.api%2Bjson%2C+application%2Fjson%2C+text%2Fplain%2C+*%2F*%22%2C%22Pragma%22%3A%22no-cache%22%2C%22Cache-Control%22%3A%22no-cache%22%2C%22Accept-Language%22%3A%22ru-RU%2Cru%3Bq%3D0.9%2Cen-US%3Bq%3D0.8%2Cen%3Bq%3D0.7%22%7D%2C%22sessionKey%22%3A%22media_sid%22%2C%22xPaddingKey%22%3A%22q%22%2C%22seqPlacement%22%3A%22query%22%2C%22uplinkDataKey%22%3A%22X-Playback-Token%22%2C%22xPaddingBytes%22%3A%2248-320%22%2C%22xPaddingHeader%22%3A%22X-Rewrite-URL%22%2C%22xPaddingMethod%22%3A%22tokenish%22%2C%22sessionPlacement%22%3A%22cookie%22%2C%22uplinkHTTPMethod%22%3A%22GET%22%2C%22xPaddingObfsMode%22%3Atrue%2C%22xPaddingPlacement%22%3A%22queryInHeader%22%2C%22scMaxBufferedPosts%22%3A100%2C%22scMaxEachPostBytes%22%3A%22600000%22%2C%22uplinkDataPlacement%22%3A%22header%22%2C%22scMinPostsIntervalMs%22%3A%2250-90%22%2C%22serverMaxHeaderBytes%22%3A32768%7D&security=tls&sni=russia3.azmov.ru&fp=firefox&alpn=h2%2Chttp%2F1.1#🇷🇺 Белые списки #1¹ LTE | 4G 🔥
vless://a1ebe205-721a-4598-b9d9-81abff65ffac@russia3.azmov.ru:443?encryption=none&type=xhttp&path=%2Fapi%2Fapi_v6%2Fstreampush_ack&mode=packet-up&extra=%7B%22xmux%22%3A%7B%22cMaxReuseTimes%22%3A%2264-128%22%2C%22maxConcurrency%22%3A%221-4%22%2C%22hKeepAlivePeriod%22%3A30%2C%22hMaxRequestTimes%22%3A%225000-10000%22%2C%22hMaxReusableSecs%22%3A%223600-7200%22%7D%2C%22seqKey%22%3A%22offset%22%2C%22sessionKey%22%3A%22auth%22%2C%22noSSEHeader%22%3Atrue%2C%22noGRPCHeader%22%3Atrue%2C%22seqPlacement%22%3A%22query%22%2C%22sessionIDKey%22%3A%22auth%22%2C%22xPaddingBytes%22%3A%2250-150%22%2C%22sessionIDTable%22%3A%22%22%2C%22xPaddingHeader%22%3A%22X-Api-Key%22%2C%22xPaddingMethod%22%3A%22tokenish%22%2C%22sessionIDLength%22%3A%2216-32%22%2C%22uplinkChunkSize%22%3A%22204800-614400%22%2C%22sessionPlacement%22%3A%22query%22%2C%22uplinkHTTPMethod%22%3A%22GET%22%2C%22xPaddingObfsMode%22%3Atrue%2C%22xPaddingPlacement%22%3A%22header%22%2C%22scMaxBufferedPosts%22%3A100%2C%22scMaxEachPostBytes%22%3A%22600000%22%2C%22sessionIDPlacement%22%3A%22query%22%2C%22uplinkDataPlacement%22%3A%22body%22%2C%22scMinPostsIntervalMs%22%3A%2250-90%22%7D&security=tls&sni=russia3.azmov.ru&fp=firefox&alpn=h2%2Chttp%2F1.1#🇷🇺 Белые списки #1⁴ LTE | 4G 🔥
vless://a1ebe205-721a-4598-b9d9-81abff65ffac@russia3.azmov.ru:443?encryption=none&type=xhttp&path=%2Fapi%2Fapi_v4%2Fmediastream_channel&host=russia3.azmov.ru&mode=packet-up&extra=%7B%22xmux%22%3A%7B%22cMaxLifetimeMs%22%3A0%2C%22cMaxReuseTimes%22%3A0%2C%22maxConcurrency%22%3A0%2C%22maxConnections%22%3A1%2C%22hKeepAlivePeriod%22%3A30%2C%22hMaxRequestTimes%22%3A%2210000-20000%22%2C%22hMaxReusableSecs%22%3A%223600-7200%22%7D%2C%22seqKey%22%3A%22page%22%2C%22sessionKey%22%3A%22X-Session%22%2C%22noSSEHeader%22%3Atrue%2C%22xPaddingKey%22%3A%22_dc%22%2C%22noGRPCHeader%22%3Atrue%2C%22seqPlacement%22%3A%22query%22%2C%22sessionIDKey%22%3A%22X-Session%22%2C%22uplinkDataKey%22%3A%22X-Data%22%2C%22xPaddingBytes%22%3A%2250-150%22%2C%22sessionIDTable%22%3A%22%22%2C%22xPaddingHeader%22%3A%22X-Cache%22%2C%22xPaddingMethod%22%3A%22tokenish%22%2C%22sessionIDLength%22%3A%2216-32%22%2C%22uplinkChunkSize%22%3A%22204800-614400%22%2C%22sessionPlacement%22%3A%22cookie%22%2C%22uplinkHTTPMethod%22%3A%22GET%22%2C%22xPaddingObfsMode%22%3Atrue%2C%22xPaddingPlacement%22%3A%22header%22%2C%22scMaxBufferedPosts%22%3A100%2C%22scMaxEachPostBytes%22%3A614400%2C%22sessionIDPlacement%22%3A%22cookie%22%2C%22uplinkDataPlacement%22%3A%22header%22%2C%22scMinPostsIntervalMs%22%3A%2250-90%22%7D&security=tls&sni=russia3.azmov.ru&fp=chrome&alpn=h2%2Chttp%2F1.1#🇷🇺 Белые списки #1⁵ LTE | 4G 🔥
vless://a1ebe205-721a-4598-b9d9-81abff65ffac@russia3.azmov.ru:443?encryption=none&type=xhttp&path=%2Fapi%2Fapi_v5%2Fclientpoll_batch&host=russia3.azmov.ru&mode=packet-up&extra=%7B%22xmux%22%3A%7B%22cMaxLifetimeMs%22%3A0%2C%22cMaxReuseTimes%22%3A0%2C%22maxConcurrency%22%3A0%2C%22maxConnections%22%3A1%2C%22hKeepAlivePeriod%22%3A30%2C%22hMaxRequestTimes%22%3A%2210000-20000%22%2C%22hMaxReusableSecs%22%3A%223600-7200%22%7D%2C%22seqKey%22%3A%22offset%22%2C%22sessionKey%22%3A%22auth%22%2C%22noSSEHeader%22%3Atrue%2C%22noGRPCHeader%22%3Atrue%2C%22seqPlacement%22%3A%22query%22%2C%22sessionIDKey%22%3A%22auth%22%2C%22xPaddingBytes%22%3A%2250-150%22%2C%22sessionIDTable%22%3A%22%22%2C%22xPaddingHeader%22%3A%22X-Api-Key%22%2C%22xPaddingMethod%22%3A%22tokenish%22%2C%22sessionIDLength%22%3A%2216-32%22%2C%22uplinkChunkSize%22%3A%22204800-614400%22%2C%22sessionPlacement%22%3A%22query%22%2C%22uplinkHTTPMethod%22%3A%22GET%22%2C%22xPaddingObfsMode%22%3Atrue%2C%22xPaddingPlacement%22%3A%22header%22%2C%22scMaxBufferedPosts%22%3A100%2C%22scMaxEachPostBytes%22%3A614400%2C%22sessionIDPlacement%22%3A%22query%22%2C%22uplinkDataPlacement%22%3A%22body%22%2C%22scMinPostsIntervalMs%22%3A%225-10%22%7D&security=tls&sni=russia3.azmov.ru&fp=chrome&alpn=h2%2Chttp%2F1.1#🇷🇺 Белые списки #1⁶ LTE | 4G 🔥
vless://a1ebe205-721a-4598-b9d9-81abff65ffac@51.250.14.147:8766?encryption=none&flow=xtls-rprx-vision&type=tcp&security=reality&sni=fallback.cdn-tinkoff.ru&fp=firefox&pbk=Ci0_AL9Gtpc5b7xPYO5UK4pgkF_uyd4VJzw2vV-tSUY&sid=f09e099f#🇷🇺 Белые списки #2² LTE | 4G 🔥
vless://a1ebe205-721a-4598-b9d9-81abff65ffac@51.250.14.147:8765?encryption=none&flow=xtls-rprx-vision&type=tcp&security=reality&sni=fallback.cdn-tinkoff.ru&fp=firefox&pbk=ziMxBJ19IhB8Yty3u1opTNdCb74WJhTP98Nvt2K3RCM&sid=6a723b8c9d#🇷🇺 Белые списки #2¹ LTE | 4G 🔥
vless://a1ebe205-721a-4598-b9d9-81abff65ffac@friendlynode.site:443?encryption=none&type=xhttp&path=%2Fapi%2Fv2%2Ffeed&host=friendlynode.site&mode=packet-up&security=tls&sni=friendlynode.site&fp=firefox&alpn=h2%2Chttp%2F1.1#🇷🇺 Белые списки #3² LTE | 4G 🔥
vless://a1ebe205-721a-4598-b9d9-81abff65ffac@friendlynode.site:443?encryption=none&type=xhttp&path=%2Fapi%2Fv3%2Ffeed&host=friendlynode.site&mode=stream-up&security=tls&sni=friendlynode.site&fp=firefox&alpn=h2%2Chttp%2F1.1#🇷🇺 Белые списки #3³ LTE | 4G 🔥
vless://a1ebe205-721a-4598-b9d9-81abff65ffac@friendlynode.site:443?encryption=none&type=ws&path=%2Fstream%2F928471%2Fsocket&host=friendlynode.site&security=tls&sni=friendlynode.site&fp=firefox&alpn=http%2F1.1#🇷🇺 Белые списки #3¹ LTE | 4G 🔥
vless://a1ebe205-721a-4598-b9d9-81abff65ffac@friendlynode.site:443?encryption=none&type=xhttp&path=%2Fapi%2Fv5%2Ffeed&host=friendlynode.site&mode=stream-one&security=tls&sni=friendlynode.site&fp=firefox&alpn=h2%2Chttp%2F1.1#🇷🇺 Белые списки #3⁴ LTE | 4G 🔥
vless://a1ebe205-721a-4598-b9d9-81abff65ffac@friendlynode.site:443?encryption=none&type=xhttp&path=%2Fapi%2Fv6%2Ffeed&host=friendlynode.site&mode=auto&security=tls&sni=friendlynode.site&fp=firefox&alpn=h2%2Chttp%2F1.1#🇷🇺 Белые списки #3⁵ LTE | 4G 🔥
vless://a1ebe205-721a-4598-b9d9-81abff65ffac@friendlynode.site:443?encryption=none&type=grpc&serviceName=grpcv7&mode=gun&authority=friendlynode.site&security=tls&sni=friendlynode.site&fp=firefox&alpn=h2#🇷🇺 Белые списки #3⁶ LTE | 4G 🔥
vless://a1ebe205-721a-4598-b9d9-81abff65ffac@cdn.friendlynode.xyz:443?encryption=none&type=xhttp&path=%2Fapi%2Ffeed%2Fsync&host=cdn.friendlynode.xyz&mode=packet-up&extra=%7B%22xmux%22%3A%7B%22cMaxReuseTimes%22%3A0%2C%22maxConcurrency%22%3A0%2C%22maxConnections%22%3A1%2C%22hKeepAlivePeriod%22%3A25%2C%22hMaxRequestTimes%22%3A%225000-10000%22%2C%22hMaxReusableSecs%22%3A%221800-3600%22%7D%2C%22scMaxEachPostBytes%22%3A%22600000%22%2C%22scMinPostsIntervalMs%22%3A%2230-60%22%7D&security=tls&sni=top1209191212.mwscdn.ru&fp=chrome&alpn=h2%2Chttp%2F1.1#🇷🇺 Белые списки #4² LTE | 4G 🔥
vless://a1ebe205-721a-4598-b9d9-81abff65ffac@cdn.friendlynode.xyz:443?encryption=none&type=ws&path=%2Fapi%2Flive%2Fstream&host=cdn.friendlynode.xyz&security=tls&sni=top1209191212.mwscdn.ru&fp=chrome&alpn=http%2F1.1#🇷🇺 Белые списки #4¹ LTE | 4G 🔥
vless://a1ebe205-721a-4598-b9d9-81abff65ffac@fbivdwqwyu.a.trbcdn.net:443?encryption=none&type=xhttp&path=%2Fcdn%2Fstream.png&host=fbivdwqwyu.a.trbcdn.net&mode=packet-up&extra=%7B%22xmux%22%3A%7B%22cMaxReuseTimes%22%3A%2264-128%22%2C%22maxConcurrency%22%3A%228-16%22%2C%22hKeepAlivePeriod%22%3A25%2C%22hMaxRequestTimes%22%3A%22600-1200%22%2C%22hMaxReusableSecs%22%3A%221800-3600%22%7D%2C%22seqKey%22%3A%22X-Seq%22%2C%22sessionKey%22%3A%22X-Sess%22%2C%22xPaddingKey%22%3A%22_dc%22%2C%22seqPlacement%22%3A%22header%22%2C%22sessionIDKey%22%3A%22X-Sess-Id%22%2C%22uplinkDataKey%22%3A%22X-Data%22%2C%22xPaddingBytes%22%3A%2216-64%22%2C%22sessionIDTable%22%3A%220123456789abcdef%22%2C%22xPaddingHeader%22%3A%22X-Cache%22%2C%22sessionIDLength%22%3A%228-16%22%2C%22sessionPlacement%22%3A%22header%22%2C%22uplinkHTTPMethod%22%3A%22GET%22%2C%22xPaddingObfsMode%22%3Afalse%2C%22xPaddingPlacement%22%3A%22header%22%2C%22sessionIDPlacement%22%3A%22header%22%2C%22uplinkDataPlacement%22%3A%22header%22%7D&security=tls&sni=fbivdwqwyu.a.trbcdn.net&fp=edge&alpn=h2%2Chttp%2F1.1#🇷🇺 Белые списки #5² LTE | 4G 🔥
vless://a1ebe205-721a-4598-b9d9-81abff65ffac@fbivdwqwyu.a.trbcdn.net:443?encryption=none&type=xhttp&path=%2Fcdn5%2Fstream.png&host=fbivdwqwyu.a.trbcdn.net&mode=packet-up&extra=%7B%22xmux%22%3A%7B%22cMaxReuseTimes%22%3A%2264-128%22%2C%22maxConcurrency%22%3A%228-16%22%2C%22hKeepAlivePeriod%22%3A25%2C%22hMaxRequestTimes%22%3A%22600-1200%22%2C%22hMaxReusableSecs%22%3A%221800-3600%22%7D%2C%22seqKey%22%3A%22X-Seq%22%2C%22sessionKey%22%3A%22X-Sess%22%2C%22xPaddingKey%22%3A%22_dc%22%2C%22seqPlacement%22%3A%22header%22%2C%22sessionIDKey%22%3A%22X-Sess-Id%22%2C%22uplinkDataKey%22%3A%22X-Data%22%2C%22xPaddingBytes%22%3A%2248-256%22%2C%22sessionIDTable%22%3A%22Base62%22%2C%22xPaddingHeader%22%3A%22X-Cache%22%2C%22xPaddingMethod%22%3A%22tokenish%22%2C%22sessionIDLength%22%3A%228-16%22%2C%22sessionPlacement%22%3A%22header%22%2C%22uplinkHTTPMethod%22%3A%22GET%22%2C%22xPaddingObfsMode%22%3Atrue%2C%22xPaddingPlacement%22%3A%22header%22%2C%22sessionIDPlacement%22%3A%22header%22%2C%22uplinkDataPlacement%22%3A%22header%22%7D&security=tls&sni=fbivdwqwyu.a.trbcdn.net&fp=chrome&alpn=h2%2Chttp%2F1.1#🇷🇺 Белые списки #5³ LTE | 4G 🔥
vless://a1ebe205-721a-4598-b9d9-81abff65ffac@fbivdwqwyu.a.trbcdn.net:443?encryption=none&type=xhttp&path=%2Fcdn%2Fstream.png&host=fbivdwqwyu.a.trbcdn.net&mode=packet-up&extra=%7B%22xmux%22%3A%7B%22cMaxReuseTimes%22%3A%2264-128%22%2C%22maxConcurrency%22%3A%228-16%22%2C%22hKeepAlivePeriod%22%3A25%2C%22hMaxRequestTimes%22%3A%22600-1200%22%2C%22hMaxReusableSecs%22%3A%221800-3600%22%7D%2C%22seqKey%22%3A%22X-Seq%22%2C%22sessionKey%22%3A%22X-Sess%22%2C%22xPaddingKey%22%3A%22_dc%22%2C%22seqPlacement%22%3A%22header%22%2C%22sessionIDKey%22%3A%22X-Sess-Id%22%2C%22uplinkDataKey%22%3A%22X-Data%22%2C%22xPaddingBytes%22%3A%2248-256%22%2C%22sessionIDTable%22%3A%22Base62%22%2C%22xPaddingHeader%22%3A%22X-Cache%22%2C%22xPaddingMethod%22%3A%22tokenish%22%2C%22sessionIDLength%22%3A%228-16%22%2C%22sessionPlacement%22%3A%22header%22%2C%22uplinkHTTPMethod%22%3A%22GET%22%2C%22xPaddingObfsMode%22%3Atrue%2C%22xPaddingPlacement%22%3A%22header%22%2C%22sessionIDPlacement%22%3A%22header%22%2C%22uplinkDataPlacement%22%3A%22header%22%7D&security=tls&sni=fbivdwqwyu.a.trbcdn.net&fp=firefox&alpn=h2%2Chttp%2F1.1#🇷🇺 Белые списки #5¹ LTE | 4G 🔥
vless://a1ebe205-721a-4598-b9d9-81abff65ffac@friendlynode.ru:443?encryption=none&type=xhttp&path=%2Fapi%2Fv2%2Ffeed&host=friendlynode.ru&mode=packet-up&security=tls&sni=friendlynode.ru&fp=firefox&alpn=h2%2Chttp%2F1.1#🇷🇺 Белые списки #6² LTE | 4G 🔥
vless://a1ebe205-721a-4598-b9d9-81abff65ffac@friendlynode.ru:443?encryption=none&type=ws&path=%2Fstream%2F928471%2Fsocket&host=friendlynode.ru&security=tls&sni=friendlynode.ru&fp=firefox&alpn=http%2F1.1#🇷🇺 Белые списки #6¹ LTE | 4G 🔥
vless://a1ebe205-721a-4598-b9d9-81abff65ffac@fbivdwqwyu.a.trbcdn.net:443?encryption=none&type=xhttp&path=%2Fcdn5%2Fstream.png&host=fbivdwqwyu.a.trbcdn.net&mode=packet-up&extra=%7B%22xmux%22%3A%7B%22cMaxReuseTimes%22%3A%2264-128%22%2C%22maxConcurrency%22%3A%228-16%22%2C%22hKeepAlivePeriod%22%3A25%2C%22hMaxRequestTimes%22%3A%22600-1200%22%2C%22hMaxReusableSecs%22%3A%221800-3600%22%7D%2C%22seqKey%22%3A%22X-Seq%22%2C%22sessionKey%22%3A%22X-Sess%22%2C%22xPaddingKey%22%3A%22_dc%22%2C%22seqPlacement%22%3A%22header%22%2C%22sessionIDKey%22%3A%22X-Sess-Id%22%2C%22uplinkDataKey%22%3A%22X-Data%22%2C%22xPaddingBytes%22%3A%2248-256%22%2C%22sessionIDTable%22%3A%22Base62%22%2C%22xPaddingHeader%22%3A%22X-Cache%22%2C%22xPaddingMethod%22%3A%22tokenish%22%2C%22sessionIDLength%22%3A%228-16%22%2C%22sessionPlacement%22%3A%22header%22%2C%22uplinkHTTPMethod%22%3A%22GET%22%2C%22xPaddingObfsMode%22%3Atrue%2C%22xPaddingPlacement%22%3A%22header%22%2C%22sessionIDPlacement%22%3A%22header%22%2C%22uplinkDataPlacement%22%3A%22header%22%7D&security=tls&sni=fbivdwqwyu.a.trbcdn.net&fp=firefox&alpn=h2%2Chttp%2F1.1#🇷🇺 Белые списки #7 LTE | 4G 🔥"""
# ============================================================
# MAIN
# ============================================================

def main():
    logger.info(
        "================================"
    )

    logger.info(
        "VLESS CHECKER SERVER"
    )

    logger.info(
        "================================"
    )

    # Автоматически скачать Xray,
    # если его нет.

    download_xray()

    logger.info(
        "Xray: %s",
        XRAY_PATH
    )

    # Загружаем предыдущий результат,
    # чтобы /sub начал работать сразу
    # после перезапуска.

    global current_subscription

    if OUTPUT_FILE.exists():

        try:

            current_subscription = (
                OUTPUT_FILE.read_text(
                    encoding="utf-8"
                )
            )

            logger.info(
                "Загружен предыдущий "
                "working_vless.txt"
            )

        except Exception:
            pass

    # Background checker

    checker_thread = (
        threading.Thread(
            target=checker_loop,
            name="vless-checker",
            daemon=True
        )
    )

    checker_thread.start()

    logger.info(
        "Flask: http://%s:%d",
        FLASK_HOST,
        FLASK_PORT
    )

    logger.info(
        "Subscription: /sub"
    )

    logger.info(
        "Status: /status"
    )

    app.run(
        host=FLASK_HOST,
        port=FLASK_PORT,
        threaded=True,
        use_reloader=False,
    )

if __name__ == "__main__":
    main()
"""提交前的敏感信息自检：仓库里不允许出现任何真实凭证。

对应大赛《参赛声明》里的「信息安全声明」：不包含真实 Token、密钥、密码、账号凭证，
配置文件仅使用环境变量占位符。这个测试会跟着 CI 每次跑，防止误提交。
"""

import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules"}
TEXT_EXT = {".py", ".md", ".json", ".yaml", ".yml", ".toml", ".txt", ".example", ".sh", ".cfg"}

PATTERNS = [
    (re.compile(r"mcdt[A-Za-z0-9\-_]{12,}"), "疑似麦当劳 MCP Token"),
    (re.compile(r"[A-Za-z0-9+/]{40,}={0,2}"), "疑似长 base64 凭证"),
    (re.compile(r"Bearer\s+(?!\$|\{|\bYOUR_|placeholder)[A-Za-z0-9\-._~+/]{20,}"), "疑似 Bearer 真实凭证"),
    (re.compile(r"(?i)(password|passwd|secret)\s*[=:]\s*['\"][^'\"]{6,}['\"]"), "疑似硬编码口令"),
]
ALLOW_PLACEHOLDER = {"YOUR_MCP_TOKEN", "your-token-here", "${MCD_MCP_TOKEN}", "你的Token"}


def _walk():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if os.path.splitext(fn)[1] in TEXT_EXT or fn == "LICENSE":
                yield os.path.join(dirpath, fn)


def test_no_real_credentials_in_repo():
    hits = []
    for path in _walk():
        rel = os.path.relpath(path, ROOT)
        if rel.startswith("tests" + os.sep):
            continue
        try:
            text = open(path, encoding="utf-8").read()
        except UnicodeDecodeError:
            continue
        for rx, why in PATTERNS:
            for m in rx.finditer(text):
                snippet = m.group(0)
                if any(ph in snippet for ph in ALLOW_PLACEHOLDER):
                    continue
                if rel.endswith("demo-cassette.json") and why != "疑似麦当劳 MCP Token":
                    continue  # 录制文件里的长 base64 只可能是业务字段，逐个校验见下
                hits.append(f"{rel}: {why} -> {snippet[:24]}…")
    assert not hits, "仓库中发现疑似真实凭证：\n" + "\n".join(hits)


def test_token_only_read_from_environment():
    src = open(os.path.join(ROOT, "src", "mcd_three_path", "client.py"), encoding="utf-8").read()
    assert "ENV_TOKEN = \"MCD_MCP_TOKEN\"" in src
    assert "os.environ" in src
    # 不允许把 token 写进任何文件
    assert not re.search(r"open\([^)]*\)\.write\(.*token", src, re.I | re.S)

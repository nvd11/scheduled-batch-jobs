#!/usr/bin/env python3
"""
cctv-pruner — 清理夸克网盘上的监控录像目录，只保留最近 N 个日历日。

设计要点（详见 docs/JOB-cctv-prune.md）：
  1. 只用标准库，镜像小、构建快、无 pip 依赖。
  2. 判定依据是「目录名里的日期」，绝不用 mtime（mtime 是上传时间，会有补传）。
  3. 删除走 AList 的 /api/fs/remove，一次调用删多个目录，天然幂等。
  4. 任何异常都要打 [ALERT]（workflow 会 grep 到并判失败）。

退出码：0 = 成功（含 dry-run），1 = 失败
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

# 中国无夏令时，固定 +08:00，避免依赖容器里的 tzdata
CST = timezone(timedelta(hours=8))
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

EXPECTED_PATH = "/Quark/CCTV_Records/锦绣世家_客厅"


# ---------------------------------------------------------------- 纯逻辑（可单测）


@dataclass
class Settings:
    alist_url: str
    username: str
    password: str
    target: str
    retention: int
    max_dirs: int
    dry_run: bool
    today: date


def compute_cutoff(today: date, retention: int) -> str:
    """保留 today-(retention-1) … today 共 retention 个日历日。"""
    return (today - timedelta(days=retention - 1)).isoformat()


def is_date_name(name: str) -> bool:
    return bool(DATE_RE.match(name or ""))


def dated_dirs(entries) -> list:
    """从 AList 的目录列表里筛出符合 YYYY-MM-DD 的目录名（升序）。"""
    return sorted(e["name"] for e in entries if e.get("is_dir") and is_date_name(e.get("name")))


def pick_victims(dated: list, cutoff: str, today_s: str) -> list:
    """删除集：日期 < cutoff。守卫：今天永不入选。"""
    victims = [d for d in dated if d < cutoff]
    return [] if today_s in victims else victims


def target_ok(target: str, expected: str = EXPECTED_PATH) -> bool:
    return (target or "").rstrip("/") == expected


def human_gb(n: int) -> str:
    return f"{n / 1024 / 1024 / 1024:.2f} GB"


def alert(msg: str) -> None:
    """业务级失败标记：workflow 会在日志里 grep [ALERT] 并判失败。"""
    print(f"[ALERT] {msg}", flush=True)


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- AList 客户端


class AList:
    def __init__(self, base: str, username: str, password: str):
        self.base = base.rstrip("/")
        self.username = username
        self.password = password
        self.token = ""

    def _post(self, path: str, payload: dict, auth: bool = True) -> dict:
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if auth:
            req.add_header("Authorization", self.token)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            return {"code": e.code, "message": f"HTTP {e.code}: {body}"}
        except Exception as e:  # noqa: BLE001
            return {"code": -1, "message": f"{type(e).__name__}: {e}"}

    def login(self) -> bool:
        r = self._post("/api/auth/login", {"username": self.username, "password": self.password}, auth=False)
        data = r.get("data")
        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            log(f"❌ 登录失败详情: code={r.get('code')} message={r.get('message')}")
            return False
        self.token = token
        log(f"✅ AList 登录成功（用户 {self.username}）")
        return True

    def list_dir(self, path: str, per_page: int = 500) -> list:
        """列目录。先试 refresh=true（绕缓存），无权限则降级并告警提示。"""
        for refresh in (True, False):
            r = self._post(
                "/api/fs/list",
                {"path": path, "password": "", "page": 1, "per_page": per_page, "refresh": refresh},
            )
            if r.get("code") == 200:
                if not refresh:
                    log("⚠️ refresh=true 被拒（权限不足），已降级为读取缓存列表——结论可能滞后一轮")
                return (r.get("data") or {}).get("content") or []
            msg = str(r.get("message", ""))
            if refresh and ("Refresh without permission" in msg or r.get("code") == 403):
                continue  # 降级重试
            if "31001" in msg or "require login" in msg:
                alert(f"AList 拒绝访问，可能是夸克 Cookie 失效: {msg}")
            else:
                alert(f"列目录失败 {path}: {r.get('code')} {msg}")
            raise RuntimeError("list failed")
        return []

    def remove(self, parent: str, names: list) -> dict:
        return self._post("/api/fs/remove", {"dir": parent, "names": names})


# ---------------------------------------------------------------- 主流程


def execute(api, s: Settings) -> int:
    """返回退出码。api 只需实现 login/list_dir/remove（便于测试注入）。"""
    log("=" * 72)
    log(f"cctv-pruner  target={s.target}")
    log(f"             today={s.today}  retention={s.retention}d  dry_run={s.dry_run}  max_dirs={s.max_dirs}")
    log("=" * 72)

    # 守卫 1：作用域必须精确匹配
    if not target_ok(s.target):
        alert(f"TARGET_PATH 与预期不符：{s.target!r} != {EXPECTED_PATH!r}")
        return 1

    if not api.login():
        alert("AList 登录失败，请检查 ALIST_USERNAME / ALIST_PASSWORD")
        return 1

    # 守卫 2：目录结构合理性
    try:
        entries = api.list_dir(s.target)
    except RuntimeError:
        return 1
    dated = dated_dirs(entries)
    log(f"发现 {len(dated)} 个符合 YYYY-MM-DD 的目录")
    if not dated:
        alert(f"{s.target} 下没有任何日期目录，挂错盘或权限异常")
        return 1
    if len(dated) > 90:
        alert(f"日期目录数量异常（{len(dated)} > 90），疑似路径错误，中止")
        return 1

    # 计算删除集
    cutoff = compute_cutoff(s.today, s.retention)
    today_s = s.today.isoformat()
    victims = pick_victims(dated, cutoff, today_s)
    if today_s in [d for d in dated if d < cutoff]:  # 理论上不可能，双保险
        alert("内部错误：今天的目录落进了删除集，中止")
        return 1

    log(f"cutoff={cutoff}（保留 {cutoff} … {today_s}）  待删 {len(victims)} 天")
    if not victims:
        log("✅ 无需清理")
        return 0

    # 守卫 4：批量上限
    if len(victims) > s.max_dirs:
        alert(f"待删目录数 {len(victims)} 超过上限 {s.max_dirs}，中止（防止跑飞）")
        return 1

    # 统计
    rows = []
    for d in victims:
        try:
            children = api.list_dir(f"{s.target}/{d}")
        except RuntimeError:
            return 1
        files = [c for c in children if not c.get("is_dir")]
        rows.append((d, len(files), sum(int(c.get("size") or 0) for c in files)))

    log("")
    log(f"{'日期':<12} {'文件数':>6}  {'大小':>10}")
    log("-" * 34)
    for d, n, size in rows:
        log(f"{d:<12} {n:>6}  {human_gb(size):>10}")
    log("-" * 34)
    log(f"{'合计':<12} {sum(r[1] for r in rows):>6}  {human_gb(sum(r[2] for r in rows)):>10}"
        f"    （{len(rows)} 天）")

    if s.dry_run:
        log("")
        log("[DRY_RUN] 以上目录将被删除（本次未做任何改动）")
        return 0

    # 执行删除：一次调用
    log("")
    log(f"🗑  删除 {len(victims)} 个目录 …")
    r = api.remove(s.target, victims)
    if r.get("code") != 200:
        alert(f"删除失败: {r.get('code')} {r.get('message')}")
        return 1
    log("✅ 删除接口返回 success")

    # 回查断言
    try:
        after = {e["name"] for e in api.list_dir(s.target) if e.get("is_dir")}
    except RuntimeError:
        return 1
    remain = [d for d in victims if d in after]
    if remain:
        alert(f"删除后仍有目录残留（{len(remain)} 个）：{', '.join(remain[:5])}")
        return 1

    still = sorted(d for d in after if is_date_name(d))
    log(f"✅ 回查通过：剩余 {len(still)} 天（{still[0] if still else '-'} … {still[-1] if still else '-'}）")
    return 0


# ---------------------------------------------------------------- CLI


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_int(name: str, default: int) -> int:
    raw = env(name, str(default))
    try:
        return int(raw)
    except ValueError:
        alert(f"环境变量 {name} 不是整数: {raw!r}")
        sys.exit(1)


def load_settings() -> Settings:
    forced = env("FORCE_NOW")  # 便于测试注入固定日期，生产留空
    today = datetime.strptime(forced, "%Y-%m-%d").date() if forced else datetime.now(CST).date()
    return Settings(
        alist_url=env("ALIST_URL", "http://10.0.1.227:5244"),
        username=env("ALIST_USERNAME", "cctv-pruner"),
        password=env("ALIST_PASSWORD"),
        target=env("TARGET_PATH", EXPECTED_PATH),
        retention=env_int("RETENTION_DAYS", 7),
        max_dirs=env_int("MAX_DELETE_DIRS", 40),
        dry_run=env("DRY_RUN", "true").lower() != "false",
        today=today,
    )


def main() -> int:
    s = load_settings()
    if not s.password:
        alert("缺少 ALIST_PASSWORD")
        return 1
    api = AList(s.alist_url, s.username, s.password)
    return execute(api, s)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        alert(f"未捕获异常: {type(e).__name__}: {e}")
        sys.exit(1)

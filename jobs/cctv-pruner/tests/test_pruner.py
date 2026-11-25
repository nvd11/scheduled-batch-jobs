#!/usr/bin/env python3
"""cctv-pruner 单元测试（仅标准库 unittest，无网络）。

运行：
    cd jobs/cctv-pruner && python3 -m unittest discover -s tests -v
"""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pruner  # noqa: E402


# ---------------------------------------------------------------- 测试替身


class FakeAList:
    """内存版 AList：dirs = {父路径: [条目]}，remove 会真的从内存里删掉。"""

    def __init__(self, tree: dict, login_ok: bool = True, remove_code: int = 200, drop_on_remove: bool = True):
        self.tree = {k: [dict(e) for e in v] for k, v in tree.items()}
        self.login_ok = login_ok
        self.remove_code = remove_code
        self.drop_on_remove = drop_on_remove
        self.remove_calls = []
        self.list_calls = []

    def login(self):
        return self.login_ok

    def list_dir(self, path, per_page=500):
        self.list_calls.append(path)
        return self.tree.get(path, [])

    def remove(self, parent, names):
        self.remove_calls.append((parent, list(names)))
        if self.remove_code == 200 and self.drop_on_remove:
            self.tree[parent] = [e for e in self.tree.get(parent, []) if e["name"] not in names]
        return {"code": self.remove_code, "message": "success" if self.remove_code == 200 else "boom"}


def d(name, is_dir=True):
    return {"name": name, "is_dir": is_dir, "size": 0}


def f(name, size=1024):
    return {"name": name, "is_dir": False, "size": size}


TARGET = pruner.EXPECTED_PATH


def make_settings(**kw):
    base = dict(
        alist_url="http://x",
        username="u",
        password="p",
        target=TARGET,
        retention=7,
        max_dirs=40,
        dry_run=True,
        today=date(2026, 11, 25),
    )
    base.update(kw)
    return pruner.Settings(**base)


def run_flow(api, settings):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = pruner.execute(api, settings)
    return code, buf.getvalue()


# ---------------------------------------------------------------- 纯逻辑


class TestPureLogic(unittest.TestCase):
    def test_cutoff(self):
        day = date(2026, 11, 25)
        self.assertEqual(pruner.compute_cutoff(day, 7), "2026-11-19")
        self.assertEqual(pruner.compute_cutoff(day, 1), "2026-11-25")   # 只留今天
        self.assertEqual(pruner.compute_cutoff(day, 14), "2026-11-12")

    def test_cutoff_crosses_month_and_year(self):
        self.assertEqual(pruner.compute_cutoff(date(2026, 1, 3), 7), "2025-12-28")

    def test_is_date_name(self):
        for good in ("2026-11-25", "1999-01-01"):
            self.assertTrue(pruner.is_date_name(good), good)
        for bad in ("2026-11-5", "20261125", "2026-11-25-1", "abc", "", "2026-13-01x"):
            self.assertFalse(pruner.is_date_name(bad), bad)

    def test_dated_dirs_filters_and_sorts(self):
        entries = [d("2026-11-20"), d("2026-11-18"), d("junk"), d("2026-11-19"), f("cctv_1.mp4")]
        self.assertEqual(pruner.dated_dirs(entries), ["2026-11-18", "2026-11-19", "2026-11-20"])

    def test_dated_dirs_ignores_files(self):
        self.assertEqual(pruner.dated_dirs([{"name": "2026-11-20", "is_dir": False}]), [])

    def test_pick_victims(self):
        dated = ["2026-11-17", "2026-11-18", "2026-11-19", "2026-11-25"]
        self.assertEqual(pruner.pick_victims(dated, "2026-11-19", "2026-11-25"),
                         ["2026-11-17", "2026-11-18"])

    def test_pick_victims_never_today(self):
        # 极端配置：retention=0 时 cutoff 会大于今天，今天必须被排除
        self.assertEqual(pruner.pick_victims(["2026-11-25"], "2026-11-26", "2026-11-25"), [])

    def test_target_ok(self):
        self.assertTrue(pruner.target_ok(TARGET))
        self.assertTrue(pruner.target_ok(TARGET + "/"))
        self.assertFalse(pruner.target_ok("/Quark/CCTV_Records"))
        self.assertFalse(pruner.target_ok(TARGET + "_x"))

    def test_human_gb(self):
        self.assertEqual(pruner.human_gb(1024 ** 3), "1.00 GB")
        self.assertEqual(pruner.human_gb(0), "0.00 GB")


# ---------------------------------------------------------------- 主流程


class TestExecute(unittest.TestCase):
    def tree(self, days, files_per_day=2):
        t = {TARGET: [d(x) for x in days]}
        for x in days:
            t[f"{TARGET}/{x}"] = [f(f"cctv_{x}_{i}.mp4", 1024 ** 3) for i in range(files_per_day)]
        return t

    def test_dry_run_does_not_delete(self):
        days = ["2026-11-17", "2026-11-18", "2026-11-19", "2026-11-25"]
        api = FakeAList(self.tree(days))
        code, out = run_flow(api, make_settings(dry_run=True))
        self.assertEqual(code, 0)
        self.assertEqual(api.remove_calls, [])
        self.assertIn("[DRY_RUN]", out)
        self.assertIn("2026-11-17", out)

    def test_real_run_deletes_all_victims_in_one_call(self):
        days = ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-25"]
        api = FakeAList(self.tree(days))
        code, out = run_flow(api, make_settings(dry_run=False))
        self.assertEqual(code, 0, out)
        self.assertEqual(len(api.remove_calls), 1, "应只调用一次 remove")
        parent, names = api.remove_calls[0]
        self.assertEqual(parent, TARGET)
        self.assertEqual(names, ["2026-11-16", "2026-11-17", "2026-11-18"])
        self.assertIn("回查通过", out)

    def test_nothing_to_delete(self):
        api = FakeAList(self.tree(["2026-11-24", "2026-11-25"]))
        code, out = run_flow(api, make_settings(dry_run=False))
        self.assertEqual(code, 0)
        self.assertEqual(api.remove_calls, [])
        self.assertIn("无需清理", out)

    def test_retention_14(self):
        days = ["2026-11-11", "2026-11-12", "2026-11-13", "2026-11-25"]
        api = FakeAList(self.tree(days))
        code, _ = run_flow(api, make_settings(dry_run=False, retention=14))
        self.assertEqual(code, 0)
        self.assertEqual(api.remove_calls[0][1], ["2026-11-11"])

    def test_guard_max_dirs(self):
        days = [f"2026-11-{i:02d}" for i in range(1, 6)] + ["2026-11-25"]
        api = FakeAList(self.tree(days))
        code, out = run_flow(api, make_settings(dry_run=False, max_dirs=2))
        self.assertEqual(code, 1)
        self.assertEqual(api.remove_calls, [], "超限时绝不能删")
        self.assertIn("[ALERT]", out)

    def test_guard_wrong_target(self):
        api = FakeAList(self.tree(["2026-11-17"]))
        code, out = run_flow(api, make_settings(target="/Quark/movie", dry_run=False))
        self.assertEqual(code, 1)
        self.assertIn("[ALERT]", out)
        self.assertEqual(api.list_calls, [], "路径不对时不该碰任何目录")

    def test_guard_no_date_dirs(self):
        api = FakeAList({TARGET: [d("random"), f("x.mp4")]})
        code, out = run_flow(api, make_settings(dry_run=False))
        self.assertEqual(code, 1)
        self.assertIn("[ALERT]", out)

    def test_guard_too_many_dirs(self):
        days = [f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}" for i in range(95)]
        api = FakeAList({TARGET: [d(x) for x in days]})
        code, out = run_flow(api, make_settings(dry_run=False))
        self.assertEqual(code, 1)
        self.assertIn("数量异常", out)

    def test_login_failure(self):
        api = FakeAList({}, login_ok=False)
        code, out = run_flow(api, make_settings(dry_run=False))
        self.assertEqual(code, 1)
        self.assertIn("[ALERT]", out)

    def test_remove_api_error(self):
        api = FakeAList(self.tree(["2026-11-17", "2026-11-25"]), remove_code=500)
        code, out = run_flow(api, make_settings(dry_run=False))
        self.assertEqual(code, 1)
        self.assertIn("删除失败", out)

    def test_residual_after_delete(self):
        api = FakeAList(self.tree(["2026-11-17", "2026-11-25"]), drop_on_remove=False)
        code, out = run_flow(api, make_settings(dry_run=False))
        self.assertEqual(code, 1)
        self.assertIn("残留", out)

    def test_list_failure_alert(self):
        class Broken(FakeAList):
            def list_dir(self, path, per_page=500):
                pruner.alert("模拟列目录失败")
                raise RuntimeError("list failed")

        code, out = run_flow(Broken({}), make_settings(dry_run=False))
        self.assertEqual(code, 1)
        self.assertIn("[ALERT]", out)


if __name__ == "__main__":
    unittest.main()

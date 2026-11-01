# -*- coding: utf-8 -*-
"""GUI 无头冒烟测试: 构建界面 -> 用真实镜像跑 _job_scan -> 检查结果行。"""
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tkinter as tk

import udf_recover_gui as gui

IMG = r"C:\Users\OhtoAi\.zcode\workspace\draft-f519c2ec-0514-4dcf-bb61-9ca96cc08901\disc.img"


def pump(root, sec):
    """泵事件循环 sec 秒(让 after 轮询和队列事件得到处理)。"""
    end = time.time() + sec
    while time.time() < end:
        root.update()
        time.sleep(0.01)

root = tk.Tk()
root.withdraw()
app = gui.App(root)
root.update()

# 1) 界面构建成功
print("UI 构建成功, 控件数:", len(app.root.winfo_children()))

# 2) 数据源解析
app.var_mode.set("image")
app.var_image.set(IMG)
ns = app._make_ns()
print("数据源:", ns.image)

# 3) 同步跑一遍真实扫描任务(和点按钮走的同一段代码)
app._job_scan(ns)
pump(root, 1.0)

# 4) 校验结果
print("扫描结果行数:", len(app.rows))
assert len(app.rows) >= 2, "应有至少两个视频条目"
vids = [r for r in app.rows if any(n.endswith(".mp4") for n in r["names"])]
for r in vids:
    print("  [%s] %s  %s  FE@%d" % (r["state"], ",".join(r["names"]),
                                    gui.core.human(r["fe"]["info_len"]), r["lba"]))
assert any("5f5af538" in n for r in vids for n in r["names"]), "视频1 未找到"
assert any("b7220fe3" in n for r in vids for n in r["names"]), "视频2 未找到"

# 5) 表格里也填上了
print("表格行数:", len(app.tree.get_children()))
assert len(app.tree.get_children()) == len(app.rows)

# 6) 恢复逻辑走一遍(恢复所选 -> 两个视频), 输出到临时目录
out = os.path.join(os.environ["TEMP"], "gui_smoke_recover")
app.tree.selection_set([str(i) for i, r in enumerate(app.rows)
                        if any(n.endswith(".mp4") for n in r["names"])])
app._job_recover([r for r in app.rows
                  if any(n.endswith(".mp4") for n in r["names"])], out)
pump(root, 0.5)
files = sorted(os.listdir(out))
print("恢复产物:", files)
sizes = {f: os.path.getsize(os.path.join(out, f)) for f in files}
for f, s in sizes.items():
    if f.endswith(".mp4"):
        assert s in (62151610, 115077158), (f, s)
        print("  %s = %d 字节 ✓" % (f, s))

root.destroy()
print("SMOKE TEST PASS")

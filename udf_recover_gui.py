#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""udf_recover_gui.py — UDF 光盘删除历史与恢复工具(图形界面)

依赖同目录的 udf_recover.py;仅用 Python 标准库(tkinter)。

运行:
    python udf_recover_gui.py          # 带控制台
    start pythonw udf_recover_gui.py   # 纯窗口,无黑框
"""

import os
import queue
import sys
import threading
import time
import traceback
import types
import ctypes

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import udf_recover as core

STATE_ORDER = {"当前": 0, "历史版本": 1, "已删除": 2, "孤儿(无目录表引用)": 3}
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(SCRIPT_DIR, "recovered")


def cdrom_letters():
    """枚举 CD-ROM 类型的盘符。只用轻量 API,不触碰卷栈(楔死也不会卡)。"""
    k = ctypes.windll.kernel32
    bits = k.GetLogicalDrives()
    out = []
    for i in range(26):
        if bits >> i & 1:
            L = chr(ord("A") + i)
            try:
                if k.GetDriveTypeW(L + ":\\") == 5:      # DRIVE_CDROM
                    out.append(L)
            except Exception:
                pass
    return out


def drive_media_state(letter, timeout=2.5):
    """检查光驱里是否有盘。返回 (0=有盘, 1=无盘, 2=无响应), label。

    在独立线程里执行并限时:卷栈楔死时 GetVolumeInformationW 可能阻塞,
    绝不能卡住 GUI 主线程。
    """
    R = {}

    def work():
        try:
            k = ctypes.windll.kernel32
            buf = ctypes.create_unicode_buffer(256)
            ok = k.GetVolumeInformationW(letter + ":\\", buf, 256,
                                         None, None, None, None, 0)
            R["v"] = (0, buf.value) if ok else (1, "")
        except Exception:
            R["v"] = (1, "")

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    return R.get("v", (2, ""))


def query_drive_names(timeout=12):
    """用 WMI 拿光驱型号名,返回 {盘符: 描述};失败/超时返回空表。"""
    import subprocess
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_CDROMDrive | "
             "ForEach-Object { '{0}|{1}' -f $_.Drive, $_.Caption }"],
            capture_output=True, text=True, timeout=timeout)
        out = {}
        for line in (r.stdout or "").splitlines():
            if "|" in line:
                drv, cap = line.split("|", 1)
                out[drv.strip().rstrip(":")] = cap.strip()
        return out
    except Exception:
        return {}


class App:
    def __init__(self, root):
        self.root = root
        root.title("UDF 光盘删除历史与文件恢复")
        root.geometry("980x680")
        root.minsize(820, 560)

        self.log_q = queue.Queue()
        self.busy = False
        self.src = None          # 扫描成功后的数据源
        self.ctx = None          # 扫描结果上下文
        self.rows = []           # 表格行: {state, names, fe, lba}
        self.current_entries = []
        self.job_name = ""

        self._build_ui()
        self._drive_names = {}
        self._last_drive_sig = None
        self._prev_media = {}
        self._polling = False
        self._refresh_drive_list()
        self.root.after(3000, self._poll_drives)
        self.root.after(80, self._poll)
        self._log("就绪。插好盘选「光驱」,或选已有镜像文件,然后点「扫描」。")

    # ------------------------------------------------------------ UI 构建

    def _build_ui(self):
        pad = {"padx": 6, "pady": 4}
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)

        # ---- 数据源 ----
        src = ttk.LabelFrame(top, text="数据源")
        src.pack(fill="x", **pad)
        self.var_mode = tk.StringVar(value="device")
        self.var_device = tk.StringVar(value=r"\\.\E:")
        self.var_image = tk.StringVar(value="")

        rb1 = ttk.Radiobutton(src, text="光驱", variable=self.var_mode, value="device",
                              command=self._on_mode)
        self.cmb_device = ttk.Combobox(src, width=40)
        self.cmb_device.bind("<<ComboboxSelected>>", self._on_drive_selected)
        btn_rfrsh = ttk.Button(src, text="刷新", width=6, command=self._refresh_drives_clicked)
        self.lbl_drive = ttk.Label(src, text="")
        rb2 = ttk.Radiobutton(src, text="镜像文件", variable=self.var_mode, value="image",
                              command=self._on_mode)
        self.ent_image = ttk.Entry(src, textvariable=self.var_image, width=44)
        btn_img = ttk.Button(src, text="浏览…", width=8, command=self._pick_image)

        rb1.grid(row=0, column=0, padx=(8, 2), pady=6)
        self.cmb_device.grid(row=0, column=1, columnspan=2, pady=6, sticky="we")
        btn_rfrsh.grid(row=0, column=3, padx=(4, 8))
        self.lbl_drive.grid(row=1, column=1, columnspan=3, sticky="w")
        rb2.grid(row=2, column=0, padx=(8, 2), pady=6)
        self.ent_image.grid(row=2, column=1, columnspan=2, pady=6, sticky="we")
        btn_img.grid(row=2, column=3, padx=(4, 8))
        src.columnconfigure(1, weight=1)

        # ---- 操作按钮 ----
        acts = ttk.Frame(top)
        acts.pack(fill="x", **pad)
        self.btn_mkimg = ttk.Button(acts, text="① 生成整盘镜像", command=self.on_make_image)
        self.btn_scan = ttk.Button(acts, text="② 扫描 / 查删除历史", command=self.on_scan)
        self.btn_sel = ttk.Button(acts, text="③ 恢复所选", command=lambda: self.on_recover(True))
        self.btn_all = ttk.Button(acts, text="恢复全部非当前", command=lambda: self.on_recover(False))
        self.btn_open = ttk.Button(acts, text="打开输出目录", command=self._open_outdir)
        self.btn_mkimg.pack(side="left", padx=(0, 6))
        self.btn_scan.pack(side="left", padx=(0, 6))
        self.btn_sel.pack(side="left", padx=(0, 6))
        self.btn_all.pack(side="left", padx=(0, 6))
        self.btn_open.pack(side="left", padx=(0, 6))

        out = ttk.Frame(top)
        out.pack(fill="x", **pad)
        ttk.Label(out, text="输出目录:").pack(side="left")
        self.var_out = tk.StringVar(value=DEFAULT_OUT)
        ttk.Entry(out, textvariable=self.var_out, width=52).pack(side="left", padx=4, fill="x", expand=True)
        ttk.Button(out, text="浏览…", width=8, command=self._pick_out).pack(side="left")

        depth = ttk.Frame(top)
        depth.pack(fill="x", **pad)
        ttk.Label(depth, text="扫描深度:").pack(side="left")
        self.var_depth = tk.StringVar(value="fast")
        ttk.Radiobutton(depth, text="预览(几秒,只看卷头+卷尾,可能漏中段旧表)",
                        variable=self.var_depth, value="preview").pack(side="left", padx=(6, 4))
        ttk.Radiobutton(depth, text="快速(默认,跳过已识别的文件数据区)",
                        variable=self.var_depth, value="fast").pack(side="left", padx=4)
        ttk.Radiobutton(depth, text="完整(最保守,扫遍整个可读区)",
                        variable=self.var_depth, value="full").pack(side="left", padx=4)

        # ---- 结果表 ----
        mid = ttk.Frame(top)
        mid.pack(fill="both", expand=True, **pad)
        cols = ("state", "ftype", "name", "size", "lba", "mtime", "note")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
        headers = [("state", "状态", 110), ("ftype", "类型", 60), ("name", "文件名", 320),
                   ("size", "大小", 80), ("lba", "FE@LBA", 80), ("mtime", "修改时间", 140),
                   ("note", "备注", 150)]
        for cid, text, w in headers:
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=w, anchor="w")
        vs = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")

        # ---- 进度 ----
        prog = ttk.Frame(top)
        prog.pack(fill="x", **pad)
        self.progress = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True)
        self.lbl_prog = ttk.Label(prog, text="", width=40, anchor="e")
        self.lbl_prog.pack(side="left", padx=(8, 0))

        # ---- 日志 ----
        logf = ttk.LabelFrame(top, text="日志 / 卷信息")
        logf.pack(fill="both", expand=True, **pad)
        self.txt = tk.Text(logf, height=9, wrap="none", state="disabled",
                           font=("Consolas", 9))
        ys = ttk.Scrollbar(logf, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=ys.set)
        self.txt.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
        ys.pack(side="right", fill="y", pady=4)

        self._on_mode()

    def _on_mode(self):
        is_dev = self.var_mode.get() == "device"
        self.cmb_device.configure(state="normal" if is_dev else "disabled")
        self.ent_image.configure(state="normal" if not is_dev else "disabled")

    # ---- 光驱枚举与插盘自动刷新 ----

    def _on_drive_selected(self, _evt=None):
        text = self.cmb_device.get()
        path = text.split(" — ")[0].strip()
        if path:
            self.var_device.set(path)
            self.var_mode.set("device")
            self._on_mode()

    def _poll_drives(self):
        """每 3 秒轮询插盘状态;轮询在后台线程做,楔死也不卡界面。"""
        if not self.busy and not self._polling:
            self._polling = True

            def job():
                data = [(L,) + tuple(drive_media_state(L)) for L in cdrom_letters()]
                self.log_q.put(("drives", (data, True)))
                self._polling = False

            threading.Thread(target=job, daemon=True).start()
        self.root.after(3000, self._poll_drives)

    def _refresh_drives_clicked(self):
        def job():
            self._drive_names = query_drive_names()
            data = [(L,) + tuple(drive_media_state(L)) for L in cdrom_letters()]
            self.log_q.put(("drives", (data, False)))

        threading.Thread(target=job, daemon=True).start()

    def _refresh_drive_list(self, quiet=False, with_names=False):
        def job():
            if with_names:
                self._drive_names = query_drive_names()
            data = [(L,) + tuple(drive_media_state(L)) for L in cdrom_letters()]
            self.log_q.put(("drives", (data, quiet)))

        threading.Thread(target=job, daemon=True).start()

    def _apply_drive_list(self, data, quiet=False):
        """把 (盘符, 状态, 标签) 列表应用到下拉框;检测插盘/弹盘事件。"""
        descs, media_now = [], {}
        for L, state, label in data:
            path = "\\\\.\\%s:" % L
            media_now[path] = state
            if state == 0:
                m = "有盘[%s]" % (label or "?")
            elif state == 1:
                m = "无盘"
            else:
                m = "无响应(光驱忙/栈异常)"
            nm = self._drive_names.get(L)
            descs.append("%s — %s%s" % (path, m, (" · " + nm) if nm else ""))
            if not quiet and self._prev_media and self._prev_media.get(path) != state:
                what = {0: "检测到新盘片: %s (%s)" % (path, label or "?"),
                        1: "盘片已弹出: %s" % path,
                        2: "光驱状态未知: %s" % path}.get(state)
                if what:
                    self._log("[*] " + what)
        self._prev_media = media_now

        sig = ";".join(descs)
        if sig != self._last_drive_sig:
            self._last_drive_sig = sig
            self.cmb_device["values"] = descs
            cur = self.var_device.get().strip()
            keep = next((x for x in descs if x.split(" — ")[0] == cur), None)
            if keep:
                self.cmb_device.set(keep)
            elif descs:
                prefer = next((x for x in descs if "有盘" in x), descs[0])
                self.cmb_device.set(prefer)
                self.var_device.set(prefer.split(" — ")[0])
                if not quiet:
                    self._log("[*] 已选择光驱: %s" % self.var_device.get())
            elif not quiet:
                self._log("[!] 未检测到光驱;仍可手动输入设备路径(如 \\\\.\\CdRom0)")
        cur = self.var_device.get().strip()
        desc = next((x for x in descs if x.split(" — ")[0] == cur), None)
        if desc:
            self.lbl_drive.configure(text="当前: " + desc.split(" — ", 1)[-1])

    # ------------------------------------------------------------ 小工具

    def _log(self, msg):
        self.log_q.put(("log", msg))

    def _prog(self, frac, text):
        self.log_q.put(("prog", (frac, text)))

    def _ask(self, kind, title, msg):
        self.log_q.put(("ask", (kind, title, msg)))

    def _poll(self):
        try:
            while True:
                kind, payload = self.log_q.get_nowait()
                if kind == "log":
                    self.txt.configure(state="normal")
                    self.txt.insert("end", payload + "\n")
                    self.txt.see("end")
                    self.txt.configure(state="disabled")
                elif kind == "prog":
                    frac, text = payload
                    if frac is None:
                        self.progress.configure(mode="indeterminate")
                        self.progress.start(60)
                    elif frac >= 1.0:
                        self.progress.stop()
                        self.progress.configure(mode="determinate", maximum=100, value=100)
                    else:
                        self.progress.stop()
                        self.progress.configure(mode="determinate", maximum=100,
                                                value=max(0, min(100, frac * 100)))
                    self.lbl_prog.configure(text=text)
                elif kind == "ask":
                    k, t, m = payload
                    getattr(messagebox, k)(t, m)
                elif kind == "fill":
                    self._fill_tree(payload)
                elif kind == "drives":
                    data, quiet = payload
                    self._apply_drive_list(data, quiet)
                elif kind == "done":
                    self.busy = False
                    self._set_buttons(True)
                    cb = payload
                    if cb:
                        cb()
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _set_buttons(self, enabled):
        state = "normal" if enabled else "disabled"
        for b in (self.btn_mkimg, self.btn_scan, self.btn_sel, self.btn_all):
            b.configure(state=state)

    def _start_job(self, name, fn, on_done=None):
        if self.busy:
            messagebox.showinfo("提示", "有任务正在运行(%s),请等它完成。" % self.job_name)
            return
        self.busy = True
        self.job_name = name
        self._set_buttons(False)
        self._prog(None, name + " 运行中…")

        def wrap():
            try:
                fn()
                self.log_q.put(("done", on_done))
            except Exception as e:
                self._log("!! 出错: %s" % e)
                self._log(traceback.format_exc(limit=4))
                self._ask("showerror", "出错", str(e))
                self.log_q.put(("done", None))

        threading.Thread(target=wrap, daemon=True).start()

    # ------------------------------------------------------------ 数据源

    def _make_ns(self):
        if self.var_mode.get() == "image":
            p = self.var_image.get().strip()
            if not p or not os.path.isfile(p):
                raise ValueError("请选择有效的镜像文件 (.img)")
            return types.SimpleNamespace(device=None, image=p)
        dev = self.var_device.get().strip() or r"\\.\E:"
        return types.SimpleNamespace(device=dev, image=None)

    def _source_changed(self):
        """数据源设置与上次扫描不同时,让旧结果失效。"""
        try:
            key = (self.var_mode.get(), self.var_device.get().strip(),
                   os.path.abspath(self.var_image.get().strip() or "?"))
        except Exception:
            key = None
        if getattr(self, "scan_key", None) is not None and key != self.scan_key:
            self.src = self.ctx = None
            self.rows = []
            self.tree.delete(*self.tree.get_children())
        self.scan_key = key

    def _pick_image(self):
        p = filedialog.askopenfilename(
            title="选择镜像文件",
            filetypes=[("镜像/所有文件", "*.img;*"), ("所有文件", "*.*")])
        if p:
            self.var_image.set(p)
            self.var_mode.set("image")
            self._on_mode()

    def _pick_out(self):
        p = filedialog.askdirectory(title="选择输出目录")
        if p:
            self.var_out.set(p)

    def _open_outdir(self):
        d = self.var_out.get().strip() or DEFAULT_OUT
        os.makedirs(d, exist_ok=True)
        os.startfile(d)

    # ------------------------------------------------------------ 任务

    def on_make_image(self):
        try:
            ns = self._make_ns()
        except ValueError as e:
            messagebox.showwarning("提示", str(e))
            return
        if ns.image:
            messagebox.showinfo("提示", "当前数据源已经是镜像,不需要再生成。\n"
                                        "要给光驱里的盘做镜像,请选「光驱」。")
            return
        out = filedialog.asksaveasfilename(
            title="镜像保存为", defaultextension=".img",
            initialfile="disc.img", filetypes=[("镜像文件", "*.img")])
        if not out:
            return

        def job():
            self._job_image(ns, out)

        self._start_job("生成镜像", job,
                        on_done=lambda: self._after_image(out))

    def _after_image(self, out):
        self.var_image.set(out)
        self.var_mode.set("image")
        self._on_mode()
        self._log("[提示] 已切换到镜像模式,点「② 扫描 / 查删除历史」开始分析。")

    def _job_image(self, ns, out):
        self._log("[*] 打开设备: %s" % ns.device)
        src = core.open_source(ns)
        try:
            max_lba = 2304000 if src.kind == "device" else 10 ** 9
            runs = []
            total = 0
            with open(out, "wb") as fh:
                for pos, data in core.iter_readable_chunks(
                        src, max_lba, runs_out=runs):
                    fh.write(data)
                    total += len(data)
                    self._prog(None, "已写入 %.1f MB" % (total / 1048576))
            if not runs:
                raise RuntimeError("一个扇区都没读到——光驱里没有盘?")
            with open(out + ".map.json", "w", encoding="utf-8") as fh:
                import json
                json.dump(runs, fh)
            self._prog(1.0, "完成")
            self._log("[OK] %s (%.1f MB, %d 个可读区段)"
                      % (out, total / 1048576, len(runs)))
        finally:
            src.close()

    def on_scan(self):
        try:
            ns = self._make_ns()
        except ValueError as e:
            messagebox.showwarning("提示", str(e))
            return
        self._source_changed()
        if self.busy:
            messagebox.showinfo("提示", "有任务正在运行,请等它完成。")
            return

        def job():
            self._job_scan(ns)

        self._start_job("扫描", job)

    def _job_scan(self, ns):
        self._log("[*] 数据源: %s" % (ns.image or ns.device))
        src = core.open_source(ns)
        try:
            if src.kind == "device":
                # 光驱闲置会停转,冷启动的首笔读可能要 1-2 分钟,显式唤醒
                self._prog(None, "正在唤醒光驱(冷启动可能需要 1-2 分钟)…")
                self._log("[*] 正在唤醒光驱;若刚插入/闲置已久,此步可能较慢…")
                try:
                    src.read(0, 32, timeout=240)
                except core.ReadUnavailable as e:
                    raise RuntimeError(
                        "光驱无响应:%s\n"
                        "若刚弹过盘/换过盘,请重新插一次盘(或重插 USB)后重试。" % e)
                self._log("[*] 光驱已就绪。")
            max_lba = 2304000 if src.kind == "device" else 10 ** 9
            depth = self.var_depth.get()
            fast = depth == "fast"
            preview = depth == "preview"
            n_mb = max(1, max_lba * core.SECTOR // (1024 * 1024))
            t0 = time.perf_counter()

            def on_prog(pos):
                dt = time.perf_counter() - t0
                mb = pos * core.SECTOR / 1048576
                spd = ("%.2f MB/s" % (mb / dt)) if dt > 0.5 and mb > 1 else ""
                self._prog(pos / max_lba, "扫描中 %.0f MB / ~%d MB %s"
                           % (mb, n_mb, spd))

            ctx, runs, skipped = core.scan_source(
                src, max_lba, fast=fast, preview=preview, progress=False,
                on_chunk=(lambda pos, data: on_prog(pos)) if not preview else None,
                log=self._log)
            if not runs:
                raise RuntimeError("没有读到任何数据——光驱里没放盘?")
            self._log("[*] 已扫读区: %s (共 %.1f MB)"
                      % (", ".join("LBA %d-%d" % (a, b - 1) for a, b in runs),
                         sum(b - a for a, b in runs) * core.SECTOR / 1048576))
            current_entries, current_icbs = ctx.walk_current()
            rows = []
            for lba, fe in sorted(ctx.fes.items()):
                state, names, _live = core.fe_state(ctx, lba, fe, current_icbs)
                rows.append({"state": state, "names": names, "fe": fe, "lba": lba})
            rows.sort(key=lambda r: (STATE_ORDER.get(r["state"], 9), r["lba"]))

            # 汇总删除事件
            cur_names = {e["path"].split("/")[-1] for e in current_entries}
            old_names = {f["name"] for f in ctx.fids if f["name"] and not f["parent"]}
            gone = old_names - cur_names

            self.src, self.ctx, self.rows = src, ctx, rows
            self.current_entries = current_entries
            self.log_q.put(("fill", gone))
        except Exception:
            try:
                src.close()
            except Exception:
                pass
            raise

    def _fill_tree(self, gone):
        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(self.rows):
            fe = r["fe"]
            name = ", ".join(n for n in r["names"] if n) or "(无名)"
            note = "  [存根:无区段]" if fe["ad_type"] == -1 and fe["info_len"] > 0 else ""
            self.tree.insert("", "end", iid=str(i), values=(
                r["state"], fe["type_name"], name[:80],
                core.human(fe["info_len"]), str(r["lba"]), fe["mtime"], note))
        v = self.ctx.vol
        self._log("── 卷信息 ──")
        for line in v.describe().splitlines():
            self._log("  " + line)
        self._log("── 条目 %d 个,当前可见 %d 个 ──"
                  % (len(self.rows), len(self.current_entries)))
        if gone:
            self._log("── 已删除(旧目录表里有、当前没有)──")
            for n in sorted(gone):
                self._log("  已删除: %s" % n)
        elif not self.rows:
            self._log("(没有发现任何文件条目)")
        self._prog(1.0, "扫描完成: %d 个条目" % len(self.rows))
        if not self.rows:
            self._ask("showinfo", "扫描完成", "没有发现任何文件条目。\n"
                                           "盘可能是空的,或不是 UDF 格式。")
        # 扫描线程里已把 src 挂到 self,这里不关(恢复时还要用)

    def on_recover(self, selected_only):
        if self.ctx is None or not self.rows:
            messagebox.showinfo("提示", "请先扫描。")
            return
        if selected_only:
            sel = self.tree.selection()
            if not sel:
                messagebox.showinfo("提示", "先在列表里选中要恢复的行(可多选)。")
                return
            rows = [self.rows[int(i)] for i in sel]
        else:
            rows = [r for r in self.rows if r["state"] != "当前"]
            if not rows:
                messagebox.showinfo("提示", "没有非当前的条目可恢复。")
                return
        outdir = self.var_out.get().strip() or DEFAULT_OUT

        def job():
            self._job_recover(rows, outdir)

        self._start_job("恢复", job,
                        on_done=lambda: self._ask(
                            "showinfo", "恢复完成",
                            "输出目录:\n%s\n\n日志里有每个文件的明细。" % outdir))

    def _job_recover(self, rows, outdir):
        src, ctx = self.src, self.ctx
        os.makedirs(outdir, exist_ok=True)
        used = set()
        ok = skip = fail = 0
        self._log("== 恢复 %d 个条目到 %s ==" % (len(rows), outdir))
        for idx, r in enumerate(rows):
            fe = r["fe"]
            base = core.sanitize_name(r["names"][0]) if r["names"] and r["names"][0] else ""
            if not base:
                head = b""
                try:
                    ext0 = fe["extents"][0]
                    if ext0["lbn"] is not None:
                        head = src.read(ctx.vol.abs_lba(ext0["lbn"], ext0["part"] or 0,
                                                        context_lba=fe["lba"]), 1)
                except Exception:
                    pass
                base = "orphan_%d%s" % (r["lba"], core.guess_ext(head))
            stem, dot, ext = base.rpartition(".")
            candidate = base
            n = 2
            while candidate.lower() in used:
                candidate = "%s__v%d%s" % (stem if dot else base, n,
                                           dot + ext if dot else "")
                n += 1
            used.add(candidate.lower())

            if not fe["extents"] and fe["info_len"] > 0:
                skip += 1
                self._log("  [跳过] %s: 存根条目(区段在 VAT 里),"
                          "用 scan 的特征码手工定位" % candidate)
                continue
            if r["state"] == "当前":
                self._log("  [提示] %s 是当前可见文件,正常复制即可,跳过" % candidate)
                skip += 1
                continue

            def prog(done, total, _name=candidate):
                self._prog(done / max(total, 1), "%s : %.1f / %.1f MB"
                           % (_name, done / 1048576, total / 1048576))

            try:
                data, bad = core.read_file_data(src, ctx.vol, fe, prog)
                with open(os.path.join(outdir, candidate), "wb") as fh:
                    fh.write(data)
                ok += 1
                note = "  (含 %d 个不可读扇区,以 0 填充)" % bad if bad else ""
                self._log("  [OK] %-46s %10s  [%s]%s"
                          % (candidate[:46], core.human(len(data)), r["state"], note))
            except Exception as e:
                fail += 1
                self._log("  [失败] %s: %s" % (candidate, e))
            self._prog((idx + 1) / len(rows), "恢复中 %d / %d" % (idx + 1, len(rows)))
        self._prog(1.0, "恢复完成: 成功 %d, 跳过 %d, 失败 %d" % (ok, skip, fail))
        self._log("== 完成: 成功 %d, 跳过 %d, 失败 %d ==" % (ok, skip, fail))


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

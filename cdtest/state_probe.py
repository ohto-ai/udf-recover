# -*- coding: utf-8 -*-
"""带硬超时的光驱状态探测:判断卷栈是否仍处于楔死状态。"""
import threading
import time

R = {}


def timed_read(lba, timeout=25):
    def work():
        try:
            f = open(r"\\.\E:", "rb", buffering=0)
            t0 = time.perf_counter()
            f.seek(lba * 2048)
            d = f.read(64 * 1024)
            f.close()
            R[lba] = ("OK %.2fs" % (time.perf_counter() - t0), len(d))
        except OSError as e:
            R[lba] = ("ERR%d" % e.errno, 0)
    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    if lba not in R:
        R[lba] = ("TIMEOUT(>%ds)" % timeout, 0)


for lba in (0, 151300, 30000):
    timed_read(lba)
    print("LBA %7d: %s (%d B)" % (lba, R[lba][0], R[lba][1]), flush=True)
print("结论:", "卷栈健康" if all("OK" in R[l][0] for l in (0, 151300))
      else "卷栈异常(楔死或盘片已更换),建议重新插一次盘")

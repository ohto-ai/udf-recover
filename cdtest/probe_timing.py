# -*- coding: utf-8 -*-
"""逐点探测:每个 LBA 的读在独立线程里做,带 40 秒硬超时,定位卡点。"""
import threading
import time

RESULT = {}


def probe(lba, timeout=40):
    def work():
        try:
            f = open(r"\\.\E:", "rb", buffering=0)
            t0 = time.perf_counter()
            f.seek(lba * 2048)
            d = f.read(32 * 2048)
            dt = time.perf_counter() - t0
            f.close()
            RESULT[lba] = ("OK" if d else "EMPTY", round(dt, 2))
        except OSError as e:
            RESULT[lba] = ("ERR%d" % e.errno, 0)
    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    if lba not in RESULT:
        RESULT[lba] = ("TIMEOUT(>=%ds)" % timeout, timeout)


for lba in (0, 100000, 150000, 151300, 151400, 152000, 200000, 400000,
            1000000, 2303968):
    probe(lba)
    print("LBA %9d: %s" % (lba, RESULT[lba]), flush=True)

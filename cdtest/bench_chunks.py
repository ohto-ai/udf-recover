# -*- coding: utf-8 -*-
"""光驱读盘基准:不同块大小、不同区域(数据区 vs 全零区)的真实吞吐。"""
import time

f = open(r"\\.\E:", "rb", buffering=0)


def bench(label, start_lba, nbytes, chunk):
    n_read = 0
    t0 = time.perf_counter()
    lba = start_lba
    while n_read < nbytes:
        f.seek(lba * 2048)
        d = f.read(chunk)
        if len(d) == 0:
            break
        n_read += len(d)
        lba += len(d) // 2048
    dt = time.perf_counter() - t0
    return n_read / 1048576 / dt if dt > 0 else 0.0


REGIONS = [
    ("视频数据区(60-90MB)", 30000),
    ("全零区(87-115MB)", 45000),
]

print("区域                         64KB   128KB  256KB  512KB   1MB    2MB")
for label, lba0 in REGIONS:
    row = []
    for chunk in (65536, 131072, 262144, 524288, 1048576, 2 * 1048576):
        try:
            mbps = bench(label, lba0, 16 * 1048576, chunk)
            row.append("%5.2f" % mbps)
        except OSError as e:
            row.append("  ERR ")
    print("%-24s %s" % (label, "  ".join(row)))

# 复测一遍 512KB,判断是否有缓存效应
print("复测 512KB(数据区): %.2f MB/s" % bench("x", 30000, 16 * 1048576, 524288))

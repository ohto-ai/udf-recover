#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
udf_recover.py — UDF 光盘"删除历史"查看与文件恢复工具
======================================================

用途:针对用 Windows「活动文件系统 (Live File System / UDF)」刻录的可写光盘
(尤其是一次性 DVD±R / CD-R)。在这种盘上删除文件只是改写 UDF 元数据,
旧目录表、旧文件条目 (File Entry) 和文件数据都还物理留在盘上。

本工具:
  1. info     解析卷结构(UDF 版本、分区、文件集位置)
  2. history  全盘扫描所有 UDF 目录表版本与文件条目,列出删除历史
  3. recover  按条目恢复文件(支持 --all / --name / --fe)
  4. scan     全盘原始扫描(可读区地图 + 特征码普查)
  5. image    把可读区转储成本地镜像文件(.img + .map.json),
              之后所有子命令都可用 --image 离线运行,不再磨光驱

只依赖 Python 标准库。Windows 下读原始卷需要能打开 \\.\X: 设备。

示例:
  python udf_recover.py info
  python udf_recover.py history
  python udf_recover.py recover --all --out recovered
  python udf_recover.py recover --name "*.mp4"
  python udf_recover.py image --out disc.img
  python udf_recover.py history --image disc.img
"""

import argparse
import fnmatch
import json
import os
import re
import struct
import sys
import time
from collections import deque

SECTOR = 2048

# ---------------------------------------------------------------- 低层读取

class ReadUnavailable(Exception):
    """LBA 区段不可读(一次性盘最后写入地址之后、未记录区等)。"""


class DeviceSource:
    kind = "device"

    def __init__(self, path):
        self.path = path
        last_err = None
        self.f = None
        for cand in DeviceSource._candidates(path):
            try:
                self.f = open(cand, "rb", buffering=0)
                self.path = cand
                break
            except OSError as e:
                last_err = e
        if self.f is None:
            raise OSError(last_err.errno if last_err else 22,
                          "无法打开 %s (尝试了 %s)"
                          % (path, ", ".join(DeviceSource._candidates(path))))

    @staticmethod
    def _candidates(path):
        """同一个设备的多种写法都试一遍(卷路径带不带尾反斜杠、CdRomN)。"""
        out = [path]
        base = path.rstrip("\\/")
        if base.startswith("\\\\.\\") and len(base) > 6 and base[-2] == ":":
            letter = base[-2]
            out += ["\\\\.\\%s:" % letter, "\\\\.\\%s:\\" % letter]
            # 最后的兜底:物理光驱设备 CdRom0..3(注意可能与虚拟光驱重名,仅作后备)
            for n in range(4):
                out.append("\\\\.\\CdRom%d" % n)
        # 去重保序
        seen = set()
        uniq = []
        for x in out:
            if x not in seen:
                seen.add(x)
                uniq.append(x)
        return uniq

    def read(self, lba, nsectors):
        self.f.seek(lba * SECTOR)
        data = self.f.read(nsectors * SECTOR)
        if len(data) != nsectors * SECTOR:
            raise ReadUnavailable("LBA %d (+%d) 短读" % (lba, nsectors))
        return data

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass


class ImageSource:
    kind = "image"

    def __init__(self, img_path):
        map_path = img_path + ".map.json"
        with open(map_path, "r", encoding="utf-8") as fh:
            self.runs = [tuple(r) for r in json.load(fh)]  # (start_lba, end_lba_exclusive)
        self.f = open(img_path, "rb")
        self.offsets = []
        off = 0
        for a, b in self.runs:
            self.offsets.append(off)
            off += (b - a) * SECTOR
        self.total_sectors = sum(b - a for a, b in self.runs)

    def has(self, lba, nsectors):
        end = lba + nsectors
        return any(a <= lba and end <= b for a, b in self.runs)

    def read(self, lba, nsectors):
        end = lba + nsectors
        for (a, b), base in zip(self.runs, self.offsets):
            if a <= lba and end <= b:
                self.f.seek(base + (lba - a) * SECTOR)
                data = self.f.read(nsectors * SECTOR)
                if len(data) != nsectors * SECTOR:
                    raise ReadUnavailable("镜像短读 LBA %d" % lba)
                return data
        raise ReadUnavailable("LBA %d..%d 不在镜像可读区内" % (lba, end))

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass


def open_source(args):
    if getattr(args, "image", None):
        src = ImageSource(args.image)
        print("[*] 从镜像读取: %s (可读 %d 扇区 / %.1f MB)"
              % (args.image, src.total_sectors, src.total_sectors * SECTOR / 1048576))
        return src
    dev = args.device or r"\\.\E:"
    print("[*] 打开设备: %s" % dev)
    try:
        return DeviceSource(dev)
    except OSError as e:
        sys.exit("[!] 无法打开设备 %s : %s" % (dev, e))


# ---------------------------------------------------------------- 扫描引擎

def iter_readable_chunks(src, max_lba, chunk=256, skip=32, stop_err_mb=96, quiet=False,
                         runs_out=None, progress=False, skip_zones=None, skip_stats=None,
                         start_lba=0):
    """顺序扫过 start_lba..max_lba,自适应跳过读不出来的区段。

    - 读错误时按 32 扇区细分重试(找块内边界);
    - 整块失败则跳步倍增(64KB -> 最大 8MB),成功后二分回探把边界找回;
    - 连续不可读超过 stop_err_mb MB 视为越过盘上最后写入地址,提前结束。
    runs_out 提供时写入可读区段 [(start, end)]。
    skip_zones 提供时,是调用方持续追加的 [start,end) 列表(已识别的文件
    数据区)——生成器每读一块之前检查,落在区内的位置直接快进;跳过的
    字节累计写入 skip_stats[0]。
    """
    runs = runs_out if runs_out is not None else []
    max_skip = 4096                    # 8MB
    stop_err_sectors = stop_err_mb * 1024 * 1024 // SECTOR
    pos = start_lba
    consec_err = 0                     # 连续失败的扇区数(含跳过的)
    cur_skip = skip
    gap_total = 0
    last_report = 0

    def read_fine(lba, nsec):
        """按 32 扇区步进读,遇到失败即停,返回能读到的部分。"""
        parts = []
        k = 0
        while k < nsec:
            step = min(32, nsec - k)
            try:
                parts.append(src.read(lba + k, step))
            except (ReadUnavailable, OSError):
                break
            k += step
        return b"".join(parts)

    def record_run(lba, data):
        n = len(data) // SECTOR
        if n == 0:
            return
        if runs and runs[-1][1] == lba:
            runs[-1][1] = lba + n
        else:
            runs.append([lba, lba + n])

    fail_times = deque(maxlen=6)
    slow_fails = 0
    max_slow_fails = 60          # 慢失败预算:超过说明盘面状态很差,保全结果提前收工

    while (pos < max_lba and consec_err < stop_err_sectors
           and slow_fails < max_slow_fails):
        # 快速模式:跳过调用方已识别为"文件数据"的区段
        if skip_zones:
            jumped = True
            while jumped:
                jumped = False
                for s, e in skip_zones:
                    if s <= pos < e:
                        if skip_stats is not None:
                            skip_stats[0] += e - pos
                        pos = e
                        jumped = True
                if pos >= max_lba:
                    break
            if pos >= max_lba:
                break
        t0 = time.perf_counter()
        try:
            data = src.read(pos, chunk)
        except (ReadUnavailable, OSError):
            dt = time.perf_counter() - t0
            fail_times.append(dt)
            # 慢失败(设备级重试,可能几秒~几十秒)改用大步长,少浪费尝试次数
            recent_slow = sum(fail_times) / len(fail_times) > 1.5
            if not recent_slow:
                fine = read_fine(pos, chunk)
                if fine:
                    consec_err = 0
                    cur_skip = skip
                    record_run(pos, fine)
                    yield pos, fine
                    pos += len(fine) // SECTOR
                    continue
            if recent_slow:
                slow_fails += 1
                step = 4096                          # 8MB
            else:
                step = cur_skip
            consec_err += step
            gap_total += step
            pos += step
            cur_skip = min(cur_skip * 2, max_skip)
            continue
        if consec_err:
            # 回探:在 (pos-consec_err, pos] 里二分找可读边界,并补读边界之后的数据
            lo = pos - consec_err
            hi = pos
            recent_slow = sum(fail_times) / len(fail_times) > 1.5 if fail_times else False
            if not recent_slow:
                while hi - lo > 32:
                    mid = (lo + hi) // 2 // 32 * 32
                    try:
                        src.read(mid, 32)
                        hi = mid
                    except (ReadUnavailable, OSError):
                        lo = mid
            else:
                # 慢失败场景:只用 8MB 步长向后试探,最多 4 步
                for _ in range(4):
                    cand = pos - consec_err if consec_err <= 4096 else pos - 4096
                    if cand < 0:
                        break
                    try:
                        src.read(cand, 32)
                        hi = cand
                        consec_err = pos - cand
                        break
                    except (ReadUnavailable, OSError):
                        break
            gap_total += hi - (pos - consec_err)
            consec_err = 0
            cur_skip = skip
            # 回探:在 (pos-consec_err, pos] 里二分找可读边界,并补读边界之后的数据
            lo = pos - consec_err
            hi = pos
            while hi - lo > 32:
                mid = (lo + hi) // 2 // 32 * 32
                try:
                    src.read(mid, 32)
                    hi = mid
                except (ReadUnavailable, OSError):
                    lo = mid
            gap_total += hi - (pos - consec_err)
            consec_err = 0
            cur_skip = skip
            b = hi
            while b < pos:
                span = min(chunk, pos - b)
                try:
                    d = src.read(b, span)
                except (ReadUnavailable, OSError):
                    d = read_fine(b, span)
                    if not d:
                        break
                record_run(b, d)
                yield b, d
                b += len(d) // SECTOR
        record_run(pos, data)
        if progress and (pos - last_report) * SECTOR >= 64 * 1024 * 1024:
            sys.stdout.write("\r    扫描进度: %d MB" % (pos * SECTOR // 1048576))
            sys.stdout.flush()
            last_report = pos
        yield pos, data
        pos += chunk
    if progress:
        sys.stdout.write("\n")
    if slow_fails >= max_slow_fails:
        print("[!] 慢失败预算用尽(%d 次),提前结束扫描,结果可能不完整" % slow_fails)
    if not quiet:
        if runs:
            print("[*] 可读区: %s  (共 %.1f MB, 跳过不可读 %.1f MB)"
                  % (", ".join("LBA %d-%d" % (a, b - 1) for a, b in runs),
                     sum(b - a for a, b in runs) * SECTOR / 1048576,
                     gap_total * SECTOR / 1048576))
        else:
            print("[!] 没有任何可读扇区")


def probe_readable_end(src, max_lba, max_slow=3):
    """二分探测最后可读 LBA(假设可读区是从 0 开始的前缀)。

    返回 (end_lba, 探测次数, 慢失败次数)。探测到慢失败过多时提前收手,
    返回当前已知下界(此时调用方仍会走原有的错误自适应逻辑)。
    """
    def ok(lba):
        t0 = time.perf_counter()
        try:
            src.read(lba, 32)
            return True, time.perf_counter() - t0
        except (ReadUnavailable, OSError):
            return False, time.perf_counter() - t0

    good, _ = ok(0)
    if not good:
        return 0, 1, 0
    if ok(max_lba - 32)[0]:
        return max_lba, 1, 0
    lo, hi = 0, max_lba
    probes, slow = 2, 0
    while hi - lo > 32:
        mid = (lo + hi) // 2 // 32 * 32
        good, dt = ok(mid)
        probes += 1
        if not good and dt > 1.5:
            slow += 1
            if slow >= max_slow:
                break
        if good:
            lo = mid
        else:
            hi = mid
    return lo + 32, probes, slow


def scan_source(src, max_lba, fast=True, chunk=512, skip=32, stop_err_mb=96,
                progress=False, on_chunk=None, log=None, preview=False):
    if log and src.kind == 'device':
        log('[*] 提示: 若光驱已闲置停转,冷启动后首次读取可能需要 1-2 分钟')
    """扫描数据源并解析全部 UDF 描述符,返回 (ctx, runs, skipped_bytes)。

    preview=True: 只读卷头(前 2MB)+ 卷尾(最后 8MB),几秒出结果,
                  适合快速看一眼;可能漏掉盘片中段的旧文件表。
    fast=True:   一边扫一边把「已识别文件的数据区段」加入跳过名单——
                 一次性介质的区段互不重叠,文件数据里不可能再藏别的
                 文件表,所以大文件的数据区可以整段不读。
                 注意:Windows VAT 式增量写入的"存根"条目不带区段,
                 无法跳过,此时快速模式退化为接近完整扫描。
    fast=False:  完整顺序扫描(最保守)。
    扫描前先二分探测可读上界,避免在不可读尾部做大量慢失败试探。
    on_chunk(pos, data): 每读一块的回调(如特征码统计)。
    """
    t0 = time.perf_counter()
    ctx = ScanContext(src, None)
    ctx.vol = Volume()
    runs = []
    skip_zones = []
    skip_stats = [0]

    def zones_to_iter(zones):
        for a, b in zones:
            if b <= a:
                continue
            sub_runs = []
            for pos, data in iter_readable_chunks(
                    src, b, chunk=chunk, skip=skip, stop_err_mb=stop_err_mb,
                    runs_out=sub_runs, start_lba=a, quiet=True):
                yield pos, data
            runs.extend(sub_runs)

    if preview:
        end, probes, slow = probe_readable_end(src, max_lba)
        if log:
            log("[*] 预览模式: 可读上界 LBA %d (探测 %d 次)"
                % (end, probes))
        head_end = min(1024, end)
        tail_start = max(0, end - 8192)
        zones = [(0, head_end)]
        if tail_start > head_end:
            zones.append((tail_start, end))
        for pos, data in zones_to_iter(zones):
            ctx.examine(pos, data)
            if on_chunk:
                on_chunk(pos, data)
    else:
        if fast:
            end, probes, slow = probe_readable_end(src, max_lba)
            if end:
                max_lba = min(max_lba, end)
            if log and probes > 1:
                log("[*] 可读上界探测: LBA %d (%d 次探测)"
                    % (end, probes))
        for pos, data in iter_readable_chunks(
                src, max_lba, chunk=chunk, skip=skip, stop_err_mb=stop_err_mb,
                runs_out=runs, progress=progress,
                skip_zones=skip_zones if fast else None,
                skip_stats=skip_stats if fast else None):
            before = len(ctx.fes)
            ctx.examine(pos, data)
            if on_chunk:
                on_chunk(pos, data)
            if fast:
                # 分区信息一到手就解析,后续 FE 的区段才能换算成绝对地址
                if not ctx.vol.partitions and ctx.tags.get(5) and ctx.tags.get(6):
                    try:
                        ctx.vol = parse_volume_descriptors(ctx)
                    except Exception:
                        pass
                if ctx.vol.partitions and len(ctx.fes) > before:
                    for fe in list(ctx.fes.values())[before:]:
                        for ext in fe["extents"]:
                            if ext["lbn"] is None or ext["flag"] != 0:
                                continue
                            if ext["len"] < 64 * 1024:   # 小段可能是元数据,不跳
                                continue
                            try:
                                a = ctx.vol.abs_lba(ext["lbn"], ext["part"] or 0,
                                                    context_lba=fe["lba"])
                            except (ReadUnavailable, OSError):
                                continue
                            nsec = (ext["len"] + SECTOR - 1) // SECTOR
                            if len(skip_zones) < 500:
                                skip_zones.append([a, min(a + nsec, max_lba)])
    ctx.after_scan()
    if log:
        elapsed = time.perf_counter() - t0
        read_mb = sum(b - a for a, b in runs) * SECTOR / 1048576
        msg = "[*] 扫描完成: 实读 %.1f MB, 耗时 %.1f 秒 (%.2f MB/s)" % (
            read_mb, elapsed, read_mb / elapsed if elapsed > 0 else 0)
        if skip_stats[0]:
            msg += ", 跳过已识别文件数据 %.1f MB" % (skip_stats[0] * SECTOR / 1048576)
        log(msg)
    return ctx, runs, skip_stats[0]


# ---------------------------------------------------------------- UDF 解析

TAG_NAMES = {
    1: "PVD", 2: "AVDP", 3: "VDP", 4: "IUVD", 5: "PD", 6: "LVD", 7: "USD",
    8: "TD", 9: "LVID", 256: "FSD", 257: "FID", 258: "AED", 259: "IE",
    260: "TE", 261: "FE", 262: "EAH", 263: "USE", 264: "SBU", 265: "SBF",
    266: "EFE", 268: "LVHD",
}

FILE_TYPES = {4: "目录", 5: "文件", 12: "符号链接", 250: "元数据文件",
              251: "元数据镜像", 253: "sparing 表"}


def parse_tag(sec, expect_lba=None):
    """校验并解析描述符标签;不合法返回 None。

    校验算法 (ECMA-167 4/14.6.5): 全部 16 字节求和后减去校验字节自身,
    对 256 取模。tagLocation 仅作软校验(分区内描述符可能是分区相对地址)。
    """
    if len(sec) < 16:
        return None
    tid, ver, cks, _r, _serial, _crc, _crclen, loc = struct.unpack_from("<HHBBHHHI", sec, 0)
    if tid < 1 or tid > 268:
        return None
    if (sum(sec[0:16]) - sec[4]) & 0xFF != cks:
        return None
    return {"id": tid, "ver": ver, "loc": loc,
            "loc_mismatch": expect_lba is not None and loc != expect_lba}


def decode_dstring(b):
    """UDF dstring: 首字节 8=Latin-1, 16=UTF-16BE, 尾部是 CRC。"""
    if not b:
        return ""
    enc = b[0]
    body = b[1:]
    if enc == 8:
        raw = body[:-2] if len(body) >= 2 else body
        return raw.decode("latin-1", "replace").rstrip("\x00")
    if enc == 16:
        raw = body[:-4] if len(body) >= 4 else body
        return raw.decode("utf-16-be", "replace").rstrip("\x00")
    return body.decode("latin-1", "replace").rstrip("\x00")


def decode_entityid(b):
    if not b:
        return ""
    ident = b[1:24].split(b"\x00")[0]
    try:
        return ident.decode("ascii", "replace")
    except Exception:
        return ""


def parse_ts(sec, off):
    try:
        _tz, year = struct.unpack_from("<Hh", sec, off)
        mo, d, h, mi, s = sec[off + 4], sec[off + 5], sec[off + 6], sec[off + 7], sec[off + 8]
        if year <= 0:
            return "?"
        return "%04d-%02d-%02d %02d:%02d:%02d" % (year, mo, d, h, mi, s)
    except Exception:
        return "?"


def parse_long_ad(sec, off):
    """返回 (length, lbn, partition_ref)"""
    ln, lbn, pref = struct.unpack_from("<IIH", sec, off)
    return (ln & 0x3FFFFFFF), lbn, pref


def _sane_ts(sec, off):
    """读一个 UDF timestamp,粗查年份是否合理(用于布局探测)。"""
    try:
        _tz, year = struct.unpack_from("<Hh", sec, off)
        mo, d = sec[off + 4], sec[off + 5]
        return 1990 <= year <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31
    except Exception:
        return False


def _try_extents(sec, ad_type, ad_off, l_ad, info_len):
    """按 ad_type 解析分配描述符;布局不合法返回 None。"""
    extents = []
    if ad_type == 0:                     # short_ad
        if l_ad % 8 or l_ad > SECTOR - ad_off:
            return None
        for o in range(ad_off, ad_off + l_ad, 8):
            ln, pos = struct.unpack_from("<II", sec, o)
            extents.append({"lbn": pos, "len": ln & 0x3FFFFFFF, "flag": ln >> 30,
                            "part": None})
    elif ad_type == 1:                   # long_ad
        if l_ad % 16 or l_ad > SECTOR - ad_off:
            return None
        for o in range(ad_off, ad_off + l_ad, 16):
            ln, pos, pref = struct.unpack_from("<IIH", sec, o)
            extents.append({"lbn": pos, "len": ln & 0x3FFFFFFF, "flag": ln >> 30,
                            "part": pref})
    elif ad_type == 3:                   # 数据内嵌在 FE 里
        if l_ad > SECTOR - ad_off:
            return None
        extents.append({"lbn": None, "len": info_len, "flag": 0, "part": None,
                        "in_icb_off": ad_off, "in_icb_data": sec[ad_off:ad_off + l_ad]})
    else:
        return None
    return extents


# Windows IMAPI2 等实现在 File Entry 上的字段布局与教科书不完全一致,
# 这里对几组 (uid, L_EA, L_AD, AD区起点, mtime偏移) 依次探测,取第一个自洽的。
FE_LAYOUTS = [
    # 实测: IMAPI2/LiveFS 老式 FE(无 objectSize/无 streamDirectoryICB)
    (160, 168, 172, 176, 84),
    # 教科书 FE
    (188, 196, 200, 204, 92),
    (180, 184, 188, 192, 92),
]
EFE_LAYOUTS = [
    # 实测: IMAPI2/LiveFS 风格(infoLen@56, 同 FE 紧凑布局)
    (240, 248, 252, 256, 84, 56),
    # 教科书 EFE(有 objectSize@56, infoLen@72, creationTime)
    (240, 248, 252, 256, 104, 72),
    (160, 168, 172, 176, 84, 56),
    (188, 196, 200, 204, 92, 72),
]


def _pick_mtime(sec, prefer):
    """在几个可能的 timestamp 位置里挑第一个年份合理的。"""
    for off in (prefer, 84, 92, 96, 104, 116, 72):
        if _sane_ts(sec, off):
            return parse_ts(sec, off)
    return "?"


def parse_file_entry(sec, lba):
    tag = parse_tag(sec, lba)
    if tag is None or tag["id"] not in (261, 266):
        return None
    tid = tag["id"]
    file_type = sec[27]
    flags = struct.unpack_from("<H", sec, 34)[0]
    ad_type = flags & 7
    layouts = FE_LAYOUTS if tid == 261 else EFE_LAYOUTS
    for lay in layouts:
        uid_off, lea_off, lad_off, ad_base, mtime_off = lay[0], lay[1], lay[2], lay[3], lay[4]
        info_off = lay[5] if tid == 266 else 56
        info_len = struct.unpack_from("<Q", sec, info_off)[0]
        unique_id = struct.unpack_from("<Q", sec, uid_off)[0]
        l_ea, l_ad = struct.unpack_from("<II", sec, lea_off)[0], struct.unpack_from("<I", sec, lad_off)[0]
        ad_off = ad_base + l_ea
        if l_ea > SECTOR - ad_base or l_ad > SECTOR - ad_off:
            continue
        if ad_type == 3 and info_len > 0 and l_ad == 0:
            continue
        extents = _try_extents(sec, ad_type, ad_off, l_ad, info_len)
        if extents is None:
            continue
        # 有数据却既没有内嵌也没有区段 -> 该布局不对
        if ad_type in (0, 1) and info_len > 0 and l_ad == 0 and len(extents) == 0:
            continue
        mtime = _pick_mtime(sec, mtime_off)
        ctime = None
        return {
            "tag_id": tid, "lba": lba, "file_type": file_type,
            "type_name": FILE_TYPES.get(file_type, "类型%d" % file_type),
            "info_len": info_len, "unique_id": unique_id,
            "ad_type": ad_type, "extents": extents, "mtime": mtime, "ctime": ctime,
            "ea_bytes": sec[ad_base:ad_base + l_ea],
            "layout": lay[:4],
        }
    # 所有布局都不自洽 —— 记为"存根"条目(Windows VAT 式增量写入会把
    # 区段放进虚拟分配表,FE 本体不带 AD)
    info_len = struct.unpack_from("<Q", sec, 56)[0]
    return {
        "tag_id": tid, "lba": lba, "file_type": file_type,
        "type_name": FILE_TYPES.get(file_type, "类型%d" % file_type),
        "info_len": info_len, "unique_id": 0,
        "ad_type": -1, "extents": [], "mtime": "?", "ctime": None,
        "ea_bytes": b"", "layout": None,
    }


def parse_fids_in_extent(piece, start_lbn, out_list, dir_ref):
    """在一段目录数据里顺着 FID 链走。piece 必须从扇区边界开始。"""
    n = len(piece)
    off = 0
    while off + 38 <= n:
        sec_idx = off // SECTOR
        tag = parse_tag(piece[off:off + 16], start_lbn + sec_idx if start_lbn is not None else None)
        if tag is None or tag["id"] != 257:
            off += 4                      # FID 4 字节对齐,向前重同步
            continue
        _fvn, chars, idlen = struct.unpack_from("<HBB", piece, off + 16)
        ln_icb, lbn, pref = parse_long_ad(piece, off + 20)
        l_iu = struct.unpack_from("<H", piece, off + 36)[0]
        total = (38 + l_iu + idlen + 3) & ~3
        if total < 38 or off + total > n + 3:
            break
        name_bytes = piece[off + 38 + l_iu: off + 38 + l_iu + idlen]
        name = decode_fid_name(name_bytes)
        out_list.append({
            "name": name,
            "deleted": bool(chars & 0x04),
            "hidden": bool(chars & 0x01),
            "is_dir": bool(chars & 0x02),
            "parent": bool(chars & 0x08),
            "icb_lbn": lbn,
            "icb_part": pref,
            "dir_ref": dir_ref,       # (目录FE的LBA, 目录uid)
        })
        off += total


def decode_fid_name(nb):
    """FID 文件名。注意:Windows LiveFS 写的 16 位名字不带尾部 CRC,
    所以不盲目去尾;BE/LE 各解一次,选控制字符少的那个。"""
    if not nb or len(nb) < 1:
        return ""
    enc = nb[0]
    body = nb[1:]
    if enc == 8:
        txt = body.decode("latin-1", "replace")
    elif enc == 16:
        n = len(body) - len(body) % 2
        best = None
        for codec in ("utf-16-be", "utf-16-le"):
            try:
                cand = body[:n].decode(codec, "replace")
            except Exception:
                continue
            bad = sum(1 for c in cand if ord(c) < 0x20 or ord(c) == 0xFFFD)
            if best is None or bad < best[0]:
                best = (bad, cand)
        txt = best[1] if best else ""
    else:
        txt = body.decode("latin-1", "replace")
    txt = txt.rstrip("\x00").rstrip()
    while txt and ord(txt[-1]) < 0x20:
        txt = txt[:-1]
    return txt


# ---------------------------------------------------------------- 卷结构

class Volume:
    def __init__(self):
        self.block_size = SECTOR
        self.udf_rev = None
        self.lvid = ""
        self.partitions = {}          # partition_ref -> dict(start, length, number)
        self.pd_starts = []           # 所有历史分区定义的起始 LBA
        self.fsd_lbn = None           # LVD contentsUse 指向的 FSD
        self.fsds = []                # 扫描到的全部 FSD
        self.root_lbn = None
        self.metadata_maps = {}       # partition_ref -> [(lbn0, abs_lba, len)] (UDF2.50)

    @property
    def phys_starts(self):
        return self.pd_starts

    def describe(self):
        lines = []
        lines.append("卷标识        : %s" % (self.lvid or "(无)"))
        if self.udf_rev:
            lines.append("UDF 版本      : %d.%02d" % (self.udf_rev >> 8, self.udf_rev & 0xFF))
        lines.append("逻辑块大小    : %d" % self.block_size)
        for pref, p in sorted(self.partitions.items()):
            if p.get("virtual"):
                lines.append("分区 #%d (映射引用 %d): 虚拟分区 (%s) —— 区段映射在 VAT 里,"
                             "按物理扇区直接解析的条目不受影响" % (p["number"], pref, p["contents"]))
            else:
                lines.append("分区 #%d (映射引用 %d): 起始 LBA %d, 长 %d 扇区 (%.1f MB), 类型 %s"
                             % (p["number"], pref, p["start"], p["length"],
                                p["length"] * SECTOR / 1048576, p["contents"]))
        if self.fsd_lbn is not None:
            p0 = self.partitions.get(0, {"start": 0})
            lines.append("文件集 (FSD)  : LBN %d (绝对 LBA %d)" % (self.fsd_lbn, p0["start"] + self.fsd_lbn))
        if self.root_lbn is not None:
            p0 = self.partitions.get(0, {"start": 0})
            lines.append("根目录 ICB    : LBN %d (最新分区定义下绝对 LBA %s)"
                         % (self.root_lbn, (p0.get("start") or 0) + self.root_lbn))
        if len(self.pd_starts) > 1:
            lines.append("历史分区定义  : 起始 LBA %s (盘被多次追加改写,区段按描述符所在时代取基址)"
                         % " / ".join(str(s) for s in self.pd_starts))
        lines.append("扫描到 FSD 数 : %d" % len(self.fsds))
        return "\n".join(lines)

    def abs_lba(self, lbn, part_ref=0, context_lba=None):
        # UDF 2.50 元数据分区映射
        if part_ref in self.metadata_maps:
            for lbn0, abs_lba, ln in self.metadata_maps[part_ref]:
                if lbn0 <= lbn < lbn0 + ln // SECTOR:
                    return abs_lba + (lbn - lbn0)
            raise ReadUnavailable("元数据分区映射未覆盖 LBN %d" % lbn)
        p = self.partitions.get(part_ref)
        if p is None:
            return lbn
        if p.get("virtual"):
            raise ReadUnavailable("LBN %d 属于虚拟分区(UDF 1.50 VAT 式增量写入),"
                                  "其映射表无法从盘上可靠解析" % lbn)
        start = p["start"]
        # 盘上可能有多代分区定义(LiveFS 追加改写会重定义分区):
        # 区段用哪个基址,取决于描述符本身写在哪个时代
        if context_lba is not None and len(self.phys_starts) > 1:
            cand = [s for s in self.phys_starts if s <= context_lba]
            if cand:
                start = max(cand)
        return start + lbn


def parse_volume_descriptors(ctx):
    """从扫描到的描述符中提取卷结构。"""
    vol = Volume()
    for lba in sorted(ctx.tags.get(5, [])):
        try:
            sec = ctx.read_sector(lba)
        except (ReadUnavailable, OSError):
            continue
        if parse_tag(sec, lba) is None or struct.unpack_from("<H", sec, 0)[0] != 5:
            continue
        pnum = struct.unpack_from("<H", sec, 22)[0]
        start = struct.unpack_from("<I", sec, 188)[0]
        length = struct.unpack_from("<I", sec, 192)[0]
        contents = decode_entityid(sec[24:56])
        # partition ref = 在 LVD 映射表中的序号,后面补
        ctx.pending_pds.append({"lba": lba, "number": pnum, "start": start,
                                "length": length, "contents": contents})
        if start not in vol.pd_starts:
            vol.pd_starts.append(start)
        vol.pd_starts.sort()
    for lba in sorted(ctx.tags.get(6, [])):
        try:
            sec = ctx.read_sector(lba)
        except (ReadUnavailable, OSError):
            continue
        if parse_tag(sec, lba) is None or struct.unpack_from("<H", sec, 0)[0] != 6:
            continue
        vol.block_size = struct.unpack_from("<I", sec, 212)[0] or SECTOR
        vol.lvid = decode_dstring(sec[84:212])
        dom = decode_entityid(sec[216:248])
        if dom and "OSTA UDF" in dom:
            # EntityID suffix: 8 bytes; UDF revision 在 suffix[2:4](大端)
            suffix = sec[216 + 24:216 + 32]
            rev_be = (suffix[2] << 8) | suffix[3]
            rev_le = (suffix[3] << 8) | suffix[2]
            vol.udf_rev = rev_be if rev_be in (0x0102, 0x0150, 0x0201, 0x0203, 0x0250, 0x0260) else rev_le
        _ln, fsd_lbn, _pref = parse_long_ad(sec, 248)
        # 后写的 LVD 覆盖先写的(最新状态优先);指针为 0 的无效
        if fsd_lbn:
            vol.fsd_lbn = fsd_lbn
        # 分区映射表:实测 Windows LiveFS 把它放在 440(教科书 442,这里两种都试)
        n_maps = sec[265] or sec[268]
        maps = None
        for map_start in (440, 442):
            if maps is None and map_start + max(n_maps * 64, n_maps * 6) <= SECTOR:
                cand = sec[map_start:map_start + max(n_maps * 64, 70)]
                if cand and cand[0] in (1, 2):
                    maps = cand
        if maps is None:
            maps = sec[440:440 + 70]
        o = 0
        ref = 0
        while o < len(maps):
            mtype = maps[o]
            mlen = maps[o + 1] if o + 1 < len(maps) else 6
            if mtype == 1 and 2 <= mlen <= 6:
                pnum = struct.unpack_from("<H", maps, o + 4)[0] if mlen >= 6 else 0
                pd = next((p for p in ctx.pending_pds if p["number"] == pnum), None)
                if pd is None and ctx.pending_pds:
                    pd = ctx.pending_pds[0]        # Windows 有时把 pnum 写成垃圾值
                if pd:
                    vol.partitions[ref] = {"number": pnum, "start": pd["start"],
                                           "length": pd["length"], "contents": pd["contents"],
                                           "virtual": False}
            elif mtype == 2 and mlen >= 36 and b"UDF Virtual" in maps[o:o + mlen]:
                # UDF 1.50 虚拟分区:VAT(虚拟分配表)负责 虚拟LBN->物理LBA 映射
                pnum = struct.unpack_from("<H", maps, o + 38)[0] if mlen >= 40 else 0
                vol.partitions[ref] = {"number": pnum, "start": None, "length": 0,
                                       "contents": "*UDF Virtual Partition",
                                       "virtual": True}
            o += max(mlen, 6)
            ref += 1
    # FSD:按 LBA 顺序,最后一份代表当前状态
    for lba in sorted(ctx.tags.get(256, [])):
        try:
            sec = ctx.read_sector(lba)
        except (ReadUnavailable, OSError):
            continue
        if parse_tag(sec, lba) is None or struct.unpack_from("<H", sec, 0)[0] != 256:
            continue
        ln, lbn, pref = parse_long_ad(sec, 396)
        vol.fsds.append({"lba": lba, "root_lbn": lbn})
        if lbn:
            vol.root_lbn = lbn
    return vol


# ---------------------------------------------------------------- 扫描上下文

class ScanContext:
    """一次全盘扫描的收集器。"""

    def __init__(self, src, vol):
        self.src = src
        self.vol = vol
        self.tags = {}                # tag_id -> [lba, ...]
        self.tag_census = {}          # tag_id -> count
        self.fes = {}                 # lba -> fe dict
        self.fids = []                # 所有目录版本里的 FID
        self.dir_versions = {}        # dir uid -> [ {lba, fids:[idx...], mtime} ]
        self.signatures = {}
        self.pending_pds = []
        self.read_sector_cache = {}

    def read_sector(self, lba):
        if lba in self.read_sector_cache:
            return self.read_sector_cache[lba]
        sec = self.src.read(lba, 1)
        if len(self.read_sector_cache) > 4096:
            self.read_sector_cache.clear()
        self.read_sector_cache[lba] = sec
        return sec

    def note_tag(self, tid, lba):
        self.tag_census[tid] = self.tag_census.get(tid, 0) + 1
        if tid in (2, 5, 6, 256, 258, 261, 266, 9):
            self.tags.setdefault(tid, []).append(lba)

    def examine(self, lba, data):
        for i in range(0, len(data) - 15, SECTOR):
            sec = data[i:i + SECTOR]
            tid = struct.unpack_from("<H", sec, 0)[0]
            if tid < 1 or tid > 268:
                continue
            lba_here = lba + i // SECTOR
            tag = parse_tag(sec, lba_here)
            if tag is None:
                continue
            self.note_tag(tag["id"], lba_here)
            if tag["id"] in (261, 266):
                fe = parse_file_entry(sec, lba_here)
                if fe:
                    self.fes[lba_here] = fe
            elif tag["id"] in (5, 6, 2, 256):
                pass                  # 卷结构稍后统一解析

    def after_scan(self):
        """解析卷结构;读取所有目录 FE 的数据并解析 FID。"""
        self.vol = parse_volume_descriptors(self)
        # 目录条目 -> 解析 FID
        for lba, fe in sorted(self.fes.items()):
            if fe["file_type"] != 4:
                continue
            piece = b""
            ok = True
            for ext in fe["extents"]:
                if ext["lbn"] is None:              # in-icb
                    piece += fe["ea_bytes"] if False else ext.get("in_icb_data", b"")
                    continue
                if ext["flag"] in (1, 2):
                    continue
                if ext["flag"] == 3:
                    continue
                try:
                    abs_lba = self.vol.abs_lba(ext["lbn"], ext["part"] or 0,
                                               context_lba=lba)
                    nsec = max(1, (ext["len"] + SECTOR - 1) // SECTOR)
                    piece += self.src.read(abs_lba, nsec)[:ext["len"]]
                except (ReadUnavailable, OSError):
                    ok = False
            if not piece:
                continue
            start_lbn = None
            if fe["extents"] and fe["extents"][0]["lbn"] is not None:
                start_lbn = fe["extents"][0]["lbn"]
            before = len(self.fids)
            parse_fids_in_extent(piece, start_lbn, self.fids, (lba, fe["unique_id"]))
            if len(self.fids) > before:
                self.dir_versions.setdefault(fe["unique_id"], []).append(
                    {"lba": lba, "mtime": fe["mtime"],
                     "fid_idx": list(range(before, len(self.fids))),
                     "size": sum(e["len"] for e in fe["extents"] if e["lbn"] is not None)})

    # ---- 当前树 ----
    def walk_current(self):
        """从 FSD 根出发,返回 (当前可见条目列表, 可达 ICB 集合, 可达目录版本集合)。"""
        reachable_dirs = set()
        entries = []

        def walk(lbn, path, depth):
            if depth > 16 or lbn in reachable_dirs:
                return
            reachable_dirs.add(lbn)
            ctx_lba = self.vol.fsds[-1]["lba"] if self.vol.fsds else None
            fe = self.fes.get(self.vol.abs_lba(lbn, 0, context_lba=ctx_lba))
            if fe is None:
                return
            # 找到指向该 ICB 的 FID 列表(当前版本 = 该目录 UID 的最大 LBA 版本)
            versions = self.dir_versions.get(fe["unique_id"], [])
            if not versions:
                return
            newest = max(versions, key=lambda v: v["lba"])
            for idx in newest["fid_idx"]:
                f = self.fids[idx]
                if f["parent"]:
                    continue
                full = path + "/" + f["name"] if path else f["name"]
                state = "已删除" if f["deleted"] else "正常"
                entries.append({"path": full, "state": state, "deleted": f["deleted"],
                                "is_dir": f["is_dir"], "icb_lbn": f["icb_lbn"],
                                "dir_version_lba": newest["lba"]})
                if f["is_dir"] and not f["deleted"]:
                    try:
                        walk(f["icb_lbn"], full, depth + 1)
                    except (ReadUnavailable, OSError):
                        pass

        if self.vol.root_lbn is not None:
            try:
                walk(self.vol.root_lbn, "", 0)
            except (ReadUnavailable, OSError):
                pass
        return entries, reachable_dirs


# ---------------------------------------------------------------- 特征码

SIGNATURES = [
    (b"ftyp", "MP4/MOV"),
    (b"\x1aE\xdf\xa3", "Matroska/WebM"),
    (b"RIFF", "RIFF(AVI/WAV)"),
    (b"OggS", "Ogg"),
    (b"\x00\x00\x01\xba", "MPEG-PS"),
    (b"0&\xb2u\x8ef\xcf\x11", "ASF/WMV"),
]


def sniff_signatures(ctx, lba, data):
    for sig, label in SIGNATURES:
        start = 0
        cnt = 0
        while True:
            idx = data.find(sig, start)
            if idx < 0:
                break
            cnt += 1
            if len(ctx.signatures.get(label, [])) < 20:
                ctx.signatures.setdefault(label, []).append(lba * SECTOR + idx)
            start = idx + 1
        if cnt:
            ctx.signatures.setdefault("_count", {})
            ctx.signatures["_count"][label] = ctx.signatures.get("_count", {}).get(label, 0) + cnt


# ---------------------------------------------------------------- 命令

def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return "%.1f %s" % (n, u) if u != "B" else "%d B" % n
        n /= 1024.0


def cmd_info(args):
    src = open_source(args)
    max_lba = args.max_lba or (2304000 if src.kind == "device" else 10 ** 9)
    ctx, _runs, _sk = scan_source(src, max_lba, fast=not getattr(args, "full", False),
                                  chunk=args.chunk, skip=args.skip,
                                  stop_err_mb=args.stop_errors_mb, progress=True)
    print("== 卷信息 ==")
    print(ctx.vol.describe())
    print()
    print("== 描述符普查 ==")
    for tid, cnt in sorted(ctx.tag_census.items()):
        name = TAG_NAMES.get(tid, "?")
        first = ctx.tags.get(tid, [])
        extra = ""
        if first:
            extra = "  首个@LBA %s" % (", ".join(str(x) for x in first[:3]))
        print("  %-6s (tag %3d): %6d 个%s" % (name, tid, cnt, extra))
    src.close()


def fe_state(ctx, lba, fe, current_icbs):
    """判断一个文件条目的状态。"""
    names = []
    deleted_hits = 0
    live_hits = 0
    for f in ctx.fids:
        if f["icb_lbn"] is not None and ctx.vol.abs_lba(
                f["icb_lbn"], f["icb_part"] or 0,
                context_lba=f["dir_ref"][0]) == lba:
            names.append(f["name"])
            if f["deleted"]:
                deleted_hits += 1
            else:
                live_hits += 1
    if lba in current_icbs:
        return "当前", names, live_hits > 0 and deleted_hits == 0
    if deleted_hits and not live_hits:
        return "已删除", names, False
    if live_hits:
        return "历史版本", names, False
    return "孤儿(无目录表引用)", names, False


def cmd_history(args):
    src = open_source(args)
    max_lba = args.max_lba or (2304000 if src.kind == "device" else 10 ** 9)
    ctx, _runs, _sk = scan_source(
        src, max_lba, fast=not getattr(args, "full", False),
        preview=getattr(args, "preview", False),
        chunk=args.chunk, skip=args.skip, stop_err_mb=args.stop_errors_mb,
        progress=True,
        on_chunk=(lambda pos, data: sniff_signatures(ctx, pos, data))
        if args.scan_signatures else None,
        log=print)
    current_entries, current_icbs = ctx.walk_current()

    vol = ctx.vol
    print("== 卷信息 ==")
    print(vol.describe())
    print()

    print("== 当前文件树(最新目录表)==")
    if current_entries:
        for e in current_entries:
            print("  [%s] %s" % (e["state"], e["path"]))
    else:
        print("  (空 —— 和资源管理器里看到的一致)")
    print()

    print("== 全盘发现的文件/目录条目(%d 个 File Entry, %d 个目录表记录)=="
          % (len(ctx.fes), len(ctx.fids)))
    rows = []
    for lba, fe in sorted(ctx.fes.items()):
        state, names, _live = fe_state(ctx, lba, fe, current_icbs)
        rows.append((state, lba, fe, names))
    order = {"当前": 0, "历史版本": 1, "已删除": 2, "孤儿(无目录表引用)": 3}
    rows.sort(key=lambda r: (order.get(r[0], 9), r[1]))
    for state, lba, fe, names in rows:
        name_s = ",".join(n for n in names if n) or "(无名)"
        stub = "  [存根:条目内无区段]" if fe["ad_type"] == -1 and fe["info_len"] > 0 else ""
        print("  [%s] %-8s %-40s 大小 %10s  FE@LBA %-8d 修改时间 %s%s"
              % (state, fe["type_name"], name_s[:40], human(fe["info_len"]),
                 lba, fe["mtime"], stub))
    print()

    print("== 目录表里引用了、但文件条目本体没找到的记录 ==")
    found_ics = set()
    for lba, fe in ctx.fes.items():
        found_ics.add(lba)
    missing = []
    for f in ctx.fids:
        if f["parent"] or not f["name"]:
            continue
        try:
            abs_icb = ctx.vol.abs_lba(f["icb_lbn"], f["icb_part"] or 0,
                                      context_lba=f["dir_ref"][0])
        except (ReadUnavailable, OSError):
            abs_icb = None
        if abs_icb is None or abs_icb not in found_ics:
            missing.append(f)
    if missing:
        for f in missing:
            print("  [%s] %-40s ICB@LBN %-8d (目录@LBA %d)"
                  % ("已删除" if f["deleted"] else "正常", f["name"][:40],
                     f["icb_lbn"], f["dir_ref"][0]))
    else:
        print("  (无 —— 所有目录表引用都能对上文件条目)")
    print()

    print("== 目录版本历史(同一目录的多份旧表)==")
    if ctx.dir_versions:
        for uid, versions in sorted(ctx.dir_versions.items(), key=lambda kv: kv[1][0]["lba"]):
            versions.sort(key=lambda v: v["lba"])
            print("  目录 uid=%d,%d 个版本:" % (uid, len(versions)))
            for v in versions:
                names = [ctx.fids[i]["name"] for i in v["fid_idx"]
                         if not ctx.fids[i]["parent"] and ctx.fids[i]["name"]]
                dele = [n for n in names
                        if any(ctx.fids[i]["name"] == n and ctx.fids[i]["deleted"]
                               for i in v["fid_idx"])]
                print("    版本@LBA %-8d 时间 %s  条目: %s"
                      % (v["lba"], v["mtime"] or "?", ", ".join(names) if names else "(空)"))
            # 版本间差异 = 删除事件
            for i in range(len(versions) - 1):
                old_names = {ctx.fids[j]["name"] for j in versions[i]["fid_idx"]
                             if not ctx.fids[j]["parent"] and ctx.fids[j]["name"]}
                new_names = {ctx.fids[j]["name"] for j in versions[i + 1]["fid_idx"]
                             if not ctx.fids[j]["parent"] and ctx.fids[j]["name"]}
                gone = old_names - new_names
                if gone:
                    print("    >>> 删除事件: 版本@LBA %d -> %d 之间被移除: %s"
                          % (versions[i]["lba"], versions[i + 1]["lba"], ", ".join(sorted(gone))))
    else:
        print("  (没有发现任何目录表记录)")
    print()

    # 汇总删除事件: 任何旧目录表里有、而当前树里没有的条目
    cur_names = {e["path"].split("/")[-1] for e in current_entries}
    old_names = {f["name"] for f in ctx.fids if f["name"] and not f["parent"]}
    gone = old_names - cur_names
    if gone:
        print("== 删除事件汇总(旧目录表里有、当前文件树里没有)==")
        for n in sorted(gone):
            print("  已删除: %s" % n)
        print()

    if args.scan_signatures:
        print("== 数据特征码(全盘扫描)==")
        for sig, label in SIGNATURES:
            cnt = ctx.signatures.get("_count", {}).get(label, 0)
            offs = ctx.signatures.get(label, [])
            if cnt:
                ex = ", ".join("LBA %d" % (o // SECTOR) for o in offs[:5])
                print("  %-16s %8d 处   例: %s" % (label, cnt, ex))
        print()
    src.close()


def sanitize_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "unnamed"


def guess_ext(data_head):
    if len(data_head) >= 8 and data_head[4:8] == b"ftyp":
        return ".mp4"
    if data_head[:4] == b"\x1aE\xdf\xa3":
        return ".mkv"
    if data_head[:4] == b"RIFF":
        return ".avi"
    if data_head[:4] == b"OggS":
        return ".ogg"
    if data_head[:4] == b"\x00\x00\x01\xba":
        return ".mpg"
    if data_head[:4] == b"%PDF":
        return ".pdf"
    if data_head[:2] == b"PK":
        return ".zip"
    return ""


def read_file_data(src, vol, fe, progress_cb=None):
    """按 FE 的区段表读取文件数据,读不到的部分补 0。返回 (bytearray, 坏扇区数)。"""
    out = bytearray()
    bad = 0
    if fe["ad_type"] == 3:
        d = fe["extents"][0].get("in_icb_data", b"")
        out += d[:fe["info_len"]]
        if len(out) < fe["info_len"]:
            out += b"\x00" * (fe["info_len"] - len(out))
        return out, 0
    for ext in fe["extents"]:
        if len(out) >= fe["info_len"]:
            break
        if ext["flag"] in (1, 2):
            take = min(ext["len"], fe["info_len"] - len(out))
            out += b"\x00" * take
            continue
        if ext["flag"] == 3:
            continue                       # AD 续链,极少见
        abs_lba = vol.abs_lba(ext["lbn"], ext["part"] or 0, context_lba=fe["lba"])
        take = min(ext["len"], fe["info_len"] - len(out))
        off = 0
        while off < take:
            nsec = min(256, (take - off + SECTOR - 1) // SECTOR)
            try:
                data = src.read(abs_lba + off // SECTOR, nsec)
            except (ReadUnavailable, OSError):
                # 细粒度重试:32 扇区步进
                data = bytearray()
                for k in range(0, nsec, 32):
                    try:
                        data += src.read(abs_lba + off // SECTOR + k, min(32, nsec - k))
                    except (ReadUnavailable, OSError):
                        data += b"\x00" * (min(32, nsec - k) * SECTOR)
                        bad += min(32, nsec - k)
            w = min(len(data), take - off)
            out += data[:w]
            off += w
            if progress_cb and len(out) // (32 * 1024 * 1024) != (len(out) - w) // (32 * 1024 * 1024):
                progress_cb(len(out), fe["info_len"])
    if len(out) < fe["info_len"]:
        out += b"\x00" * (fe["info_len"] - len(out))
    return bytes(out[:fe["info_len"]]), bad


def cmd_recover(args):
    src = open_source(args)
    ctx = ScanContext(src, None)
    ctx.vol = Volume()
    max_lba = args.max_lba or (2304000 if src.kind == "device" else 10 ** 9)
    ctx, _runs, _sk = scan_source(src, max_lba, fast=not getattr(args, "full", False),
                                  chunk=args.chunk, skip=args.skip,
                                  stop_err_mb=args.stop_errors_mb, progress=True)
    _current, current_icbs = ctx.walk_current()

    # 候选集合
    cands = []
    for lba, fe in sorted(ctx.fes.items()):
        if fe["file_type"] == 4 and not args.include_dirs:
            continue
        state, names, _live = fe_state(ctx, lba, fe, current_icbs)
        cands.append({"lba": lba, "fe": fe, "state": state, "names": names})

    sel = []
    if args.fe:
        want = set(args.fe)
        sel = [c for c in cands if c["lba"] in want]
    elif args.name:
        pat = [n.lower() for n in args.name]
        sel = [c for c in cands
               if any(any(fnmatch.fnmatch(n.lower(), p) for p in pat) for n in c["names"])]
    elif args.all:
        sel = [c for c in cands if c["state"] != "当前"]
    else:
        print("未指定选择器:用 --all / --name \"模式\" / --fe LBA。候选列表:")
        for c in cands:
            print("  FE@%-8d [%s] %s  %s" % (c["lba"], c["state"],
                                             ",".join(c["names"]) or "(无名)",
                                             human(c["fe"]["info_len"])))
        src.close()
        return

    if not sel:
        print("没有匹配的条目。")
        src.close()
        return

    outdir = args.out or "recovered"
    os.makedirs(outdir, exist_ok=True)
    used = set()
    print("== 恢复 %d 个条目到 %s ==" % (len(sel), outdir))
    total_bad = 0
    for c in sorted(sel, key=lambda c: c["lba"]):
        fe = c["fe"]
        base = sanitize_name(c["names"][0]) if c["names"] and c["names"][0] else ""
        if not base:
            head = b""
            try:
                ext0 = fe["extents"][0]
                if ext0["lbn"] is not None:
                    head = src.read(ctx.vol.abs_lba(ext0["lbn"], ext0["part"] or 0,
                                                    context_lba=fe["lba"]), 1)
            except Exception:
                pass
            base = "orphan_%d%s" % (c["lba"], guess_ext(head))
        stem, dot, ext = base.rpartition(".")
        candidate = base
        n = 2
        while candidate.lower() in used:
            candidate = "%s__v%d%s" % (stem if dot else base, n, dot + ext if dot else "")
            n += 1
        used.add(candidate.lower())
        path = os.path.join(outdir, candidate)

        def prog(done, total, _name=candidate):
            sys.stdout.write("\r    %s : %d/%d MB" % (_name, done // 1048576, total // 1048576))
            sys.stdout.flush()

        try:
            data, bad = read_file_data(src, ctx.vol, fe, prog)
        except Exception as e:
            print("  [失败] FE@%d: %s" % (c["lba"], e))
            continue
        if not fe["extents"] and fe["info_len"] > 0:
            print("  [跳过] FE@%d (%s): 条目里没有区段信息 —— Windows VAT 式增量写入"
                  "把区段放在虚拟分配表里,本 FE 只是存根;数据通常仍物理存在于盘上,"
                  "可用 scan 的特征码定位后按扇区手工提取" % (c["lba"], candidate))
            continue
        sys.stdout.write("\n")
        with open(path, "wb") as fh:
            fh.write(data)
        total_bad += bad
        note = ""
        if bad:
            note = "  (含 %d 个不可读扇区,以 0 填充)" % bad
        if len(data) != fe["info_len"]:
            note += "  [警告:预期 %d 字节]" % fe["info_len"]
        print("  [OK] %-44s %10s  FE@LBA %d  [%s]%s"
              % (candidate[:44], human(len(data)), c["lba"], c["state"], note))
    print("完成。不可读扇区总计: %d" % total_bad)
    src.close()


def cmd_scan(args):
    src = open_source(args)
    max_lba = args.max_lba or (2304000 if src.kind == "device" else 10 ** 9)
    ctx, runs, _sk = scan_source(
        src, max_lba, fast=not getattr(args, "full", False),
        chunk=args.chunk, skip=args.skip, stop_err_mb=args.stop_errors_mb,
        on_chunk=lambda pos, data: sniff_signatures(ctx, pos, data),
        log=print)
    print("== 可读区地图 ==")
    for a, b in runs:
        print("  LBA %8d - %8d   (%.1f MB)" % (a, b - 1, (b - a) * SECTOR / 1048576))
    print()
    print("== 描述符普查 ==")
    for tid, cnt in sorted(ctx.tag_census.items()):
        print("  %-6s (tag %3d): %6d 个" % (TAG_NAMES.get(tid, "?"), tid, cnt))
    print()
    print("== 数据特征码 ==")
    any_sig = False
    for sig, label in SIGNATURES:
        cnt = ctx.signatures.get("_count", {}).get(label, 0)
        offs = ctx.signatures.get(label, [])
        if cnt:
            any_sig = True
            ex = ", ".join("LBA %d" % (o // SECTOR) for o in offs[:6])
            print("  %-16s %8d 处   例: %s" % (label, cnt, ex))
    if not any_sig:
        print("  (未发现已知媒体文件特征码)")
    print()
    print("== 文件条目 ==")
    _cur, current_icbs = ctx.walk_current()
    for lba, fe in sorted(ctx.fes.items()):
        state, names, _ = fe_state(ctx, lba, fe, current_icbs)
        print("  FE@%-8d [%s] %-8s %-40s %10s" %
              (lba, state, fe["type_name"], (",".join(names) or "(无名)")[:40],
               human(fe["info_len"])))
    src.close()


def cmd_image(args):
    out = args.out or "disc.img"
    src = open_source(args)
    max_lba = args.max_lba or (2304000 if src.kind == "device" else 10 ** 9)
    runs = []
    total = 0
    with open(out, "wb") as fh:
        for pos, data in iter_readable_chunks(src, max_lba, args.chunk, args.skip,
                                              args.stop_errors_mb, quiet=True,
                                              runs_out=runs):
            fh.write(data)
            total += len(data)
            sys.stdout.write("\r    已写入 %.1f MB" % (total / 1048576))
            sys.stdout.flush()
    sys.stdout.write("\n")
    with open(out + ".map.json", "w", encoding="utf-8") as fh:
        json.dump(runs, fh)
    print("[OK] 镜像: %s (%.1f MB, %d 个可读区段) + %s.map.json"
          % (out, total / 1048576, len(runs), out))
    src.close()


def main():
    ap = argparse.ArgumentParser(
        description="UDF 光盘删除历史查看与文件恢复(纯标准库)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python udf_recover.py info\n"
               "  python udf_recover.py history\n"
               "  python udf_recover.py recover --all --out recovered\n"
               "  python udf_recover.py image --out disc.img\n"
               "  python udf_recover.py history --image disc.img\n")
    ap.add_argument("--device", default=r"\\.\E:", help="光驱设备路径,默认 \\\\.\\E:")
    ap.add_argument("--image", help="改用镜像文件(由 image 子命令生成)")
    ap.add_argument("--max-lba", type=int, default=None, help="扫描上限(LBA)")
    ap.add_argument("--chunk", type=int, default=512, help="每次读取扇区数(默认512,即1MB)")
    ap.add_argument("--skip", type=int, default=32, help="读错误时前跳扇区数(默认32)")
    ap.add_argument("--stop-errors-mb", type=int, default=96,
                    help="连续不可读超过多少 MB 就停止(默认96)")
    ap.add_argument("--full", action="store_true",
                    help="关闭快速模式: 完整扫过整个可读区(默认会跳过已识别的文件数据区)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(p):
        """子命令里也接受全局参数;SUPPRESS 避免覆盖主解析器已收到的值。"""
        p.add_argument("--device", default=argparse.SUPPRESS,
                       help="光驱设备路径,默认 \\\\.\\E:")
        p.add_argument("--image", default=argparse.SUPPRESS,
                       help="改用镜像文件(由 image 子命令生成)")
        p.add_argument("--max-lba", type=int, default=argparse.SUPPRESS,
                       help="扫描上限(LBA)")
        p.add_argument("--chunk", type=int, default=argparse.SUPPRESS,
                       help="每次读取扇区数(默认256)")
        p.add_argument("--skip", type=int, default=argparse.SUPPRESS,
                       help="读错误时前跳扇区数(默认32)")
        p.add_argument("--stop-errors-mb", type=int, default=argparse.SUPPRESS,
                       help="连续不可读超过多少 MB 就停止(默认96)")
        p.add_argument("--full", action="store_true", default=argparse.SUPPRESS,
                       help="关闭快速模式: 完整扫过整个可读区(最保守,"
                            "默认快速模式会跳过已识别的文件数据区)")

    p = sub.add_parser("info", help="解析卷结构")
    add_common(p)
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("history", aliases=["list"], help="全盘扫描文件表,列出删除历史")
    p.add_argument("--scan-signatures", action="store_true", help="同时做媒体特征码扫描")
    p.add_argument("--preview", action="store_true",
                   help="预览模式: 只读卷头+卷尾(几秒),可能漏掉中段旧文件表")
    add_common(p)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("recover", help="恢复文件")
    p.add_argument("--all", action="store_true", help="恢复所有非当前条目(删除/历史/孤儿)")
    p.add_argument("--name", nargs="+", help="按文件名通配符选择(如 *.mp4)")
    p.add_argument("--fe", type=int, nargs="+", help="按 FE 所在 LBA 选择")
    p.add_argument("--out", default="recovered", help="输出目录(默认 recovered)")
    p.add_argument("--include-dirs", action="store_true", help="也导出目录数据")
    add_common(p)
    p.set_defaults(func=cmd_recover)

    p = sub.add_parser("scan", help="全盘原始扫描:可读区地图 + 特征码 + 文件条目")
    add_common(p)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("image", help="把可读区转储为镜像文件")
    p.add_argument("--out", default="disc.img", help="镜像输出路径")
    add_common(p)
    p.set_defaults(func=cmd_image)

    args = ap.parse_args()
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args.func(args)


if __name__ == "__main__":
    main()

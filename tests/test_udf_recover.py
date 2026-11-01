# -*- coding: utf-8 -*-
"""udf_recover 的功能测试:在内存里合成一个迷你 UDF 镜像,跑完整解析/恢复流程。

镜像布局(分区起始 LBA = 40):
    16-18  卷识别序列 (BEA01/NSR03/TEA01)
    20     LVD   (分区映射表 @440, type-1)
    21     PD    (start=40, length=2000)
    40     FSD   (根目录 ICB -> LBN 2 = 绝对 42)
    41     根目录数据(FID 链: ".." + hello.txt + big.bin)
    42     根目录 FE (short_ad -> LBN 1 = 绝对 41)
    44     hello.txt FE (short_ad -> LBN 5 = 绝对 45, 16B)
    45     hello.txt 数据
    46     big.bin FE   (short_ad -> LBN 7 = 绝对 47, 204800B)
    47-146 big.bin 数据
    256    AVDP(MVDS 指针清零 —— 仿 Windows LiveFS 行为)

既测纯解析函数,也测 scan_source 全流程与快速模式的跳区行为。
"""

import json
import os
import struct
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import udf_recover as core

PART_START = 40
HELLO = b"HELLO_UDF_TEST!\x00"
BIG_LEN = 204800                       # 100 扇区,触发快速模式跳区(阈值 64KB)


# ---------------------------------------------------------------- 构造工具

def mktag(tid, ver, loc, serial=1):
    b = struct.pack("<HHBBHHHI", tid, ver, 0, 0, serial, 0, 0, loc)
    cks = sum(b) & 0xFF                # 此时字节4为 0,sum 即"除校验字节外"的和
    return struct.pack("<HHBBHHHI", tid, ver, cks, 0, serial, 0, 0, loc)


def ts_bytes(year=2026, mo=11, d=1):
    return struct.pack("<HhBBBBBB", 0x1000, year, mo, d, 12, 0, 0, 0)


def long_ad(length, lbn, pref=0):
    return struct.pack("<IIH", length, lbn, pref) + b"\x00" * 6


def fe_sector(tid, loc, file_type, info_len, extent_lbn, extent_len, uid=0,
              l_ea=0, l_ad=8):
    """按 IMAPI2 实测布局构造一个 File Entry:uid@160/L_EA@168/L_AD@172/AD@176。"""
    s = bytearray(2048)
    s[0:16] = mktag(tid, 2 if tid == 261 else 3, loc)
    s[16:20] = struct.pack("<I", 0)              # priorRecordedNumberOfDirectEntries
    s[20:22] = struct.pack("<H", 4)              # strategyType
    s[24:26] = struct.pack("<H", 1)              # maximumNumberOfEntries
    s[27] = file_type                            # fileType
    s[28:34] = b"\x00" * 6                       # parentICBLocation
    s[34:36] = struct.pack("<H", 0)              # flags: ad_type = 0 (short_ad)
    s[36:40] = b"\xff\xff\xff\xff"               # gid
    s[40:44] = b"\xff\xff\xff\xff"               # permissions
    s[48:50] = struct.pack("<H", 1)              # fileLinkCount
    s[56:64] = struct.pack("<Q", info_len)       # informationLength
    s[64:72] = struct.pack("<Q", (info_len + 2047) // 2048)  # logicalBlocksRecorded
    s[84:96] = ts_bytes()                        # modificationTime
    s[96:108] = ts_bytes()                       # attributeTime
    s[108:112] = struct.pack("<I", 1)            # checkpoint
    s[160:168] = struct.pack("<Q", uid)          # uniqueID
    s[168:172] = struct.pack("<I", l_ea)         # lengthOfExtendedAttributes
    s[172:176] = struct.pack("<I", l_ad)         # lengthOfAllocationDescriptors
    s[176:184] = struct.pack("<II", extent_len, extent_lbn)  # short_ad
    return bytes(s)


def fid_bytes(loc, chars, name, icb_lbn):
    """构造一个 FID(此写入器风格:8 位名不带 CRC)。"""
    name_b = b"\x08" + name.encode("latin-1")
    idlen = len(name_b)
    total = (38 + idlen + 3) & ~3
    f = bytearray(total)
    f[0:16] = mktag(257, 2, loc)
    f[16:18] = struct.pack("<H", 1)              # fileVersionNumber
    f[18] = chars                                # fileCharacteristics
    f[19] = idlen                                # fileIdentifierLength
    f[20:36] = long_ad(2048, icb_lbn, 0)         # ICB
    f[38:38 + idlen] = name_b
    return bytes(f)


def build_image():
    """返回 (300 扇区的镜像 bytes, 期望的 big.bin 内容)。"""
    img = bytearray(300 * 2048)

    def put(lba, data):
        img[lba * 2048:lba * 2048 + len(data)] = data

    # 卷识别序列
    put(16, b"BEA01" + b"\x00" * 2043)
    put(17, b"NSR03" + b"\x00" * 2043)
    put(18, b"TEA01" + b"\x00" * 2043)

    # LVD @20:blockSize、contentsUse、分区映射表 @440
    lvd = bytearray(2048)
    lvd[0:16] = mktag(6, 3, 20)
    lvd[212:216] = struct.pack("<I", 2048)                   # logicalBlockSize
    lvd[248:264] = long_ad(2048, 0, 0)                       # contentsUse -> FSD LBN 0
    lvd[264] = 6                                             # mapTableLength
    lvd[265] = 1                                             # numberOfPartitionMaps
    lvd[440:446] = b"\x01\x06\x00\x01\x00\x00"               # type-1 map, pnum 0
    put(20, bytes(lvd))

    # PD @21:分区 0,start=40
    pd = bytearray(2048)
    pd[0:16] = mktag(5, 3, 21)
    pd[22:24] = struct.pack("<H", 0)                         # partitionNumber
    pd[24:32] = b"\x00+NSR03"                                # partitionContents
    pd[184:188] = struct.pack("<I", 1)                       # accessType: read only
    pd[188:192] = struct.pack("<I", PART_START)              # partitionStartingLocation
    pd[192:196] = struct.pack("<I", 2000)                    # partitionLength
    put(21, bytes(pd))

    # AVDP @256:MVDS 指针清零(仿 Windows LiveFS)
    avdp = bytearray(2048)
    avdp[0:16] = mktag(2, 2, 256)
    put(256, bytes(avdp))

    # FSD @40:根目录 ICB -> LBN 2
    fsd = bytearray(2048)
    fsd[0:16] = mktag(256, 3, PART_START)
    fsd[300:332] = b"\x08" + b"test-disc".ljust(31, b"\x00")[:31]  # fileSetIdent
    fsd[396:412] = long_ad(2048, 2, 0)
    put(PART_START, bytes(fsd))

    # 根目录数据 @41:FID 链
    fids = (fid_bytes(1, 0x0A, "", 2)                 # ".."  (目录|父)
            + fid_bytes(1, 0x00, "hello.txt", 4)
            + fid_bytes(1, 0x00, "big.bin", 6))
    put(PART_START + 1, fids)

    # 根目录 FE @42
    put(PART_START + 2, fe_sector(261, PART_START + 2, 4, len(fids), 1, len(fids)))

    # hello.txt:FE @44,数据 @45
    put(PART_START + 4, fe_sector(261, PART_START + 4, 5, len(HELLO), 5, len(HELLO), uid=1))
    put(PART_START + 5, HELLO)

    # big.bin:FE @46,数据 @47 起 100 扇区
    big = bytes((i * 7 + 3) % 251 for i in range(BIG_LEN))
    put(PART_START + 6, fe_sector(261, PART_START + 6, 5, BIG_LEN, 7, BIG_LEN, uid=2))
    put(PART_START + 7, big)

    return bytes(img), big


@pytest.fixture(scope="module")
def image_env(tmp_path_factory):
    img, big = build_image()
    d = tmp_path_factory.mktemp("synthetic")
    img_path = d / "test.img"
    img_path.write_bytes(img)
    (d / "test.img.map.json").write_text(json.dumps([[0, 300]]))
    return str(img_path), big


# ---------------------------------------------------------------- 纯解析测试

def test_tag_checksum():
    from udf_recover import parse_tag
    good = mktag(261, 2, 42) + b"\x00" * 2032
    assert parse_tag(good, 42) is not None
    bad = bytearray(good)
    bad[4] ^= 0xFF
    assert parse_tag(bytes(bad), 42) is None
    tid_only = mktag(999, 2, 0) + b"\x00" * 2032
    assert parse_tag(tid_only, 0) is None            # tid 越界


def test_abs_lba_generations():
    v = core.Volume()
    v.partitions = {0: {"number": 0, "start": 40, "length": 2000,
                        "contents": "+NSR03", "virtual": False}}
    v.pd_starts = [40]
    assert v.abs_lba(2, 0, context_lba=40) == 42
    # 两代分区定义:描述符落在旧代时用旧基址
    v.pd_starts = [40, 1000]
    v.partitions[0]["start"] = 1000
    assert v.abs_lba(2, 0, context_lba=40) == 42     # 旧描述符 -> 旧基址
    assert v.abs_lba(2, 0, context_lba=1000) == 1002  # 新描述符 -> 新基址


def test_fe_layout_detection():
    from udf_recover import parse_file_entry
    sec = fe_sector(261, 44, 5, 16, 5, 16, uid=1)
    fe = parse_file_entry(sec, 44)
    assert fe is not None and fe["info_len"] == 16
    assert fe["extents"][0]["lbn"] == 5 and fe["extents"][0]["len"] == 16
    assert fe["ad_type"] == 0 and fe["file_type"] == 5


# ---------------------------------------------------------------- 全流程测试

def test_scan_and_recover(image_env, tmp_path):
    img_path, big = image_env
    ns = types_ns(image=img_path)
    src = core.open_source(ns)
    try:
        ctx, runs, skipped = core.scan_source(src, 300, fast=True, chunk=4)
        # 分区与卷
        assert ctx.vol.partitions.get(0, {}).get("start") == PART_START
        # 三个 FE 全部找到
        assert set(ctx.fes) == {PART_START + 2, PART_START + 4, PART_START + 6}
        # 目录表解出两个文件名
        names = {f["name"] for f in ctx.fids if f["name"]}
        assert names == {"hello.txt", "big.bin"}
        # 当前树可走通
        entries, _icbs = ctx.walk_current()
        paths = {e["path"] for e in entries}
        assert paths == {"hello.txt", "big.bin"}
        # 快速模式确实跳过了 big.bin 的数据扇区(单位:扇区;
        # 首块已含第 47 扇区,故为 99 而非 100)
        assert skipped >= 99
        # 恢复两个文件并校验内容
        out = tmp_path / "rec"
        out.mkdir()
        for lba, expect, fname in ((PART_START + 4, HELLO, "hello.txt"),
                                   (PART_START + 6, big, "big.bin")):
            data, bad = core.read_file_data(src, ctx.vol, ctx.fes[lba])
            assert data == expect and bad == 0
            (out / fname).write_bytes(data)
        assert (out / "hello.txt").read_bytes() == HELLO
        assert (out / "big.bin").read_bytes() == big
    finally:
        src.close()


def test_full_scan_same_result(image_env):
    """fast 与 full 模式必须得到相同的条目集合。"""
    img_path, _ = image_env
    res = []
    for fast in (True, False):
        src = core.open_source(types_ns(image=img_path))
        try:
            ctx, _runs, _sk = core.scan_source(src, 300, fast=fast, chunk=4)
            res.append((set(ctx.fes),
                        {f["name"] for f in ctx.fids if f["name"]}))
        finally:
            src.close()
    assert res[0] == res[1]


def types_ns(image):
    import types as _t
    return _t.SimpleNamespace(device=None, image=image)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

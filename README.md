# udf-recover

[![CI](https://github.com/ohto-ai/udf-recover/actions/workflows/ci.yml/badge.svg)](https://github.com/ohto-ai/udf-recover/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.8%2B-blue.svg)](https://www.python.org/)

**English:** View the deletion history of UDF "Live File System" optical discs (Windows-burned DVD±R / CD-R) and recover "deleted" files — deletion on write-once media only rewrites metadata; the old file tables and file data remain physically on the disc.

---

用 Windows「活动文件系统 (Live File System)」刻录的可写光盘(尤其是一次性
DVD±R / CD-R)上,"删除文件"只是改写了 UDF 元数据:**旧目录表、旧文件条目
和文件数据都还物理留在盘上**。本工具绕过文件系统当前状态,直接解析盘上的
全部 UDF 描述符,把删除历史完整还原出来,并按区段表恢复数据。

纯 Python 标准库,**零第三方依赖**;带 tkinter 图形界面与合成镜像测试套件。

## ✨ 功能

- 🔍 **删除历史查询** — 全盘扫描文件表,列出:当前文件 / 历史版本 / 已删除
  条目 / 孤儿条目,并自动汇总"删除事件"
- 🛠 **文件恢复** — 按文件条目里的区段表提取数据(支持一次性 DVD±R / CD-R)
- 💿 **整盘镜像** — 可读区一次性转储 + 区段地图,之后所有操作离线秒级完成
- ⚡ **快速模式** — 边扫边跳过已识别的文件数据区(293MB 实测只读 124MB,
  结果与完整扫描逐行一致;数据越满加速越明显)
- 🧯 **自适应读错误** — 瞬时失败细步长回探边界,慢失败大步长 + 预算制,
  划伤/半空盘也能扫
- 🖥 **GUI + CLI** — tkinter 三步操作,或命令行管道式使用
- 🧪 **可测试** — 内置合成 UDF 镜像测试套件,CI 无需任何硬件

## 🚀 快速开始

```bash
git clone https://github.com/ohto-ai/udf-recover.git
cd udf-recover
python udf_recover_gui.py        # 图形界面(推荐)
```

或仅用命令行:

```bash
python udf_recover.py image --out disc.img     # ① 新盘先做镜像(几分钟)
python udf_recover.py history --image disc.img # ② 查删除历史(秒级,离线)
python udf_recover.py recover --image disc.img --all --out recovered  # ③ 恢复
```

<details>
<summary><b>命令行完整用法</b></summary>

```text
用法: udf_recover.py [--device \\.\E: | --image disc.img] [--full] <子命令>

  info     解析卷结构(UDF 版本、分区、文件集位置)
  history  全盘扫描文件表,列出删除历史(别名 list)
  recover  恢复文件(--all / --name "*.mp4" / --fe 292)
  scan     全盘原始扫描:可读区地图 + 媒体特征码 + 文件条目
  image    把可读区转储为镜像文件 + .map.json

全局参数:--device(光驱设备路径)、--image(镜像)、--max-lba、
--chunk、--skip、--stop-errors-mb、--full(关闭快速模式)
全局参数放在子命令前或后均可。
```

</details>

## 🖥 GUI 三步操作

1. **选数据源**:插好盘选「光驱」(默认 `\\.\E:`),或选已有 `.img` 镜像
2. **① 生成整盘镜像 → ② 扫描/查删除历史**:表格列出全部条目,
   `[历史版本]` 即被删除的文件;日志区汇总"已删除"名单
3. **③ 恢复所选**:按住 Ctrl 多选行,进度条实时显示,完成后一键打开输出目录

## 🔬 工作原理

为什么"删除"的文件还能找回来,以及为什么必须扫盘:

1. **指针被清零**:Windows LiveFS 把 AVDP 锚点里的卷描述符指针写成 0,
   正常 UDF 的"锚点 → 卷描述符 → 文件集"指针链断了,只能扫描定位;
2. **已删文件表天生无指针**:删除 = 从当前目录表抹掉条目,旧目录表与旧
   文件条目从此无人引用,唯一可靠的办法是逐扇区识别描述符标签
   (16 字节带校验和,误报率 ≈ 0);
3. **慢在光驱不在解析**:USB 光驱持续读速 ~4.6MB/s(块 ≥128KB;64KB 会掉到
   1.9MB/s),而 15 万扇区的解析不到 3 秒。另有两大坑:光驱闲置会停转,
   冷启动后首笔读可能要 1-2 分钟(工具已做显式唤醒);扫描至可读区尽头时
   对不可读区的试探可能触发设备级慢失败(工具改为先二分探测可读上界)。

**快速模式的依据**:一次性介质的区段互不重叠——文件条目声明了
"我的数据在 LBA X、长 N",这一段就绝不可能再藏别的文件表,
因此识别出一个文件就整段跳过它的数据。解析过程中的所有非教科书偏移
(标签校验和、FE/EFE 字段布局、分区跨代重定义、VAT 存根等)均已实测
适配,细节见源码内注释与 `tests/test_udf_recover.py`。

## 📊 实测案例

一次性 DVD±R(TSSTcorp TS-L633C,USB)+ Windows 10 LiveFS:

- 盘面 4.7GB,已用 ~310MB,资源管理器显示根目录为空
- 扫出 **4 份历史目录表、13 个文件条目**,还原删除事件:
  两个视频(`5f5af538….mp4` 59.3MB、`b7220fe3….mp4` 109.7MB)被删除
- 两个视频完整恢复,SHA-256 与盘面原始扇区逐字节一致,MP4 box 结构完好
  可直接播放;快速模式跳过 168.6MB 已识别数据区

> ⚠️ 反过来提醒:在一次性盘上"删除"≠销毁,数据只能靠物理破坏盘片清除。

## 🧪 测试

测试在内存中合成一个迷你 UDF 镜像(含卷结构、目录表、文件区段),
不需要光驱与盘片:

```bash
pip install pytest
pytest -q
```

覆盖:标签校验、跨代分区基址选择、FE 布局自动探测、
全流程扫描/恢复、快速模式跳区行为、fast 与 full 结果一致性。

## ⚠️ 已知限制

- UDF 1.50 VAT(虚拟分配表)的条目语义未完全逆向:Windows 在追加写入时
  会把新文件条目写成"存根"(FE 内无区段),这类条目会明确标注
  `[存根:条目内无区段]`,其数据通常仍在盘上,可用 `scan` 的特征码定位
- 仅支持 Windows(依赖 `\\.\X:` 设备路径语义);镜像文件分析理论上
  跨平台,未验证
- BD-R / HD DVD 未测试;BD-RE / DVD-RAM 等可覆写介质的删除是真删除,
  不在本工具的目标场景内

## 🤝 贡献

Issue / PR 欢迎欢迎!尤其是:其他刻录软件(ImgBurn、Nero InCD 等)产出的
UDF 盘布局样本、VAT 条目语义的逆向、Linux/macOS 镜像分析适配。

## 📄 License

[MIT](LICENSE)

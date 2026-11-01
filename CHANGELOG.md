# Changelog

All notable changes to this project will be documented in this file.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.0.0] - 2026-11-01

### Added
- UDF 卷结构解析:支持 Windows「活动文件系统 (Live File System)」在
  DVD±R / CD-R 上的实际布局(教科书 ECMA-167 之外的多处私有偏差均已适配)
- `history`:全盘扫描文件表,列出当前文件、历史版本、已删除条目、孤儿条目,
  并汇总删除事件
- `recover`:按区段表恢复文件(--all / --name / --fe 三种选择方式)
- `scan`:全盘原始扫描(可读区地图、媒体特征码普查、文件条目)
- `image`:整盘可读区转储为镜像 + 区段地图,后续操作全部离线
- tkinter 图形界面:三步操作,后台线程扫描,实时进度与日志
- 快速模式:边扫边跳过已识别的文件数据区(一次性介质区段互不重叠),
  实测 293MB 盘只读 124MB,结果与完整扫描一致
- 自适应读错误处理:瞬时失败细步长回探、慢失败 8MB 大步长 + 预算制
- 合成 UDF 镜像测试套件(无需光驱/盘片即可在 CI 运行)
- 实战验证:从一次性 DVD±R 恢复两个被删视频共 169MB,
  SHA-256 与盘面原始数据逐字节一致

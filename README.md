# SSAD_HW2 · MiniRM

本地需求管理系统，支持需求录入、内容版本、独立评审、冻结基线、批量变更、追踪验证、成员权限与审计。

## 运行

需要 Python 3.10 或以上版本，无第三方运行依赖。

```bash
cd mini-rm
python server.py
```

浏览器访问 `http://127.0.0.1:8765`。Windows 也可以双击 `mini-rm/启动MiniRM.cmd`。按 Ctrl+C 停止服务；数据库在首次启动时自动初始化。

演示账户为 `analyst`、`reviewer`、`developer`、`admin`，默认密码为 `MiniRM2026!`。它们分别负责需求编辑、独立审批、工件登记与成员管理。服务默认只监听本机地址；首次初始化前可用 `MINIRM_DEMO_PASSWORD` 自定义演示密码。

## 测试与备份

在 `mini-rm` 目录执行：

```bash
python -m unittest discover -s tests -v
python tools/backup.py
```

测试使用独立临时数据库，覆盖业务流程、事务回滚、历史冻结、权限、HTTP 与生成代码门禁。备份使用 SQLite backup API，保存到本地 `backups` 目录。

## 智能体功能

界面内提供四角色的需求审查流程，默认使用离线规则。外部模型模式通过 `MINIRM_LLM_API_KEY`、`MINIRM_LLM_BASE_URL` 和 `MINIRM_LLM_MODEL` 配置；密钥只从环境读取。

六角色代码生成与审查工具接受用户自行提供的 UTF-8 任务文件：

```bash
python tools/development_pipeline.py --task-file /path/to/task.txt
```

代码候选只写入隔离目录，不自动执行或覆盖正式源码。外部模型调用需要自行配置并验证。

## 项目结构

```text
mini-rm/
  server.py       HTTP、会话、CSRF 与事务边界
  schema.sql      数据模型与历史保护
  minirm/         领域规则与智能体编排
  static/         管理界面
  tests/          自动化测试
  tools/          代码协作与备份工具
```

仓库只包含项目代码与运行说明。作业文章、Word 报告、截图、数据库、备份、密钥和本地运行日志均不上传。

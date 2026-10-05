# 全球健康创新试点运营服务

这是一个面向健康科技展会、临床合作机构、康复机构和产业伙伴的 Python 后端。服务使用 FastAPI 与 SQLite 管理健康创新产品、试点场地、证据材料、公众体验反馈、参数化体验方案、排队场次、站点租约、失败恢复、观察记录版本和人工干预。所有运行状态保存在单个本地数据库文件中，不需要另行部署数据库、缓存、消息队列或浏览器界面。

## 已有能力

- 产品目录：登记来源国家、所属机构、产品类别、用途、风险级别与当前合规状态。
- 场地目录：维护展会体验点、医院、康复机构、研究机构和产业伙伴的能力与并发上限。
- 证据材料：按产品保存临床、性能、安全、合规和体验材料，使用内容摘要实现重复提交幂等，并支持接受或驳回。
- 体验反馈：按场地、场次引用和受众类型保存评分、标签、意见以及后续联系授权，重复反馈不会创建第二条记录。
- 体验方案：使用参数规则描述外骨骼、辅助诊断、数字疗法、慢病管理和数字中医等设备或服务的运行边界。
- 场次调度：提交方按项目和幂等键创建场次，执行站点按能力领取并获得有期限的租约。
- 执行回执：站点可以续租、提交观察记录或报告失败；可重试失败按照确定的退避时间重新排队。
- 失败恢复：租约到期后由恢复入口重新排队，达到最大尝试次数的场次转为失败。
- 人工干预：取消、人工重试、优先级调整和批量操作均保留操作者、原因、前后状态与批次标识。
- 身份审计：保留用户、角色、团队、会话、权限、审计事件和后台维护能力，敏感凭据只保存摘要。
- 用途授权：按告知版本保存参与者对采集、生成即时结论、内部改进、向指定合作方共享和后续联系五个用途的分别决定；支持监护人代签、授权有效期、部分撤回、跨场地重放收敛和撤回后派生记录的保留、隔离与删除队列。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `./data/health-innovation.db`。可复制 `.env.example`，并通过 `HEALTH_INNOVATION_DATABASE_PATH` 指定其他本地文件。

## 数据库初始化与检查

```bash
python -m app.cli init-db
python -m app.cli check-db
python tools/verify_sqlite.py
```

## 启动 API

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

产品、场地、证据和体验反馈接口使用 `/api/catalog` 前缀，体验方案、场次、领取、回执与恢复接口使用 `/api/pilots` 前缀，用途授权接口使用 `/api/consent` 前缀。

## 用途授权链路

1. 隐私负责人通过 `POST /api/consent/notices` 发布告知版本，版本绑定本次告知覆盖的用途清单与正文摘要。
2. 参与者（或监护人，需提供关系与法定依据）通过 `POST /api/consent/grants` 按用途分别选择允许或拒绝，可指定有效期、产品、场地和场次引用；`partner_sharing` 必须指定合作方编码。决定按参与者摘要、告知版本、范围和用途选择计算 `grant_key`，重复请求、跨场地重放和进程崩溃重试都只形成同一个最终决定。
3. 场次提交门控 `collect`，观察报告完成门控 `instant_report`，反馈提交门控 `internal_improvement` 和 `follow_up_contact`，合作方导出 `POST /api/catalog/feedback/partner-export/{product_code}` 门控 `partner_sharing`。
4. 告知版本新增用途后，旧签署不含该用途行，必须用新版本重新签署，不能沿用旧决定。
5. `POST /api/consent/withdrawals` 支持整体或按用途撤回：尚未开始的场次立即取消，运行中场次进入取消请求；已产生的反馈、观察报告等派生记录按来源用途进入保留（附保存义务与保留期限）、隔离或删除队列，由 `POST /api/consent/dispositions/run` 执行。
6. `GET /api/consent/subjects/{subject_digest}/summary` 只返回各用途最新状态与依据版本的必要摘要；`GET /api/consent/grants/{grant_key}`（需 `consent.read` 权限）向审计人员展示告知版本、授权主体、监护人信息、用途决定和全部事件流水，可从任意一次允许或拒绝完成追溯。
7. `POST /api/consent/expiry/run` 巡检有效期届满的授权并触发同样的撤回效果。

## 测试

```bash
python -m pytest
```

测试覆盖身份与审计、产品和场地登记、证据重复提交、证据审阅、反馈幂等、参数校验、场次提交、优先级领取、能力匹配、租约续期、失败退避、观察版本、取消、人工重试、批量操作和租约恢复，以及用途授权的告知版本、监护人代签、五用途分别决定、新增用途重新签署、幂等重放、业务门控、有效期、撤回处置和审计追溯。

## 编译检查

```bash
python -m compileall -q app tests tools
```

## API 与命令行冒烟

```bash
python -m app.cli smoke
python -m app.cli pilot-demo
```

`smoke` 在进程内检查根路径和健康接口。`pilot-demo` 会登记一个康复设备和体验场地，创建外骨骼步态体验方案，提交并领取场次，用于快速确认目录与试点运营链路。

## 目录结构

```text
app/
  catalog/          健康产品、试点场地、证据材料与公众反馈
  pilots/           体验方案、场次、租约、观察记录和人工干预
  consent/          告知版本、用途级授权决定、撤回与派生记录处置队列
  api/              用户、角色、团队、认证、审计和系统接口
  core/             时钟、安全、隐私、异常与分页
  repositories/     身份、审计和团队数据访问
  services/         身份、后台任务、维护和通用服务
  cli.py            初始化、检查和冒烟入口
  database.py       SQLite 连接、事务、表结构与基础权限
tests/               核心、目录、试点运营和身份回归测试
tools/               数据库完整性检查
```

## 数据一致性

SQLite 连接启用外键、WAL、busy timeout 和同步写入策略。产品目录、证据审阅、反馈提交、场次领取、执行回执与人工干预使用即时事务；领取通过条件更新避免同一场次被重复分配。服务保存 UTC 时间字符串，测试可注入固定时钟验证退避、租约到期和跨日配额。审计记录会清理密码、令牌等敏感字段，体验反馈仅保存联系人摘要和是否允许后续联系。

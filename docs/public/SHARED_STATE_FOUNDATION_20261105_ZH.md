# 容量阶段二A：共享状态与执行租约底座

日期：2026-11-05。基线`80f08e7`。
**独立PostgreSQL后端已实现并通过真实数据库/多进程契约验证，未接入正式API。**
这不是上线切换、完整Agent多实例验收、真实模型压测或3000用户容量证明。

## 要解决的问题

正式服务仍使用SQLite和进程内所有权。直接增加worker有这些风险：

- 会话锁互不相通，同一会话可能同时修改历史。
- 现有`RunStore.__init__`把所有running任务改成interrupted，第二实例会误伤第一实例。
- 各实例分别计执行名额/IP限频，简单加worker会无意放大配额。
- 失联的旧执行者恢复后可能覆盖新执行者；只把数据库换掉不够。

本轮单独建立共享状态模块，保留正式SQLite路径，不隐式迁移或双写。
选择PostgreSQL是为了让任务、额度、历史及名额领取在同一事务内完成，不额外维护Redis副本。
这不表示数据库可以提高LLM或本地模型的推理速度。

## 实现范围

代码：`app/database/shared_state.py`、`shared_schema.sql`；可选驱动`asyncpg==0.30.0`。

| 能力 | 实现 |
| --- | --- |
| 会话 | 仅存随机Bearer凭据的SHA-256；JSONB数据、修订号、24小时闲置有效期 |
| 注册上限 | 短事务检查有效会话数，防止跨实例同时越过上限 |
| 执行权 | 数据库时间判断租约；随机holder加递增fence标识一代执行权 |
| 续租/接管 | 提供renew接口；到期后必须重新领取，不能复活旧代号 |
| 防旧写 | 历史、任务、事件、步骤和检查点写入核验租约；任务写入还核验执行名额 |
| 全局容量 | 实例共用ta_slots，SKIP LOCKED领取；不是每实例各分4个 |
| 扣次 | 任务登记、UTC日预算、占名额、last_request同事务提交或回滚 |
| 幂等 | 同键同问题、同版本且证据仍新鲜才重放；冲突拒绝，不重复收费 |
| 完成提交 | 历史与最终结果同事务保存，修订号不符时全部回滚 |
| 步骤/检查点 | 私有结果、计数、下一节点持久化；同payload成功步骤可重放 |
| 安全启动 | 初始化仅建表/核验配置；取得某会话的新执行权后才标记其旧任务中断 |
| 取消意图 | 所属会话可跨实例请求取消，执行者renew时读取；尚未接线自动取消本地任务 |
| 删除/清理 | 会话删除级联清除任务/步骤/事件、不退额度；有界清理过期会话和旧任务 |

租约默认15秒；测试用2秒缩短故障等待。模块提供续租函数，**尚无自动心跳调度器**。
库中核对实例配置，不同容量/会话数/租期拒绝初始化，不悄悄覆盖。

例如A持有fence=10后失联，过期后B拿到11。A恢复时写入/续租被拒，释放也不能解除B的租约。
会话和执行名额同时核验：名额已丢失时不能只续会话继续执行。
这保护状态提交和逻辑执行权，不保证已发出的第三方请求立即停止，也不保证外部副作用exactly-once。
后续执行器必须在丢租时停止后续模型/工具调用并拒绝交付旧结果。

行锁只围住短事务，不持锁等模型或工具；初始化advisory lock只防同时建表/配配置。
SKIP LOCKED用于领取可用名额，不把跳过的行当作完整查询结果。
参考[PostgreSQL行锁](https://www.postgresql.org/docs/15/explicit-locking.html)、
[SKIP LOCKED](https://www.postgresql.org/docs/15/sql-select.html)、
[asyncpg连接池/事务](https://magicstack.github.io/asyncpg/current/api/index.html)。

## 实际验证

最终证据：私有`reports/shared_state_20261105/run5/`，记录驱动/服务端版本、镜像ID和源码SHA-256。

- **23/23真实PostgreSQL契约通过，无跳过。**
- **8个独立Python进程**同时争抢全局4名额：4个成功、4个capacity_full；有效名额4、扣次4，
  成功进程提交后所有holder/名额释放。没有执行完整Agent或模型。
- 独立进程领取后被SIGKILL：到期前不能接管，2秒租期后新fence接管；任务变interrupted，
  检查点/模型计数保留，running步骤变unknown，不自动重跑或重复扣次。
- 旧持有者不能写回、续租、删除或误释放；额度竞争、容量拒绝、事务取消和修订冲突无半笔提交。
- 重放冲突、版本/证据过期、会话归属、隐私删除和24小时清理通过。
- 正式路径回归：**245项Python、8项Node通过**；新增5项无数据库参数/隔离检查。
- **0次付费调用，未调用商品API或Milvus。**

这些是共享状态正确性，不是数据库QPS、回答准确率、8路Agent吞吐或3000真实用户容量。

### 保留的失败记录

- run1：测试TRUNCATE CASCADE连执行名额行也清空，导致capacity_full。
  改为专属库DELETE和名额字段重置，没有放宽容量。
- run2：Unix就绪检查通过后，宿主机TCP连接失败；未保存当时服务端日志，精确启动时序未确认。
  原探测可能早于最终TCP就绪，现改为容器TCP加真实驱动确认；端口检查不再把TIME_WAIT误判为监听。
- run3：20项通过；补入旧任务保留期和过期租约后，run4通过23项。
  隔离校验辅助函数重命名避免被pytest误收集后，run5再次23项通过；不据此证明所有建连故障都已排除。
- 代码复核补入“名额丢失时不能只续会话”分支及回归。

以上为开发验证问题，不冒充线上事故。失败原始记录保留。

## 隔离与复现

驱动只安装在reports独立venv，不修改现有Conda环境。
专属容器`tayune-capacity-postgres-test`仅绑定127.0.0.1:25432，内存上限256MiB。
独立库`tayune_capacity_test`使用随机临时密码；数据保留在私有reports目录，不上传Git。
容器/专属网络测试后移除。课程PostgreSQL5433、Redis6379、Milvus及正式.env/SQLite/API不改动。

```sh
python3.11 -m venv reports/shared-state-venv
reports/shared-state-venv/bin/python -m pip install -r requirements-capacity.txt
reports/shared-state-venv/bin/python -m tests.performance.verify_shared_state \
  --output reports/next_shared_state
```

需Docker已有postgres:15-alpine；不自动拉镜像，不接管同名容器。
Compose仅为一次性验证，不是生产数据库的安全、迁移或备份方案。
测试拒绝远程/默认库/课程端口，勿在非专属库手动执行清理。

## 正式接线门槛

1. Runtime显式选择共享后端；设计存量会话迁移/失效和可回滚切换，不做隐式双写。
2. Service、Execution、Graph及模型/工具网关异步持久化，外部调用前可靠记录尝试计数。
3. 自动续租、丢租取消、跨实例取消意图、有界等待联动；数据库失败不能静默退回本地锁。
4. 安全恢复接口核验版本、证据60秒时效、会话/记忆修订号和剩余预算。
   目前只保留中断检查点，重复start不会冒充恢复或重新收费。
5. 共享IP限频、协调/拆分重模型，避免每个API worker重复加载全部模型。
6. 两个完整API实例联调后，才做异机混合负载、故障测试及另行授权的真实模型验收。

**此前不可增加正式Uvicorn workers；正式服务仍为单进程SQLite。**
本模块不是同步RunStore的即插即用替换件，不可在当前同步Execution里直接调用异步方法。

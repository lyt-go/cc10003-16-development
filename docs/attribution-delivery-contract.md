# 归因结果与数据服务的版本契约

数据服务页面上的每个服务版本，都通过一份“归因结果摘要”与归因规则绑定。契约决定了
服务返回哪些字段、字段值来自哪条规则版本、有哪些查询参数和消费方。

## 冻结字段

服务契约的返回字段固定为下面六项，来源取自现有归因与数据服务链路：

| 返回字段 | 来源 | 说明 |
| --- | --- | --- |
| `rule_version` | 归因规则发布摘要.规则版本 | 形如 `ATTR-0003@v6` |
| `channel` | 归因规则.关联渠道 | 触点渠道 |
| `first_touch_weight` | 归因规则.首次触点权重 | 触点权重 |
| `last_touch_weight` | 归因规则.末次触点权重 | 触点权重 |
| `middle_decay` | 归因规则.中间触点衰减 | 触点权重 |
| `attributed_value` | 归因结果集（按该摘要计算） | 归因金额，两位小数 |

字段缺项、出现未冻结字段或顺序变化都会被拒绝，服务不会写入半套契约。

## 流程

```text
归因规则（草稿 -> 规则评审中 -> 已发布 -> 已下线）
                                  │ 发布时
                                  ▼
                        不可变结果摘要 digest
                        digest_ref = 规则编号@规则版本
                                  │ 服务契约按摘要登记
                                  ▼
数据服务（草稿 -> 评审 -> 发布 -> 下线）  返回字段 / 查询参数 / 消费方
```

- 归因规则发布时生成摘要，摘要冻结触点权重、回溯窗口和规则版本，`immutable=true`。
- 数据服务只能引用已发布的摘要；发布服务版本时会把契约一并写为当前版本。
- 规则下线不会改写已发布契约，服务仍按原摘要返回字段；要换规则必须发布新的服务版本。
- 同一规则版本重复发布只生效一次；同一份契约重复发布也会复用原记录。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/attribution/digests?rule_number=ATTR-0003` | 归因结果摘要列表 |
| GET | `/api/attribution/digests/{digest_hash}` | 单份摘要 |
| POST | `/api/attribution/{id}/actions` | 发布规则时生成摘要 |
| POST | `/api/delivery/{id}/contract/preview` | 契约预览（不落库） |
| POST | `/api/delivery/{id}/contract` | 发布契约 |
| GET | `/api/delivery/contracts?service_number=DELI-0003` | 发布记录 |
| GET | `/api/delivery/contracts/{contract_id}` | 单份契约（含逐字段追溯） |
| GET | `/api/delivery/result/{service_number}` | 当前契约的接口响应样本 |
| GET | `/api/delivery/resolve?...` | 按任一返回字段回查规则版本 |
| GET | `/api/delivery/audit` | 契约审计记录 |

预览与发布记录使用同一个 `preview_hash`，同时携带同一个 `digest_hash`，因此从预览
进入发布不会换规则版本。

## 并发与失败

- 校验、写入与审计在同一把锁内完成，并发发布只会留下完整的当前版本。
- 同一服务版本出现两份摘要时，先完整写入的契约保持为当前版本，后一份返回冲突说明，
  并指出当前契约编号与摘要。
- 写字段失败时回收本次写入的契约，并恢复被替代版本为当前版本。

## 状态机

服务版本只允许 `草稿 -> 评审 -> 发布 -> 下线`，每次只前进一步；跳级、倒序、重复
归档和下线后恢复都会被拒绝，拒绝原因写入审计。

## 示例

ATTR-0003（末次触点，规则版本 v6）发布到 DELI-0003：

```json
{
  "attributed_users": 18420,
  "attributed_revenue": "926500.00",
  "rule_version": "ATTR-0003@v6",
  "channel": "付费搜索、信息流、自然搜索",
  "first_touch_weight": "0.4",
  "last_touch_weight": "0.6",
  "middle_decay": "0.15",
  "attributed_value": "926500.00"
}
```

契约预览会列出三个消费方（增长看板、渠道结算、归因复盘）和可回退版本
（v1 / ATTR-0003@v2、v2 / ATTR-0003@v6）。发布后从任意一个返回字段调用
`/api/delivery/resolve` 都能回到 `ATTR-0003@v6`。

## 测试

```bash
cd backend && .venv/bin/python -m unittest discover -s tests -v
```

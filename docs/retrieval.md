# 检索范围与引用

## 从主题到原文

1. 使用 `search_topics(query=...)` 搜索 Bilibili 视频，或向 `resolve_topic(target=...)` 提供 BV 号、标准视频链接，保存返回的 `topic_ref`。
2. 用 `get_topic(topic_ref=...)` 获取主题详情和平台能力。`list_responses` 适合浏览原文，`search_responses` 适合在有限范围内找包含检索词的回应。
3. 将命中的 `response_ref` 传给 `get_response_context`，核对父回应、所属主楼和 `missing_parts`。引用时保留原文、来源 URL 及原生回应 ID，并说明实际采集范围。

## 如何扩大搜索范围

`search_responses` 默认从热门排序的前 5 个上游主评论页构造候选集。主评论、内嵌回复和预算内展开的楼中楼各自作为一条文档。每页大小由 Bilibili 决定。

| 要做的事 | 参数 |
| --- | --- |
| 在同一批原文里换关键词 | 传入同一 `topic_ref`、`candidate_set_id` 和新的 `query`；可调整 `top_k`。 |
| 继续搜索尚未采集的位置 | 将 `next_scan_cursor` 作为下一次调用的 `start_cursor`。本次取得的回应单独评分。 |
| 对更大的整体范围统一排名 | 从起点重新调用，增大 `max_pages`。 |
| 强制重新采集 | 从起点调用并传 `refresh=true`，获取新的候选集。 |
| 翻阅原文 | 将 `list_responses` 返回的 `next_cursor` 传给下一次调用的 `cursor`，沿用原主题和排序。 |

`candidate_set_id` 指向进程内的固定快照。缓存淘汰、到期或服务重启后，重新从起点采集。使用候选集时应省略 `sort` 和 `max_pages`，或提供与快照相同的值。一个候选集内，分数只反映正文与查询词的相关性，同分按采集顺序返回。

## 如何读覆盖信息

- `requested_pages` 和 `fetched_pages` 记录本次请求和实际读到的主评论列表页数。额外读取回复分支也消耗请求与时间预算。
- `unique_responses` 表示本次候选范围内去重后的回应条数。`top_k` 限制返回的命中数，不能用命中数量估计整个视频有多少讨论。
- `expansion_status=partial` 表示还有已知分支可展开；`unknown` 表示上游提供的信息不足。`exhausted` 描述可访问列表及已知分支的进度。
- `stop_reason` 说明本次为何停止，例如到达页数上限、请求预算或上游限流。上游失败前取得的部分数据仍可用于检索，引用时应注明覆盖限制。
- `source.link_type=topic` 时，URL 只能打开视频。用 `native_id` 和原文标识具体回应。`locator_status` 单独描述定位方案的验证状态。

## 登录态提示

读取回应的工具会在 `meta.warnings` 说明访问范围。这里的提示不影响 `ok`，数据仍然可用。

| code | 含义 | 建议 |
| --- | --- | --- |
| `ANONYMOUS_ACCESS` | 未配置 Bilibili 登录凭据，翻页和最新排序可能受限。 | 需要更大范围时配置 Cookie。 |
| `AUTH_EXPIRED` | 已配置的凭据被上游判为失效。 | 更新 Cookie；本次已取得的匿名范围结果仍可引用。 |
| `AUTH_REQUIRED` | 已配置凭据，但上游仍未返回预期内容。 | 凭据可能权限不足，或该内容本身受限。 |

`get_topic`、`resolve_topic`、`search_topics` 不返回这类提示。凭据配置方法见[部署配置](configuration.md)。

## 上游临时故障

Bilibili 只读请求遇到网络超时、连接中断或 HTTP `502/503/504` 时，仅在原有预算内短暂退避后补发。整个工具调用最多补发一次，跨主评论页和楼中楼共享。如果上游要求等待更久且本次时间预算不足，就不补发。补发成功时返回正常结果。读取或检索回应时，补发仍失败且已有可用回应，就返回部分结果并标注 `coverage.error`；没有取得可用数据时返回错误。

HTTP `412`、`429`、明确的风控或认证失败以及分页全零均不立即重试。搜索视频受限时，可用已知的视频链接或 BV 号来解析主题。若返回 `retry_after_seconds`，调用方应等待后再决定要不要重试。

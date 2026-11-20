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

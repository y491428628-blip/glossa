# 问题记录

## v0.2.0 已修复

**BUG-1 复数等变体匹配不上术语**
- 现象：UNSW Key dates 页面上，"Census dates" 被译成"人口普查日期"，单数 "Census date" 则正常。
- 原因：匹配规则要求术语后面不能紧跟字母，复数的 s 挡住了匹配；不换行空格和连字符写法也匹配不上。没匹配上的段落会按"无术语"版本存进缓存，之后一直错。
- 修复：支持 s/es/'s、y→ies、空格/不换行空格/连字符等变体。测试：`test_plurals_hyphens_and_spaces_match`。

**BUG-2 Gemini 429 限流导致"第三方繁忙"**
- 现象：插件翻页时部分段落显示"重试"。原因是 Gemini 3.5 Flash Lite 免费额度为每分钟 15 次请求，插件翻页时峰值达到 19 次。
- 修复：
  - 合并请求：0.35 秒内到达的段落（来自一个或多个请求）合并成一次 AI 调用；
  - 全局限流：每分钟请求数可在设置里调整，默认 12，超出的请求排队，不报错；
  - 429/5xx 自动重试，按服务商给出的 retryDelay 等待；
  - 最终仍失败时，原样返回 429（带 Retry-After 头），不再改成 502。
- 测试：`test_concurrent_requests_are_merged_into_one_call`、`test_rate_limit_retry_uses_provider_delay`、`test_rate_limit_exhausted_is_reported_as_429`、`test_outage_is_retried`、`test_rate_limiter_spaces_calls`。

**BUG-3 OpenAI 路线下 Paragraphs 统计的是请求数**
- 修复：OpenAI 路线改为按 `[[pN]]` 拆分段落处理，统计的就是真实段落数。

**BUG-4 上游报错只保存前 300 字符，Gemini 的数组格式报错没解析**
- 修复：解析列表格式的报错，提取 message 和 retryDelay；完整报错保存到数据库并打印到服务器终端。测试：`test_parse_google_list_error`。

**改进：OpenAI 路线按段落缓存**
- 原因：插件把相邻 6–8 段打包成一个请求，按整包缓存的话，打包方式一变（滚动、窗口大小、队友设备不同）就全部命中失败。
- 修复：拆开 `[[pN]]` 逐段查缓存，只把没翻过的段落发给 AI，再按原格式拼回去。段落缓存和自定义 API、试译共用。测试：`test_openai_paragraph_mode_caches_per_paragraph`。

**改进：整段就是一个术语时直接返回术语表译法**
- 菜单、标签这类整段只有一个术语的内容（例如 "Key dates"）不再调用 AI。测试：`test_exact_term_paragraphs_skip_the_ai`。

**改进：翻完检查术语，没用上就重翻一次**
- 测试：`test_glossary_compliance_retry`。

**改进：一批失败不再连累其他段落**
- 成功的段落会存进缓存，重试时只需要重翻失败的那部分。测试：`test_failed_batch_does_not_sink_the_others`。

**改进：统计**
- 失败数只算最近 24 小时；总览显示过去一小时的 AI 调用次数和最忙一分钟的次数，可以对照服务商的限额；最近活动增加"AI 调用"列。

**改进：界面支持 6 种语言**
- English（默认）、简体中文、繁體中文、日本語、한국어、Tiếng Việt。选中一种语言后，界面文字和报错提示全部是该语言。
- 保持原样的有：品牌名（Glossa、OpenAI、Immersive Translate）、技术名词（API、CSV、token）、语言选择器里各语言的本名、用户自己输入的术语和备注。
- 测试：`tests/test_i18n.py`，检查每种语言的词条齐全、占位符一致，并且中日韩词表里没有遗留英文单词。

## 已解决（非代码问题）
- 截至/截止：术语表录入时打错字，改正后清掉插件缓存即可生效。
- 重新 `zenith up` 后要重填设置：临时部署的设计如此（默认 20 分钟、数据临时）。本地调试用 `DATA_DIR=./data`，数据会保留。

## 待观察
- 插件自身的请求超时时长未知。按每分钟上限排队太久时，插件可能先超时并显示"重试"，不过已经翻好的段落都在缓存里，重试会很快。
- 目前只用模拟 AI 测试过新的分段批量格式。v1 的真实流量里 Gemini 已经能正确处理 `[[pN]]` 格式，但仍需用真实 key 验证一次。

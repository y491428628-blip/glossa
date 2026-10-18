# 问题记录

## v0.3.0 防止术语被乱设置

**问题：术语表设错后，错误会被强制推给全队**
- 一条错误的术语会通过“整段是术语直接返回”和“漏用术语强制重翻”两条路径强制生效；常见词（date、term）会影响几乎所有句子；改错了没有记录，也没法撤销。

**改进 1：保存前检查短词、常见词和影响范围过大的词**
- 添加或修改术语时，如果原文很短（≤3 个字母且不区分大小写）、是日常用词，或者匹配了记忆中 20% 以上的段落，会弹窗说明原因并列出例句，确认后才保存。术语表里这类术语会标“需检查”。
- 批量导入和 AI 推荐不弹窗（可以整批撤销）。
- 测试：`test_common_or_broad_terms_need_confirmation`。

**改进 2：备注发给 AI，新增“视含义而定”**
- 备注会随术语一起发给 AI，作为翻译时的参考。
- 勾选“视含义而定”的术语：备注写明适用条件，由 AI 判断用不用；不会强制重翻，不会走“整段是术语直接返回”，也不计入术语遵循率。勾选时必须填写备注。
- 缓存键只在术语有备注或勾选了“视含义而定”时才变化，所以升级后没有备注的术语对应的记忆仍然有效；有备注的术语对应的段落会重新翻译一次。
- 测试：`test_notes_reach_the_ai_and_conditional_terms_are_not_forced`、`test_editing_a_note_retranslates_but_old_memory_survives_the_upgrade`。

**改进 3：修改记录和撤销**
- 每次添加、修改、删除都会记录修改前后的内容；一次导入或批量添加算作一组，可以整组撤销。撤销本身也会记录，可以再撤销（相当于重做）。
- 如果这条术语之后又被改过，旧的修改不能直接撤销（会覆盖较新的修改），需要先撤销较新的那次。
- 限制：只有一个管理员密码，所以只能记录“什么时候改了什么”，记录不了“是谁改的”；来自队员提议的修改会显示提议人填写的名字。
- 测试：`test_history_and_undo`、`test_undo_a_whole_import`。

**改进 4：队员提议，管理员审核**
- 新页面 `/suggest`：队员凭团队密钥查看术语表，可以提议新术语或修改现有术语。管理员在术语表页审核，可以先修改译法和给 AI 的备注再批准，也可以拒绝。导航栏上会显示待审核的数量。
- 链接格式是 `/suggest#key=…`。密钥放在 `#` 后面，浏览器不会把它发给服务器，页面打开后也会从地址栏里去掉。
- 防刷：每个地址每小时最多提交 30 条；待审核的提议最多 300 条；重复的提议只保留一条。
- 测试：`test_teammates_propose_and_admin_reviews`、`test_proposals_are_rate_limited`。

**数据库**：新增 `glossary.conditional` 列和 `glossary_history`、`proposals` 两张表，启动时自动升级，原有数据保留。测试：`test_upgrade_from_v2_glossary`。

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

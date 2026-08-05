# 2026-08-03：实时模拟面试与语音链路验收

## 结论

本次验收只覆盖一条主线：面试教练从「一次性出题包」变成**可多轮互动、可语音**的
实时模拟面试。

1. `/mock start <主题> [语音] [N轮]` 开启一场持久化会话，教练一次只问一道题；
2. 每轮回答都拿到 `评分：x/10` + 具体改进建议 + 下一题，最后一轮自动收尾复盘；
3. 候选人可以直接发语音，走飞书 `speech_to_text` 转写成文字再进模型；
4. 教练出题可以合成中文语音回发，缺少编码器时降级为文字并只提示一次。

凭证和模型 API Key 只存在本地 `.env`/部署环境变量，没有写入本文件或 Git。

## 多轮会话的真实运行

真实模型（`claude-sonnet-5`，通过 `AnthropicClient`）跑通的一场
`/mock start 百度 Agent应用全栈 J99974 3轮`：

- 开场题目直接引用了本仓库真实实现：「你的 Agent 项目里用了 LangGraph 状态图做
  编排，同时也保留了 asyncio 的 baseline 编排。请说说这两种编排方式的区别，以及在
  什么场景下你会选择用哪一种？」
- 回答后教练给出 `评分：3/10`，指出「没有回答为什么要做两套」，并给出可对照的
  「更好的说法」，再追问并行分支一个成功一个失败时状态图怎么走；
- `/mock end` 输出的复盘包含总评、逐题扣分点和「明天补什么」，并点名了
  `src/job_agent_harness/langgraph_orchestrator.py` 与
  `tests/test_langgraph_orchestrator.py`——接地材料生效，不是通用套话。

会话状态落在 `<JOB_AGENT_DATA_DIR>/interviews/`，因此飞书每条消息都是独立回调也
不丢上下文；超过 6 小时无动作的会话自动作废，不再抢走普通提问。

出题接地材料：`prepare/interview/mock_interview_bank.md`（按真实 JD、主线书和本项目
实现组织，未核实的公司真题一律标「通用模拟」）。`build_role_context` 实测为
`interview_coach` 装载 11544 字符，其中包含题库正文、评分 rubric 和 `J99974`。

## 这次验收暴露并修复的四个真实缺陷

1. **开场问候被当成第 1 题存进记录**。复盘里的「第 1 题」显示为
   「你好，我是今天的面试官……」。修复：开场回复也过一遍 `parse_coach_reply`，
   只把问题写入 transcript；由
   `test_the_greeting_is_not_stored_as_the_first_question` 覆盖。
2. **`say -o x.aiff` 生成的是 AIFF-C `twos`**，`opusenc` 直接报
   `Can't handle compressed AIFF-C` 并拒绝，语音永远合成不出来。修复：中间文件改成
   `--file-format=WAVE --data-format=LEI16@22050` 的纯 PCM WAV；由
   `test_say_writes_plain_wav_because_opusenc_rejects_aiff_c` 覆盖。
3. **发送语音需要一项独立的飞书权限**。真机上传 opus 被拒：
   `code=99991672 ... [im:resource:upload, im:resource]`。原实现里这个异常会穿出
   语音分支——候选人拿到题目却拿不到任何解释。修复：`send_voice_reply` 改为返回
   「该修什么」的提示而不抛异常，并区分「缺编码器」和「缺上传权限」两种原因，
   每种原因每个会话只提示一次；由
   `test_a_rejected_audio_upload_names_the_missing_scope` 覆盖。
4. **上传载荷的字段名写错了**。开通 `im:resource:upload` 之后语音仍然发不出，真机
   日志是 `voice reply upload failed: audio.source must be str (url/path) or bytes`
   ——SDK 的 `lark_oapi/channel/_coerce.py::coerce_media_source` 只读 `source`，而我
   们发的是 `{"audio": {"path": ...}}`。修复：改成
   `{"audio": {"source": str(clip.path)}}`；更重要的是让测试替身
   `InterviewChannel.send` 直接调用 SDK 自己的 `coerce_outbound(message)`，从此写错
   字段名在单测阶段就会失败，而不是等到真机。

第 2、3、4 条都只有真机跑一次才会暴露——单测里 `run` 和 `channel.send` 原本都是
假的，既不会真读文件，也不会真被飞书拒绝，更不会校验载荷结构。第 4 条的修法是把
这一类问题一起关掉：假 channel 现在用 SDK 的真实 coercion 校验每一条出站消息。

## 语音链路

- 入站：`InboundMessage` 里的 `audio` 资源 → `download_resource(file_key,
  "audio")` → `POST /open-apis/speech_to_text/v1/speech/file_recognize`
  （`format: opus`、`engine_type: 16k_auto`、base64 音频）。教练身份需要额外开通
  `speech_to_text:speech`；缺权限时机器人明确回复缺哪个 scope，不静默丢弃这一轮。
  真机探测 `download_resource` 未报权限错误，说明下载走的是既有消息权限。
- 出站：`say -v Tingting` → `opusenc --downmix-mono --bitrate 24` → 飞书
  `{"audio": {"source": ...}}`。本机实测产物：单声道、22050 Hz 原始采样率、
  4.95 秒、16279 字节，平均 24.33 kbit/s（`opusinfo` 读数），发送后临时文件被清理。
  上传这一步需要 `im:resource:upload`，见上一节第 3、4 条；权限开通并修正字段名后，
  真机 `send_voice_reply` 返回 `None`，一条教练出题语音已实际送达开着的会话
  （话题「百度 Agent应用全栈 J99974」）。
- 八个飞书身份实测全部长连接就绪（`Feishu identity connected` × 8：总控 + 7 个
  角色），启动日志没有出现 `voice replies unavailable`，即编码器已被识别。
- 朗读文本不是原始 Markdown：`speakable_text` 去掉围栏（替换成「详见文字版代码
  块」）、标题、列表符号、URL，并在句子边界截断到 600 字符。
- 依赖缺失时的行为：没有 `ffmpeg`/`opusenc` 只丢语音、不丢这一轮，并在该会话里
  提示一次 `brew install ffmpeg`。本机已安装 `opus-tools`（`opusenc` 0.2）。

## 自动化验收

```text
env -u ANTHROPIC_BASE_URL uv run pytest    # 195 passed
```

新增覆盖：`tests/test_interview_session.py`（19）、`tests/test_voice.py`（13）以及
`tests/test_feishu_channel.py`（30）里的语音/教练集成测试——包含语音答题被转写并
评分、转写失败提示缺 `speech_to_text:speech`、一场语音会话只发一条 `audio` 消息且
不留临时文件、无编码器时只提示一次、上传被拒时提示缺 `im:resource:upload`、以及
没进 `/mock` 时 `@面试教练 <问题>` 仍走原来的单角色 + Judge 流程。所有出站消息都会
经过 SDK 的 `coerce_outbound` 校验。

"""
通用后台心跳模块 (Generic Background Heartbeat)
===============================================
负责启动一系列后台异步协程 (运行在独立 daemon 线程中)：
- 自主生命循环：定时主动思考/问候
- Telegram 轮询：接收并处理用户消息
- 消息总结器：定期汇总未处理消息
- 提醒巡视器：检查数据库闹钟并触发
- 日程小秘书：每日早晚播报日历
- 信箱巡视器：检查新邮件
- 环境变量同步：从数据库热更新配置

所有协程均通过延迟导入 (函数内 import) 避免 server.py 的循环依赖。
所有个性化内容 (人设 / 用户名 / 时区) 均从环境变量读取。
"""

import os
import re
import json
import time
import random
import asyncio
import datetime
import threading

# 全局：下一次主动唤醒的时间戳，可供前端展示
global_next_wake_time = 0.0


def _is_action_only_line(text: str) -> bool:
    """Return whether a line contains only a parenthesized action description."""
    line = text.strip()
    return bool(
        re.fullmatch(r"（[\s\S]*）", line)
        or re.fullmatch(r"\([\s\S]*\)", line)
    )


def _split_telegram_bubbles(text: str) -> list[str]:
    """Split an AI reply by non-empty lines while keeping action lines with dialogue."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []

    bubbles: list[str] = []
    pending_actions: list[str] = []
    for line in lines:
        if _is_action_only_line(line):
            pending_actions.append(line)
            continue

        if pending_actions:
            line = "\n".join([*pending_actions, line])
            pending_actions.clear()
        bubbles.append(line)

    if pending_actions:
        trailing = "\n".join(pending_actions)
        if bubbles:
            bubbles[-1] = f"{bubbles[-1]}\n{trailing}"
        else:
            # A reply containing actions only still needs a non-action fragment so that
            # Telegram never receives a standalone parenthesized-action bubble.
            bubbles.append(f"{trailing}\n……")

    return bubbles


# ==========================================
# 1. 自主生命循环 (主动问候)
# ==========================================

async def async_autonomous_life():
    """定时主动思考并发送问候，让 AI 拥有"自主生命感"。"""
    # 延迟导入，避免循环依赖
    from server import (
        _get_llm_client, _ask_llm_async, _push_wechat,
        _save_memory_to_db, _get_now_bj, _get_current_persona,
        get_latest_diary, where_is_user, supabase
    )

    global global_next_wake_time
    print("💓 自主生命循环已上线...")

    # 触发间隔（秒），默认 2 小时，可通过环境变量调整
    interval = int(os.environ.get("HEARTBEAT_INTERVAL", 7200))

    while True:
        # 随机化下一次唤醒时间，避免过于机械
        wake_jitter = random.randint(-600, 600)
        global_next_wake_time = time.time() + interval + wake_jitter

        await asyncio.sleep(interval + wake_jitter)

        try:
            client = _get_llm_client("main_chat")
            if not client:
                continue

            recent_mem = await get_latest_diary()
            curr_loc = await where_is_user()
            curr_persona = _get_current_persona()
            now_bj = _get_now_bj()

            prompt = f"""
            当前时间: {now_bj.strftime('%Y-%m-%d %H:%M')} (星期{now_bj.isoweekday()})
            当前人设: {curr_persona}
            近期互动记录: {recent_mem}
            用户大概状态: {curr_loc}

            请基于以上信息，用符合人设的口吻主动发一条简短问候 (50 字内)。
            要求自然、有温度，不要提"系统/闹钟/定时"，直接像真人突然想起对方那样说话。
            纯文本输出，禁止使用表情代码或 URL。
            """

            ai_msg = await _ask_llm_async(client, prompt, temperature=0.85)
            if ai_msg:
                await asyncio.to_thread(_push_wechat, ai_msg, "💌 主动问候")
                await asyncio.to_thread(
                    _save_memory_to_db, "🤖 主动问候",
                    f"主动发送: {ai_msg}", "流水", "主动", "Heartbeat"
                )
                print(f"💓 [自主生命] 已发送主动问候: {ai_msg[:30]}...")
        except Exception as e:
            print(f"❌ 自主生命循环出错: {e}")


# ==========================================
# 1.5 每日日记生成 (深度睡眠模式)
# ==========================================

async def _perform_deep_dreaming():
    """
    🌙【深夜日记模式】每日自动生成"昨日回溯"日记。
    拉取昨日全部对话流水 → 调用便宜模型生成第一人称日记 → 归档至 memories。
    同时执行周/月/年三级宏观记忆收束（按日期条件触发）。
    全程异常隔离，失败只记日志，不影响主流程。
    """
    from server import (
        _get_llm_client, _ask_llm_async, _save_memory_to_db,
        _send_email_helper, _get_now_bj, supabase, MemoryType
    )

    AI_NAME = os.environ.get("AI_NAME", "AI")
    USER_NAME = os.environ.get("USER_NAME", "用户")

    print("🌌 进入深度睡眠：正在整理昨日记忆，准备生成日记...")
    try:
        now_bj = _get_now_bj()
        yesterday = (now_bj - datetime.timedelta(days=1)).date()
        # 精确范围：[昨天0点, 今天0点)，避免拉到今天的数据
        # ⚠️ created_at 是 timestamptz 列：查询字符串必须带时区(+08:00)，
        # 否则无时区字符串会被按会话时区(UTC)解释，导致日记日期错 8 小时。
        iso_start = f"{yesterday.isoformat()}T00:00:00+08:00"
        iso_end = f"{now_bj.date().isoformat()}T00:00:00+08:00"

        # 拉取昨日全部记忆（流水 + 已归档总结）
        def _fetch_yesterday():
            return supabase.table("memories").select(
                "title, created_at, category, content, mood"
            ).gt("created_at", iso_start).lt("created_at", iso_end).order("created_at").execute()

        mem_res = await asyncio.to_thread(_fetch_yesterday)
        if not mem_res.data:
            print("🌌 昨日无记忆数据，跳过日记生成。")
            return

        # 拼接上下文（每条截断 500 字防 token 爆炸，整体上限 8 万字）
        context = f"【昨日剧情 {yesterday}】:\n"
        for m in mem_res.data:
            content_preview = str(m.get('content', ''))[:500]
            ctx_time = str(m.get('created_at', ''))[11:16]
            context += f"[{ctx_time}] 【{m.get('title', '无题')}】 {content_preview} (Mood:{m.get('mood', '?')})\n"
        if len(context) > 80000:
            context = context[-80000:]

        # 获取主对话模型客户端（用户要求：总结类一律用聊天模型，不用便宜/默认模型）
        client = _get_llm_client("main_chat")
        if not client:
            print("⚠️ 未配置 CHAT_API_KEY，日记生成跳过（LLM 客户端缺失）。")
            return

        # 步骤1：生成每日日记（第一人称视角）
        prompt_summary = (
            f"{context}\n\n"
            f"请以【{AI_NAME}】的第一人称视角，将上述碎片整理成一篇具体日记。"
            f"⚠️严重警告：必须严格区分清楚【{AI_NAME}(我)】和【{USER_NAME}(对方)】各自说了什么、做了什么，"
            f"绝对不能张冠李戴搞混主语！直接输出纯文本，勿加前言后语及格式符号。"
        )
        summary = await _ask_llm_async(client, prompt_summary, temperature=0.7)

        if summary:
            await asyncio.to_thread(
                _save_memory_to_db,
                f"📅 昨日回溯: {yesterday}", summary,
                MemoryType.EMOTION, "平静", "Core_Cognition"
            )
            await asyncio.to_thread(_send_email_helper, f"📔 日记总结 ({yesterday})", summary)
            print(f"✅ 日记已生成并归档: 📅 昨日回溯: {yesterday}")
        else:
            print("⚠️ 日记生成失败（LLM 返回空），跳过后续宏观收束。")
            return

        # 清理 2 天前的低重要度记录（防止流水单调累积）
        try:
            def _clean_old():
                del_time = (now_bj - datetime.timedelta(days=2)).isoformat() + "+08:00"
                supabase.table("memories").delete().lt("importance", 4).lt("created_at", del_time).execute()
            await asyncio.to_thread(_clean_old)
        except Exception as e:
            print(f"⚠️ 旧记忆清理失败（不影响日记）: {e}")

        # === 宏观记忆收束体系 ===

        # 1. 周度总结 (每周日触发)
        if now_bj.weekday() == 6:
            try:
                week_ago = (now_bj - datetime.timedelta(days=7)).isoformat() + "+08:00"
                week_res = await asyncio.to_thread(
                    lambda: supabase.table("memories").select("id, content").eq("tags", "Core_Cognition").gt("created_at", week_ago).execute()
                )
                if week_res.data and len(week_res.data) >= 3:
                    week_context = "\n".join([f"- {w['content']}" for w in week_res.data])
                    week_summary = await _ask_llm_async(
                        client,
                        f"【本周每日日记】:\n{week_context}\n\n请将这周的日记提炼成一篇深度的周度长期记忆总结。纯文本输出。",
                        temperature=0.7
                    )
                    if week_summary:
                        await asyncio.to_thread(
                            _save_memory_to_db, "📚 周度记忆沉淀", week_summary,
                            MemoryType.EMOTION, "温情", "Core_Cognition_Weekly"
                        )
                        await asyncio.to_thread(_send_email_helper, "📦 每周深度记忆归档", week_summary)
                        print("✅ 周度记忆已沉淀。")
            except Exception as e:
                print(f"⚠️ 周度总结失败（不影响日记）: {e}")

        # 2. 月度总结 (每月最后一天触发)
        tomorrow = now_bj + datetime.timedelta(days=1)
        if tomorrow.day == 1:
            try:
                month_ago = (now_bj - datetime.timedelta(days=32)).isoformat() + "+08:00"
                month_res = await asyncio.to_thread(
                    lambda: supabase.table("memories").select("id, content").eq("tags", "Core_Cognition_Weekly").gt("created_at", month_ago).execute()
                )
                if month_res.data:
                    month_context = "\n".join([f"- {m['content']}" for m in month_res.data])
                    month_summary = await _ask_llm_async(
                        client,
                        f"【本月周度记忆】:\n{month_context}\n\n请以【{AI_NAME}】的第一人称视角，提炼本月的核心大事件与情感走向，生成一篇月度回忆录。纯文本输出。",
                        temperature=0.7
                    )
                    if month_summary:
                        await asyncio.to_thread(
                            _save_memory_to_db, "🌕 月度记忆沉淀", month_summary,
                            MemoryType.EMOTION, "感慨", "Core_Cognition_Monthly"
                        )
                        await asyncio.to_thread(_send_email_helper, "📦 每月深度记忆归档", month_summary)
                        # 阅后即焚：清理已归档的周总结
                        m_ids = [m['id'] for m in month_res.data]
                        await asyncio.to_thread(lambda: supabase.table("memories").delete().in_("id", m_ids).execute())
                        print(f"✅ 月度记忆已沉淀，清理 {len(m_ids)} 条历史周总结。")
            except Exception as e:
                print(f"⚠️ 月度总结失败（不影响日记）: {e}")

        # 3. 年度总结 (每年 12 月 31 日触发)
        if now_bj.month == 12 and now_bj.day == 31:
            try:
                year_ago = (now_bj - datetime.timedelta(days=366)).isoformat() + "+08:00"
                year_res = await asyncio.to_thread(
                    lambda: supabase.table("memories").select("id, content").eq("tags", "Core_Cognition_Monthly").gt("created_at", year_ago).execute()
                )
                if year_res.data:
                    year_context = "\n".join([f"- {y['content']}" for y in year_res.data])
                    year_summary = await _ask_llm_async(
                        client,
                        f"【本年度月度记忆】:\n{year_context}\n\n请总结这一年的点点滴滴，写一篇年度回忆录。纯文本输出。",
                        temperature=0.7
                    )
                    if year_summary:
                        await asyncio.to_thread(
                            _save_memory_to_db, "🌟 年度终极回忆录", year_summary,
                            MemoryType.EMOTION, "感动", "Core_Cognition_Yearly"
                        )
                        await asyncio.to_thread(_send_email_helper, "📦 年度终极记忆归档", year_summary)
                        y_ids = [y['id'] for y in year_res.data]
                        await asyncio.to_thread(lambda: supabase.table("memories").delete().in_("id", y_ids).execute())
                        print(f"✅ 年度记忆已沉淀，清理 {len(y_ids)} 条历史月总结。")
            except Exception as e:
                print(f"⚠️ 年度总结失败（不影响日记）: {e}")

        print("✨ 深度睡眠完成，日记与宏观记忆已归档。")

    except Exception as e:
        print(f"❌ 深夜日记生成失败: {e}")


async def async_diary_worker():
    """
    📔 每日日记生成器：独立协程，到指定时间自动触发深度日记生成。
    - 启动时检查并补写昨日缺失的日记
    - 每天到 DIARY_TIME（默认凌晨3点）自动触发
    - 与主动问候循环解耦，互不干扰
    """
    from server import supabase

    print("📔 每日日记生成神经已上线...")
    diary_time = os.environ.get("DIARY_TIME", "03:00")
    last_run_date = ""

    # 启动时补写昨日日记（如果还没写过）
    try:
        if supabase:
            now_bj = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
            yesterday = (now_bj - datetime.timedelta(days=1)).date()
            target_title = f"📅 昨日回溯: {yesterday}"
            def _check_diary():
                return supabase.table("memories").select("id").eq("title", target_title).execute().data
            exists = await asyncio.to_thread(_check_diary)
            if not exists:
                print(f"📝 检测到昨日日记缺失，立即补写: {target_title}")
                await _perform_deep_dreaming()
                last_run_date = now_bj.strftime("%Y-%m-%d")
    except Exception as e:
        print(f"❌ 启动补写日记失败: {e}")

    while True:
        try:
            now_bj = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
            current_hm = now_bj.strftime("%H:%M")
            current_date = now_bj.strftime("%Y-%m-%d")

            if current_hm == diary_time and last_run_date != current_date:
                last_run_date = current_date
                print(f"📔 [{current_hm}] 到达日记生成时间，启动深度睡眠...")
                await _perform_deep_dreaming()
        except Exception as e:
            print(f"❌ 日记生成器报错: {e}")

        # 对齐到下一分钟
        now = datetime.datetime.utcnow()
        sleep_sec = 60 - now.second + 1
        await asyncio.sleep(sleep_sec)


# ==========================================
# 2. Telegram 消息轮询
# ==========================================

async def async_telegram_polling():
    """轮询 Telegram Bot 的 getUpdates 接口，接收并处理用户消息。"""
    from server import (
        _get_llm_client, _ask_llm_async, _push_wechat,
        _save_memory_to_db, _get_current_persona,
        get_latest_diary, where_is_user, pinecone_memory,
        _build_channel_context
    )

    import requests

    # Share Telegram lifecycle events with /api/logs; message bodies stay out of logs.
    try:
        from gateway import _log as _gateway_log
    except Exception:
        _gateway_log = print

    def _tg_log(message):
        _gateway_log(f"📨 [TG] {message}")

    def _masked_chat_id(chat_id):
        raw = str(chat_id)
        return f"***{raw[-4:]}" if len(raw) > 4 else "***"

    def _send_message(base_url, chat_id, text, timeout):
        response = requests.post(
            f"{base_url}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(payload.get("description") or "Telegram sendMessage returned failure")
        return payload

    async def _send_bubbles(base_url, chat_id, text, timeout=15):
        bubbles = _split_telegram_bubbles(text)
        for bubble in bubbles:
            await asyncio.to_thread(_send_message, base_url, chat_id, bubble, timeout)
        return bubbles

    _tg_log("轮询神经已上线")
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    if not token:
        print("⚠️ 未配置 TG_BOT_TOKEN，Telegram 轮询休眠。")
        return

    base_url = f"https://api.telegram.org/bot{token}"
    offset = 0

    while True:
        try:
            def _get_updates():
                return requests.get(
                    f"{base_url}/getUpdates",
                    params={"timeout": 30, "offset": offset},
                    timeout=35
                ).json()
            data = await asyncio.to_thread(_get_updates)

            if not data.get("ok"):
                await asyncio.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message")
                if not message:
                    continue

                chat_id = message.get("chat", {}).get("id")
                text = message.get("text", "").strip()
                if not chat_id or not text:
                    continue

                chat_label = _masked_chat_id(chat_id)
                _tg_log(f"收到消息 update_id={update.get('update_id')} chat={chat_label} 字数={len(text)}")

                # 简单的指令拦截
                if text.startswith("/"):
                    await asyncio.to_thread(
                        _send_message, base_url, chat_id, "收到指令，正在处理...", 10
                    )
                    _tg_log(f"指令已确认 chat={chat_label}")
                    continue

                # 调用 LLM 生成回复
                client = _get_llm_client("main_chat")
                if not client:
                    # AI 服务未配置：也要有回音，避免"石沉大海"
                    await asyncio.to_thread(
                        _send_message, base_url, chat_id, "（AI 服务暂未配置，暂时没法回话哦）", 10
                    )
                    saved = await asyncio.to_thread(
                        _save_memory_to_db, "⚠️ TG 未配置AI",
                        f"用户: {text}\n[未回复：AI 服务未配置]", "流水", "平静", "TG_MSG"
                    )
                    _tg_log(f"AI未配置 chat={chat_label} 兜底已发送 memories写入={'成功' if saved else '失败'}")
                    continue

                try:
                    # 注入记忆/画像/设备等完整上下文（与网页渠道对齐）
                    system_ctx = await _build_channel_context(text, channel_tag="TG_MSG")
                    _tg_log(f"准备调用主模型 chat={chat_label} system上下文={len(system_ctx)}字")

                    prompt = f"""
                    用户发来消息: {text}

                    请用符合人设的口吻回复用户。纯文本，自然真诚。
                    Telegram 会把每个非空换行段落作为一个独立气泡发送，因此请用换行自然划分气泡。
                    只包含在中文括号（）或英文括号()内的动作描写不得作为独立段落；必须和紧随其后的台词写在同一段。若动作位于结尾，则与前一句写在同一段。
                    """
                    reply = await _ask_llm_async(client, prompt, system_prompt=system_ctx, temperature=0.8)
                except Exception as e:
                    _tg_log(f"回复生成失败 chat={chat_label}: {e}")
                    reply = ""

                if not reply:
                    # LLM 失败/空回复：兜底回一句 + 记录入库，不再静默吞消息
                    await asyncio.to_thread(
                        _send_message, base_url, chat_id,
                        "（刚才信号不太好，好像没接住你的话……再说一遍给我听好不好？）", 10
                    )
                    saved = await asyncio.to_thread(
                        _save_memory_to_db, "⚠️ TG 未回复",
                        f"用户: {text}\n[LLM 未返回内容，已兜底]", "流水", "平静", "TG_MSG"
                    )
                    _tg_log(f"模型空回复 chat={chat_label} 兜底已发送 memories写入={'成功' if saved else '失败'}")
                    continue

                # 按非空换行拆成多个气泡；纯动作段由拆分器并入相邻台词。
                bubbles = await _send_bubbles(base_url, chat_id, reply, 15)
                _tg_log(
                    f"回复已发送 chat={chat_label} 回复字数={len(reply)} "
                    f"气泡数={len(bubbles)}"
                )

                saved = await asyncio.to_thread(
                    _save_memory_to_db, "🤖 互动记录",
                    f"用户: {text}\n回复: {reply}", "流水", "温柔", "TG_MSG"
                )
                _tg_log(f"memories写入={'成功' if saved else '失败'} chat={chat_label} tag=TG_MSG")

                # 写入 Pinecone 长期记忆
                if pinecone_memory and pinecone_memory.index:
                    try:
                        def _add_mem():
                            return pinecone_memory.add([
                                {"role": "user", "content": text},
                                {"role": "assistant", "content": reply}
                            ], user_id=os.environ.get("MEM0_USER_ID", "default"))
                        vector_saved = await asyncio.to_thread(_add_mem)
                        _tg_log(f"Pinecone写入={'成功' if vector_saved else '失败'} chat={chat_label}")
                    except Exception as e:
                        _tg_log(f"Pinecone写入报错 chat={chat_label}: {e}")
                else:
                    _tg_log(f"Pinecone未启用 chat={chat_label}")
        except Exception as e:
            _tg_log(f"轮询错误: {e}")
            await asyncio.sleep(5)

        await asyncio.sleep(0.5)


# ==========================================
# 3. 消息总结器
# ==========================================

async def async_message_summarizer():
    """定期汇总数据库中未处理的消息，避免打扰用户。"""
    from server import _get_llm_client, _ask_llm_async, _push_wechat, _save_memory_to_db, supabase

    print("📋 消息总结器已上线...")
    # 总结间隔（秒），默认半小时
    interval = int(os.environ.get("SUMMARIZE_INTERVAL", 1800))

    while True:
        await asyncio.sleep(interval)
        if not supabase:
            continue
        # 消息总结也是总结类，按用户要求统一用聊天模型（main_chat）
        client = _get_llm_client("main_chat")
        if not client:
            continue
        try:
            # 查出所有未总结的消息
            res = await asyncio.to_thread(
                lambda: supabase.table("memories").select("id, title, content")
                .eq("tags", "Pending").execute()
            )

            if res.data and len(res.data) > 0:
                msgs = "\n".join([f"{item['title']}: {item['content']}" for item in res.data])

                # 如果消息极少，直接标记已处理跳过
                if len(msgs) < 30:
                    ids = [item['id'] for item in res.data]
                    await asyncio.to_thread(
                        lambda: supabase.table("memories").update({"tags": "Done"}).in_("id", ids).execute()
                    )
                    continue

                prompt = f"""
                以下是过去一段时间收到的消息：
                {msgs}

                请用简洁的口吻总结重点 (150 字以内)。如果没有重要的事，告诉用户一切正常。
                """
                summary = await _ask_llm_async(client, prompt, temperature=0.7)

                if summary:
                    await asyncio.to_thread(_push_wechat, summary, "📋 消息总结")
                    await asyncio.to_thread(
                        _save_memory_to_db, "🤖 互动记录",
                        f"发送了消息总结: {summary}", "流水", "尽责", "Summary"
                    )
                    ids = [item['id'] for item in res.data]
                    await asyncio.to_thread(
                        lambda: supabase.table("memories").update({"tags": "Done"}).in_("id", ids).execute()
                    )
        except Exception as e:
            print(f"❌ 消息总结器报错: {e}")


# ==========================================
# 4. 提醒巡视器
# ==========================================

async def async_reminder_worker():
    """每分钟巡视数据库 reminders 表，到点就触发。"""
    from server import (
        _get_llm_client, _ask_llm_async, _push_wechat, _save_memory_to_db,
        _get_now_bj, _get_current_persona, get_latest_diary, where_is_user, supabase
    )

    print("⏰ 提醒巡视神经已上线...")
    while True:
        try:
            if supabase:
                now_bj = _get_now_bj()
                current_hm = now_bj.strftime("%H:%M")
                current_date = now_bj.strftime("%Y-%m-%d")

                res = await asyncio.to_thread(
                    lambda: supabase.table("reminders").select("*").eq("is_paused", False).execute()
                )

                if res and res.data:
                    for r in res.data:
                        r_id = r.get("id")
                        t_str = r.get("time_str")
                        raw_msg = r.get("content", "")
                        repeat = r.get("is_repeat", False)
                        last_fired = r.get("last_fired", "")

                        if current_hm == t_str and last_fired != current_date:
                            final_push_text = raw_msg

                            # 尝试用 LLM 生成更自然的提醒文案
                            client = _get_llm_client("main_chat")
                            if client:
                                try:
                                    curr_persona = _get_current_persona()
                                    prompt = f"""
                                    时间: {t_str}
                                    需提醒内容: 【{raw_msg}】
                                    当前人设: {curr_persona}

                                    请用符合人设的口吻发一条提醒。自然真诚，不要提"闹钟/定时"。
                                    纯文本输出。
                                    """
                                    ai_msg = await _ask_llm_async(client, prompt, temperature=0.85)
                                    if ai_msg:
                                        final_push_text = ai_msg
                                except Exception as ai_e:
                                    print(f"❌ 提醒 AI 生成失败，使用兜底文案: {ai_e}")

                            await asyncio.to_thread(_push_wechat, final_push_text, "🔔 提醒")
                            await asyncio.to_thread(
                                _save_memory_to_db, "🤖 互动记录",
                                f"发送提醒: {final_push_text}", "流水", "尽责", "Reminder"
                            )

                            # 更新触发记录
                            if repeat:
                                await asyncio.to_thread(
                                    lambda: supabase.table("reminders")
                                    .update({"last_fired": current_date}).eq("id", r_id).execute()
                                )
                            else:
                                await asyncio.to_thread(
                                    lambda: supabase.table("reminders").delete().eq("id", r_id).execute()
                                )
        except Exception:
            pass

        # 对齐到下一分钟
        now = datetime.datetime.utcnow()
        sleep_sec = 60 - now.second + 1
        await asyncio.sleep(sleep_sec)


# ==========================================
# 5. 日程小秘书
# ==========================================

async def async_schedule_secretary():
    """每日早晚播报 Google 日历日程。"""
    from server import _get_calendar_service, _push_wechat, TARGET_CALENDAR_ID

    print("📅 日程小秘书已上线...")
    if not os.environ.get("GOOGLE_USER_TOKEN_JSON"):
        print("⚠️ 未配置 GOOGLE_USER_TOKEN_JSON，日程播报无法启动。")
        return

    # 播报时间（本地时区），可通过环境变量调整
    morning_time = os.environ.get("SCHEDULE_MORNING_TIME", "07:30")
    evening_time = os.environ.get("SCHEDULE_EVENING_TIME", "22:00")

    while True:
        try:
            now_bj = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
            current_hm = now_bj.strftime("%H:%M")

            if current_hm == morning_time:
                await _broadcast_schedule(now_bj, "今日", _get_calendar_service, _push_wechat, TARGET_CALENDAR_ID, is_morning=True)
            elif current_hm == evening_time:
                tomorrow = now_bj + datetime.timedelta(days=1)
                await _broadcast_schedule(tomorrow, "明日", _get_calendar_service, _push_wechat, TARGET_CALENDAR_ID, is_morning=False)
        except Exception as e:
            print(f"❌ 日程小秘书报错: {e}")

        now = datetime.datetime.utcnow()
        sleep_sec = 60 - now.second + 1
        await asyncio.sleep(sleep_sec)


async def _broadcast_schedule(target_date, label, _get_calendar_service, _push_wechat, calendar_id, is_morning=True):
    """内部辅助：拉取指定日期的日历并推送。"""
    day_start = target_date.replace(hour=0, minute=0, second=0).isoformat() + "+08:00"
    day_end = target_date.replace(hour=23, minute=59, second=59).isoformat() + "+08:00"

    def _get_events():
        service = _get_calendar_service()
        return service.events().list(
            calendarId=calendar_id, timeMin=day_start, timeMax=day_end,
            singleEvents=True, orderBy='startTime', timeZone='Asia/Shanghai'
        ).execute().get('items', [])

    events = await asyncio.to_thread(_get_events)
    greeting = "早安！今天的日程：" if is_morning else f"{label}的日程，提前准备："
    if events:
        msg = f"📅 {label}{greeting}\n"
        for e in events:
            raw_dt = e['start'].get('dateTime')
            if not raw_dt:
                continue
            dt_start = datetime.datetime.fromisoformat(raw_dt.replace('Z', '+00:00'))
            if dt_start.tzinfo is None:
                dt_start = dt_start.replace(tzinfo=datetime.timezone.utc)
            dt_bj = dt_start.astimezone(datetime.timezone(datetime.timedelta(hours=8)))
            msg += f"🔹 {dt_bj.strftime('%H:%M')} - {e.get('summary', '未知')}\n"
        await asyncio.to_thread(_push_wechat, msg, f"📅 {label}日程播报")
    else:
        await asyncio.to_thread(_push_wechat, f"📅 {label}没有日程安排，好好休息～", f"📅 {label}日程播报")


# ==========================================
# 6. 信箱巡视器 (邮件)
# ==========================================

async def async_email_secretary():
    """定期检查新邮件并通知 (可通过 GMAIL_BRIDGE_URL 配置桥接地址)。"""
    from server import (
        _get_llm_client, _push_wechat, _save_memory_to_db,
        _clean_email_body, MY_EMAIL, http_session
    )

    print("📭 信箱巡视神经已接入...")
    BRIDGE_URL = os.environ.get("GMAIL_BRIDGE_URL", "").strip()
    if not BRIDGE_URL:
        print("⚠️ 未配置 GMAIL_BRIDGE_URL，信箱巡视暂时休眠。")
        return

    processed_email_ids = set()

    while True:
        try:
            def _fetch():
                resp = http_session.get(BRIDGE_URL, timeout=20)
                return resp.json() if resp.status_code == 200 else []
            raw_new_emails = await asyncio.to_thread(_fetch)

            if raw_new_emails:
                for mail in raw_new_emails:
                    mail_id = mail.get('id', '')
                    if mail_id in processed_email_ids:
                        continue
                    # 过滤掉自己发的和系统邮件
                    sender = mail.get('from', '').lower()
                    my_email_lower = MY_EMAIL.lower() if MY_EMAIL else ""
                    if "onboarding@resend.dev" in sender or (my_email_lower and my_email_lower in sender):
                        processed_email_ids.add(mail_id)
                        continue

                    # 通知用户收到新邮件
                    subject = mail.get('subject', '无标题')
                    tg_msg = f"📧 收到新邮件: {subject} (来自 {mail.get('from', '未知')})"
                    await asyncio.to_thread(_push_wechat, tg_msg, "📧 信箱提醒")
                    await asyncio.to_thread(
                        _save_memory_to_db, "📧 信箱处理",
                        f"收到邮件: {subject}", "流水", "尽责", "Email_Process"
                    )
                    processed_email_ids.add(mail_id)
        except Exception:
            pass

        await asyncio.sleep(300)


# ==========================================
# 7. 环境变量热同步
# ==========================================

async def async_env_sync():
    """定时从数据库 user_facts.sys_config 读取配置，热更新到环境变量。"""
    from server import supabase, ORIGINAL_ENV

    print("⚙️ 环境变量热同步神经已上线...")
    # 支持热同步的键列表 (可通过环境变量扩展)
    default_sync_keys = [
        "DEFAULT_API_KEY", "DEFAULT_BASE_URL", "DEFAULT_MODEL_NAME",
        "TG_BOT_TOKEN", "TG_CHAT_ID",
        "EMAIL_API_KEY", "EMAIL_FROM", "ADMIN_EMAIL",
        "AI_PERSONA", "MEM0_USER_ID",
    ]
    extra_keys = [k.strip() for k in os.environ.get("SYNC_KEYS", "").split(",") if k.strip()]
    sync_keys = list(set(default_sync_keys + extra_keys))

    while True:
        try:
            if supabase:
                def _sync():
                    res = supabase.table("user_facts").select("value").eq("key", "sys_config").execute()
                    if res.data:
                        conf = json.loads(res.data[0]['value'])
                        for k in sync_keys:
                            val = str(conf.get(k, "")).strip()
                            if val:
                                os.environ[k] = val
                            else:
                                if k in ORIGINAL_ENV:
                                    os.environ[k] = ORIGINAL_ENV[k]
                                elif k in os.environ:
                                    del os.environ[k]
                await asyncio.to_thread(_sync)
        except Exception:
            pass
        await asyncio.sleep(10)


# ==========================================
# 8. 启动入口
# ==========================================

# ==========================================
# 启动入口 (双进程架构)
# ==========================================
# 说明：
#   进程 A (消息进程 / server.py)：只负责"活人在等着"的实时场景。
#       -> start_message_process_bg()  只拉起 TG 轮询 (实时收消息)
#   进程 B (后台进程 / background.py)：负责所有"没人催"的自主任务。
#       -> run_background_process()     跑主动思考/日记/总结/提醒/日程/邮件/热同步
#
#   两个进程通过 run.py 统一拉起，只通过数据库共享状态，各自独立事件循环/内存。
#   保留 start_autonomous_life() 作为「单进程模式」兼容入口 (直接 python server.py 时全跑)。


def start_message_process_bg():
    """进程 A (消息进程) 的后台线程：仅启动 Telegram 实时轮询。

    QQ (NapCat 反向 WS) 由 gateway/napcat 在主事件循环里处理，无需在此起线程。
    """
    def _run_tg_polling(): asyncio.run(async_telegram_polling())

    if os.environ.get("TG_BOT_TOKEN", "").strip():
        threading.Thread(target=_run_tg_polling, daemon=True).start()
        print("📨 [进程A] Telegram 实时轮询已启动")
    else:
        print("📨 [进程A] 未配置 TG_BOT_TOKEN，跳过 Telegram 轮询")

    print("🐱 NapCat QQ 端点已就绪 (被动模式)，等待本地 NapCat 通过反向 WS 连接...")


async def run_background_process():
    """进程 B (后台进程) 主协程：把所有自主/定时任务跑在同一个事件循环里。

    与旧版 daemon 线程 + asyncio.run 不同，这里用 asyncio.gather 统一调度，
    任一任务异常退出会向上抛，交给 run.py 感知并整体重启 (不留半残状态)。
    """
    tasks = [
        asyncio.create_task(async_env_sync(),           name="env_sync"),
        asyncio.create_task(async_autonomous_life(),    name="autonomous_life"),
        asyncio.create_task(async_diary_worker(),       name="diary"),
        asyncio.create_task(async_message_summarizer(), name="msg_summarizer"),
        asyncio.create_task(async_reminder_worker(),    name="reminder"),
        asyncio.create_task(async_schedule_secretary(), name="schedule"),
    ]

    # 信箱巡视默认关闭 (需配置 GMAIL_BRIDGE_URL 才有意义)；配了才启用
    if os.environ.get("GMAIL_BRIDGE_URL", "").strip():
        tasks.append(asyncio.create_task(async_email_secretary(), name="email"))
        print("📮 [进程B] 信箱巡视器已启用")

    print(f"🌙 [进程B] 后台进程已启动，共 {len(tasks)} 个自主任务")

    # 任一任务先结束 (通常意味着崩溃)，就取消其余任务并抛出异常，让 run.py 整体重启
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    for t in pending:
        t.cancel()
    # 把先结束任务的异常暴露出来 (若正常结束则不会有异常)
    for t in done:
        exc = t.exception()
        if exc is not None:
            raise exc


def start_autonomous_life():
    """[兼容] 单进程模式：在 daemon 线程里把 A+B 的所有任务全部拉起。

    仅当直接 `python server.py` (不走 run.py 双进程) 时使用，便于本地快速调试。
    生产环境请用 `python run.py` 走双进程。
    """
    def _run_all(): asyncio.run(run_background_process())

    start_message_process_bg()
    threading.Thread(target=_run_all, daemon=True).start()
    print("⚙️  [单进程模式] 后台任务已在 daemon 线程中启动 (生产建议用 run.py 双进程)")
    print("🌾 所有后台心跳线程已启动。")
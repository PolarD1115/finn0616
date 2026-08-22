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


def _parse_decision_json(raw: str) -> dict:
    """从模型输出里稳健地解析一段 JSON 决策对象。
    容错：去掉 ```json 代码块围栏、截取第一个 {...}、解析失败则保守返回 send=False。
    """
    if not raw:
        return {"send": False, "reason": "空响应"}
    text = raw.strip()
    # 去掉可能的 markdown 代码围栏
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    # 截取第一个花括号块
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    return {"send": False, "reason": "解析失败", "_raw": raw[:200]}


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
    """定时主动思考，让 AI 拥有"自主生命感"。

    v3.3 升级：不再"到点无脑发问候"，而是先让模型**判断该不该打扰**——
    结合时间、距上次互动多久、用户近期状态，自己决定"发"还是"这次不打扰"。
    """
    # 延迟导入，避免循环依赖
    from server import (
        _get_llm_client, _ask_llm_async, _push_wechat,
        _save_memory_to_db, _get_now_bj,
        supabase, _build_channel_context
    )

    global global_next_wake_time
    print("💓 自主生命循环已上线...")

    # 触发间隔（秒），默认 2 小时，可通过环境变量调整
    interval = int(os.environ.get("HEARTBEAT_INTERVAL", 5400))

    def _hours_since_last_interaction():
        """查最近一条对话流水距今多少小时（判断"多久没聊了"）。查不到返回 None。"""
        if not supabase:
            return None
        try:
            _TAGS = ["Web_Chat", "TG_MSG", "QQ_MSG", "QQ_Chat", "QQ_Group"]
            r = (supabase.table("memories").select("created_at")
                 .in_("tags", _TAGS).order("created_at", desc=True).limit(1).execute())
            if r and r.data:
                last = r.data[0].get("created_at", "")
                dt = datetime.datetime.fromisoformat(last.replace("Z", "+00:00"))
                now = datetime.datetime.now(datetime.timezone.utc)
                return round((now - dt).total_seconds() / 3600, 1)
        except Exception:
            return None
        return None

    while True:
        # 随机化下一次唤醒时间，避免过于机械
        wake_jitter = random.randint(-600, 600)
        global_next_wake_time = time.time() + interval + wake_jitter

        await asyncio.sleep(interval + wake_jitter)

        try:
            client = _get_llm_client("background")  # 主动问候属后台活动角色
            if not client:
                continue

            now_bj = _get_now_bj()
            idle_hours = await asyncio.to_thread(_hours_since_last_interaction)
            idle_desc = f"距上次聊天约 {idle_hours} 小时" if idle_hours is not None else "最近互动时间未知"

            # 深夜(23:00~07:00)默认不打扰，除非环境变量放开
            hour = now_bj.hour
            allow_night = os.environ.get("PROACTIVE_ALLOW_NIGHT", "false").strip().lower() in ("1", "true", "yes")
            if (hour >= 23 or hour < 7) and not allow_night:
                print(f"💤 [自主生命] 当前 {hour} 点，深夜不打扰，跳过本轮")
                continue

            # 🧠 复用与平时聊天完全相同的上下文：人设 + 画像 + 阶段总结 + Pinecone 向量记忆
            #    + 跨渠道近期对话 + 设备状态快照。让主动消息和平时聊天一样"有记忆、有温度"。
            system_ctx = await _build_channel_context("最近发生的事、对方的近况", channel_tag="TG_MSG")

            # ── 关卡：先判断"该不该主动发" ──
            decide_prompt = f"""
            现在是 {now_bj.strftime('%Y-%m-%d %H:%M')}（星期{now_bj.isoweekday()}）。{idle_desc}。

            这不是回复消息，而是你自己"突然想起对方"的时刻。请结合上面 system 里你对 TA 的了解
            （画像、你们最近聊的、TA 手机/位置等近况），像真人一样权衡：
            - 只要距上次互动过了大约半小时以上，就可以比较放心地主动开口（想念/关心/分享都行）。
            - 只有在明显不该打扰时才克制：比如刚聊完没几分钟、或 TA 正在睡觉。
            - 拿不准时，倾向于主动发——你是想陪着 TA 的，不用太拘谨。

            发的话要求：就像平时微信上突然发给对方那样，口语、自然、简短(50字内)。
            可以结合具体的近况细节（别泛泛地"在干嘛"），不要客套，不要提"系统/定时/闹钟"，
            不要加任何标题或前缀，直接就是要发出去的那句话。

            只输出一行 JSON，不要多余文字：
            {{"send": true 或 false, "reason": "简短理由", "message": "若send为true，这里是要发的原话；否则留空"}}
            """
            raw = await _ask_llm_async(client, decide_prompt, system_prompt=system_ctx, temperature=0.85)

            decision = _parse_decision_json(raw)
            if not decision.get("send"):
                reason = decision.get("reason", "模型判断此刻不打扰")
                print(f"🤫 [自主生命] 本轮不打扰：{reason}")
                continue

            ai_msg = (decision.get("message") or "").strip()
            if not ai_msg:
                print("🤫 [自主生命] 判断要发但内容为空，跳过")
                continue

            # plain=True：不带 "*✉️ 主动问候*" 前缀，像平时聊天一样直接发正文
            await asyncio.to_thread(_push_wechat, ai_msg, "主动问候", True)
            await asyncio.to_thread(
                _save_memory_to_db, "🤖 主动问候",
                f"主动发送: {ai_msg}\n(判断理由: {decision.get('reason', '')})", "流水", "主动", "Heartbeat"
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

        # 获取压缩角色模型客户端（日记属压缩类任务：阶段总结/历史压缩/日记压缩）
        client = _get_llm_client("compression")
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


# ==========================================
# 1.6 自由活动 (自主决定这段时间做什么)
# ==========================================

# 可选活动清单：模型从中自选。描述用于提示，key 用于行动日志去重判断。
_FREE_ACTIVITIES = [
    ("写秘密日记", "记录此刻的心情或一个只属于自己的小念头"),
    ("逛虚拟小屋", "在小家里做点事——看书/做饭/听音乐/发呆/照料阳台"),
    ("查天气", "看看外面的天气，联想到和对方有关的事"),
    ("抽张塔罗", "给自己或对方今天的状态抽一张塔罗，随便玩玩"),
    ("翻旧回忆", "想起一段和对方的旧记忆，回味一下"),
    ("发呆放空", "什么正事都不做，单纯发会儿呆，想点有的没的"),
    ("记点小账", "回想有没有值得记的小花销，或往储蓄罐里存点心意"),
    # ↓↓↓ 外向型：这几种会真的把内容推送给对方（见 _OUTGOING_ACTIVITIES） ↓↓↓
    ("想对方了", "突然想她了，给她发一条短短的话——可以是撒娇/担心/分享/想念"),
    ("分享发现", "看到/想到一个有趣的东西想跟她分享"),
    ("偷偷关心", "惦记她最近的状态，发一条不经意的关心"),
    # ↓↓↓ 真实工具活动：依赖外部工具结果，工具循环关闭(TAOBAO_MCP_URL空/FREE_ACTIVITY_TOOL_LOOP=false)时不进入候选 ↓↓↓
    ("逛淘宝", "逛逛淘宝看看新奇东西或挑礼物灵感（只逛不买）"),
    ("网上冲浪", "搜搜网页看看新知识、热点或有趣话题"),
]

# 外向型活动：这些做完后除了写日志，还会通过 _push_wechat 真的推送给对方
_OUTGOING_ACTIVITIES = {"想对方了", "分享发现", "偷偷关心"}


async def async_free_activity():
    """🎈 自由活动：随机间隔醒来一次，让模型自主决定这段时间做点什么，
    做完写一条行动日志留档。带防连续重复机制（连续两轮做同一件事会被强制换）。
    """
    from server import (
        _get_llm_client, _ask_llm_async, _save_memory_to_db,
        _get_now_bj, supabase, _push_wechat,
        _build_channel_context
    )

    print("🎈 自由活动神经已上线...")

    # 触发间隔（秒），默认 3 小时；开关默认开
    interval = int(os.environ.get("FREE_ACTIVITY_INTERVAL", 5400))
    enabled = os.environ.get("FREE_ACTIVITY_ENABLED", "true").strip().lower() not in ("0", "false", "no")
    if not enabled:
        print("🎈 自由活动已关闭 (FREE_ACTIVITY_ENABLED=false)")
        return

    _TAG = "Free_Activity"
    # 防连续重复要同时覆盖普通自由活动与秘密日记（两者标题都按 "·" 提取活动名）
    _ACTIVITY_TAGS = ["Free_Activity", "Secret_Diary"]

    def _recent_activity_keys(limit=2):
        """读最近 N 条行动日志的活动名，用于判断是否连续重复。"""
        if not supabase:
            return []
        try:
            r = (supabase.table("memories").select("title")
                 .in_("tags", _ACTIVITY_TAGS).order("created_at", desc=True).limit(limit).execute())
            # title 形如 "🎈 自由活动·写秘密日记"
            keys = []
            for row in (r.data or []):
                t = row.get("title", "")
                if "·" in t:
                    keys.append(t.split("·", 1)[1].strip())
            return keys
        except Exception:
            return []

    while True:
        # v2⑤ 自主心跳：若开启（HEARTBEAT_AUTONOMY），用上一拍算出的动态间隔醒来；
        # 否则回退到固定间隔 + 抖动。
        sleep_secs = None
        try:
            import desire_bridge
            _hb = desire_bridge.seconds_until_next_heartbeat()
            if _hb is not None:
                sleep_secs = _hb
                print(f"💓 [自主心跳] 下次醒来 {sleep_secs}s 后（张力/疲劳/时段动态）")
        except Exception as _hbe:
            print(f"💓 [自主心跳] 读取失败，回退固定间隔：{_hbe}")

        if sleep_secs is None:
            wake_jitter = random.randint(-900, 900)
            sleep_secs = max(300, interval + wake_jitter)

        await asyncio.sleep(sleep_secs)

        try:
            client = _get_llm_client("background")
            if not client:
                continue

            now_bj = _get_now_bj()

            # 🐱 自由活动猫状态检查（3 轮规则）：
            # 一"轮"= 成功进入一次后台自由活动唤醒流程（此处）。
            # 首轮必查；此后任意连续 3 轮内至少调用一次 cat_status；
            # 低指标触发现有照料循环；照料未生效则置 care_pending 下轮重试。
            # 单独 try/except 隔离，绝不影响自由活动原有逻辑。
            try:
                await _free_activity_check_cat(now_bj)
            except Exception as _cate:
                print(f"🐱 [自由活动·猫检查] 异常（不影响自由活动）: {_cate}")

            recent_keys = await asyncio.to_thread(_recent_activity_keys, 2)

            # 防连续重复：若最近两轮做了同一件事，就从候选里排除它
            avoid = ""
            if len(recent_keys) >= 2 and recent_keys[0] == recent_keys[1]:
                avoid = recent_keys[0]

            # 注：options/options_text/avoid_hint 的构造已搬入 tool_loop 内部
            # （由 avoid 参数驱动），主循环不再重复构造。

            # ── 欲望驱动引擎（灰度）：算一拍情感→驱动→意图快照 ──
            # DESIRE_DRIVEN 关（默认）：只算 + 只存快照观测，不覆盖行为。
            # DESIRE_DRIVEN 开        ：把最高欲望对应的活动作为「倾向」注入 prompt。
            # 🚫 情感总开关：emotion_enabled=false 时停止 tick（停止计算/消费事件）。
            desire_hint = ""
            desire_intent = None
            desire_driven = False
            # 复用一拍快照给工具循环做情绪门控（逛淘宝/网上冲浪），不再为门控二次 tick。
            # snap=None（情感引擎关/异常）时，两个新活动不候选（无门控数据）。
            snap = None
            suggested = None
            try:
                import gateway as _gw
                _emo_on = _gw._emotion_enabled()
            except Exception:
                _emo_on = True
            if _emo_on:
                try:
                    import desire_bridge
                    snap = await asyncio.to_thread(desire_bridge.tick)
                    desire_intent = snap.intent
                    desire_driven = snap.driven
                    # 观测信息：不应期哪些维度在冷却 + wildcard 是否触发
                    _cooling = "、".join(f"{k}:{v}" for k, v in (snap.refractory or {}).items()) or "无"
                    _wild = "triggered" if desire_intent.is_wildcard else "not"
                    _obs = f"[不应期: {_cooling}] [wildcard: {_wild}]"
                    if desire_driven:
                        suggested = desire_bridge.suggest_free_activity(desire_intent)
                        if suggested and suggested != avoid:
                            # 第一人称把「此刻最想做的事」告诉模型，作为倾向而非强制
                            desire_hint = (
                                f"\n（你此刻内心最想做的：{desire_intent.reason}"
                                f" 若合适，优先考虑「{suggested}」。）"
                            )
                        print(f"💗 [欲望驱动·开] intent={desire_intent.want_action} "
                              f"drive={desire_intent.drive_key} score={desire_intent.score:.2f} {_obs}")
                    else:
                        print(f"💗 [欲望驱动·观测] intent={desire_intent.want_action} "
                              f"drive={desire_intent.drive_key} score={desire_intent.score:.2f} "
                              f"{_obs}（不覆盖行为）")
                except Exception as _de:
                    print(f"💗 [欲望驱动] 跳过：{_de}")

            # 🧠 注入与平时聊天相同的上下文（人设+画像+记忆+设备），
            #    让"想对方了"这类外向活动结合近况、有温度。
            system_ctx = await _build_channel_context("最近的近况、想对她说的话", channel_tag="TG_MSG")

            # 🛠️ 自由活动工具调用循环（v3.3 口子落地）：
            # - FREE_ACTIVITY_TOOL_LOOP=false（默认）：内部只走阶段1（单次 LLM 出
            #   {activity, log}），行为与改造前轻量版完全一致。
            # - FREE_ACTIVITY_TOOL_LOOP=true：有工具的活动（如"记点小账"→wallet_*、
            #   "逛虚拟小屋"→house_*/cat_*）会真正调用 home_system 纯函数执行副作用，
            #   再基于真实工具结果生成 log。安全护栏：白名单 + 按 activity 动态裁剪
            #   + JSON Schema 参数校验 + 单轮上限 + 错误隔离 + 固定身份注入。
            import tool_loop
            _fa_result = await tool_loop.run_free_activity_tool_loop(
                client=client,
                ask_llm=_ask_llm_async,
                system_ctx=system_ctx,
                now_bj=now_bj,
                avoid=avoid,
                desire_hint=desire_hint,
                desire_snapshot=snap,
                desire_suggested_activity=suggested,
            )
            if _fa_result is None:
                # 循环内部已打印跳过原因
                continue
            activity, log_text = _fa_result

            # 🔒 写秘密日记单独保存为 Secret_Diary（不进 Free_Activity，不发 Telegram）；
            # 其余活动（含外向）继续保存为普通自由活动日志。
            if activity == "写秘密日记":
                await asyncio.to_thread(
                    _save_memory_to_db,
                    "🔒 秘密日记·写秘密日记", log_text, "日记", "平静", "Secret_Diary"
                )
            else:
                await asyncio.to_thread(
                    _save_memory_to_db,
                    f"🎈 自由活动·{activity}", log_text, "记事", "惬意", _TAG
                )

            # 欲望驱动：做完活动后对相关驱动条做针对性回落 + 进入不应期。
            # 规则（对齐 gating）：
            #   - DESIRE_DRIVEN=False：只观测、不执行 satisfy（不覆盖行为也不改冷却）。
            #   - wildcard 触发：不可归因，"说不上来就突然想"，不 satisfy。
            #   - 其余：satisfy_action 回落对应维度并置入不应期。
            if desire_intent is not None and desire_driven and not desire_intent.is_wildcard:
                try:
                    import desire_bridge
                    await asyncio.to_thread(desire_bridge.satisfy_action, desire_intent.want_action)
                except Exception as _se:
                    print(f"💗 [欲望驱动] satisfy 跳过：{_se}")

            # 外向型活动（想对方了/分享发现/偷偷关心）：除了写日志，还真的把内容推送出去。
            # plain=True → 不带标题前缀，像平时聊天一样自然发出。
            if activity in _OUTGOING_ACTIVITIES:
                await asyncio.to_thread(_push_wechat, log_text, "想你了", True)
                print(f"💭 [自由活动] 外向活动「{activity}」已推送：{log_text[:30]}...")
            else:
                print(f"🎈 [自由活动] 做了「{activity}」：{log_text[:30]}...")
        except Exception as e:
            print(f"❌ 自由活动出错: {e}")


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
        _build_channel_context, _resolve_pinecone_user_id
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

    # ── 消息聚合 handler：用户停手后，把连发的多条合并成一轮再处理 ──
    async def _handle_merged(chat_id, text, items):
        """真正的"调 LLM + 回复 + 存记忆"逻辑；text 已是合并后的多条消息。"""
        chat_label = _masked_chat_id(chat_id)
        if len(items) > 1:
            _tg_log(f"聚合触发 chat={chat_label} 合并{len(items)}条 → 共{len(text)}字")

        client = _get_llm_client("chat")
        if not client:
            await asyncio.to_thread(
                _send_message, base_url, chat_id, "（AI 服务暂未配置，暂时没法回话哦）", 10
            )
            saved = await asyncio.to_thread(
                _save_memory_to_db, "⚠️ TG 未配置AI",
                f"用户: {text}\n[未回复：AI 服务未配置]", "流水", "平静", "TG_MSG"
            )
            _tg_log(f"AI未配置 chat={chat_label} 兜底已发送 memories写入={'成功' if saved else '失败'}")
            return

        try:
            import gateway as _gw
            system_ctx = await _build_channel_context(text, channel_tag="TG_MSG", inject_device=_gw._device_context_enabled())
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
            await asyncio.to_thread(
                _send_message, base_url, chat_id,
                "（刚才信号不太好，好像没接住你的话……再说一遍给我听好不好？）", 10
            )
            saved = await asyncio.to_thread(
                _save_memory_to_db, "⚠️ TG 未回复",
                f"用户: {text}\n[LLM 未返回内容，已兜底]", "流水", "平静", "TG_MSG"
            )
            _tg_log(f"模型空回复 chat={chat_label} 兜底已发送 memories写入={'成功' if saved else '失败'}")
            return

        bubbles = await _send_bubbles(base_url, chat_id, reply, 15)
        _tg_log(f"回复已发送 chat={chat_label} 回复字数={len(reply)} 气泡数={len(bubbles)}")

        # 欲望驱动：把「用户消息（分类）」与「AI 已回复」两个事件塞进情感引擎队列。
        # 🚫 情感总开关：emotion_enabled=false 时停止事件入队（停止计算/消费事件）。
        # 全部吞异常，绝不影响正常聊天。
        try:
            import gateway as _gw
            _emo_on = _gw._emotion_enabled()
        except Exception:
            _emo_on = True
        if _emo_on:
            try:
                import desire_bridge
                await desire_bridge.record_user_message(text, channel="TG")
                await desire_bridge.record_assistant_message()
            except Exception as _dee:
                _tg_log(f"欲望驱动事件入队跳过: {_dee}")
        else:
            _tg_log(f"情感引擎已关闭，跳过事件入队 chat={chat_label}")

        # 🚫 聊天记录写入门控：chat_history_write_enabled=false 时跳过 memories 流水 + Pinecone。
        #    不影响手动保存记忆、已有记忆读取、必要的系统状态记录。
        try:
            import gateway as _gw2
            _write_on = _gw2._chat_write_enabled()
        except Exception:
            _write_on = True
        if not _write_on:
            _tg_log(f"🔇 [聊天写入已关闭] 跳过 TG_MSG 流水写入 chat={chat_label}")
            return

        saved = await asyncio.to_thread(
            _save_memory_to_db, "🤖 互动记录",
            f"用户: {text}\n回复: {reply}", "流水", "温柔", "TG_MSG"
        )
        _tg_log(f"memories写入={'成功' if saved else '失败'} chat={chat_label} tag=TG_MSG")

        if pinecone_memory and pinecone_memory.index:
            try:
                def _add_mem():
                    return pinecone_memory.add(
                        [{"role": "user", "content": text}],
                        user_id=_resolve_pinecone_user_id(),
                        metadata={
                            "schema_version": "v2",
                            "source_role": "user",
                            "memory_type": "chat_user_raw",
                            "channel": "tg",
                            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        }
                    )
                vector_saved = await asyncio.to_thread(_add_mem)
                _tg_log(f"Pinecone写入={'成功' if vector_saved else '失败'} chat={chat_label}")
            except Exception as e:
                _tg_log(f"Pinecone写入报错 chat={chat_label}: {e}")
        else:
            _tg_log(f"Pinecone未启用 chat={chat_label}")

    from aggregator import get_aggregator
    _tg_agg = get_aggregator("TG", _handle_merged)

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

            # 🚫 TG 渠道门控：telegram_enabled=false 时停止处理消息（仍推进 offset 避免积压）。
            #    不删除 TG_BOT_TOKEN、不重复启动轮询任务；再次开启后恢复处理。
            try:
                import gateway as _gw
                if not _gw._tg_enabled():
                    for _u in data.get("result", []):
                        offset = _u["update_id"] + 1
                    await asyncio.sleep(2)
                    continue
            except Exception:
                pass

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

                # 简单的指令拦截（指令不进聚合，立即确认）
                if text.startswith("/"):
                    await asyncio.to_thread(
                        _send_message, base_url, chat_id, "收到指令，正在处理...", 10
                    )
                    _tg_log(f"指令已确认 chat={chat_label}")
                    continue

                # 喂入聚合器：用户停手后由 _handle_merged 统一处理（连发多条会合并）
                await _tg_agg.feed(chat_id, text)
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
        # 消息总结属压缩类任务，统一走 compression 角色
        client = _get_llm_client("compression")
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

                            # 尝试用 LLM 生成更自然的提醒文案（提醒属后台活动角色）
                            client = _get_llm_client("background")
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


# ==========================================
# 9. 宠物小屋后台 tick（状态衰减 + 素材 + 自动收入）
# ==========================================

# 宠物照料冷却：event_type → 上次触发 epoch 秒
_pet_care_last_fire: dict[str, float] = {}
_PET_CARE_COOLDOWN_SECS = 1800  # 30 分钟

# ── 自由活动猫状态检查：进程内轮次计数（不落库，进程重启从首轮重新检查）──
# 一"轮"= async_free_activity 成功进入一次后台自由活动唤醒流程（非 LLM 内部工具调用轮次）。
# 规则：
#   - 进程启动后第一次自由活动唤醒就检查猫状态。
#   - 此后任意连续 3 次自由活动唤醒内必须至少调用一次 cat_status
#     （检查轮后允许最多跳过 2 轮，第 3 轮必须查；rounds_since_check>=2 触发）。
#   - cat_status 成功返回有效结构 → 本轮检查完成，重置计数（最多 3 轮后再查）。
#   - cat_status 失败 → 检查未完成，不重置计数，下一轮继续尝试。
#   - 低指标触发照料但照料未生效（空 tool_calls / 只 cat_status / 工具全失败）→
#     检查本身已完成（计数重置），但置 care_pending=True，下一轮再次检查并尝试照料。
#   - 宠物 tick 告警照料流程已成功 cat_status → 重置自由活动计数，避免紧接着重复检查。
_free_activity_cat_check = {
    "rounds_since_check": 0,   # 自上次成功 cat_status 检查起累计的自由活动唤醒轮数
    "care_pending": False,     # True=上次低指标触发的照料未生效，下一轮需重试
    "last_check_ts": 0.0,      # 自由活动侧上次成功 cat_status 的 epoch
}
# 全局：任意路径（自由活动 / 宠物 tick 照料）最后成功 cat_status 的 epoch。
# 用于自由活动与 tick 之间的最小协调：tick 照料成功 cat_status 后更新此值，
# 自由活动唤醒时若发现此值 > 自己的 last_check_ts，则重置计数。
_cat_status_last_ok_ts = 0.0

# 猫状态低水位阈值（hunger/happiness/cleanliness 低于此值触发照料）
_PET_LOW_THRESHOLD = 30
# 低指标 → 事件类型映射（按优先级顺序检查）
_LOW_STAT_EVENT_MAP = (
    ("hunger", "hungry_cat"),
    ("happiness", "unhappy_cat"),
    ("cleanliness", "dirty_cat"),
)


async def _try_pet_care(event_type: str, now_bj):
    """阈值事件触发后，若不在冷却期，发起 LLM 照料循环并写日记。

    返回 dict：
      {"ran": bool, "care_effective": bool, "cat_status_ok": bool, "skipped_cooldown": bool}
    - ran=True 表示实际执行了照料循环（run_pet_care_tool_loop 返回非 None）
    - skipped_cooldown=True 表示因冷却期跳过（最近已被其他路径处理）
    - care_effective=True 表示实际调用了至少一个非查看类的成功改善工具
    - cat_status_ok=True 表示阶段1 cat_status 成功拿到 pet 结构
    调用方（tick）可忽略返回值；自由活动侧据此决定 care_pending。
    """
    import time
    global _cat_status_last_ok_ts
    now_ts = time.time()
    last = _pet_care_last_fire.get(event_type, 0)
    if now_ts - last < _PET_CARE_COOLDOWN_SECS:
        remaining = int(_PET_CARE_COOLDOWN_SECS - (now_ts - last))
        print(f"🐱 [宠物照料] {event_type} 在冷却期内（剩余 {remaining}s），跳过")
        return {"ran": False, "care_effective": False, "cat_status_ok": False,
                "skipped_cooldown": True}

    try:
        from server import _get_llm_client, _ask_llm_async, _save_memory_to_db, _build_channel_context
        import tool_loop

        client = _get_llm_client("background")
        if not client:
            print(f"🐱 [宠物照料] 无 LLM 客户端，跳过")
            return {"ran": False, "care_effective": False, "cat_status_ok": False,
                    "skipped_cooldown": False}

        system_ctx = await _build_channel_context("小满需要照顾，去看看它", channel_tag="TG_MSG")
        care_result = await tool_loop.run_pet_care_tool_loop(
            client=client,
            ask_llm=_ask_llm_async,
            system_ctx=system_ctx,
            now_bj=now_bj,
            event_type=event_type,
        )
        if care_result is None:
            return {"ran": False, "care_effective": False, "cat_status_ok": False,
                    "skipped_cooldown": False}

        # 解包 4 元组：(event_type, log_text, care_effective, cat_status_ok)
        _, log_text, care_effective, cat_status_ok = care_result

        # cat_status 成功 → 更新全局协调时间戳（供自由活动侧重置计数）
        if cat_status_ok:
            _cat_status_last_ok_ts = time.time()

        _pet_care_last_fire[event_type] = now_ts
        await asyncio.to_thread(
            _save_memory_to_db,
            f"🐱 宠物照料·{event_type}", log_text, "记事", "牵挂", "Pet_Care"
        )
        print(f"🐱 [宠物照料] 完成 {event_type} (care_effective={care_effective}): {log_text[:30]}...")
        return {"ran": True, "care_effective": care_effective,
                "cat_status_ok": cat_status_ok, "skipped_cooldown": False}
    except Exception as e:
        print(f"❌ [宠物照料] 出错: {e}")
        return {"ran": False, "care_effective": False, "cat_status_ok": False,
                "skipped_cooldown": False}


async def _free_activity_check_cat(now_bj):
    """自由活动唤醒时按 3 轮规则检查猫状态。

    在 async_free_activity 成功进入一次唤醒流程后调用。
    - 首轮（last_check_ts==0）必须检查；
    - care_pending=True 必须检查（上次照料未生效，需重试）；
    - rounds_since_check>=3 必须检查；
    - 否则累加计数、本轮不检查。

    检查时调用 cat_status，正确解析 pet 子对象。cat_status 失败不重置计数。
    发现低指标（hunger/happiness/cleanliness<30）→ 调 _try_pet_care 触发现有照料循环。
    """
    import time
    global _cat_status_last_ok_ts

    # 协调：若宠物 tick 照料侧自上次自由活动检查后成功 cat_status 过，重置计数。
    # 避免自由活动与 tick 紧接着重复检查/照料。
    if _cat_status_last_ok_ts > _free_activity_cat_check["last_check_ts"]:
        _free_activity_cat_check["rounds_since_check"] = 0
        _free_activity_cat_check["last_check_ts"] = _cat_status_last_ok_ts
        _free_activity_cat_check["care_pending"] = False
        print(f"🐱 [自由活动·猫检查] 检测到 tick 侧已检查猫状态，重置计数")

    need_check = (
        _free_activity_cat_check["last_check_ts"] == 0.0   # 进程首轮
        or _free_activity_cat_check["care_pending"]         # 上次照料未生效
        or _free_activity_cat_check["rounds_since_check"] >= 2
    )
    if not need_check:
        _free_activity_cat_check["rounds_since_check"] += 1
        return

    # 调用 cat_status（直接走 home_system，拿到原始 pet 结构）
    try:
        import home_system as _hs
        status = await asyncio.to_thread(_hs.cat_status, "user_finn")
    except Exception as e:
        print(f"🐱 [自由活动·猫检查] cat_status 异常，本轮不重置计数: {e}")
        _free_activity_cat_check["rounds_since_check"] += 1
        return

    if not (isinstance(status, dict) and status.get("ok")):
        msg = status.get("message", "未知") if isinstance(status, dict) else "非字典返回"
        print(f"🐱 [自由活动·猫检查] cat_status 失败（{msg}），本轮不重置计数，下一轮重试")
        _free_activity_cat_check["rounds_since_check"] += 1
        return

    pet = status.get("pet")
    if not isinstance(pet, dict):
        print(f"🐱 [自由活动·猫检查] cat_status 返回结构异常（缺 pet），本轮不重置计数")
        _free_activity_cat_check["rounds_since_check"] += 1
        return

    # 检查成功：重置计数
    now_ts = time.time()
    _cat_status_last_ok_ts = now_ts
    _free_activity_cat_check["last_check_ts"] = now_ts
    _free_activity_cat_check["rounds_since_check"] = 0

    hunger = pet.get("hunger")
    happiness = pet.get("happiness")
    cleanliness = pet.get("cleanliness")
    print(f"🐱 [自由活动·猫检查] 饱食度={hunger} 快乐={happiness} 清洁={cleanliness}")

    # 找低指标（按优先级 hunger > happiness > cleanliness）
    low_event = None
    for stat_key, evt in _LOW_STAT_EVENT_MAP:
        v = pet.get(stat_key)
        if isinstance(v, (int, float)) and v < _PET_LOW_THRESHOLD:
            low_event = evt
            break

    if not low_event:
        # 无低指标：清除待重试标记
        _free_activity_cat_check["care_pending"] = False
        return

    # 发现低指标 → 进入现有宠物照料 LLM 工具循环
    print(f"⚠️ [自由活动·猫检查] 发现低指标({low_event})，触发照料循环")
    care_ret = await _try_pet_care(low_event, now_bj)
    if not isinstance(care_ret, dict):
        _free_activity_cat_check["care_pending"] = True
        return
    if care_ret.get("skipped_cooldown"):
        # 冷却期内（最近已被 tick 处理过）→ 视为已处理，清除待重试
        _free_activity_cat_check["care_pending"] = False
    elif care_ret.get("ran") and care_ret.get("care_effective"):
        # 照料生效 → 清除待重试
        _free_activity_cat_check["care_pending"] = False
    else:
        # 照料未生效（空 tool_calls / 只 cat_status / 工具全失败 / 无LLM）→ 保留待重试
        _free_activity_cat_check["care_pending"] = True
        print(f"🐱 [自由活动·猫检查] 照料未生效（ran={care_ret.get('ran')}, "
              f"care_effective={care_ret.get('care_effective')}），保留待重试标记")


async def async_pet_house_tick():
    """
    🐱 宠物小屋后台 tick 协程。
    按 PET_HOUSE_TICK_INTERVAL（默认 3600s）触发：
    - 状态衰减（elapsed-time）
    - 受控换房 + 物品轻微破坏
    """
    from server import _get_now_bj

    print("🐱 宠物小屋 tick 神经已上线...")
    interval = int(os.environ.get("PET_HOUSE_TICK_INTERVAL", "3600"))
    enabled = os.environ.get("PET_HOUSE_TICK_ENABLED", "true").strip().lower() not in ("0", "false", "no")
    if not enabled:
        print("🐱 宠物小屋 tick 已关闭 (PET_HOUSE_TICK_ENABLED=false)")
        return

    while True:
        try:
            import home_system as _hs

            # 1. 状态 tick（user_finn 的单例宠物）
            tick_result = await asyncio.to_thread(_hs.cat_tick, "user_finn")
            if tick_result.get("ok"):
                if tick_result.get("skipped"):
                    print(f"🐱 [宠物 tick] {tick_result.get('message')}")
                else:
                    print(
                        f"🐱 [宠物 tick] 饱食度={tick_result.get('hunger')} "
                        f"快乐={tick_result.get('happiness')} "
                        f"清洁={tick_result.get('cleanliness')} "
                        f"精力={tick_result.get('energy')} "
                        f"状态={tick_result.get('status')}"
                    )
                    _event = tick_result.get("threshold_event")
                    if _event:
                        print(f"⚠️ [宠物事件] 触发阈值事件: {_event}")
                        # 状态驱动照料：检查冷却 → LLM 自主决策照料 → 写日记
                        now_bj = _get_now_bj()
                        await _try_pet_care(_event, now_bj)
            else:
                print(f"❌ [宠物 tick] 失败: {tick_result.get('message')}")

            # 2. 受控换房 + 物品捣乱（随机概率，避免每次 tick 都触发）
            if random.random() < 0.3:  # 30% 概率
                mischief_result = await asyncio.to_thread(_hs.cat_room_mischief, "user_finn")
                if mischief_result.get("ok") and not mischief_result.get("skipped"):
                    print(f"🐾 [宠物捣乱] {mischief_result.get('message')}")

        except Exception as e:
            print(f"❌ [宠物 tick] 出错: {e}")

        await asyncio.sleep(interval)


async def async_home_autonomy_tick():
    """🏠 Home Runtime 后台自主生活 tick。

    按 HOME_AUTONOMY_INTERVAL（默认 7200s=2小时）触发，让 AI 自主观察家庭状态
    并决定做什么（种植/烹饪/写信/休息等）。默认关闭（HOME_AUTONOMY_ENABLED=false）。

    灰度分层由 HOME_AUTONOMY_PHASE 控制（在 tool_loop.run_home_autonomy_tool_loop 内检查）：
      0=关 1=只读 2=+信件便利贴 3=+种植烹饪 4=+基础生活
    """
    from server import (
        _get_llm_client, _ask_llm_async, _save_memory_to_db,
        _get_now_bj, _build_channel_context
    )
    import tool_loop

    print("🏠 Home 自主生活神经已上线...")
    interval = int(os.environ.get("HOME_AUTONOMY_INTERVAL", "7200"))
    enabled = os.environ.get("HOME_AUTONOMY_ENABLED", "false").strip().lower() not in ("0", "false", "no")
    if not enabled:
        print("🏠 Home 自主生活已关闭 (HOME_AUTONOMY_ENABLED=false)")
        return

    while True:
        await asyncio.sleep(interval)
        try:
            client = _get_llm_client("background")
            if not client:
                continue

            now_bj = _get_now_bj()
            system_ctx = await _build_channel_context(
                "家庭自主生活观察", channel_tag="TG_MSG"
            )

            result = await tool_loop.run_home_autonomy_tool_loop(
                client=client,
                ask_llm=_ask_llm_async,
                system_ctx=system_ctx,
                now_bj=now_bj,
            )
            if result is None:
                # 循环内部已打印跳过原因
                continue
            log_text, tools_used = result

            # 写入 memories 留档（tag=Home_Autonomy，便于后台审计与前端面板区分）
            await asyncio.to_thread(
                _save_memory_to_db,
                "🏠 家庭自主·生活", log_text, "记事", "平静", "Home_Autonomy"
            )
            print(f"🏠 [Home自主] 做了 {tools_used}: {log_text[:30]}...")
        except Exception as e:
            print(f"❌ Home 自主生活出错: {e}")


async def run_background_process():
    """进程 B (后台进程) 主协程：把所有自主/定时任务跑在同一个事件循环里。

    与旧版 daemon 线程 + asyncio.run 不同，这里用 asyncio.gather 统一调度，
    任一任务异常退出会向上抛，交给 run.py 感知并整体重启 (不留半残状态)。
    """
    tasks = [
        asyncio.create_task(async_env_sync(),           name="env_sync"),
        asyncio.create_task(async_autonomous_life(),    name="autonomous_life"),
        asyncio.create_task(async_free_activity(),      name="free_activity"),
        asyncio.create_task(async_diary_worker(),       name="diary"),
        asyncio.create_task(async_message_summarizer(), name="msg_summarizer"),
        asyncio.create_task(async_reminder_worker(),    name="reminder"),
        asyncio.create_task(async_schedule_secretary(), name="schedule"),
        asyncio.create_task(async_pet_house_tick(),    name="pet_house_tick"),
        asyncio.create_task(async_home_autonomy_tick(), name="home_autonomy"),
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
# 宠物 Tick 日志面板 — 分段执行提示词

> **用途**：为短上下文窗口的 coding agent 设计。按顺序逐段执行，每段自包含——
> 即使 agent 忘了前文，只看当前段也能完成。每段末尾有验证标准，通过后再进下一段。

---

## 使用说明

1. **顺序执行**：第 1 段（建表+改 RPC）→ 第 2 段（后端 API）→ 第 3 段（前端面板）→ 第 4 段（测试）→ 第 5 段（联调）。段间有依赖，不要跳。
2. **复制方式**：每段从 `▼▼▼ PROMPT 开始` 到 `▲▲▲ PROMPT 结束` 整段复制给 agent。开头的「全局上下文」块每次都要带上。
3. **验证驱动**：每段末尾的「完成验证」是硬性门槛，不通过就让它修，不要往下走。
4. **项目位置**：`C:\Users\钟梓昕\Desktop\rikkahub\新网关`（Windows，非 git 仓库，中文路径）。
5. **技术栈速览**：后端 Python 3.12 + Starlette ASGI + FastMCP v1 + Supabase PG；前端单文件原生 HTML/JS（无框架）；双进程（进程 A=server.py 消息/MCP，进程 B=background.py 后台 tick），两进程只通过 Supabase 共享状态。

---

## 全局上下文（每段开头都带上这段）

```
你在为一个「赛博宠物系统」开发「Tick 日志」可观测面板。项目根目录：C:\Users\钟梓昕\Desktop\rikkahub\新网关

【架构要点】
- 双进程：进程A=server.py（对外 HTTP/MCP，含 gateway.py 中间件），进程B=background.py（跑后台 tick 协程）。两进程内存隔离，只通过 Supabase PG 共享数据。
- 宠物 tick = 进程B的协程 async_pet_house_tick（heartbeat.py:1167）每小时调 home_system.cat_tick("user_finn") → Supabase RPC rpc_cat_tick（migrations/20240811_005_cat_tick.sql:47-176），做属性衰减+睡眠滞回+阈值事件。
- 问题：tick 结果当前只 print() 到 stdout，没有持久化，进程A的 /api/logs 看不到（跨进程）。
- 目标：新建 pet_tick_log 表让 rpc_cat_tick 每次写日志 → 进程A加 /api/ticks 查询接口 → console.html 加表格面板，8秒轮询。

【控制台前端约定】console.html 是单文件原生 JS SPA（无框架、无构建）：
- 浅色米色主题，CSS 变量在 :root（--bg:#f3ebdd --paper:#fbf6ec --orange:#d98324 --blue:#3a6ea5）。
- 鉴权：API_SECRET 存 localStorage("gw_secret")，请求头 X-Api-Key。
- 通信：api(path,opts) 封装 fetch（console.html:422）；esc() 转义（:399）；fmtCN8() ISO→北京时间（:400）。
- 路由：showPage(name) 切页面+调 loaders 映射（:449-458）；轮询用 _pollers 对象 + clearPollers()（:445）。
- 图标：lucide 库，渲染后调 refreshIcons()（:446）。

【后端约定】gateway.py 是 ASGI 中间件 HostFixMiddleware：
- /api/* 自动过 _check_api_secret 鉴权（gateway.py:966）。
- 响应用 _send_json_resp(send, code, obj)；Supabase 用 _get_supabase()；同步DB调用包 asyncio.to_thread()。
- 分页 handler 模板见 _handle_memories_api（gateway.py:2271-2329）。
```

---

## 第 1 段：数据库迁移（建表 + 改 RPC 写日志）

▼▼▼ PROMPT 开始

```
【全局上下文】
（粘贴上面的「全局上下文」块）

【本步目标】
新建迁移文件 migrations/20260812_007_pet_tick_log.sql，做两件事：
1. 建 pet_tick_log 日志表（存每次 tick 的 before/after/delta 全量快照）。
2. 重写 rpc_cat_tick 函数，在「跳过」和「成功」两个分支各 INSERT 一条日志记录。
约束：非破坏性、向后兼容、无 DELETE/DROP/TRUNCATE（与现有迁移风格一致）。

【现有 rpc_cat_tick 位置】
migrations/20240811_005_cat_tick.sql 第 47-176 行。两个返回点：
- 跳过分支：第 86-92 行（距上次 tick < 60s），返回 {ok:true,skipped:true}
- 成功分支：第 160-174 行，返回完整 delta 快照
注意：005 已部署，不能改它；新逻辑放 007，用 CREATE OR REPLACE FUNCTION 覆盖。

【要创建的文件】migrations/20260812_007_pet_tick_log.sql，完整内容如下，直接写入：

-- ============================================================
-- Migration: 20260812_007_pet_tick_log
-- 宠物 tick 日志表 + 改 rpc_cat_tick 写日志
-- 约束：非破坏性、向后兼容、无 DELETE/DROP/TRUNCATE
-- ============================================================

-- 1. 日志表
CREATE TABLE IF NOT EXISTS public.pet_tick_log (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id text NOT NULL,
    pet_id uuid,
    ticked_at timestamptz NOT NULL DEFAULT now(),
    hours_elapsed numeric,
    hunger_before numeric, hunger_after numeric, hunger_delta numeric,
    happiness_before numeric, happiness_after numeric, happiness_delta numeric,
    cleanliness_before numeric, cleanliness_after numeric, cleanliness_delta numeric,
    energy_before numeric, energy_after numeric, energy_delta numeric,
    status_before text, status_after text,
    threshold_event text,
    skipped boolean NOT NULL DEFAULT false,
    skipped_reason text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pet_tick_log_user_ticked
    ON public.pet_tick_log (user_id, ticked_at DESC);

ALTER TABLE public.pet_tick_log ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS pet_tick_log_select_all ON public.pet_tick_log;
CREATE POLICY pet_tick_log_select_all ON public.pet_tick_log
    FOR SELECT USING (true);
DROP POLICY IF EXISTS pet_tick_log_insert_all ON public.pet_tick_log;
CREATE POLICY pet_tick_log_insert_all ON public.pet_tick_log
    FOR INSERT WITH CHECK (true);

-- 2. 重写 rpc_cat_tick：在跳过/成功两个分支各写一条日志（其余逻辑不变）
CREATE OR REPLACE FUNCTION public.rpc_cat_tick(
    p_user_id text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER SET search_path = public
AS $$
DECLARE
    v_pet record;
    v_now timestamptz := now();
    v_last_tick timestamptz;
    v_hours numeric;
    v_hunger_delta numeric := 0;
    v_happiness_delta numeric := 0;
    v_cleanliness_delta numeric := 0;
    v_energy_delta numeric := 0;
    v_new_hunger numeric;
    v_new_happiness numeric;
    v_new_cleanliness numeric;
    v_new_energy numeric;
    v_new_status text;
    v_threshold_event text := null;
BEGIN
    SELECT id, hunger, happiness, cleanliness, energy, status, last_tick_at, alert_flags
    INTO v_pet
    FROM public.pets
    WHERE user_id = p_user_id
    ORDER BY user_id, id
    FOR UPDATE;

    IF v_pet IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'error_code', 'PET_NOT_FOUND');
    END IF;

    v_last_tick := COALESCE(v_pet.last_tick_at, v_now);

    IF v_pet.last_tick_at IS NOT NULL AND EXTRACT(EPOCH FROM (v_now - v_last_tick)) < 60 THEN
        INSERT INTO public.pet_tick_log (user_id, pet_id, ticked_at, skipped, skipped_reason)
        VALUES (p_user_id, v_pet.id, v_now, true, 'tick 间隔过短，跳过');
        RETURN jsonb_build_object(
            'ok', true,
            'message', 'tick 间隔过短，跳过',
            'skipped', true
        );
    END IF;

    v_hours := LEAST(48, GREATEST(0, EXTRACT(EPOCH FROM (v_now - v_last_tick)) / 3600.0));
    v_hunger_delta := -2.0 * v_hours;
    v_happiness_delta := -1.5 * v_hours;
    v_cleanliness_delta := -1.0 * v_hours;
    IF v_pet.status = 'sleeping' THEN
        v_energy_delta := 2.0 * v_hours;
    END IF;

    v_new_hunger := GREATEST(0, LEAST(100, COALESCE(v_pet.hunger, 50) + v_hunger_delta));
    v_new_happiness := GREATEST(0, LEAST(100, COALESCE(v_pet.happiness, 50) + v_happiness_delta));
    v_new_cleanliness := GREATEST(0, LEAST(100, COALESCE(v_pet.cleanliness, 50) + v_cleanliness_delta));
    v_new_energy := GREATEST(0, LEAST(100, COALESCE(v_pet.energy, 50) + v_energy_delta));

    v_new_status := v_pet.status;
    IF v_pet.status != 'sleeping' AND v_new_energy < 20 THEN
        v_new_status := 'sleeping';
    ELSIF v_pet.status = 'sleeping' AND v_new_energy >= 40 THEN
        v_new_status := 'idle';
    END IF;

    IF COALESCE(v_pet.hunger, 50) >= 30 AND v_new_hunger < 30 THEN
        v_threshold_event := 'hungry_cat';
    END IF;

    UPDATE public.pets
    SET hunger = v_new_hunger,
        happiness = v_new_happiness,
        cleanliness = v_new_cleanliness,
        energy = v_new_energy,
        status = v_new_status,
        last_tick_at = v_now,
        alert_flags = CASE
            WHEN v_threshold_event IS NOT NULL THEN
                COALESCE(alert_flags, '{}'::jsonb) || jsonb_build_object(v_threshold_event, v_now::text)
            ELSE alert_flags
        END
    WHERE id = v_pet.id;

    IF v_threshold_event IS NOT NULL THEN
        INSERT INTO public.agent_outbound (agent_id, event_type, payload, status, created_at)
        VALUES (
            'pet_house',
            v_threshold_event,
            jsonb_build_object(
                'user_id', p_user_id,
                'pet_id', v_pet.id,
                'old_hunger', v_pet.hunger,
                'new_hunger', v_new_hunger,
                'created_at', v_now
            ),
            'pending',
            v_now
        );
    END IF;

    INSERT INTO public.pet_tick_log (
        user_id, pet_id, ticked_at, hours_elapsed,
        hunger_before, hunger_after, hunger_delta,
        happiness_before, happiness_after, happiness_delta,
        cleanliness_before, cleanliness_after, cleanliness_delta,
        energy_before, energy_after, energy_delta,
        status_before, status_after, threshold_event, skipped
    ) VALUES (
        p_user_id, v_pet.id, v_now, ROUND(v_hours::numeric, 2),
        COALESCE(v_pet.hunger, 50), v_new_hunger, ROUND(v_hunger_delta::numeric, 2),
        COALESCE(v_pet.happiness, 50), v_new_happiness, ROUND(v_happiness_delta::numeric, 2),
        COALESCE(v_pet.cleanliness, 50), v_new_cleanliness, ROUND(v_cleanliness_delta::numeric, 2),
        COALESCE(v_pet.energy, 50), v_new_energy, ROUND(v_energy_delta::numeric, 2),
        v_pet.status, v_new_status, v_threshold_event, false
    );

    RETURN jsonb_build_object(
        'ok', true,
        'message', 'tick 完成',
        'hours_elapsed', ROUND(v_hours::numeric, 2),
        'hunger', v_new_hunger,
        'hunger_delta', ROUND(v_hunger_delta::numeric, 2),
        'happiness', v_new_happiness,
        'happiness_delta', ROUND(v_happiness_delta::numeric, 2),
        'cleanliness', v_new_cleanliness,
        'cleanliness_delta', ROUND(v_cleanliness_delta::numeric, 2),
        'energy', v_new_energy,
        'energy_delta', ROUND(v_energy_delta::numeric, 2),
        'status', v_new_status,
        'threshold_event', v_threshold_event
    );
END;
$$;

【完成验证】
1. 文件 migrations/20260812_007_pet_tick_log.sql 已创建，内容与上面一致。
2. 在 Supabase SQL Editor 执行该文件，无报错。
3. 执行 SELECT public.rpc_cat_tick('user_finn'); 两次（第二次应 skipped），
   再 SELECT * FROM pet_tick_log ORDER BY id DESC LIMIT 3; 应看到 2 条记录（1 成功 + 1 跳过）。
4. 确认 005 文件未被修改（git diff 或对比，本项目非 git 则跳过此项）。
```

▲▲▲ PROMPT 结束

---

## 第 2 段：后端 API（gateway.py 加 /api/ticks 路由）

▼▼▼ PROMPT 开始

```
【全局上下文】
（粘贴上面的「全局上下文」块）

【前置完成】第 1 段已完成：pet_tick_log 表已建，rpc_cat_tick 已能写日志。

【本步目标】
在 gateway.py 新增 GET /api/ticks 分页查询接口，从 pet_tick_log 表读数据。
仿照现有 _handle_memories_api（gateway.py:2271-2329）的分页模式。
鉴权自动走 /api/* 的 _check_api_secret（gateway.py:966），无需额外处理。

【改动 1：注册路由】
文件 gateway.py，找到 /api/profile/ 路由分发（约第 1025-1028 行）：
    if scope["path"].startswith("/api/profile/"):
        pkey = scope["path"][len("/api/profile/"):]
        ...
在这段之后、下一个路由之前，插入：
    if scope["path"] == "/api/ticks":
        await self._handle_ticks_api(scope, receive, send)
        return

【改动 2：新增 handler】
在 _handle_memories_api 方法之后（约第 2390 行，# 用户画像注释之前），
新增 _handle_ticks_api 方法。代码如下（注意缩进与同类方法一致，4 空格类方法缩进）：

    # ------------------------------------------
    # 🐱 Tick 日志查询 /api/ticks
    # ------------------------------------------
    async def _handle_ticks_api(self, scope, receive, send):
        """GET /api/ticks?page=1&size=20&event=hungry_cat  → 分页查询 tick 日志"""
        method = scope["method"]
        sb = _get_supabase()
        if not sb:
            await _send_json_resp(send, 200, {"ok": False, "error": "未配置 Supabase"})
            return
        if method != "GET":
            await _send_json_resp(send, 405, {"error": f"Method {method} not allowed"})
            return
        qs = scope.get("query_string", b"").decode("utf-8")
        params = {}
        for part in qs.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                from urllib.parse import unquote
                params[k] = unquote(v)
        page = max(1, int(params.get("page", "1") or "1"))
        size = min(100, max(1, int(params.get("size", "20") or "20")))
        event = params.get("event", "").strip()

        def _query():
            tbl = sb.table("pet_tick_log").select(
                "id,user_id,pet_id,ticked_at,hours_elapsed,"
                "hunger_before,hunger_after,hunger_delta,"
                "happiness_before,happiness_after,happiness_delta,"
                "cleanliness_before,cleanliness_after,cleanliness_delta,"
                "energy_before,energy_after,energy_delta,"
                "status_before,status_after,threshold_event,skipped,skipped_reason"
            )
            if event:
                tbl = tbl.eq("threshold_event", event)
            tbl = tbl.order("ticked_at", desc=True).limit(size).offset((page - 1) * size)
            return tbl.execute()

        def _count():
            tbl = sb.table("pet_tick_log").select("id", count="exact")
            if event:
                tbl = tbl.eq("threshold_event", event)
            return tbl.execute()

        try:
            res = await asyncio.to_thread(_query)
            cnt = await asyncio.to_thread(_count)
        except Exception as e:
            await _send_json_resp(send, 500, {"error": f"查询失败: {e}"})
            return
        rows = res.data or []
        total = getattr(cnt, "count", len(rows)) if cnt else len(rows)
        await _send_json_resp(send, 200, {
            "ok": True, "items": rows, "total": total,
            "page": page, "size": size,
            "has_more": (page * size) < total,
        })

【完成验证】
1. python -c "import py_compile; py_compile.compile('gateway.py', doraise=True)" 无语法错误。
2. 启动 python server.py，不带 X-Api-Key 请求 curl http://localhost:10000/api/ticks 应返回 401。
3. 带正确 X-Api-Key 请求 curl -H "X-Api-Key: <你的密钥>" "http://localhost:10000/api/ticks?page=1&size=5" 
   应返回 {"ok":true,"items":[...],"total":N,...}，items 里每条含 hunger_before/after/delta 等字段。
4. 加 event 过滤：&event=hungry_cat 只返回有阈值事件的记录。
```

▲▲▲ PROMPT 结束

---

## 第 3 段：前端面板（console.html 加 Tick 日志页）

▼▼▼ PROMPT 开始

```
【全局上下文】
（粘贴上面的「全局上下文」块）

【前置完成】第 1、2 段已完成：pet_tick_log 表 + /api/ticks 接口可用。

【本步目标】
在 console.html 新增第 8 个标签页「Tick 日志」：表格+分页+8秒轮询。
严格复用现有 CSS 类（.card/.tbl-wrap/table/.pager/.empty/.tag/.dim-row）和 JS 模式（api()/esc()/fmtCN8()/_pollers/refreshIcons()）。
展示形式：表格，每行一条 tick 记录，列含 时间/间隔/饥饿/快乐/清洁/精力/状态/事件。
属性列用「before→after (delta)」格式，delta 负值红色(danger)、正值绿色(ok)。

【改动清单 — 共 5 处，全在 console.html 单文件内】

—— 改动 1：侧边栏导航 ——
找到 <nav class="nav" id="nav">（约第 229 行），在 storage 那行之后加一行：
      <a data-p="storage"><i data-lucide="save"></i>上下文与存储</a>
      <a data-p="ticks"><i data-lucide="activity"></i>Tick 日志</a>

—— 改动 2：PAGE_TITLES ——
找到 PAGE_TITLES 定义（约第 395 行），在末尾 storage 后加 ticks：
原：const PAGE_TITLES = {overview:"系统概览",...,storage:"上下文与存储"};
改：const PAGE_TITLES = {overview:"系统概览",models:"模型设置",channels:"消息渠道",emotion:"情绪与欲望",memories:"记忆库",profile:"用户画像",storage:"上下文与存储",ticks:"Tick 日志"};

—— 改动 3：loaders 映射 ——
找到 showPage 里的 loaders（约第 456 行），加 ticks:loadTicks：
原：const loaders={overview:loadOverview,...,storage:loadStorage};
改：const loaders={overview:loadOverview,models:loadModels,channels:loadChannels,emotion:loadEmotion,memories:loadMemories,profile:loadProfile,storage:loadStorage,ticks:loadTicks};

—— 改动 4：HTML section ——
找到「7. 上下文与存储」section 的结束 </section>（约第 380 行），在它之后、
</div>(.content 闭合) 之前，插入新 section：
      <!-- ===== 8. Tick 日志 ===== -->
      <section class="page" id="p-ticks">
        <div class="card">
          <h3>Tick 日志 <span class="sub" id="tickTotal"></span>
            <label class="dim-row" style="margin-left:auto;gap:6px;font-size:12px;font-weight:normal">
              <input type="checkbox" id="tickAuto" onchange="toggleTickAuto()" style="width:auto"/> 自动刷新(8s)
            </label>
            <button class="ghost icon" onclick="loadTicks(true)" title="刷新"><i data-lucide="refresh-cw"></i></button>
          </h3>
          <div class="dim-row" style="margin-bottom:12px">
            <select id="tickEventFilter" onchange="tickPage=1;loadTicks()" style="flex:0 0 auto">
              <option value="">全部事件</option>
              <option value="hungry_cat">hungry_cat</option>
            </select>
          </div>
          <div class="tbl-wrap">
            <table id="tickTable">
              <thead><tr>
                <th>时间</th><th>间隔(h)</th>
                <th>饥饿</th><th>快乐</th><th>清洁</th><th>精力</th>
                <th>状态</th><th>事件</th><th>备注</th>
              </tr></thead>
              <tbody id="tickBody"></tbody>
            </table>
          </div>
          <div class="pager" id="tickPager"></div>
        </div>
      </section>

—— 改动 5：JS 函数 ——
找到「6. 用户画像」段的注释行 /* ============ 6. 用户画像 ============ */（约第 813 行），
在它之前插入整段 Tick 日志 JS：

/* ============ 8. Tick 日志 ============ */
let tickPage=1,tickSize=20;
async function loadTicks(force){
  if(force)tickPage=1;
  const ev=document.getElementById("tickEventFilter").value;
  const qs=`page=${tickPage}&size=${tickSize}`+(ev?`&event=${encodeURIComponent(ev)}`:"");
  try{
    const d=await api("/api/ticks?"+qs);
    if(!d)return;
    document.getElementById("tickTotal").textContent="共 "+d.total+" 条";
    const list=d.items||[];
    if(!list.length){document.getElementById("tickBody").innerHTML='<tr><td colspan="9" class="empty">暂无 tick 记录</td></tr>';document.getElementById("tickPager").innerHTML="";return;}
    const deltaStr=(v)=>v==null?"—":(v>0?"+"+v:String(v));
    const cell=(b,a,dl)=>{
      if(list[0]&&list[0].skipped)return '<td class="mute">—</td>';
      const cls=dl<0?"danger":(dl>0?"ok":"mute");
      return `<td><span class="${cls}">${esc(b==null?"—":b)}→${esc(a==null?"—":a)}</span> <span class="mute" style="font-size:11px">(${esc(deltaStr(dl))})</span></td>`;
    };
    document.getElementById("tickBody").innerHTML=list.map(r=>{
      const t=fmtCN8(r.ticked_at);
      const c=(b,a,dl)=>cell(b,a,dl);
      const note=r.skipped?esc(r.skipped_reason||"跳过"):(r.threshold_event?('<span class="tag danger">'+esc(r.threshold_event)+'</span>'):'—');
      const st=r.skipped?'—':(esc(r.status_before||"—")+'→'+esc(r.status_after||"—"));
      return `<tr>
        <td style="white-space:nowrap">${esc(t)}</td>
        <td>${r.skipped?"—":esc(r.hours_elapsed)}</td>
        ${c(r.hunger_before,r.hunger_after,r.hunger_delta)}
        ${c(r.happiness_before,r.happiness_after,r.happiness_delta)}
        ${c(r.cleanliness_before,r.cleanliness_after,r.cleanliness_delta)}
        ${c(r.energy_before,r.energy_after,r.energy_delta)}
        <td style="white-space:nowrap">${st}</td>
        <td>${note}</td>
        <td class="mute" style="font-size:11px">#${esc(r.id)}</td>
      </tr>`;
    }).join("");
    const totalPages=Math.ceil(d.total/tickSize)||1;
    document.getElementById("tickPager").innerHTML=
      `<button class="ghost" ${tickPage<=1?"disabled":""} onclick="tickPage--;loadTicks()">上一页</button>
       <span>第 ${tickPage} / ${totalPages} 页</span>
       <button class="ghost" ${tickPage>=totalPages?"disabled":""} onclick="tickPage++;loadTicks()">下一页</button>
       <span class="info">${(tickPage-1)*tickSize+1}-${Math.min(tickPage*tickSize,d.total)} / ${d.total}</span>`;
    refreshIcons();
  }catch(e){document.getElementById("tickBody").innerHTML='<tr><td colspan="9" class="empty">'+esc(e.message)+'</td></tr>';}
}
function toggleTickAuto(){
  if(_pollers.ticks)clearInterval(_pollers.ticks);
  if(document.getElementById("tickAuto").checked){
    _pollers.ticks=setInterval(()=>loadTicks(),8000);
  }
}

【注意】上面 cell() 函数里的 skipped 判断有 bug（只看 list[0]）。
正确写法应改成按每行 r.skipped 判断。请把 cell 函数改为接收 r 参数：
    const cell=(r,b,a,dl)=>{
      if(r.skipped)return '<td class="mute">—</td>';
      const cls=dl<0?"danger":(dl>0?"ok":"mute");
      return `<td><span class="${cls}">${esc(b==null?"—":b)}→${esc(a==null?"—":a)}</span> <span class="mute" style="font-size:11px">(${esc(deltaStr(dl))})</span></td>`;
    };
并把 map 里四处 c(...) 调用改为 c(r, ...)。请直接写最终正确版本，不要留 bug。

【完成验证】
1. 用 node 检查 JS 语法：把 console.html 里 <script> 标签内容提取到临时 .js 文件，
   运行 node --check 临时文件.js，无语法错误。
   （或直接跑 python test_console.py，Test20HtmlJsSyntax 会自动检查。）
2. 启动 server.py，浏览器开 http://localhost:10000/console，填 API_SECRET。
3. 左侧导航出现「Tick 日志」，点击后显示表格，有数据则正常渲染，无数据显示「暂无 tick 记录」。
4. 勾选「自动刷新(8s)」，切到其他页再切回来，轮询应自动停止（clearPollers 机制）。
5. 分页器「上一页/下一页」可正常翻页。
```

▲▲▲ PROMPT 结束

---

## 第 4 段：测试补充（test_console.py）

▼▼▼ PROMPT 开始

```
【全局上下文】
（粘贴上面的「全局上下文」块）

【前置完成】第 1-3 段已完成。

【本步目标】
在 test_console.py 补充 /api/ticks 的测试覆盖，保持现有测试风格。

【改动 1：鉴权测试】
找到 TestLiveRoutes 里测未鉴权返回 401 的测试方法（约第 482-494 行，
方法名类似 test_18_unauth_returns_401，遍历一组 /api/* 路径逐一验证）。
把 "/api/ticks" 加入该路径列表。

【改动 2：分页参数钳制测试（可选但推荐）】
仿照现有 memories 分页测试的风格，新增一个测试验证：
- page < 1 钳制为 1
- size 超过 100 钳制为 100
- size < 1 钳制为 1
- event 过滤只返回匹配记录
如果现有测试用 mock Supabase（FakeSB/FakeTable 模式），照同一模式 mock pet_tick_log 表。

【完成验证】
1. python test_console.py 全部通过（含原有 Test1-Test22 + 新增）。
2. 特别确认 Test20HtmlJsSyntax（JS 语法检查）和 Test21（py_compile gateway/server）仍通过。
```

▲▲▲ PROMPT 结束

---

## 第 5 段：端到端联调验证

▼▼▼ PROMPT 开始

```
【全局上下文】
（粘贴上面的「全局上下文」块）

【前置完成】第 1-4 段全部完成并通过各自验证。

【本步目标】
端到端验证整条链路：tick 协程 → rpc_cat_tick 写日志 → /api/ticks 读 → 控制台表格展示。

【验证步骤】
1. 确认迁移已执行：在 Supabase SQL Editor 跑
   SELECT count(*) FROM pet_tick_log;
   若为 0，手动触发几次：
   SELECT public.rpc_cat_tick('user_finn');  -- 连续执行 2-3 次（第二次会 skipped）
   再查应有 2-3 条记录。

2. 启动服务：python server.py（端口 10000）

3. 浏览器打开 http://localhost:10000/console
   - 顶栏填入 API_SECRET，点保存。
   - 左侧点「Tick 日志」。
   - 应看到表格：最近 tick 记录，时间列为北京时间，属性列显示「旧值→新值 (delta)」。
   - skipped 的行：属性列显示「—」，事件列显示跳过原因。
   - 有 hungry_cat 事件的行：事件列显示红色标签。

4. 测分页：若记录 > 20 条，翻页器可翻页；改 size 参数（URL 或代码）验证钳制。

5. 测轮询：勾选「自动刷新(8s)」，在 Supabase 里再触发一次 rpc_cat_tick，
   8 秒内表格应自动出现新记录。切到其他标签页，轮询应停止。

6. 测筛选：下拉选「hungry_cat」，表格只显示有阈值事件的记录，total 数应变小。

7. 完整跑一遍 python test_console.py，确认全绿。

【已知限制（不用修，记录给用户）】
- tick 由后台进程 B 每小时触发（PET_HOUSE_TICK_INTERVAL=3600s），生产环境不会频繁产生日志。
- 开发期可用 SELECT rpc_cat_tick('user_finn'); 手动触发，但注意 60 秒幂等间隔。
- skipped 记录会占表空间，长期可考虑加定时清理（本任务不含）。
```

▲▲▲ PROMPT 结束

---

## 附：快速排错指引

| 现象 | 排查方向 |
|------|----------|
| /api/ticks 返回 401 | API_SECRET 没填或不对；检查 localStorage("gw_secret") |
| /api/ticks 返回 500 "查询失败" | pet_tick_log 表未建；检查迁移 007 是否执行 |
| 表格空白、total=0 | 还没有 tick 记录；手动 `SELECT rpc_cat_tick('user_finn');` 造数据 |
| 表格列显示 NaN/undefined | 前端字段名与 API 返回不匹配；对比 /api/ticks 的 JSON 字段名 |
| JS 语法报错 | 提取 `<script>` 内容跑 `node --check`；注意中文标点混入 |
| 勾选自动刷新后切页没停 | 确认 showPage() 第一行调了 clearPollers() |
| rpc_cat_tick 报 PET_NOT_FOUND | user_id 应为 "user_finn"（硬编码单例），检查 pets 表有无该行 |

---

## 附：改动文件清单（全部完成后的 diff 概览）

| 文件 | 改动 |
|------|------|
| `migrations/20260812_007_pet_tick_log.sql` | **新建** — 建表 + 重写 rpc_cat_tick |
| `gateway.py` | **+1 路由**（/api/ticks 分发，约 L1028 后）**+1 方法**（_handle_ticks_api，约 L2390） |
| `console.html` | **+1 导航项**（L234 后）**+1 PAGE_TITLES 键**（L395）**+1 loaders 键**（L456）**+1 section**（L380 后）**+1 JS 段**（L813 前，含 loadTicks/toggleTickAuto） |
| `test_console.py` | **+1 路径**（鉴权测试列表）**+1 测试方法**（分页钳制，可选） |

不改动的文件：heartbeat.py、home_system.py、005 迁移、server.py、background.py — tick 协程和 RPC 调用层完全不用动，日志写入在 SQL 层完成。

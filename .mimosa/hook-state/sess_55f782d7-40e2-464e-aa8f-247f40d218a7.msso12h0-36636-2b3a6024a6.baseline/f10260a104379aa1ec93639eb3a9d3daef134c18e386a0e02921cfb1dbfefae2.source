import asyncio
import sys
import traceback
import datetime

async def mock_ask_llm(client, prompt, **kw):
    return '模拟日记：走到阳台，空气闷热，远处传来雨声。'

async def main():
    print("=== Step 6: 测试查天气专用路径 ===")
    sys.stdout.flush()
    try:
        import tool_loop
        result = await tool_loop._finalize_weather_activity(
            None, mock_ask_llm, '测试上下文', '草稿log', datetime.datetime.now(),
            log_prefix='[TEST]'
        )
        print(f"result: {result}")
    except Exception as e:
        print(f"ERROR: {e}")
        traceback.print_exc()
    sys.stdout.flush()

if __name__ == "__main__":
    asyncio.run(main())

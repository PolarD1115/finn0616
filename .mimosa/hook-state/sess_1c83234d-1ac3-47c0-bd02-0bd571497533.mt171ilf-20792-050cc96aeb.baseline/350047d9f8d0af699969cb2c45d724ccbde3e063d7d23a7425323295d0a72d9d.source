import argparse
import json
from pathlib import Path


def short(text: object, limit: int = 160) -> str:
    value = str(text or "").replace("\n", " ").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "..."


def load_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except UnicodeDecodeError:
        with open(path, "r", encoding="gb18030") as f:
            return json.load(f)


def load_existing_mapping(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        raw = load_json(path)
    except Exception:
        return {}
    mappings = raw.get("mappings", [])
    if not isinstance(mappings, list):
        return {}
    result = {}
    for item in mappings:
        if not isinstance(item, dict):
            continue
        feel_id = str(item.get("feel_id") or "").strip()
        if feel_id:
            result[feel_id] = item
    return result


def mapping_from_choice(plan: dict, action: str, source_bucket_id: str = "") -> dict:
    candidates = plan.get("candidates") or []
    top = candidates[0] if candidates else {}
    if action == "whisper":
        return {
            "feel_id": plan.get("feel_id", ""),
            "action": "whisper",
            "source_bucket_id": "",
            "suggested_source_bucket_id": top.get("bucket_id", ""),
            "suggested_source_name": top.get("name", ""),
            "chosen_source_name": "",
            "confidence": "source_less",
            "score": 0,
            "common_keywords": [],
            "note": "keep_as_whisper",
        }
    if action == "skip":
        return {
            "feel_id": plan.get("feel_id", ""),
            "action": "skip",
            "source_bucket_id": "",
            "suggested_source_bucket_id": top.get("bucket_id", ""),
            "suggested_source_name": top.get("name", ""),
            "chosen_source_name": "",
            "confidence": top.get("confidence", ""),
            "score": top.get("score", 0),
            "common_keywords": top.get("common_keywords", []),
            "note": "skipped",
        }
    chosen = next((item for item in candidates if item.get("bucket_id") == source_bucket_id), None)
    if source_bucket_id and not chosen:
        return {
            "feel_id": plan.get("feel_id", ""),
            "action": "comment",
            "source_bucket_id": source_bucket_id,
            "suggested_source_bucket_id": top.get("bucket_id", ""),
            "suggested_source_name": top.get("name", ""),
            "chosen_source_name": "",
            "confidence": "manual",
            "score": 0,
            "common_keywords": [],
            "note": "interactive_review_manual",
        }
    chosen = chosen or top
    return {
        "feel_id": plan.get("feel_id", ""),
        "action": "comment" if source_bucket_id else "skip",
        "source_bucket_id": source_bucket_id,
        "suggested_source_bucket_id": top.get("bucket_id", ""),
        "suggested_source_name": top.get("name", ""),
        "chosen_source_name": chosen.get("name", ""),
        "confidence": chosen.get("confidence", top.get("confidence", "")),
        "score": chosen.get("score", top.get("score", 0)),
        "common_keywords": chosen.get("common_keywords", top.get("common_keywords", [])),
        "note": "interactive_review_confirmed" if source_bucket_id else "skipped",
    }


def print_plan(index: int, total: int, plan: dict, existing: dict | None) -> None:
    print("\n" + "-" * 60)
    print(f"[{index}/{total}] feel: {plan.get('feel_name') or plan.get('feel_id')}")
    print(f"feel_id: {plan.get('feel_id', '')}")
    print(f"created: {plan.get('feel_created', '')}")
    print(f"preview: {short(plan.get('feel_preview'))}")
    if existing:
        if existing.get("action") == "whisper":
            print("已有确认: 保留为 whisper/无源 feel")
        elif existing.get("source_bucket_id"):
            print(f"已有确认: {existing.get('source_bucket_id')}")

    candidates = plan.get("candidates") or []
    if not candidates:
        print("没有候选源记忆。")
        return

    print("\n候选源记忆：")
    for idx, candidate in enumerate(candidates, 1):
        keywords = ", ".join(str(item) for item in candidate.get("common_keywords", [])[:8])
        print(
            f"{idx}. {candidate.get('name') or candidate.get('bucket_id')} "
            f"({candidate.get('bucket_id')}) "
            f"confidence={candidate.get('confidence')} score={candidate.get('score')}"
        )
        if keywords:
            print(f"   keywords: {keywords}")
        print(f"   preview: {short(candidate.get('preview'), 120)}")


def ask_choice(plan: dict, existing: dict | None) -> tuple[str, str]:
    candidates = plan.get("candidates") or []
    while True:
        if candidates:
            prompt = "输入 y 接受第 1 个候选，1-3 选择候选，n 手动输入 bucket_id，w 保留为 whisper，s 跳过，q 退出"
        else:
            prompt = "输入 n 手动输入 bucket_id，w 保留为 whisper，s 跳过，q 退出"
        if existing and (existing.get("source_bucket_id") or existing.get("action") == "whisper"):
            prompt += "，回车保留已有"
        answer = input(f"{prompt}: ").strip()

        if not answer and existing:
            if existing.get("action") == "whisper":
                return "whisper", ""
            if existing.get("source_bucket_id"):
                return "comment", str(existing["source_bucket_id"]).strip()
        if answer.lower() == "q":
            raise KeyboardInterrupt
        if answer.lower() == "s":
            return "skip", ""
        if answer.lower() == "w":
            return "whisper", ""
        if answer.lower() == "y" and candidates:
            return "comment", str(candidates[0].get("bucket_id") or "").strip()
        if answer.isdigit() and candidates:
            idx = int(answer)
            if 1 <= idx <= len(candidates):
                return "comment", str(candidates[idx - 1].get("bucket_id") or "").strip()
        if answer.lower() == "n":
            manual = input("请输入源记忆 bucket_id（空=跳过）: ").strip()
            return ("comment", manual) if manual else ("skip", "")
        print("没看懂输入，再来一次。")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Interactively confirm old feel -> source bucket mappings."
    )
    parser.add_argument("--plan", required=True, help="Plan JSON from plan_feel_comment_backfill.py.")
    parser.add_argument("--mapping", required=True, help="Mapping JSON to write.")
    args = parser.parse_args()

    plan_path = Path(args.plan)
    mapping_path = Path(args.mapping)
    raw = load_json(plan_path)
    plans = raw.get("plans", [])
    if not isinstance(plans, list):
        raise SystemExit("plan 文件格式不对：缺少 plans 数组")

    existing_by_feel = load_existing_mapping(mapping_path)
    mappings = []
    total = len(plans)

    print(f"读取计划：{plan_path}")
    print(f"写入 mapping：{mapping_path}")
    print("说明：只会写 mapping，不会改 bucket。真正写入年轮要回到菜单走“预演/应用”。")

    try:
        for index, plan in enumerate(plans, 1):
            feel_id = str(plan.get("feel_id") or "").strip()
            existing = existing_by_feel.get(feel_id)
            print_plan(index, total, plan, existing)
            action, source_bucket_id = ask_choice(plan, existing)
            mappings.append(mapping_from_choice(plan, action, source_bucket_id))
    except (KeyboardInterrupt, EOFError):
        print("\n已提前退出，保留已确认的条目。")

    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    with open(mapping_path, "w", encoding="utf-8") as f:
        json.dump({"mappings": mappings}, f, ensure_ascii=False, indent=2)

    confirmed = sum(1 for item in mappings if item.get("source_bucket_id"))
    whispers = sum(1 for item in mappings if item.get("action") == "whisper")
    skipped = len(mappings) - confirmed - whispers
    print(f"\n完成：确认年轮 {confirmed} 条，保留 whisper {whispers} 条，跳过 {skipped} 条。")
    print(f"已写入：{mapping_path}")


if __name__ == "__main__":
    main()

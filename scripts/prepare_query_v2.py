"""第二版实验数据：保留旧训练/验证归属，补充英文对照，固定新测试集。"""
import hashlib
import json
from pathlib import Path

from scripts.prepare_query_data import PROJECT_ROOT, LABEL2ID, write_splits

SOURCE = PROJECT_ROOT / "data/training/query_v2_supplement.jsonl"
OLD_DIR = PROJECT_ROOT / "data/generated/query_groups_v1"
OUTPUT_DIR = PROJECT_ROOT / "data/generated/query_groups_v2"


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


def validate_splits(splits: dict) -> None:
    seen_ids, seen_texts = set(), set()
    group_locations = {}
    for name, rows in splits.items():
        labels = set()
        for row in rows:
            if row["language"] != "en" or row["label"] not in LABEL2ID:
                raise ValueError("本轮只接受已定义标签的英文问题")
            if row["review_status"] not in ("approved", "assistant_reviewed_experiment"):
                raise ValueError("样本来源未确认")
            text = " ".join(row["text"].casefold().split())
            if not text or text in seen_texts or row["id"] in seen_ids:
                raise ValueError(f"空文本或重复样本: {row['id']}")
            seen_ids.add(row["id"])
            seen_texts.add(text)
            group = row["group_id"]
            if group in group_locations and group_locations[group] != name:
                raise ValueError(f"关联组跨集合: {group}")
            group_locations[group] = name
            labels.add(row["label"])
        if labels != set(LABEL2ID):
            raise ValueError(f"{name} 未覆盖三类")


def main():
    splits = {"train": [], "validation": [], "test": []}
    # 旧验证数据仍用于验证；旧测试保留为已看过的诊断集，不加入训练。
    for name in ("train", "validation"):
        for row in read_jsonl(OLD_DIR / f"{name}.jsonl"):
            if row["language"] == "en":
                splits[name].append(row)
    for row in read_jsonl(SOURCE):
        splits[row["split"]].append(row)
    validate_splits(splits)
    write_splits(splits, OUTPUT_DIR)
    manifest = {"seed": 42, "selection": "validation macro_f1 only", "max_epochs": 12,
                "learning_rate": 5e-5, "early_stopping_patience": 3,
                "note": "新样本为助手生成与复核，用户授权实验，不冒称逐条人工确认；同源合成测试不是生产验收。", "files": {}}
    for name, rows in splits.items():
        counts = dict.fromkeys(LABEL2ID, 0)
        for row in rows:
            counts[row["label"]] += 1
        digest = hashlib.sha256((OUTPUT_DIR / f"{name}.jsonl").read_bytes()).hexdigest()
        manifest["files"][name] = {"count": len(rows), "labels": counts, "sha256": digest}
        print(name, len(rows), counts)
    with (OUTPUT_DIR / "manifest.json").open("w", encoding="utf-8") as file:
        json.dump(manifest, file, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()

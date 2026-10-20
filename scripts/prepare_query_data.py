"""
5.8 意图分类的数据准备参考版：不训练模型，不访问数据库或 API。
人工标注 JSONL
        ↓
load_examples()
清洗 + 校验 + 去重
        ↓
split_examples()
按类别分别切分
        ↓
train / validation / test
        ↓
write_splits()
保存成 3 个 JSONL

"""
import argparse
import json
import random
from pathlib import Path

from app.core.logger import setup_logging

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INPUT_FILE = PROJECT_ROOT / "data/training/query_examples_1.jsonl"
OUTPUT_DIR = PROJECT_ROOT / "data/generated/query_classifier"

# 项目业务标签，不是框架内置标签；后续训练和预测必须使用同一份映射。
LABEL2ID = {"in_scope": 0, "out_of_scope": 1, "restricted": 2}

def load_examples(input_file: Path) -> list[dict]:
    """输入人工标注的 JSONL，输出干净的 text + label 列表。"""
    examples = []
    seen_texts = set()
    with input_file.open("r", encoding="utf-8-sig") as file:
        for line_number, line in enumerate(file,start=1):
            if not line.strip():
                continue
            location = f"{input_file.name} 第 {line_number} 行"
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{location} 不是有效 JSON") from error
            if not isinstance(record,dict):
                raise ValueError(f"{location} 必须是 JSON 对象")

            text = record.get("text")
            label = record.get("label")
            if not isinstance(text,str) or not text.strip():
                raise ValueError(f"{location} 缺少有效 text")
            if not isinstance(label,str) or label not in LABEL2ID:
                raise ValueError(f"{location} 标签必须是 {tuple(LABEL2ID)}")

            text = text.strip()
            # 忽略大小写和连续空白后去重，防止相同问题同时落入训练集和测试集。
            # 这里只识别文本重复，不能识别语义相同的改写句。
            normalized_text = " ".join(text.casefold().split())
            # casefold() 是更彻底的.lower()，用于国际化比较
            if normalized_text in seen_texts:
                raise ValueError(f"{location} 问题重复, 请检查是否重复标注或标签冲突")
            seen_texts.add(normalized_text)
            examples.append({"text": text,"label":label})

        if not examples:
            raise ValueError("分类数据为空")
        return examples

def split_examples(examples: list[dict], seed: int =42) -> dict[str,list[dict]]:
    """按类别分别打乱，约 80% / 10% / 10% 切分；小样本每类至少留一条评估。"""
    groups = {}
    for label in LABEL2ID:
        groups[label] = []
    for example in examples:
        groups[example["label"]].append(example)

    splits = {"train": [], "validation": [], "test":[]}
    # 局部随机数对象不改变其他模块的随机状态；固定 seed 便于复现实验。
    random_generator = random.Random(seed)
    for label, group in groups.items():
        # 这是流程的最低数量要求，不代表五条就能训练出可用模型。
        if len(group) < 5:
            raise ValueError(f"{label}至少需要 5 条, 当前只有{len(group)}条")
        random_generator.shuffle(group)
        test_count = max(1, int(len(group) * 0.1))
        validation_count = max(1,int(len(group)* 0.1))
        train_start = test_count + validation_count
        splits["test"].extend(group[:test_count])
        splits["validation"].extend(group[test_count:train_start])
        splits["train"].extend(group[train_start:])

    for rows in splits.values():
        random_generator.shuffle(rows)
    return splits

def write_splits(splits: dict[str, list[dict]], output_dir: Path) -> None:
    """写入一个新目录，拒绝覆盖，防止无意中改变已固定的测试集"""
    output_dir.mkdir(parents=True,exist_ok=False)
    for split_name, rows in splits.items():
        output_file = output_dir / f"{split_name}.jsonl"
        with output_file.open("w",encoding="utf-8") as file:
            for row in rows:
                file.write(json.dumps(row,ensure_ascii=False) + "\n")

def main() -> int:
    parser = argparse.ArgumentParser(description="准备分类数据，默认仅检查")
    parser.add_argument("--input", type=Path, default=INPUT_FILE)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--write", action="store_true", help="将切分结果写入新目录")
    args = parser.parse_args()
    logger = setup_logging(PROJECT_ROOT / "logs/prepare_query_data.log")
    try:
        examples = load_examples(args.input)
        splits = split_examples(examples)
        for split_name, rows in splits.items():
            counts = dict.fromkeys(LABEL2ID,0)
            for row in rows:
                counts[row["label"]] += 1
            logger.info("%s: %d 条，各类数量: %s", split_name, len(rows), counts)
        if not args.write:
            logger.info("仅检查完成；加 --write 才保存数据，不会训练模型")
            return 0
        write_splits(splits,args.output_dir)
        logger.info("分类数据已保存: %s；示例集不能证明线上分类效果", args.output_dir)
        return 0
    except Exception:
        logger.exception("分类数据准备失败")
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
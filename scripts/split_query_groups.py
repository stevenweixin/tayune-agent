"""
读取已审核问题，按组切分；不训练、不下载模型
读取已审核数据
↓
检查 id / text / label / group_id
↓
确认 review_status == approved
↓
确认一个 group_id 只能属于一个 label
↓
按 label 分组
↓
再按 group_id 整组切分
↓
train / validation / test
↓
整组写入，不拆散
"""
import json
import random
from pathlib import Path

from app.core.logger import setup_logging
from scripts.prepare_query_data import LABEL2ID,PROJECT_ROOT,write_splits

INPUT_FILE = PROJECT_ROOT / "data/training/query_examples_approved_300.jsonl"
OUTPUT_DIR = PROJECT_ROOT / "data/generated/query_groups_v1"
SEED = 42

def load_reviewed_examples(input_file:Path) -> list[dict]:
    """检查文件入口，保留整条记录，避免丢失 id、group_id 和审核信息。"""
    examples = []
    seen_ids = set()
    seen_texts = set()
    group_labels = {}
    with input_file.open("r",encoding="utf-8-sig") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            location = f"{input_file.name} 第 {line_number} 行"
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{location} JSON 无效") from error
            if not isinstance(record,dict):
                raise ValueError(f"{location} 必须是 JSON 对象")
            for field in ("id" , "text","label","group_id"):
                value = record.get(field)
                if not isinstance(value,str) or not value.strip():
                    raise ValueError(f"{location} 缺少有效 {field}")
                record[field] = value.strip()
            if record['label'] not in LABEL2ID:
                raise ValueError(f"{location} 标签未知")
            if record.get("review_status") != "approved":
                raise ValueError(f"{location} 未通过审核")

            normalized_text = " ".join(record["text"].casefold().split())
            if record["id"] in seen_ids or normalized_text in seen_texts:
                raise ValueError(f"{location} 编号或问题重复")
            seen_ids.add(record["id"])
            seen_texts.add(normalized_text)

            group_id = record["group_id"]
            label = record["label"]
            # 当前算法先按类别分组，因此要求每组只有一个标签。
            # 遇到跨标签关联组先停下来讨论，不能偷偷拆开组来凑比例。
            if group_id in group_labels and group_labels[group_id] != label:
                raise ValueError(f"{location} 同组存在不同标签，需先确认分组方案")
            group_labels[group_id] = label
            examples.append(record)
    if not examples:
        raise ValueError("已审核问题为空")
    return examples

def split_by_group(examples: list[dict], seed: int = 42) -> dict[str, list[dict]]:
    """输入经过入口校验的记录，按每类的组数约 80/10/10 切分。"""
    label_groups = {}
    for label in LABEL2ID:
        label_groups[label] = {}
    for record in examples:
        groups = label_groups[record["label"]]
        group_id = record["group_id"]
        if group_id not in groups:
            groups[group_id] = []
        groups[group_id].append(record)

    splits = {"train": [], "validation":[],"test":[]}
    random_generator = random.Random(seed)
    for label, groups in label_groups.items():
        # 排序后再打乱：输入文件仅调整行顺序时，组的分配仍保持一致。
        group_ids = sorted(groups)
        if len(group_ids) < 3:
            raise ValueError(f"{label} 至少需要 3 个组，才能分别留给三个集合")
        random_generator.shuffle(group_ids)
        test_count = max(1,int(len(group_ids) * 0.1))
        validation_end = test_count + max(1, int(len(group_ids) * 0.1))
        for index,group_id in enumerate(group_ids):
            if index < test_count:
                splits_name = "test"
            elif index < validation_end:
                splits_name = "validation"
            else:
                splits_name = "train"
            # extend 一次放入整组，不对组内问题再次随机分配。
            splits[splits_name].extend(groups[group_id])
    return splits

def main() -> int:
    logger = setup_logging(PROJECT_ROOT / "logs/split_query_groups.log")
    try:
        logger.info("输入 %s; 输出: %s", INPUT_FILE, OUTPUT_DIR)
        examples = load_reviewed_examples(INPUT_FILE)
        splits = split_by_group(examples,SEED)
        for split_name, rows in splits.items():
            counts= dict.fromkeys(LABEL2ID, 0)
            group_ids = set()
            for row in rows:
                counts[row["label"]] += 1
                group_ids.add(row["group_id"])
            logger.info("%s: %d 条，各类: %s，组: %s", split_name, len(rows), counts, sorted(group_ids))
        write_splits(splits,OUTPUT_DIR)
        logger.info("已保存: %s；seed=%d；主题组隔离不等于消除全部语义泄漏", OUTPUT_DIR, SEED)
        return 0
    except FileExistsError:
        logger.error("输出目录已存在，未覆盖: %s。新实验请修改文件顶部 OUTPUT_DIR 使用新目录。", OUTPUT_DIR)
        return 1
    except Exception:
        logger.exception("分组切分失败")
        return 1

if __name__ == '__main__':
    raise SystemExit(main())

"""5.8 分类器训练参考版。"""

import json
from pathlib import Path

from app.core.logger import setup_logging
from scripts.prepare_query_data import LABEL2ID,PROJECT_ROOT
from scripts.split_query_groups import load_reviewed_examples

# 训练更新内存中的参数，保存到下面的 TAYUNE 新目录。
BASE_MODEL = Path("/Users/steven/PycharmProjects/EduAgent/backend/models/classifier/all-MiniLM-L6-v2")
DATA_DIR = PROJECT_ROOT / "data/generated/query_groups_v1"
OUTPUT_DIR = PROJECT_ROOT / "data/generated/query_classifier_model_v1"
EPOCHS = 3
BATCH_SIZE = 8
MAX_LENGTH = 128
SEED = 42


def load_splits(data_dir:Path) -> dict[str,list[dict]]:
    """复用正式加载器，再检查三份文件之间是否有数据泄漏。"""
    splits = {}
    seen_ids, seen_groups, seen_texts = set(),set(),set()
    for name in ("train", "validation", "test"):
        rows = load_reviewed_examples(data_dir / f"{name}.jsonl")
        ids, groups, texts, labels = set(), set(), set(), set()
        for row in rows:
            ids.add(row["id"])
            groups.add(row["group_id"])
            texts.add(" ".join(row["text"].casefold().split()))
            labels.add(row["label"])
        if labels != set(LABEL2ID):
            raise ValueError(f"{name} 没有覆盖全部三个类别")
        if ids & seen_ids or groups & seen_groups or texts & seen_texts:
            raise ValueError(f"{name} 与前面的集合存在编号、组或重复文本交叉")
        seen_ids.update(ids)
        seen_groups.update(groups)
        seen_texts.update(texts)
        splits[name] = rows
    return splits

def make_dataset(rows:list[dict], tokenizer):
    """text -> Token ID；字符串标签 -> 整数标签。元数据不送给模型。"""
    from datasets import Dataset

    records = []
    for row in rows:
        encoded = tokenizer(row["text"],truncation = False)
        # 不静默截掉问题尾部的否定词或危险请求；超长先明确处理。
        if len(encoded["input_ids"]) > MAX_LENGTH:
            raise ValueError (f"{row['id']} 超过 {MAX_LENGTH} Token，请先检查长问题")
        encoded["labels"] = LABEL2ID[row["label"]]
        records.append(dict(encoded))
    return Dataset.from_list(records)


def compute_metrics(prediction) -> dict:
    """Trainer 在验证/测试时调用；不是每一条用户提问都会运行。"""
    from sklearn.metrics import accuracy_score, f1_score,recall_score

    predicted_ids = prediction.predictions.argmax(axis = 1)
    true_ids = prediction.label_ids
    return {
        "accuracy": float(accuracy_score(true_ids, predicted_ids)),
        # macro 平均让三个类别各占同样权重，不让大类掩盖小类。
        "macro_f1": float(f1_score(true_ids, predicted_ids, labels=[0, 1, 2], average="macro", zero_division=0)),
        "restricted_recall": float(recall_score(true_ids, predicted_ids, labels=[LABEL2ID["restricted"]], average="macro", zero_division=0)),
    }


def train_classifier(base_model: Path,data_dir: Path, output_dir: Path) -> dict:
    """加载基座 -> 训练 -> 验证集选模型 -> 最后测试 -> 保存。"""
    from transformers import (
        AutoConfig,AutoModelForSequenceClassification,AutoTokenizer,
        DataCollatorWithPadding, Trainer,TrainingArguments,set_seed,
    )

    if output_dir.exists():
        raise FileExistsError(f"模型输出目录已存在，请修改 OUTPUT_DIR 使用新版本: {output_dir}")
    if not base_model.is_dir():
        raise ValueError(f"本地基座不存在，不自动下载: {base_model}")
    splits = load_splits(data_dir)
    set_seed(SEED)
    tokenizer = AutoTokenizer.from_pretrained(base_model,local_files_only=True,trust_remote_code=False)
    datasets = {}
    for name, rows in splits.items():
        datasets[name] = make_dataset(rows, tokenizer)

    id2label = {}
    for label, index in LABEL2ID.items():
        id2label[index] = label
    config = AutoConfig.from_pretrained(base_model, local_files_only=True, trust_remote_code=False)
    config.num_labels = len(LABEL2ID)
    config.label2id = dict(LABEL2ID)
    config.id2label = id2label
    # 基座没有茶叶分类头；新分类头随机初始化的提示在这里是正常的。
    model = AutoModelForSequenceClassification.from_pretrained(
        base_model, config=config, local_files_only=True, trust_remote_code=False,
    )
    output_dir.mkdir(parents=True,exist_ok=False)
    arguments = TrainingArguments(
        output_dir=str(output_dir / "checkpoints"),
        use_cpu=True, num_train_epochs=EPOCHS,
        per_device_train_batch_size=BATCH_SIZE,
        per_device_eval_batch_size=BATCH_SIZE,
        learning_rate=2e-5, weight_decay=0.01,
        eval_strategy="epoch", save_strategy="epoch",
        load_best_model_at_end=True, metric_for_best_model="macro_f1",
        greater_is_better=True, save_total_limit=1,
        seed=SEED, data_seed=SEED, report_to="none",
        dataloader_num_workers=0, dataloader_pin_memory=False,
        logging_steps=10,
    )
    trainer = Trainer(
        model=model,args=arguments,
        train_dataset=datasets["train"], eval_dataset=datasets["validation"],
        # 每一批动态补齐长度，而不是所有问题统一填到128个Token。
        data_collator=DataCollatorWithPadding(tokenizer),
        compute_metrics=compute_metrics,
    )
    trainer.train()
    # 选最优模型只用validation，test只在训练完成后报告，不用于调参。
    metrics = trainer.evaluate(datasets["test"], metric_key_prefix="test")
    for language in ("en", "zh"):
        language_rows =[]
        for row in splits["test"]:
            if row.get("language") == language:
                language_rows.append(row)
        if language_rows:
            language_metrics = trainer.evaluate(make_dataset(language_rows,tokenizer),metric_key_prefix=f"test_{language}")
            metrics.update(language_metrics)
    trainer.save_model(str(output_dir))
    tokenizer.save_pretrained(output_dir)
    with (output_dir / "test_metrics.json").open("w", encoding="utf-8") as file:
        json.dump(metrics, file, ensure_ascii=False,indent=2)
    return metrics

def main() -> int:
    logger = setup_logging(PROJECT_ROOT / "logs/train_query_classifier.log")
    try:
        logger.info("开始CPU训练；基座只读: %s；输出: %s", BASE_MODEL, OUTPUT_DIR)
        metrics = train_classifier(BASE_MODEL,DATA_DIR,OUTPUT_DIR)
        logger.info("训练与保存完成，测试结果: %s", metrics)
        return 0
    except Exception:
        logger.exception("分类器训练失败；不要删除初始模型，检查错误后使用新的输出目录重试")
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
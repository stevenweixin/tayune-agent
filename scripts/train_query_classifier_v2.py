"""一次预先固定设置的英文实验：不修改第一版，不根据测试成绩重训。"""
import hashlib
import json

import torch
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix, f1_score
from transformers import (
    AutoModelForSequenceClassification, AutoTokenizer, DataCollatorWithPadding,
    EarlyStoppingCallback, Trainer, TrainingArguments, set_seed,
)

from app.core.logger import setup_logging
from scripts.train_query_classifier import BASE_MODEL, make_dataset, compute_metrics
from scripts.prepare_query_data import LABEL2ID, PROJECT_ROOT
from scripts.prepare_query_v2 import OUTPUT_DIR as DATA_DIR, OLD_DIR, read_jsonl, validate_splits

OUTPUT_DIR = PROJECT_ROOT / "data/generated/query_classifier_model_v2"
V1_MODEL = PROJECT_ROOT / "data/generated/query_classifier_model_v1"


def evaluate_model(model_dir, rows):
    """相同评估实现比较两个模型，记录每道题，不能只保留总分。"""
    tokenizer = AutoTokenizer.from_pretrained(model_dir, local_files_only=True)
    model = AutoModelForSequenceClassification.from_pretrained(model_dir, local_files_only=True).eval()
    truth, predicted, details = [], [], []
    for start in range(0, len(rows), 16):
        batch = rows[start:start + 16]
        inputs = tokenizer([row["text"] for row in batch], padding=True, return_tensors="pt")
        with torch.inference_mode():
            probabilities = model(**inputs).logits.softmax(-1)
        for row, probability in zip(batch, probabilities):
            prediction = int(probability.argmax())
            truth.append(LABEL2ID[row["label"]])
            predicted.append(prediction)
            details.append({"id": row["id"], "text": row["text"], "label": row["label"],
                            "prediction": model.config.id2label[prediction], "score": float(probability.max())})
    report = classification_report(truth, predicted, labels=[0, 1, 2], target_names=list(LABEL2ID), output_dict=True, zero_division=0)
    return {"count": len(rows), "accuracy": float(accuracy_score(truth, predicted)),
            "macro_f1": float(f1_score(truth, predicted, labels=[0, 1, 2], average="macro", zero_division=0)),
            "per_class": report, "confusion_matrix": confusion_matrix(truth, predicted, labels=[0, 1, 2]).tolist(),
            "predictions": details}


def main():
    logger = setup_logging(PROJECT_ROOT / "logs/train_query_classifier_v2.log")
    if OUTPUT_DIR.exists():
        raise FileExistsError(f"不覆盖已有实验: {OUTPUT_DIR}")
    with (DATA_DIR / "manifest.json").open(encoding="utf-8") as file:
        manifest = json.load(file)
    splits = {}
    for name in ("train", "validation", "test"):
        path = DATA_DIR / f"{name}.jsonl"
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["files"][name]["sha256"]:
            raise ValueError(f"冻结数据发生变化: {name}")
        splits[name] = read_jsonl(path)
    validate_splits(splits)
    torch.set_num_threads(4)
    set_seed(manifest["seed"])
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL, local_files_only=True)
    model = AutoModelForSequenceClassification.from_pretrained(
        BASE_MODEL, num_labels=3, label2id=LABEL2ID,
        id2label={index: label for label, index in LABEL2ID.items()}, local_files_only=True,
    )
    train_data = make_dataset(splits["train"], tokenizer)
    validation_data = make_dataset(splits["validation"], tokenizer)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=False)
    args = TrainingArguments(
        output_dir=str(OUTPUT_DIR / "checkpoints"), use_cpu=True,
        num_train_epochs=manifest["max_epochs"], learning_rate=manifest["learning_rate"],
        per_device_train_batch_size=8, per_device_eval_batch_size=16, weight_decay=0.01,
        eval_strategy="epoch", save_strategy="epoch", load_best_model_at_end=True,
        metric_for_best_model="macro_f1", greater_is_better=True, save_total_limit=1,
        seed=42, data_seed=42, report_to="none", logging_steps=32,
        dataloader_num_workers=0, dataloader_pin_memory=False,
    )
    trainer = Trainer(model=model, args=args, train_dataset=train_data, eval_dataset=validation_data,
                      data_collator=DataCollatorWithPadding(tokenizer), compute_metrics=compute_metrics,
                      callbacks=[EarlyStoppingCallback(early_stopping_patience=manifest["early_stopping_patience"])])
    logger.info("开始第二版英文实验，训练%d条，验证%d条；仅验证集选择最佳模型", len(train_data), len(validation_data))
    trainer.train()
    trainer.save_model(str(OUTPUT_DIR))
    tokenizer.save_pretrained(OUTPUT_DIR)
    trainer.save_state()
    results = {"best_validation_macro_f1": trainer.state.best_metric,
               "best_checkpoint": trainer.state.best_model_checkpoint,
               "epochs_run": trainer.state.epoch, "data_manifest": manifest}
    # 模型已经确定，才打开固定测试集算分；不以结果反馈调整本次训练。
    results["v1_new_test"] = evaluate_model(V1_MODEL, splits["test"])
    results["v2_new_test"] = evaluate_model(OUTPUT_DIR, splits["test"])
    old_test = [row for row in read_jsonl(OLD_DIR / "test.jsonl") if row["language"] == "en"]
    results["v2_old_test_diagnostic"] = evaluate_model(OUTPUT_DIR, old_test)
    with (OUTPUT_DIR / "comparison.json").open("w", encoding="utf-8") as file:
        json.dump(results, file, ensure_ascii=False, indent=2)
    for name in ("v1_new_test", "v2_new_test", "v2_old_test_diagnostic"):
        result = results[name]
        logger.info("%s: accuracy=%.4f macro_f1=%.4f", name, result["accuracy"], result["macro_f1"])


if __name__ == "__main__":
    main()

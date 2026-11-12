"""RAGAS评历史真实回答：冻结数据、隔离ML进程、物理请求预算、逐项保留失败。"""
import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import math
import os
import statistics
import subprocess
from pathlib import Path

from tests.performance.ragas_dataset import file_hash, prepare
from tests.performance.ragas_judge import ACTIVE_SCORE, JudgeBudget, JudgeTransport

ROOT = Path(__file__).resolve().parents[2]
METRICS = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    path.chmod(0o600)


def summarize(samples, results):
    groups = {}
    for name, selected in (("all_archived", samples),
                           ("single_turn_knowledge", [s for s in samples if s["category"] == "knowledge"]),
                           ("multi_turn_projection", [s for s in samples if s["category"] != "knowledge"])):
        for profile in ("sequential", "concurrent4"):
            ids = {s["sample_id"] for s in selected if s["profile"] == profile}
            scores = {}
            for metric in METRICS:
                rows = [r for r in results if r["sample_id"] in ids and r["metric"] == metric]
                values = [r["score"] for r in rows if r["status"] == "scored"]
                scores[metric] = {"mean": statistics.mean(values) if values else None,
                                  "valid": len(values), "planned": len(ids),
                                  "statuses": {status: sum(r["status"] == status for r in rows)
                                               for status in ("scored", "not_applicable", "undefined", "error")}}
            groups[name + ":" + profile] = scores
    return groups


async def start_embeddings(ml_python, model_path, output):
    from ragas.embeddings.base import BaseRagasEmbedding

    class LocalEmbeddings(BaseRagasEmbedding):
        def __init__(self):
            super().__init__()
            self.process = None
            self.vectors = {}
            self.gate = asyncio.Lock()
            self.log = None

        async def start(self):
            env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
            self.log = (output / "embedding_worker.log").open("x")
            self.process = await asyncio.create_subprocess_exec(str(ml_python), "-m", "tests.performance.ragas_embed_worker",
                "--model", str(model_path), cwd=ROOT, env=env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=self.log, limit=2**20)
            line = await asyncio.wait_for(self.process.stdout.readline(), 120)
            ready = json.loads(line)
            if ready != {"ready": True, "dimension": 1024}:
                raise RuntimeError("Local embedding worker was not ready")

        def embed_text(self, text, **kwargs):
            raise RuntimeError("This isolated evaluator uses async embeddings only")

        async def aembed_text(self, text, **kwargs):
            return (await self.aembed_texts([text]))[0]

        async def aembed_texts(self, texts, **kwargs):
            async with self.gate:
                missing = []
                for text in texts:
                    if text not in self.vectors and text not in missing:
                        missing.append(text)
                if missing:
                    self.process.stdin.write((json.dumps({"texts": missing}) + "\n").encode())
                    await self.process.stdin.drain()
                    line = await asyncio.wait_for(self.process.stdout.readline(), 35)
                    vectors = json.loads(line)["vectors"]
                    if len(vectors) != len(missing):
                        raise RuntimeError("Embedding batch shape mismatch")
                    for text, vector in zip(missing, vectors):
                        if len(vector) != 1024 or not all(math.isfinite(value) for value in vector):
                            raise RuntimeError("Invalid local embedding")
                        self.vectors[text] = vector
                return [self.vectors[text] for text in texts]

        async def close(self):
            if self.process and self.process.returncode is None:
                try:
                    self.process.stdin.write(b'{"close":true}\n')
                    await self.process.stdin.drain()
                    await asyncio.wait_for(self.process.wait(), 5)
                except (OSError, TimeoutError):
                    self.process.kill()
                    await self.process.wait()
            if self.log:
                self.log.close()

    embeddings = LocalEmbeddings()
    try:
        await embeddings.start()
    except BaseException:
        await embeddings.close()
        raise
    return embeddings


async def execute(args):
    output = args.output.resolve()
    if output.exists() or not output.is_relative_to(ROOT / "reports"):
        raise ValueError("Output must be a new reports/ directory")
    if not args.ledger.resolve().is_relative_to(ROOT / "reports") or not args.paid:
        raise ValueError("Explicit --paid and a private reports/ authorization ledger required")
    output.mkdir(parents=True)
    # 在导入评测库之前关闭遥测；缓存/用户目录只落在本次工作区。
    home = output / "isolated_home"
    home.mkdir()
    os.environ.update(RAGAS_DO_NOT_TRACK="true", LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false",
                      HF_HUB_DISABLE_TELEMETRY="1", HOME=str(home), XDG_CACHE_HOME=str(home / "cache"))
    from dotenv import dotenv_values
    import httpx
    from openai import AsyncOpenAI
    from ragas.llms import llm_factory
    from ragas.metrics.collections import Faithfulness, AnswerRelevancy, ContextPrecision, ContextRecall

    values = dotenv_values(ROOT / ".env", interpolate=False)
    key = os.getenv("DEEPSEEK_API_KEY") or values.get("DEEPSEEK_API_KEY")
    if not key:
        raise ValueError("Missing backend DeepSeek credential; never pass it on the command line")
    samples = prepare(args.archive, args.references)
    save(output / "frozen_dataset.json", samples)
    save(output / "frozen_references.json", json.loads(args.references.read_text()))
    archive_manifest = json.loads(args.archive.with_name("manifest.json").read_text())
    manifest = {"evaluation_date": "2026-11-12", "evaluator_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "archive_sha256": file_hash(args.archive), "archive_commit": archive_manifest["commit"],
        "archive_model": archive_manifest["model"], "judge_model": "deepseek-flash",
        "dataset_sha256": file_hash(output / "frozen_dataset.json"), "reference_sha256": file_hash(args.references),
        "samples": len(samples), "case_ids": len({s["case_id"] for s in samples}),
        "distinct_original_questions": len({s["original_question"] for s in samples}),
        "independent_human_gold": False, "generation_was_rerun": False,
        "reference_method": json.loads(args.references.read_text())["method"],
        "metrics": list(METRICS), "relevancy_strictness": 3, "embedding": "local BGE-M3, normalized1024, separate ML interpreter",
        "rates_usd_per_million": {"input_cache_miss_peak": .30, "output_peak": 1.20},
        "price_source": "https://api-docs.deepseek.com/quick_start/pricing/",
        "source_sha256": {}, "dependencies": {p: importlib.metadata.version(p) for p in
            ("ragas", "instructor", "openai", "httpx", "numpy", "langchain-core", "langchain-openai", "pydantic")}}
    for name in ("run_ragas.py", "ragas_dataset.py", "ragas_judge.py", "ragas_embed_worker.py", "requirements-ragas.txt"):
        path = ROOT / "tests/performance" / name
        manifest["source_sha256"][str(path.relative_to(ROOT))] = file_hash(path)
    manifest["reference_corpus_sha256"] = {name: file_hash(ROOT / "data/generated" / name)
                                           for name in ("articles.jsonl", "policies.jsonl")}
    save(output / "manifest.json", manifest)
    (output / "pip-freeze.txt").write_text(subprocess.check_output([str(Path(os.sys.executable)), "-m", "pip", "freeze"], text=True))
    budget = JudgeBudget(args.ledger)
    embeddings, results, stopped = None, [], None
    try:
        # 先确保本地Embedding能用，再开始任何付费请求。
        embeddings = await start_embeddings(args.ml_python, args.model_path, output)
        transport = JudgeTransport(budget, output / "judge_responses.jsonl")
        async with httpx.AsyncClient(transport=transport, trust_env=False, timeout=45,
                                     follow_redirects=False, headers={"Accept-Encoding": "identity"}) as http:
            async with AsyncOpenAI(api_key=key, base_url="https://api.deepseek.com/v1", http_client=http,
                                    max_retries=0, timeout=45) as client:
                judge = llm_factory("deepseek-flash", provider="openai", client=client, temperature=0,
                    max_tokens=2048, max_retries=1, extra_body={"thinking": {"type": "disabled"}})
                metrics = {"faithfulness": Faithfulness(llm=judge),
                           "answer_relevancy": AnswerRelevancy(llm=judge, embeddings=embeddings, strictness=3),
                           "context_precision": ContextPrecision(llm=judge), "context_recall": ContextRecall(llm=judge)}
                for index, sample in enumerate(samples):
                    for name in METRICS:
                        if budget.stopped:
                            stopped = budget.stopped
                            break
                        row = {"sample_id": sample["sample_id"], "metric": name, "score": None}
                        marker = ACTIVE_SCORE.set({"sample_id": sample["sample_id"], "metric": name})
                        try:
                            if name in ("context_precision", "context_recall") and not sample["reference"]:
                                row.update(status="not_applicable", reason="no_positive_fact_reference_or_unresolved_antecedent")
                            elif name != "answer_relevancy" and not sample["retrieved_contexts"]:
                                row.update(status="not_applicable", reason="no_model_visible_context")
                            else:
                                kwargs = {"user_input": sample["user_input"]}
                                if name in ("faithfulness", "answer_relevancy"):
                                    kwargs["response"] = sample["response"]
                                if name != "answer_relevancy":
                                    kwargs["retrieved_contexts"] = sample["retrieved_contexts"]
                                if name in ("context_precision", "context_recall"):
                                    kwargs["reference"] = sample["reference"]
                                score = await asyncio.wait_for(metrics[name].ascore(**kwargs), 180)
                                value = float(score.value)
                                row.update(status="scored" if math.isfinite(value) else "undefined",
                                           score=value if math.isfinite(value) else None,
                                           reason=score.reason)
                        except Exception as error:
                            row.update(status="error", error_type=type(error).__name__)
                            if index == 0:
                                stopped = "smoke_failure"
                        finally:
                            ACTIVE_SCORE.reset(marker)
                        results.append(row)
                        with (output / "scores.jsonl").open("a", encoding="utf-8") as file:
                            file.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                        print(json.dumps({**row, "attempts": budget.attempts,
                                          "estimated_usd": round(budget.estimated_usd, 6)}, ensure_ascii=False), flush=True)
                        if stopped:
                            break
                    save(output / "summary.json", {"budget": budget.summary(), "stopped": stopped,
                        "scores_written": len(results), "planned_scores": len(samples) * len(METRICS),
                        "groups": summarize(samples, results)})
                    if stopped:
                        break
    finally:
        if embeddings:
            await embeddings.close()
        save(output / "summary.json", {"budget": budget.summary(), "stopped": stopped or budget.stopped,
            "scores_written": len(results), "planned_scores": len(samples) * len(METRICS), "groups": summarize(samples, results)})
        budget.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--references", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--ml-python", type=Path, required=True)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--paid", action="store_true")
    args = parser.parse_args()
    asyncio.run(execute(args))


if __name__ == "__main__":
    main()

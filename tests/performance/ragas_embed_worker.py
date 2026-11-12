"""在既有ML解释器中只读加载本地BGE-M3，JSON行协议；不打开网络或Milvus。"""
import argparse
import json
import os
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false",
                      HF_HUB_DISABLE_TELEMETRY="1", OMP_NUM_THREADS="2", MKL_NUM_THREADS="2")
    from pathlib import Path
    from app.rag.embeddings import LocalBGEmbeddings
    model = LocalBGEmbeddings(Path(args.model))
    print(json.dumps({"ready": True, "dimension": model.dimension}), flush=True)
    for line in sys.stdin:
        payload = json.loads(line)
        if payload.get("close"):
            return
        vectors = model.embed_documents(payload["texts"])
        print(json.dumps({"vectors": vectors}, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()

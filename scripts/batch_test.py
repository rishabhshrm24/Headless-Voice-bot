"""
Batch-test the voicebot from an external tool.

Usage:
  python scripts/batch_test.py questions.json output.csv
  python scripts/batch_test.py --repeat "What is your refund policy?" 5 output.csv

questions.json format: {"questions": ["...", "..."]}  OR  ["...", "..."]
"""
import sys
import json
import csv
import requests

API_URL = "http://localhost:8000/chat/batch"


def load_questions_from_file(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        return data["questions"]
    return data


def run_batch(questions: list[str]) -> list[dict]:
    resp = requests.post(API_URL, json={"questions": questions}, timeout=120)
    resp.raise_for_status()
    return resp.json()["results"]


def write_csv(results: list[dict], out_path: str) -> None:
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["question", "answer", "sources"])
        for r in results:
            sources = "; ".join(s["source"] for s in r.get("sources", []))
            writer.writerow([r["question"], r["answer"], sources])


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--repeat":
        question, count, out_path = sys.argv[2], int(sys.argv[3]), sys.argv[4]
        questions = [question] * count
    elif len(sys.argv) >= 3:
        questions = load_questions_from_file(sys.argv[1])
        out_path = sys.argv[2]
    else:
        print(__doc__)
        sys.exit(1)

    results = run_batch(questions)
    write_csv(results, out_path)
    print(f"Wrote {len(results)} results to {out_path}")


if __name__ == "__main__":
    main()

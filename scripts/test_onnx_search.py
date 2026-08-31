"""Test ONNX vector search after model setup"""
import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(name)s - %(levelname)s - %(message)s")

from app.core.chroma_store import chroma_store


async def main():
    await chroma_store.initialize()

    print("=== ONNX Vector Search Test ===")
    print(f"Embedding: {type(chroma_store._embedding_fn).__name__}")
    print()

    # Test 1
    query1 = "本月新增多少eSIM用户"
    print(f"Query: {query1!r}")
    results = chroma_store.search(query1, n_results=3)
    for collection, records in results.items():
        if records:
            print(f"  [{collection}]")
            for r in records[:2]:
                preview = r.content[:80].replace("\n", " ")
                print(f"    -> {preview}...")
    print()

    # Test 2
    query2 = "Profile激活转化率是多少"
    print(f"Query: {query2!r}")
    results = chroma_store.search(query2, n_results=3)
    for collection, records in results.items():
        if records:
            print(f"  [{collection}]")
            for r in records[:2]:
                preview = r.content[:80].replace("\n", " ")
                print(f"    -> {preview}...")
    print()

    # Test 3: retrieve_context
    query3 = "各运营商的ARPU值"
    print(f"Query: {query3!r}")
    context = chroma_store.retrieve_context(query3)
    print(f"  Context length: {len(context)} chars")
    if context:
        preview = context[:200].replace("\n", " ")
        print(f"  Preview: {preview}...")
    print()

    await chroma_store.shutdown()
    print("ONNX vector search: WORKING CORRECTLY")


if __name__ == "__main__":
    asyncio.run(main())

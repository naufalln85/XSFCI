# ============================================================
# XFSCI RAG Indexer — ChromaDB Vector Store Builder
# ============================================================
# Script ini membaca semua file runbook Markdown di folder
# knowledge_base/runbooks/ dan membangun vector index di
# ChromaDB agar AI Agent bisa melakukan semantic search.
#
# Cara pakai:
#   python knowledge_base/rag_indexer.py
#
# Cara kerja:
#   1. Baca semua file .md di runbooks/
#   2. Split menjadi chunks (500 karakter + 50 overlap)
#   3. Embedding dengan sentence-transformers (all-MiniLM-L6-v2)
#   4. Simpan ke ChromaDB (persist ke disk)
#
# Output:
#   - knowledge_base/vectordb/ (ChromaDB persistent store)
# ============================================================

import os
import sys
import glob
from pathlib import Path

import yaml
import chromadb
from chromadb.utils import embedding_functions
from loguru import logger


def load_config() -> dict:
    """Load konfigurasi RAG dari config.yaml."""
    config_path = Path(__file__).parent.parent / "configs" / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return config.get("rag", {})


def split_into_chunks(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """
    Split teks panjang menjadi chunks yang lebih kecil.
    
    Mengapa perlu di-chunk?
    - Embedding model memiliki batas token (~256-512 token)
    - Chunk yang lebih kecil = hasil pencarian yang lebih presisi
    - Overlap memastikan konteks tidak terputus
    
    Args:
        text: Teks lengkap dari file runbook
        chunk_size: Ukuran maksimal setiap chunk (karakter)
        overlap: Jumlah karakter yang overlap antar-chunk
    
    Returns:
        List of text chunks
    """
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        
        # Coba potong di akhir kalimat terdekat agar tidak terpotong di tengah kata
        if end < len(text):
            last_period = chunk.rfind(".")
            last_newline = chunk.rfind("\n")
            cut_point = max(last_period, last_newline)
            if cut_point > chunk_size * 0.5:  # Minimal 50% dari chunk terisi
                chunk = chunk[:cut_point + 1]
                end = start + cut_point + 1
        
        chunks.append(chunk.strip())
        start = end - overlap  # Mundur sedikit untuk overlap
    
    return [c for c in chunks if len(c) > 20]  # Filter chunk yang terlalu pendek


def index_runbooks(config: dict = None) -> chromadb.Collection:
    """
    Baca semua file runbook dan index ke ChromaDB.
    
    Proses:
    1. Inisialisasi ChromaDB client (persistent)
    2. Buat/reset collection
    3. Baca setiap file .md di runbooks/
    4. Split menjadi chunks
    5. Generate embedding dan simpan
    
    Returns:
        ChromaDB Collection yang sudah terisi
    """
    if config is None:
        config = load_config()
    
    # Path setup
    base_dir = Path(__file__).parent
    runbook_dir = base_dir / "runbooks"
    persist_dir = base_dir / config.get("persist_directory", "vectordb").replace("knowledge_base/", "")
    
    # Pastikan direktori ada
    persist_dir.mkdir(parents=True, exist_ok=True)
    
    # Inisialisasi embedding function
    embedding_model = config.get("embedding_model", "all-MiniLM-L6-v2")
    logger.info(f"Loading embedding model: {embedding_model}")
    embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=embedding_model
    )
    
    # Inisialisasi ChromaDB
    logger.info(f"Initializing ChromaDB at: {persist_dir}")
    client = chromadb.PersistentClient(path=str(persist_dir))
    
    # Buat collection (reset jika sudah ada)
    collection_name = config.get("collection_name", "xfsci_runbooks")
    try:
        client.delete_collection(name=collection_name)
        logger.info(f"Deleted existing collection: {collection_name}")
    except Exception:
        pass
    
    collection = client.create_collection(
        name=collection_name,
        embedding_function=embed_fn,
        metadata={"description": "XFSCI SRE Incident Runbooks for RAG"}
    )
    
    # Baca dan index semua runbook
    chunk_size = config.get("chunk_size", 500)
    chunk_overlap = config.get("chunk_overlap", 50)
    
    runbook_files = sorted(glob.glob(str(runbook_dir / "*.md")))
    if not runbook_files:
        logger.warning(f"No runbook files found in {runbook_dir}")
        return collection
    
    total_chunks = 0
    for filepath in runbook_files:
        filename = os.path.basename(filepath)
        logger.info(f"Processing: {filename}")
        
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        
        # Extract metadata dari file
        runbook_id = filename.replace(".md", "").upper().replace("_", "-")
        
        # Split menjadi chunks
        chunks = split_into_chunks(content, chunk_size, chunk_overlap)
        
        # Tambahkan ke collection
        for i, chunk in enumerate(chunks):
            doc_id = f"{runbook_id}_chunk_{i:03d}"
            collection.add(
                ids=[doc_id],
                documents=[chunk],
                metadatas=[{
                    "source_file": filename,
                    "runbook_id": runbook_id,
                    "chunk_index": i,
                    "total_chunks": len(chunks),
                    "type": "runbook"
                }]
            )
        
        total_chunks += len(chunks)
        logger.info(f"  → {len(chunks)} chunks indexed from {filename}")
    
    logger.success(f"RAG Index complete: {len(runbook_files)} files → {total_chunks} chunks")
    logger.info(f"Collection '{collection_name}' has {collection.count()} documents")
    
    return collection


def query_runbooks(query: str, top_k: int = 3, config: dict = None) -> list[dict]:
    """
    Semantic search di runbook collection.
    
    Args:
        query: Query teks (misal: "memory usage terus naik tanpa turun")
        top_k: Jumlah hasil teratas
        config: Konfigurasi RAG
    
    Returns:
        List of {document, metadata, distance} dictionaries
    """
    if config is None:
        config = load_config()
    
    base_dir = Path(__file__).parent
    persist_dir = base_dir / config.get("persist_directory", "vectordb").replace("knowledge_base/", "")
    
    # Inisialisasi
    embedding_model = config.get("embedding_model", "all-MiniLM-L6-v2")
    embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=embedding_model
    )
    
    client = chromadb.PersistentClient(path=str(persist_dir))
    collection_name = config.get("collection_name", "xfsci_runbooks")
    collection = client.get_collection(
        name=collection_name,
        embedding_function=embed_fn
    )
    
    # Query
    results = collection.query(
        query_texts=[query],
        n_results=top_k,
        include=["documents", "metadatas", "distances"]
    )
    
    # Format hasil
    formatted = []
    for i in range(len(results["ids"][0])):
        formatted.append({
            "id": results["ids"][0][i],
            "document": results["documents"][0][i],
            "metadata": results["metadatas"][0][i],
            "distance": results["distances"][0][i],
            # Konversi distance ke similarity score (0-1, semakin tinggi semakin mirip)
            "similarity": max(0.0, 1.0 - results["distances"][0][i])
        })
    
    return formatted


# ============================================================
# Main: Jalankan indexing
# ============================================================
if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("XFSCI RAG Indexer — Building Runbook Vector Store")
    logger.info("=" * 60)
    
    collection = index_runbooks()
    
    # Test query
    logger.info("\n--- Test Query ---")
    test_queries = [
        "memory usage meningkat terus dan pod restart",
        "CPU tinggi dan latency naik",
        "pod crash loop berkali-kali",
        "jaringan lambat timeout",
    ]
    
    for query in test_queries:
        results = query_runbooks(query, top_k=2)
        logger.info(f"\nQuery: '{query}'")
        for r in results:
            logger.info(f"  → [{r['metadata']['source_file']}] "
                       f"similarity={r['similarity']:.3f}")
    
    logger.success("\nRAG Indexer complete! Vector store ready.")

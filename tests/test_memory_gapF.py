"""Gap F — semantic memory passa a un embedder TF-IDF lessicale deterministico.

Sostituisce di default NgramHashEmbedder (n-grammi di caratteri hashati,
separazione debole) con un vettore term-frequency pesato IDF e L2-normalizzato,
senza dipendenze di rete / modelli non deterministici.
"""

from __future__ import annotations

import json

import pytest

from locus.db import Database
from locus.memory import Memory, TfidfEmbedder, _cosine_similarity


@pytest.fixture
async def db() -> Database:
    d = Database()
    await d.initialize(":memory:")
    props = json.load(open("data/properties.json"))
    await d.seed_properties(props)
    yield d
    await d.close()


async def test_tfidf_deterministic() -> None:
    """Stesse feature -> stesso vettore (nessuna casualita)."""
    e = TfidfEmbedder()
    a = await e.encode("is the passphrase two words?")
    b = await e.encode("is the passphrase two words?")
    assert a == b
    assert e.dimension == 256
    assert len(a) == e.dimension


async def test_tfidf_bytes_roundtrip_deterministic() -> None:
    """Il vettore passa per il pipeline float32 (_vec_to_blob/_blob_to_vec)."""
    from locus.memory import _blob_to_vec, _vec_to_blob

    e = TfidfEmbedder()
    a = await e.encode("word games are fun")
    blob = _vec_to_blob(a)
    restored = _blob_to_vec(blob)
    assert restored == pytest.approx(a)
    assert len(restored) == e.dimension


async def test_tfidf_identical_text_is_duplicate(db: Database) -> None:
    """Testo identico -> coseno ~1.0, sopra la soglia di dedup 0.9."""
    mem = Memory(db)
    await mem.remember("do you like word games?")
    dup, matches = await mem.dedup("do you like word games?", threshold=0.9)
    assert dup is True
    assert matches[0][1] == pytest.approx(1.0, abs=1e-6)


async def test_tfidf_unrelated_text_is_not_duplicate(db: Database) -> None:
    """Testi non correlati -> similarita bassa, sotto la soglia di dedup."""
    e = TfidfEmbedder()
    a = await e.encode("is the passphrase two words?")
    c = await e.encode("what is your favourite color?")
    sim = _cosine_similarity(a, c)
    assert sim < 0.9
    mem = Memory(db)
    await mem.remember("completely unrelated sentence about weather")
    dup, _ = await mem.dedup("what is the capital of france?", threshold=0.9)
    assert dup is False


async def test_tfidf_better_separation_than_ngram() -> None:
    """Separazione piu netta del char-ngram hashing: vicini-duplicati ~1.0 e
    non correlati piu bassi (meno rumore da collisioni di n-grammi)."""
    from locus.memory import NgramHashEmbedder

    tfidf = TfidfEmbedder()
    ngram = NgramHashEmbedder()
    a_t = await tfidf.encode("the password is hunter two")
    b_t = await tfidf.encode("the password is hunter two")
    c_t = await tfidf.encode("how is the weather today")
    assert _cosine_similarity(a_t, b_t) == pytest.approx(1.0)
    tfidf_unrelated = _cosine_similarity(a_t, c_t)
    a_n = await ngram.encode("the password is hunter two")
    c_n = await ngram.encode("how is the weather today")
    ngram_unrelated = _cosine_similarity(a_n, c_n)
    # il TF-IDF lessicale separa meglio dei soli n-grammi di caratteri
    assert tfidf_unrelated <= ngram_unrelated


async def test_tfidf_recall_ranks(db: Database) -> None:
    """La recall trova il contenuto piu rilevante per vocaboli condivisi."""
    mem = Memory(db)
    await mem.remember("the sky is blue")
    await mem.remember("word games are fun")
    recalled = await mem.recall_texts("word games", top_k=5)
    assert recalled[0] == "word games are fun"


def test_memory_uses_tfidf_by_default(db: Database) -> None:
    """Il default di Memory e' TfidfEmbedder, non piu' NgramHashEmbedder."""
    mem = Memory(db)
    assert isinstance(mem.embedder, TfidfEmbedder)


async def test_tfidf_idf_weighting() -> None:
    """Con un corpus disponibile, il pesaggio IDF riduce le parole comuni."""
    corpus = [
        "alpha bravo",
        "alpha charlie",
        "delta echo",
    ]
    e = TfidfEmbedder(corpus=corpus)
    # "alpha" compare in 2 doc, "bravo"/"charlie"/"delta" in 1 solo -> IDF piu basso
    assert e.idf("alpha") is not None
    assert e.idf("alpha") < e.idf("bravo")
    assert e.idf("bravo") == e.idf("delta")
    assert e.idf("bravo") is not None
    # le stopword sono escluse: non hanno peso IDF
    assert e.idf("the") is None
    # ancora deterministico
    a = await e.encode("alpha delta")
    b = await e.encode("alpha delta")
    assert a == b

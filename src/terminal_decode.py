"""Incremental text decoding helpers for fragmented serial input."""

from __future__ import annotations

import codecs
from collections.abc import Iterable


def decode_payload_chunks(chunks: Iterable[bytes], encoding: str) -> list[str]:
    """Decode chunks as one stream while retaining one output item per chunk."""
    try:
        decoder_factory = codecs.getincrementaldecoder(encoding)
    except LookupError:
        decoder_factory = codecs.getincrementaldecoder("utf-8")
    decoder = decoder_factory(errors="replace")
    return [decoder.decode(bytes(chunk), final=False) for chunk in chunks]

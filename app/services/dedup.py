from __future__ import annotations

import hashlib
import re
from datetime import datetime
from itertools import groupby

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Appeal, Region


_SPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


def normalize_for_dedup(title: str, content: str) -> str:
    text = f"{title}\n{content}".lower()
    text = _PUNCT_RE.sub("", text)
    return _SPACE_RE.sub("", text)


def exact_content_hash(title: str, content: str) -> str:
    return hashlib.sha256(normalize_for_dedup(title, content).encode("utf-8")).hexdigest()


def _shingles(text: str, width: int = 4) -> set[str]:
    normalized = normalize_for_dedup("", text)
    if len(normalized) <= width:
        return {normalized} if normalized else set()
    return {normalized[index : index + width] for index in range(len(normalized) - width + 1)}


def simhash64(title: str, content: str) -> str:
    features = _shingles(f"{title}{content}")
    if not features:
        return "0" * 16
    weights = [0] * 64
    for feature in features:
        value = int(hashlib.blake2b(feature.encode("utf-8"), digest_size=8).hexdigest(), 16)
        for bit in range(64):
            weights[bit] += 1 if value & (1 << bit) else -1
    fingerprint = sum(1 << bit for bit, weight in enumerate(weights) if weight >= 0)
    return f"{fingerprint:016x}"


def duplicate_group_key(
    city_code: str,
    received_at: datetime,
    title: str,
    content: str,
) -> str:
    # Near-duplicate candidates are intentionally scoped by city and month.
    # The SimHash stays explainable and can later be clustered by Hamming
    # distance in an offline snapshot job.
    return f"{city_code or 'unknown'}:{received_at:%Y-%m}:{simhash64(title, content)}"


def hamming_distance(left: str, right: str) -> int:
    return bin(int(left, 16) ^ int(right, 16)).count("1")


def _fingerprint_from_key(value: str) -> str:
    candidate = (value or "").rsplit(":", 1)[-1]
    return candidate if re.fullmatch(r"[0-9a-f]{16}", candidate) else ""


def _cluster_block(
    records: list[tuple[int, str, bool]],
    *,
    group_prefix: str,
    max_hamming_distance: int,
) -> tuple[list[dict[str, object]], int]:
    """Cluster one city-month block with four-band SimHash LSH."""

    parents = list(range(len(records)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[max(left_root, right_root)] = min(left_root, right_root)

    buckets: dict[tuple[int, int], list[int]] = {}
    fingerprints: list[str] = []
    for index, (_, fingerprint, _) in enumerate(records):
        fingerprints.append(fingerprint)
        if not fingerprint:
            continue
        numeric = int(fingerprint, 16)
        candidates: set[int] = set()
        for band in range(4):
            band_value = (numeric >> (band * 16)) & 0xFFFF
            candidates.update(buckets.get((band, band_value), []))
        for candidate in candidates:
            if hamming_distance(fingerprint, fingerprints[candidate]) <= max_hamming_distance:
                union(index, candidate)
        for band in range(4):
            band_value = (numeric >> (band * 16)) & 0xFFFF
            buckets.setdefault((band, band_value), []).append(index)

    clusters: dict[int, list[int]] = {}
    for index in range(len(records)):
        clusters.setdefault(find(index), []).append(index)
    mappings: list[dict[str, object]] = []
    for members in clusters.values():
        canonical_index = min(members, key=lambda item: records[item][0])
        canonical_id = records[canonical_index][0]
        cluster_fingerprint = fingerprints[canonical_index]
        for member in members:
            appeal_id = records[member][0]
            mappings.append(
                {
                    "id": appeal_id,
                    "is_canonical": appeal_id == canonical_id,
                    "canonical_appeal_id": None if appeal_id == canonical_id else canonical_id,
                    "duplicate_group_key": f"{group_prefix}:{cluster_fingerprint}",
                }
            )
    return mappings, len(clusters)


def cluster_near_duplicates(
    session: Session,
    quarter: str,
    *,
    max_hamming_distance: int = 3,
) -> dict[str, int]:
    """Resolve exact and near duplicates per prefecture city and month.

    Processing is streamed one city-month at a time, so memory is bounded by a
    single local block rather than the national dataset.
    """

    rows = session.execute(
        select(
            Appeal.id,
            Region.city_code,
            Region.prefecture_city,
            Region.city,
            Appeal.received_at,
            Appeal.duplicate_group_key,
            Appeal.title,
            Appeal.content,
            Appeal.is_canonical,
        )
        .join(Region, Region.id == Appeal.region_id)
        .where(Appeal.quarter == quarter)
        .order_by(
            Region.city_code,
            Region.prefecture_city,
            Region.city,
            Appeal.received_at,
            Appeal.id,
        )
        .execution_options(yield_per=10000)
    )

    def block_key(row) -> tuple[str, str]:
        city = row[1] or row[2] or row[3] or "unknown"
        return str(city), row[4].strftime("%Y-%m")

    processed = 0
    canonical_before = 0
    canonical_after = 0
    group_count = 0
    for (city_key, month_key), grouped_rows in groupby(rows, key=block_key):
        records: list[tuple[int, str, bool]] = []
        for row in grouped_rows:
            fingerprint = _fingerprint_from_key(row[5]) or simhash64(row[6] or "", row[7] or "")
            records.append((int(row[0]), fingerprint, bool(row[8])))
        if not records:
            continue
        mappings, blocks = _cluster_block(
            records,
            group_prefix=f"{city_key}:{month_key}",
            max_hamming_distance=max_hamming_distance,
        )
        session.bulk_update_mappings(Appeal, mappings)
        processed += len(records)
        canonical_before += sum(int(item[2]) for item in records)
        canonical_after += blocks
        group_count += blocks
    session.flush()
    return {
        "processed": processed,
        "canonical_before": canonical_before,
        "canonical_after": canonical_after,
        "near_duplicates_collapsed": max(canonical_before - canonical_after, 0),
        "group_count": group_count,
        "max_hamming_distance": max_hamming_distance,
    }

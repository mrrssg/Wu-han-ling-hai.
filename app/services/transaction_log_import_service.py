import csv
import hashlib
import io
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from app.models.db_manager import DBManager


TARGET_DB = "order_system"


CANONICAL_COLUMNS = [
    "Date created",
    "Date received",
    "Transaction Date",
    "Seller",
    "Order number",
    "Invoice number",
    "Transaction Number",
    "Quantity",
    "Category Label",
    "Offer SKU",
    "Description",
    "Type",
    "Payment status",
    "Payment reference",
    "Amount",
    "Debit",
    "Credit",
    "Balance",
    "Currency",
    "Customer order reference",
    "Seller order reference",
    "Billing cycle date",
    "Seller ID",
    "Order line ID",
    "Refund ID",
    "Sales channel",
]


HEADER_ALIASES = {
    "Store": "Seller",
    "Store order reference": "Seller order reference",
    "Store ID": "Seller ID",
}


STORE_CONFIG = {
    "macy_kuyotq": {
        "label": "Macy-Kuyotq",
        "table": "macy_kuyotq_transaction_logs",
        "seller": "Kuyotq",
    },
    "macy_wopet": {
        "label": "Macy-Wopet",
        "table": "macy_wopet_transaction_logs",
        "seller": "Wopet",
    },
    "bestbuy_delphi": {
        "label": "Bestbuy-Delphi",
        "table": "bestbuy_delphi_transaction_logs",
        "seller": "Ecooso",
    },
}


MAX_ERROR_ROWS_SAVED = 200
REQUIRED_HEADERS = {"Seller", "Order number", "Type", "Amount"}
CSV_DECODE_ENCODINGS = (
    "utf-8-sig",
    "utf-8",
    "gb18030",
    "gbk",
    "cp1252",
    "utf-16",
    "utf-16-le",
    "utf-16-be",
)


@dataclass
class ImportStats:
    store_key: str
    store_label: str
    target_table: str
    source_filename: str
    total_rows: int = 0
    empty_rows: int = 0
    valid_rows: int = 0
    inserted_rows: int = 0
    duplicate_rows_file: int = 0
    duplicate_rows_db: int = 0
    mismatch_rows: int = 0
    error_rows: int = 0
    status: str = "success"
    message: str = ""
    job_id: Optional[int] = None
    duration_ms: int = 0

    @property
    def duplicate_rows(self) -> int:
        return self.duplicate_rows_file + self.duplicate_rows_db


def get_transaction_store_options() -> Dict[str, str]:
    return {k: v["label"] for k, v in STORE_CONFIG.items()}


def _fqtn(table_name: str) -> str:
    return f"`{TARGET_DB}`.`{table_name}`"


def _normalize_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _map_header(header: str) -> str:
    key = _normalize_text(header)
    return HEADER_ALIASES.get(key, key)


def _normalize_input_row(raw_row: Dict[str, str]) -> Dict[str, str]:
    normalized = {}
    for key, value in raw_row.items():
        mapped_key = _map_header(key)
        normalized[mapped_key] = _normalize_text(value)
    return normalized


def _is_effective_empty_row(row: Dict[str, str]) -> bool:
    for col in CANONICAL_COLUMNS:
        if _normalize_text(row.get(col)):
            return False
    return True


def _row_fingerprint(row: Dict[str, str]) -> str:
    payload = "\x1f".join(_normalize_text(row.get(col)) for col in CANONICAL_COLUMNS)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256_of_file(path: str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _column_exists(cursor, table_name: str, column_name: str) -> bool:
    cursor.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
          AND column_name = %s
        LIMIT 1
        """,
        (TARGET_DB, table_name, column_name),
    )
    return cursor.fetchone() is not None


def _index_exists(cursor, table_name: str, index_name: str) -> bool:
    cursor.execute(f"SHOW INDEX FROM {_fqtn(table_name)} WHERE Key_name = %s", (index_name,))
    return cursor.fetchone() is not None


def _ensure_import_schema(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS order_system.import_job_logs (
            id BIGINT PRIMARY KEY AUTO_INCREMENT,
            store_key VARCHAR(64) NOT NULL,
            target_table VARCHAR(128) NOT NULL,
            source_filename VARCHAR(255) NOT NULL,
            file_sha256 CHAR(64) NOT NULL,
            total_rows INT NOT NULL DEFAULT 0,
            valid_rows INT NOT NULL DEFAULT 0,
            inserted_rows INT NOT NULL DEFAULT 0,
            duplicate_rows INT NOT NULL DEFAULT 0,
            mismatch_rows INT NOT NULL DEFAULT 0,
            error_rows INT NOT NULL DEFAULT 0,
            status VARCHAR(32) NOT NULL DEFAULT 'running',
            message TEXT NULL,
            created_by VARCHAR(64) NULL,
            started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at DATETIME NULL,
            duration_ms INT NOT NULL DEFAULT 0
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS order_system.import_job_error_rows (
            id BIGINT PRIMARY KEY AUTO_INCREMENT,
            job_id BIGINT NOT NULL,
            row_no INT NOT NULL,
            error_type VARCHAR(64) NOT NULL,
            error_message TEXT NULL,
            raw_json LONGTEXT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            KEY idx_job_id (job_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )

    for cfg in STORE_CONFIG.values():
        table = cfg["table"]
        if not _column_exists(cursor, table, "row_fingerprint"):
            cursor.execute(
                f"ALTER TABLE {_fqtn(table)} ADD COLUMN `row_fingerprint` CHAR(64) NULL"
            )
        _backfill_row_fingerprints(cursor, table)
        if not _index_exists(cursor, table, "uq_row_fingerprint"):
            cursor.execute(
                f"CREATE UNIQUE INDEX `uq_row_fingerprint` ON {_fqtn(table)} (`row_fingerprint`)"
            )


def _backfill_row_fingerprints(cursor, table_name: str):
    cursor.execute(
        f"SELECT COUNT(1) AS c FROM {_fqtn(table_name)} WHERE `row_fingerprint` IS NULL OR `row_fingerprint` = ''"
    )
    row = cursor.fetchone() or {}
    remain = int(row.get("c") or 0)
    if remain <= 0:
        return

    concat_parts = ", ".join([f"COALESCE(`{col}`, '')" for col in CANONICAL_COLUMNS])
    cursor.execute(
        f"""
        UPDATE {_fqtn(table_name)}
        SET `row_fingerprint` = SHA2(CONCAT_WS(CHAR(31), {concat_parts}), 256)
        WHERE `row_fingerprint` IS NULL OR `row_fingerprint` = ''
        """
    )


def _insert_job(cursor, stats: ImportStats, file_sha256: str, created_by: str) -> int:
    cursor.execute(
        """
        INSERT INTO order_system.import_job_logs (
            store_key, target_table, source_filename, file_sha256,
            status, message, created_by
        ) VALUES (%s, %s, %s, %s, 'running', %s, %s)
        """,
        (
            stats.store_key,
            stats.target_table,
            stats.source_filename,
            file_sha256,
            "running",
            created_by,
        ),
    )
    return int(cursor.lastrowid)


def _finalize_job(cursor, stats: ImportStats):
    cursor.execute(
        """
        UPDATE order_system.import_job_logs
        SET total_rows = %s,
            valid_rows = %s,
            inserted_rows = %s,
            duplicate_rows = %s,
            mismatch_rows = %s,
            error_rows = %s,
            status = %s,
            message = %s,
            duration_ms = %s,
            finished_at = NOW()
        WHERE id = %s
        """,
        (
            stats.total_rows,
            stats.valid_rows,
            stats.inserted_rows,
            stats.duplicate_rows,
            stats.mismatch_rows,
            stats.error_rows,
            stats.status,
            stats.message,
            stats.duration_ms,
            stats.job_id,
        ),
    )


def _save_error_rows(cursor, job_id: int, error_rows: List[Dict]):
    if not error_rows:
        return
    payload = []
    for item in error_rows[:MAX_ERROR_ROWS_SAVED]:
        payload.append(
            (
                job_id,
                int(item.get("row_no") or 0),
                _normalize_text(item.get("error_type")) or "invalid_row",
                _normalize_text(item.get("error_message")),
                json.dumps(item.get("raw_row", {}), ensure_ascii=False),
            )
        )
    cursor.executemany(
        """
        INSERT INTO order_system.import_job_error_rows (
            job_id, row_no, error_type, error_message, raw_json
        ) VALUES (%s, %s, %s, %s, %s)
        """,
        payload,
    )


def _build_insert_sql(table_name: str) -> str:
    db_columns = CANONICAL_COLUMNS + ["row_fingerprint"]
    col_expr = ", ".join(f"`{col}`" for col in db_columns)
    placeholders = ", ".join(["%s"] * len(db_columns))
    return f"INSERT IGNORE INTO {_fqtn(table_name)} ({col_expr}) VALUES ({placeholders})"


def _chunked(items: List[tuple], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _set_csv_field_limit():
    limit = sys.maxsize
    while limit > 131072:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit = limit // 10
    csv.field_size_limit(131072)


def _header_score(fieldnames: Optional[List[str]]) -> int:
    if not fieldnames:
        return 0
    mapped = {_map_header(h) for h in fieldnames if h is not None}
    return len(REQUIRED_HEADERS.intersection(mapped))


def _candidate_delimiters(sample_text: str) -> List[str]:
    candidates: List[str] = []
    try:
        dialect = csv.Sniffer().sniff(sample_text, delimiters=",;\t|")
        candidates.append(dialect.delimiter)
    except Exception:
        pass
    for d in [",", ";", "\t", "|"]:
        if d not in candidates:
            candidates.append(d)
    return candidates


def _build_csv_reader(file_path: str) -> Tuple[csv.DictReader, str, str]:
    _set_csv_field_limit()
    with open(file_path, "rb") as f:
        raw = f.read()

    decode_errors: List[str] = []
    best = None
    best_score = -1
    for enc in CSV_DECODE_ENCODINGS:
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError as exc:
            decode_errors.append(f"{enc}[{exc.start}-{exc.end}]")
            continue

        sample = text[:65536]
        for delimiter in _candidate_delimiters(sample):
            reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter)
            score = _header_score(reader.fieldnames)
            if score > best_score:
                best = (reader, enc, delimiter)
                best_score = score
            if score == len(REQUIRED_HEADERS):
                return reader, enc, delimiter

    if best is not None:
        return best

    raise ValueError("CSV decode failed. tried encodings: " + ", ".join(decode_errors))


def import_transaction_log_csv(
    store_key: str,
    file_path: str,
    source_filename: str,
    created_by: str = "",
) -> ImportStats:
    if store_key not in STORE_CONFIG:
        raise ValueError("invalid store_key")
    if not os.path.exists(file_path):
        raise ValueError("file not found")

    cfg = STORE_CONFIG[store_key]
    stats = ImportStats(
        store_key=store_key,
        store_label=cfg["label"],
        target_table=f"{TARGET_DB}.{cfg['table']}",
        source_filename=source_filename,
    )
    started_at = time.time()
    file_sha256 = _sha256_of_file(file_path)

    conn = DBManager.get_connection()
    error_rows: List[Dict] = []
    try:
        with conn.cursor() as cursor:
            _ensure_import_schema(cursor)
            stats.job_id = _insert_job(cursor, stats, file_sha256, created_by)
            conn.commit()

        reader, used_encoding, used_delimiter = _build_csv_reader(file_path)
        if not reader.fieldnames:
            stats.status = "failed"
            stats.message = "CSV header is empty"
            raise ValueError(stats.message)

        mapped_headers = {_map_header(h) for h in reader.fieldnames}
        missing_headers = sorted(REQUIRED_HEADERS - mapped_headers)
        if missing_headers:
            stats.status = "failed"
            stats.message = (
                f"missing required columns: {', '.join(missing_headers)} "
                f"(decoded_as={used_encoding}, delimiter={repr(used_delimiter)})"
            )
            raise ValueError(stats.message)

        rows_to_insert: List[tuple] = []
        seen_fingerprints = set()
        expected_seller = cfg["seller"].strip().lower()

        for i, raw in enumerate(reader, start=2):
            stats.total_rows += 1
            row = _normalize_input_row(raw)
            canonical = {col: _normalize_text(row.get(col)) for col in CANONICAL_COLUMNS}
            if _is_effective_empty_row(canonical):
                stats.empty_rows += 1
                continue

            seller_value = _normalize_text(canonical.get("Seller")).lower()
            if not seller_value or seller_value != expected_seller:
                stats.mismatch_rows += 1
                error_rows.append(
                    {
                        "row_no": i,
                        "error_type": "seller_mismatch",
                        "error_message": f"expected seller={cfg['seller']}, got={canonical.get('Seller', '')}",
                        "raw_row": raw,
                    }
                )
                continue

            fingerprint = _row_fingerprint(canonical)
            if fingerprint in seen_fingerprints:
                stats.duplicate_rows_file += 1
                continue
            seen_fingerprints.add(fingerprint)

            stats.valid_rows += 1
            rows_to_insert.append(
                tuple(canonical[col] for col in CANONICAL_COLUMNS) + (fingerprint,)
            )

        insert_sql = _build_insert_sql(cfg["table"])
        with conn.cursor() as cursor:
            attempted = 0
            inserted = 0
            for chunk in _chunked(rows_to_insert, 1000):
                cursor.executemany(insert_sql, chunk)
                attempted += len(chunk)
                inserted += int(cursor.rowcount or 0)
            stats.inserted_rows = inserted
            stats.duplicate_rows_db = max(0, attempted - inserted)
            stats.error_rows = len(error_rows)

            if stats.valid_rows == 0 and stats.mismatch_rows > 0:
                stats.status = "failed"
                stats.message = "all rows rejected by store mismatch check"
            elif stats.error_rows > 0:
                stats.status = "partial"
                stats.message = (
                    f"imported={stats.inserted_rows}, "
                    f"duplicates={stats.duplicate_rows}, "
                    f"mismatch={stats.mismatch_rows}, errors={stats.error_rows}"
                )
            else:
                stats.status = "success"
                stats.message = (
                    f"imported={stats.inserted_rows}, "
                    f"duplicates={stats.duplicate_rows}, mismatch={stats.mismatch_rows}"
                )

            _save_error_rows(cursor, stats.job_id, error_rows)
            stats.duration_ms = int((time.time() - started_at) * 1000)
            _finalize_job(cursor, stats)
            conn.commit()

        return stats
    except Exception as exc:
        stats.duration_ms = int((time.time() - started_at) * 1000)
        if not stats.message:
            stats.message = str(exc)
        stats.status = "failed"
        try:
            with conn.cursor() as cursor:
                if stats.job_id:
                    _save_error_rows(cursor, stats.job_id, error_rows)
                    _finalize_job(cursor, stats)
                    conn.commit()
        except Exception:
            conn.rollback()
        raise
    finally:
        conn.close()


def list_recent_import_jobs(limit: int = 50) -> List[Dict]:
    conn = DBManager.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, store_key, target_table, source_filename,
                       total_rows, valid_rows, inserted_rows, duplicate_rows,
                       mismatch_rows, error_rows, status, message,
                       created_by, started_at, finished_at, duration_ms
                FROM order_system.import_job_logs
                ORDER BY id DESC
                LIMIT %s
                """,
                (int(limit),),
            )
            return cursor.fetchall()
    finally:
        conn.close()

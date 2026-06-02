"""Google Sheets integration for Aarogya India lead + order management."""

import asyncio
import logging
import os
from datetime import datetime
from typing import Optional

logger = logging.getLogger("sheets")


def _get_client():
    """Return authenticated gspread client using service account JSON."""
    import gspread
    from google.oauth2.service_account import Credentials

    sa_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if sa_json:
        import json
        info = json.loads(sa_json)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    else:
        creds = Credentials.from_service_account_file(sa_file, scopes=scopes)

    return gspread.authorize(creds)


def _get_sheet():
    """Return the configured Google Sheet worksheet."""
    client = _get_client()
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
    sheet_name = os.getenv("GOOGLE_SHEET_NAME", "Sheet1")
    if sheet_id:
        spreadsheet = client.open_by_key(sheet_id)
    else:
        spreadsheet = client.open(os.getenv("GOOGLE_SHEET_TITLE", "Aarogya India Leads"))
    return spreadsheet.worksheet(sheet_name)


# Column positions (1-indexed) — must match your Google Sheet layout
COL = {
    "name":         1,
    "phone":        2,
    "city":         3,
    "symptom":      4,
    "status":       5,
    "language":     6,
    "product":      7,
    "variant":      8,
    "amount":       9,
    "address":      10,
    "pincode":      11,
    "landmark":     12,
    "alt_phone":    13,
    "retry_count":  14,
    "called_at":    15,
    "wa_sent":      16,
    "notes":        17,
}


async def get_new_leads() -> list[dict]:
    """Return all rows where Status = 'new'."""
    loop = asyncio.get_event_loop()
    try:
        records = await loop.run_in_executor(None, _fetch_new_leads)
        return records
    except Exception as exc:
        logger.error("sheets.get_new_leads failed: %s", exc)
        return []


def _fetch_new_leads() -> list[dict]:
    sheet = _get_sheet()
    rows = sheet.get_all_records()
    result = []
    for i, row in enumerate(rows, start=2):  # row 1 is header
        if str(row.get("Status", "")).strip().lower() == "new":
            result.append({"row": i, **{k.lower().replace(" ", "_"): v for k, v in row.items()}})
    return result


async def update_row_status(row: int, status: str) -> None:
    """Update only the Status column for a given row."""
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _write_status, row, status)
    except Exception as exc:
        logger.error("sheets.update_row_status failed: %s", exc)


def _write_status(row: int, status: str) -> None:
    sheet = _get_sheet()
    sheet.update_cell(row, COL["status"], status)


async def update_order_details(
    row: int,
    status: str,
    language: str = "",
    product: str = "3A Piles Kit",
    variant: str = "",
    amount: str = "",
    address: str = "",
    pincode: str = "",
    landmark: str = "",
    alt_phone: str = "",
    retry_count: int = 0,
    wa_sent: str = "No",
    notes: str = "",
) -> None:
    """Write full order details back to the lead's row."""
    loop = asyncio.get_event_loop()
    called_at = datetime.now().strftime("%Y-%m-%d %H:%M IST")
    try:
        await loop.run_in_executor(
            None, _write_order,
            row, status, language, product, variant, amount,
            address, pincode, landmark, alt_phone,
            retry_count, called_at, wa_sent, notes,
        )
    except Exception as exc:
        logger.error("sheets.update_order_details failed: %s", exc)


def _write_order(
    row: int, status: str, language: str, product: str, variant: str,
    amount: str, address: str, pincode: str, landmark: str, alt_phone: str,
    retry_count: int, called_at: str, wa_sent: str, notes: str,
) -> None:
    sheet = _get_sheet()
    updates = {
        COL["status"]:       status,
        COL["language"]:     language,
        COL["product"]:      product,
        COL["variant"]:      variant,
        COL["amount"]:       amount,
        COL["address"]:      address,
        COL["pincode"]:      pincode,
        COL["landmark"]:     landmark,
        COL["alt_phone"]:    alt_phone,
        COL["retry_count"]:  str(retry_count),
        COL["called_at"]:    called_at,
        COL["wa_sent"]:      wa_sent,
        COL["notes"]:        notes,
    }
    for col, value in updates.items():
        if value:
            sheet.update_cell(row, col, value)


async def increment_retry(row: int, current_count: int) -> None:
    """Bump retry_count and update called_at timestamp."""
    loop = asyncio.get_event_loop()
    called_at = datetime.now().strftime("%Y-%m-%d %H:%M IST")
    try:
        await loop.run_in_executor(None, _write_retry, row, current_count + 1, called_at)
    except Exception as exc:
        logger.error("sheets.increment_retry failed: %s", exc)


def _write_retry(row: int, count: int, called_at: str) -> None:
    sheet = _get_sheet()
    sheet.update_cell(row, COL["retry_count"], str(count))
    sheet.update_cell(row, COL["called_at"], called_at)


async def get_retry_leads() -> list[dict]:
    """Return rows where Status='no_answer' and retry_count < 3."""
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _fetch_retry_leads)
    except Exception as exc:
        logger.error("sheets.get_retry_leads failed: %s", exc)
        return []


def _fetch_retry_leads() -> list[dict]:
    sheet = _get_sheet()
    rows = sheet.get_all_records()
    result = []
    for i, row in enumerate(rows, start=2):
        status = str(row.get("Status", "")).strip().lower()
        try:
            retries = int(row.get("Retry_Count", 0) or 0)
        except ValueError:
            retries = 0
        if status == "no_answer" and retries < 3:
            result.append({"row": i, "retry_count": retries, **{k.lower(): v for k, v in row.items()}})
    return result

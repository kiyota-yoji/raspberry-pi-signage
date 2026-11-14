"""点灯スケジュールと、動画ごとの掲出期間の判定。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _parse_range(text: str) -> tuple[time, time]:
    try:
        start_s, end_s = (part.strip() for part in text.split("-"))
        start = time.fromisoformat(start_s)
        # "24:00" は終端として許可
        end = time(23, 59, 59, 999999) if end_s == "24:00" else time.fromisoformat(end_s)
    except ValueError as e:
        raise ValueError(f"時間帯の形式が不正です（HH:MM-HH:MM）: {text!r}") from e
    return start, end


def validate(schedule: dict) -> None:
    """不正な設定を起動時に検出するため、全項目をパースしてみる。"""
    for day in DAYS:
        for r in schedule.get(day, []):
            _parse_range(r)
    for d in schedule.get("off_dates", []):
        date.fromisoformat(d)


def is_on(now: datetime, schedule: dict) -> bool:
    """now の時点でディスプレイを点けて再生すべきか。"""
    off_dates = {date.fromisoformat(d) for d in schedule.get("off_dates", [])}
    today = now.date()
    yesterday = today - timedelta(days=1)
    t = now.time()

    if today not in off_dates:
        for r in schedule.get(DAYS[today.weekday()], []):
            start, end = _parse_range(r)
            if start <= end:
                if start <= t < end:
                    return True
            elif t >= start:  # 日付をまたぐ範囲の前半
                return True

    # 前日から日付をまたいで続いている範囲の後半
    if yesterday not in off_dates:
        for r in schedule.get(DAYS[yesterday.weekday()], []):
            start, end = _parse_range(r)
            if start > end and t < end:
                return True
    return False


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    # タイムゾーン付きならローカル時刻の naive に揃える
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def in_window(now: datetime, valid_from: str | None, valid_until: str | None) -> bool:
    """動画の掲出期間内か。日付のみ ("2026-12-01") の valid_until はその日の終わりまで有効。"""
    start = _parse_dt(valid_from)
    end = _parse_dt(valid_until)
    if end is not None and valid_until and len(valid_until) == 10:
        end = end + timedelta(days=1)
    if start is not None and now < start:
        return False
    if end is not None and now >= end:
        return False
    return True

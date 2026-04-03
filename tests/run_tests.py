"""
Revory AI Test Runner
Usage: python tests/run_tests.py [--tz Europe/Moscow] [--filter <id_prefix>]

Прогоняет test_cases.json через реальный API Together AI и считает accuracy.
Запускать при изменении системного промпта.
"""

import asyncio
import json
import sys
import argparse
from datetime import datetime, timedelta
from pathlib import Path

# Добавляем корень проекта в path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

# Ищем .env сначала в корне воркдерева, потом поднимаемся до основного репо
_root = Path(__file__).parent.parent
for _candidate in [_root / ".env", _root.parent.parent.parent / ".env"]:
    if _candidate.exists():
        load_dotenv(_candidate)
        break

from services.ai import parse_message

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WEEKDAYS_RU = {
    "monday": "понедельник", "tuesday": "вторник", "wednesday": "среда",
    "thursday": "четверг", "friday": "пятница", "saturday": "суббота", "sunday": "воскресенье"
}


def resolve_date(template: str, now: datetime) -> str | None:
    """Резолвит шаблон даты в YYYY-MM-DD."""
    if template is None:
        return None
    # +N — относительные дни
    if template.startswith("+"):
        days = int(template[1:])
        return (now + timedelta(days=days)).strftime("%Y-%m-%d")
    # next_<weekday>
    if template.startswith("next_"):
        day_name = template[5:]
        target = WEEKDAYS.index(day_name)
        current = now.weekday()
        days = (target - current) % 7
        if days == 0:
            days = 7
        return (now + timedelta(days=days)).strftime("%Y-%m-%d")
    # Фиксированная дата YYYY-MM-DD
    return template


def resolve_expected(expected: dict, now: datetime) -> dict:
    """Резолвит все шаблоны в expected."""
    resolved = {}
    for k, v in expected.items():
        if k == "date" and isinstance(v, str):
            resolved[k] = resolve_date(v, now)
        else:
            resolved[k] = v
    return resolved


def compare_field(actual, expected, field: str) -> bool:
    """Сравнивает поле с учётом специальных значений."""
    # Специальное значение: просто проверяем что не null
    if expected == "__not_null__":
        return actual is not None

    if expected is None and actual is None:
        return True
    if expected is None or actual is None:
        return False

    # title и list_name — substring match без учёта регистра
    if field in ("title", "list_name") and isinstance(expected, str) and isinstance(actual, str):
        e, a = expected.lower().strip(), actual.lower().strip()
        return e in a or a in e

    # items — сортированное сравнение без учёта регистра
    if field == "items" and isinstance(expected, list) and isinstance(actual, list):
        return sorted(i.lower().strip() for i in expected) == sorted(i.lower().strip() for i in actual)

    return actual == expected


async def run_tests(tz: str = "Europe/Moscow", filter_prefix: str = "") -> tuple[int, int]:
    cases_path = Path(__file__).parent / "test_cases.json"
    with open(cases_path, encoding="utf-8") as f:
        cases = json.load(f)

    if filter_prefix:
        cases = [c for c in cases if c["id"].startswith(filter_prefix)]

    now = datetime.now()
    passed = 0
    failed = 0

    print(f"\nRevory AI Test Suite — {now.strftime('%Y-%m-%d %H:%M')}")
    print(f"Timezone: {tz} | Cases: {len(cases)}\n")

    for case in cases:
        case_id = case["id"]
        user_input = case["input"]
        check_fields = case.get("check_fields", ["intent"])
        expected = resolve_expected(case["expected"], now)

        try:
            result = await parse_message(user_input, user_now=now, tz_name=tz)
        except Exception as e:
            failed += 1
            print(f"✗ [{case_id}] EXCEPTION: {e}")
            continue

        field_errors = []
        for field in check_fields:
            actual_val = result.get(field)
            expected_val = expected.get(field)
            if not compare_field(actual_val, expected_val, field):
                field_errors.append(
                    f"    {field}: expected={expected_val!r}  got={actual_val!r}"
                )

        if not field_errors:
            passed += 1
            print(f"✓ [{case_id}]  {user_input!r}")
        else:
            failed += 1
            print(f"✗ [{case_id}]  {user_input!r}")
            for err in field_errors:
                print(err)

    total = passed + failed
    pct = round(passed / total * 100) if total else 0
    print(f"\n{'─' * 55}")
    print(f"  {passed}/{total} passed  ({pct}%)")
    if failed:
        print(f"  {failed} failed")
    print()

    return passed, failed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Revory AI Test Runner")
    parser.add_argument("--tz", default="Europe/Moscow", help="User timezone")
    parser.add_argument("--filter", default="", dest="filter_prefix",
                        help="Run only cases whose id starts with this prefix")
    args = parser.parse_args()

    asyncio.run(run_tests(tz=args.tz, filter_prefix=args.filter_prefix))

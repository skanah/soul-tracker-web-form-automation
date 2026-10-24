import re
import random
import asyncio

def is_valid_phone(phone: str) -> bool:
    if not phone:
        return False
    digits = re.sub(r"\D", "", str(phone))
    return 7 <= len(digits) <= 15

def is_valid_email(email: str) -> bool:
    if not email:
        return False
    return bool(re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", str(email).strip()))

async def simulate_phone_validation_delay(min_sec: int = 10, max_sec: int = 20):
    """Mimics the server-side phone validation the form performs."""
    await asyncio.sleep(random.uniform(min_sec, max_sec))
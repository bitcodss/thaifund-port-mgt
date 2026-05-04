"""
AI portfolio analysis via Ollama.
Generates a Thai-language summary of the portfolio's current situation.
Stores result in portfolio_ai_summaries table; refreshes when NAV is stale.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.portfolio import PortfolioAiSummary

logger = logging.getLogger(__name__)

_PROMPT = """\
คุณเป็นที่ปรึกษาการลงทุนกองทุนรวมในประเทศไทย วิเคราะห์พอร์ตต่อไปนี้และเขียนสรุปภาษาไทย 3-4 ประโยค

หมายเหตุ: fund_return_pct และ return_since_entry_pct คือผลตอบแทนนับตั้งแต่วันที่เข้าซื้อ/สับเปลี่ยนเข้ากองทุนนี้ (ไม่ใช่ต้นทุนเดิม)

ข้อมูลพอร์ต:
{data}

วิเคราะห์:
- ภาพรวมผลกำไร/ขาดทุน และ XIRR
- กองทุนที่ให้ผลตอบแทนสูงสุด/ต่ำสุดนับตั้งแต่วันที่เข้าซื้อ
- ความเสี่ยงและความหลากหลาย (Sharpe, Max Drawdown ถ้ามี)
- คำแนะนำสั้นๆ 1 ประโยค

ตอบเป็นภาษาไทยเท่านั้น เขียนต่อเนื่องไม่ต้องใช้หัวข้อหรือ bullet"""


def _build_prompt(portfolio_data: dict) -> str:
    return _PROMPT.format(data=json.dumps(portfolio_data, ensure_ascii=False, indent=2))


async def generate_summary(portfolio_id: UUID, portfolio_data: dict, db: AsyncSession) -> str:
    """Call Ollama, persist result, return content string."""
    prompt = _build_prompt(portfolio_data)
    content = ""
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(
                f"{settings.OLLAMA_URL}/api/generate",
                json={"model": settings.OLLAMA_MODEL, "prompt": prompt, "stream": False},
            )
            resp.raise_for_status()
            content = resp.json().get("response", "").strip()
    except Exception as exc:
        logger.warning("Ollama call failed: %s", exc)
        content = f"ไม่สามารถสร้างการวิเคราะห์ได้ในขณะนี้ ({type(exc).__name__})"

    existing = await db.get(PortfolioAiSummary, portfolio_id)
    now = datetime.now(timezone.utc)
    if existing:
        existing.content = content
        existing.generated_at = now
    else:
        db.add(PortfolioAiSummary(portfolio_id=portfolio_id, content=content, generated_at=now))
    await db.flush()
    return content


async def get_or_generate(portfolio_id: UUID, portfolio_data: dict, db: AsyncSession) -> tuple[str, datetime]:
    """Return cached summary if available, else generate a new one."""
    existing = await db.get(PortfolioAiSummary, portfolio_id)
    if existing:
        return existing.content, existing.generated_at
    content = await generate_summary(portfolio_id, portfolio_data, db)
    return content, datetime.now(timezone.utc)

"""Load and format tenant catalog for injection into system prompt.

Called once at call start. Fetches available items from Postgres,
formats them into a human-readable block that Priya can use.
"""
import re
from dataclasses import dataclass
from typing import Optional

import asyncpg
from loguru import logger


@dataclass
class CatalogItem:
    id: int
    name: str
    description: Optional[str]
    category: str
    price_min_paise: Optional[int]
    price_max_paise: Optional[int]
    duration_mins: int
    available: bool

    def price_display(self) -> str:
        """Human-readable price string for injection into system prompt."""
        if self.price_min_paise is None:
            return "Price on request"
        if self.price_min_paise == 0 and self.price_max_paise == 0:
            return "Free"
        min_inr = self.price_min_paise // 100
        max_inr = self.price_max_paise // 100 if self.price_max_paise is not None else None
        if max_inr is not None and max_inr != min_inr:
            return f"₹{min_inr:,}–₹{max_inr:,}"
        return f"₹{min_inr:,}"

    def duration_display(self) -> str:
        if self.duration_mins < 60:
            return f"{self.duration_mins} min"
        hours = self.duration_mins // 60
        mins = self.duration_mins % 60
        return f"{hours}h {mins}min" if mins else f"{hours}h"


async def load_catalog(pool: asyncpg.Pool, tenant_id: int) -> list[CatalogItem]:
    """Fetch all available catalog items for a tenant, ordered for display."""
    rows = await pool.fetch(
        """
        SELECT id, name, description, category,
               price_min_paise, price_max_paise,
               duration_mins, available
        FROM catalog_items
        WHERE tenant_id = $1 AND available = true
        ORDER BY category, display_order, name
        """,
        tenant_id,
    )
    return [CatalogItem(**dict(r)) for r in rows]


def format_catalog_for_prompt(items: list[CatalogItem]) -> str:
    """Format catalog items into a system-prompt-ready block grouped by category."""
    if not items:
        return ""

    categories: dict[str, list[CatalogItem]] = {}
    for item in items:
        categories.setdefault(item.category, []).append(item)

    lines = ["# SERVICES & PRICING\n"]
    lines.append(
        "The following is the complete list of services offered. "
        "Always quote prices from this list exactly. "
        "Never invent prices for services not listed here. "
        "For services showing a price range, tell the caller the range "
        "and say the doctor will confirm the exact amount after examination.\n"
    )

    for category, cat_items in categories.items():
        lines.append(f"## {category}")
        for item in cat_items:
            price = item.price_display()
            duration = item.duration_display()
            line = f"- **{item.name}** — {price}, {duration}"
            if item.description:
                line += f"\n  {item.description}"
            lines.append(line)
        lines.append("")

    return "\n".join(lines)


async def build_system_prompt_with_catalog(
    base_prompt: str,
    pool: asyncpg.Pool,
    tenant_id: int,
) -> str:
    """Fetch catalog and replace any hardcoded SERVICES section in the base prompt.

    If catalog is empty, returns base_prompt unchanged — tenants without a catalog
    still work fine (falls back to whatever service info is in the base prompt).
    """
    items = await load_catalog(pool, tenant_id)
    logger.info(f"Catalog: loaded {len(items)} items for tenant {tenant_id}")
    if not items:
        return base_prompt

    catalog_block = format_catalog_for_prompt(items)

    # Strip any hardcoded SERVICES section to avoid duplicate/conflicting info.
    cleaned = re.sub(
        r'# SERVICES.*?(?=\n# |\Z)',
        '',
        base_prompt,
        flags=re.DOTALL,
    )

    return cleaned.strip() + "\n\n" + catalog_block

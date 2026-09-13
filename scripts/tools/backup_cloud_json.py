"""Export all Paprika cloud recipes (+ categories) to dated JSON backup."""
from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from lib import BACKUPS_DIR, PAPRIKA_API, paprika_credentials  # noqa: E402


def safe_name(name: str, uid: str) -> str:
    base = re.sub(r"[^\w\-]+", "_", (name or "unnamed").strip())[:80].strip("_")
    return f"{base}__{uid}.json"


async def main() -> None:
    user, password = paprika_credentials()
    stamp = datetime.now().strftime("%Y-%m-%d")
    out = BACKUPS_DIR / "paprika-json" / stamp
    recipes_dir = out / "recipes"
    recipes_dir.mkdir(parents=True, exist_ok=True)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as session:
        async with session.post(
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": password},
        ) as r:
            token = (await r.json())["result"]["token"]
        headers = {"Authorization": f"Bearer {token}"}

        async with session.get(f"{PAPRIKA_API}/v2/sync/categories/", headers=headers) as r:
            categories = (await r.json())["result"]
        (out / "categories.json").write_text(
            json.dumps(categories, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        async with session.get(f"{PAPRIKA_API}/v2/sync/recipes/", headers=headers) as r:
            index = (await r.json())["result"]
        (out / "recipes_index.json").write_text(
            json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        recipes = []
        for entry in index:
            uid = entry["uid"]
            async with session.get(
                f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=headers
            ) as r:
                recipe = (await r.json()).get("result") or {}
            recipes.append(recipe)
            path = recipes_dir / safe_name(recipe.get("name") or "recipe", uid)
            path.write_text(json.dumps(recipe, indent=2, ensure_ascii=False), encoding="utf-8")

        manifest = {
            "created": datetime.now().isoformat(timespec="seconds"),
            "source": "paprika cloud api v2",
            "recipe_count": len(recipes),
            "category_count": len(categories),
            "active_count": sum(1 for r in recipes if not r.get("in_trash")),
            "trashed_count": sum(1 for r in recipes if r.get("in_trash")),
            "field_doctor_style_count": sum(
                1
                for r in recipes
                if "Field Doctor-Style" in (r.get("name") or "") and not r.get("in_trash")
            ),
        }
        (out / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        (out / "recipes_all.json").write_text(
            json.dumps(recipes, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    print(f"Wrote {manifest['recipe_count']} recipes to {out}")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

---
name: shopping-plan
description: Build the week's Paprika shopping list from the planned Meals calendar (food process step 2). Replaces the main grocery list with every staple as a top-up reminder plus everything the planned meals and chosen batch cooks need, grouped by aisle. Use after the meal plan is done, when the user asks for the shopping list or shopping plan.
---

# Shopping plan

Step 2 of the food process (see `docs/food-planning-process.md`): it runs after the meal plan is in the Paprika Meals calendar and before the shop. UK English, Celsius.

## What goes on the list
- **Every staple** ("Staple - " pantry items) at its pantry quantity. This is a reminder to top up; the user ticks off what they already have.
- **Ingredients for the planned meals** in the date range, merged across meals and placed in Paprika's aisles.
- **Batch cooks** the user has chosen (`--batch "name"`), scaled to 8 portions once (`--portions` to change).
- **Extra makes** that aren't on the calendar, such as a granola (`--extra "name"`), added once at the recipe's own batch size.
- **Left off:**
  - meals that come from pantry stock (frozen batch meals, and lines such as "bolognese sauce" when the pantry holds bolognese);
  - Waitrose ready meals and recipes covered by a staple (they are covered by the ready-meals and fruit-salad staples);
  - lines a staple covers;
  - salt, pepper and water.

## Steps
1. Ask for the shop day and the meal range. The default is from the shop day for 7 days, ending before the next shop. Ask which TBC slots are being batch-cooked this week. Never choose batch recipes for the user.
2. Dry run, from the user's Paprika Recipe Manager repo:
   `python scripts/tools/build_shopping_list.py --from YYYY-MM-DD --to YYYY-MM-DD --batch "Fish Pie"`
3. Show the list by aisle, marked STAPLE or BUY, and the meals left off as "from stock". Point out anything odd: a cupboard item such as sugar, a choice line ("or another veg option"), or an amount that looks wrong.
4. The apply replaces the existing grocery list, so get the user's explicit yes first. Then add `--apply`. The script:
   - backs up the old items to `docs/groceries-backup.jsonl`;
   - soft-deletes them and posts the new list through the sync API;
   - calls notify and reads back.

   It refuses while the desktop app has pending grocery rows (see `docs/desktop-sync-troubleshooting.md`).
5. Report the count added and the count removed, and anything the user should check while shopping.

## Rules
- Write only through the Paprika sync API, never the local SQLite, and only the main grocery list. Don't touch the pantry; stock updates go through the paprika-pantry skill.
- Every staple goes on the list every week. The user dismisses the ones they don't need.
- If a line's aisle is wrong, fix the keyword in `scripts/tools/paprika_grocery_aisles.py`, not the list.
- After the shop, step 4 of the food process records bought ready meals and items in the pantry. The batch cooks then follow.

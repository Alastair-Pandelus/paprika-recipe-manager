# Food planning process

Working draft, set 2026-10-13; refine as we go.

**Core rule:** the planner only schedules food that is known to be in the
pantry. An item that has not been cooked (or bought) yet is never planned to be
eaten. Planned batches stay "to cook" until they are logged into the pantry.

## Steps

1. **Audit the pantry.** The user gives the full stock list. It is exhaustive:
   anything previously in the pantry and missing from the list is used up and
   is offered for deletion. Apply with
   `update_pantry_quantities.py --stock ...` (trial run first, user confirms).
2. **Plan shopping.** From the audited pantry, plan the batches to cook and the
   ready-made items to buy, and build the shopping list.
3. **Do the shopping.** Not everything planned may be available; the user says
   what was and wasn't bought.
4. **Update the pantry with immediate items.** Ready meals and other
   ready-to-eat items bought go straight into the pantry (`--add`).
5. **Cook-and-store loop.** For each batch cooked and stored, add it to the
   pantry (or raise its quantity). Only then can the planner schedule it.

Meals are planned from what step 4 and each step-5 batch have put in the
pantry, and re-planned as batches land.

## Staples (proposal)

Staples live in the pantry as items named `Staple - <item>` (e.g.
`Staple - Eggs` = 12, `Staple - Sourdough loaf` = 1), so they sort together in
the app. They replace the "♡ Staples" recipe, which is retired once they exist.

- **Always present:** the planner assumes every staple is available and never
  waits for it to be bought.
- **Always shopped:** step 2 puts every staple on the shopping list at its
  pantry quantity; the user dismisses what isn't needed that week.
- **Never audited away:** a full-stock list (step 1) leaves `Staple - ` rows
  alone; they are only changed or removed on request.

Initial list, 23 items, quantity 1 unless stated (normal shop aisles, notes
"staple"; `scripts/tools/staples_initial.args`):

- Dairy: Eggs 12, Parmesan, Lactose-free cheddar
- Bread: Sourdough bread
- Fruit and Veg: Grapefruit, Fruit salad fruit, Mixed salad leaves, Lemons 4,
  Baking potatoes 4
- Canned and Jar Goods, Pasta and Rice: Jam, Gluten-free spaghetti, Microwave
  rice pouches 3, Garlic-free pesto, Olives, Tinned sardines
- Snacks: Bars 20, Kallo corn cakes, Oatcakes (Waitrose, no palm oil), Nut and
  seed mix
- Cereals: Low FODMAP cereal
- Beverages: Coffee
- Frozen Foods: Lactose-free ice cream
- Ready Meals: Waitrose ready meals 4

No general cupboard items (maple syrup, olive oil, tamari and so on).

The pantry holds meals and the vital constituents of meals only; it is not a
full household pantry.

**Waitrose ready meals are a staple** (`Staple - Waitrose ready meals` = 4,
named generically rather than by dish). They are always on the shopping list
and the planner assumes up to 4 a week for lunches or dinners, never more.

**Staples on the shopping list (step 2, every week):**
`scripts/tools/add_staples_to_groceries.py` adds every in-stock staple to the
main grocery list (name without the prefix, pantry quantity and aisle),
skipping any already on the list unpurchased. Dry-run unless `--apply`.
`apply_weekly_favourites_to_paprika.py --groceries` adds the same staple lines
itself and drops meal lines a staple covers. The old Staples recipe and
`ensure_favourites_staples.py` are retired.

## Pantry rules

- Write through the sync API only (`scripts/tools/update_pantry_quantities.py`);
  never copy changes into the local SQLite.
- Before writing, the local SQLite must have no pending pantry rows (the
  script checks). See `desktop-sync-troubleshooting.md`.
- Item names match the Favourites recipe title exactly; aisle Frozen Foods;
  quantity in portions.
- Show the trial run (SAME / UPDATE / ADD / DELETE) and wait for the user's yes
  before `--apply`; read the pantry back afterwards.

## Meal frequency rules

- Milk to use up: when the pantry holds a milk (usually left over from a batch
  cook), add "Coffee with milk" to every breakfast until it is recorded as
  used, in the meal plan and the Todoist breakfast task (for example
  "🍳 ♡ Cereal and Grapefruit ☕ Coffee with milk"). It is a reminder, not a
  separate meal slot. (User, 14 Oct 2026.)
- Milk is unsweetened almond milk, written in recipes and lists as
  "unsweetened almond milk (or soy-protein soy milk, or lactose-free
  semi-skimmed milk)" and scored as almond (user, 21 Oct 2026). The bracketed
  alternatives are all low FODMAP, ordered by saturated fat (almond about 0.1 g,
  soy-protein soy about 0.3 g, lactose-free semi-skimmed about 1.1 g per
  100 ml). Not whole-bean soy milk (ingredients "soya beans, water", high in
  GOS), oat milk above about 100 ml, or ordinary cow's milk.

- Snack: daily at 17:00 for 15 minutes, from Favourites › Snacks, varied as
  much as possible. Icon ☕ (next to 🍳 breakfast, 🥗 lunch, 🍽️ dinner,
  🍰 pudding). (User, 14 Oct 2026.)

- French Toasty (✅ ❣, cheddar and prosciutto): at most once a week. It is a
  bread meal, so only on a day with a non-bread breakfast, such as cereal.
  (User, 14 Oct 2026.)
- Sourdough French Bread Pizza (ℹ️ ❣): at most twice a week. Plan it as a pair
  on consecutive days, because each pizza uses half a loaf of sourdough French
  bread, so a bought loaf covers exactly two. This is an exception to "no
  repeat of the same recipe in the same slot on consecutive days". It is a bread
  meal, so only on days with a non-bread breakfast. The loaf is not a staple:
  add one sourdough French loaf to the shopping list in weeks the pair is
  planned. (User, 14 Oct 2026.)

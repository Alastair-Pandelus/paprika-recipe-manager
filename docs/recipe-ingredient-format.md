# Ingredient line format (editing or importing recipes)

Paprika reads the **start** of each ingredient line as the amount. Anything
before the quantity stops it being recognised, so the line can't be scaled,
added to the grocery list with an amount, or saved cleanly.

**Rule:** every ingredient line starts with the quantity and unit, then the
ingredient, with qualifiers such as "optional" at the end in brackets.

| Don't | Do |
|---|---|
| `Optional: 50 g dark chocolate chips` | `50 g dark chocolate chips (optional)` |
| `To serve: 2 tbsp yoghurt` | `2 tbsp yoghurt (to serve)` |
| `Pinch of salt` | `1 pinch salt` |
| `~300g noodles` / `About 100 ml milk` / `Approx. 2 tbsp oil` | `300 g noodles (about)` / `100 ml milk (about)` |
| `Up to 2 tbsp maple syrup` / `Maple syrup, up to 2 tbsp` | `2 tbsp maple syrup (up to)` |
| `Juice of 1/2 lemon` / `Zest of 1 lime` | `1/2 lemon (juice only)` / `1 lime (zest only)` |
| `Four 6-oz salmon fillets` / `One 15-ounce can coconut milk` | `24 oz salmon fillets (4 x 6 oz)` / `15 oz coconut milk (1 can)` |
| `- 1 stick cinnamon` (a bullet) | `1 stick cinnamon` |
| `A good glug of olive oil` | `1 tsp olive oil` |

**This is a hard rule:** nothing goes before the amount, not a symbol (`~`, `-`),
a word (`about`, `approx`, `up to`, `juice of`, number words) or a label.
Method sentences never go in the ingredient list. Lines with no amount at all
are fine only for "to taste" items, garnishes and equipment. Check every
import and every scripted rewrite for this before writing to Paprika
(`scripts/tools/fix_amount_first_lines.py` finds and fixes the common
patterns). (User, 20 Oct 2026.)

**Rule 2: one amount per line.** Paprika scales only the leading amount; a
second amount in the text (`1 3/4 cups (about 230 g) flour`) stays as written,
so after scaling the two disagree. Keep the leading amount and unit as they
are (no conversion or rescaling) and remove the second, bracketed amount:

| Don't | Do |
|---|---|
| `1 3/4 cups (about 230 g) gluten-free 1:1 flour blend` | `1 3/4 cups gluten-free 1:1 flour blend` |
| `1/8 cup (120g) whole-egg mayonnaise` | `1/8 cup whole-egg mayonnaise` |
| `1/6 medium eggplant (340 g, diced)` | `1/6 medium eggplant (diced)` |

**Exception, pack sizes:** when the bracket gives a pack, tin, jar or whole
item's actual weight or volume, that becomes the amount and the count goes,
multiplied out when the count isn't 1:

| Don't | Do |
|---|---|
| `1/4 (12 oz) package extra firm tofu` | `3 oz extra firm tofu` |
| `1/4 can (400g) lentils` | `100 g lentils` |
| `1 pack Waitrose Chicken Pad Thai (400 g)` | `400 g Waitrose Chicken Pad Thai` |
| `1 boneless pork loin (about 3.5 lb)` | `3.5 lb boneless pork loin` |

Counts with a size note are fine (`2 large eggs`). Non-amount notes in
brackets stay (`(optional)`, `(not pumpkin pie filling)`).

**Rule 3: no duplicate ingredients.** A recipe lists each ingredient once.
Metric and imperial copies, US and UK names (zucchini/courgette,
cilantro/coriander, eggplant/aubergine) and "Salt" next to "Salt, to taste" are
the same line: keep one, in the recipe's leading unit, with its tags. The same
ingredient used for a different job (cake and icing, oil for frying and for
dressing, extra to serve) is not a duplicate; say what each is for, or put
them under separate section headers.

**Rule 4: record deliberate changes in the notes.** Any intentional change
to a recipe versus its website (an ingredient omitted, swapped or reduced,
for example for low FODMAP) gets a line in the recipe's notes starting
"Low FODMAP:" or "Changed:", saying what changed from what. A later
reconciliation with the website must treat those lines as deliberate and
not undo them.

**Rule 5: the "Low Fodmap" folder holds only low FODMAP recipes.** Anything
in the Low Fodmap folder tree must be low FODMAP; a recipe that scores high
there gets adapted (and noted, rule 4) or moved out. Recipes in other folders
("Regular (with Fodmaps)", "Alison's", none) may or may not be low FODMAP and
are not assumed to be: low FODMAP meal planning uses only the Low Fodmap
tree, and FODMAP clean-ups, garlic checks and ❌ follow-ups cover only that
tree. Other recipes are still scored, for information.

"Regular (with Fodmaps)" holds the user's own pre-diet recipes, which they may
cook for guests. Website recipes that turn out not to be low FODMAP don't go
there: the user decides whether each is adapted (and noted) or moved to
Paprika's Trash (restorable; backed up to docs/trashed-recipes-backup.jsonl).

**Rule 6: milk is always the house low FODMAP milk.** In the Low Fodmap tree,
any drinking-type milk (cow's, lactose-free, almond, soy, oat, rice, "milk of
your choice") is written as
`unsweetened almond milk (or soy-protein soy milk, or lactose-free semi-skimmed milk)`,
keeping the line's amount and any cooking note ("warmed", "or water", "for
brushing"). Almond milk is the one used and the one scored; the alternatives,
all low FODMAP, sit in brackets ordered by saturated fat (about 0.1, 0.3 and
1.1 g per 100 ml). (User, 21 Oct 2026; replaces "low FODMAP milk (…)".) Not whole-bean soy milk
("soya beans, water", high in GOS), oat milk above about 100 ml, or ordinary
cow's milk. Coconut milk, condensed or evaporated milk, buttermilk, cream,
cheese and yoghurt are different ingredients and are not changed. Imports
follow this rule too, with a "Changed:" note (rule 4). (User, 14 Oct 2026.)

**Rule 7: name the favoured option first; alternatives go in brackets.** When a
line offers a choice ("sour cream or Greek yoghurt", "butter or olive oil"),
the ingredient actually used comes first and is the one scored; the others go
in brackets after it: `2 tbsp lactose-free sour cream (or Greek yoghurt)`. The
scorers read only the text outside brackets, so they never have to guess.
Where the options differ in saturated fat, the leaner one is the favoured one
(unless it is higher in FODMAPs), e.g. olive oil before butter, turkey mince
before beef, Greek yoghurt before sour cream (scripts/tools/favour_lower_fat_option.py).
(User, 21 Oct 2026.)

Applies to hand edits, import scripts and anything that rewrites ingredient
lines. FODMAP and saturated-fat tags go after the ingredient text, as now.
Section headers (e.g. `For the sauce:`) are not ingredients and stay as they
are.

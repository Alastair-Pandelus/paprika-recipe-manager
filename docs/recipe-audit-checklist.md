# Recipe audit checklist (version 5)

Mirror of the project checklist (meal-planning/recipe-audit-checks.md). Each
recipe is audited once against every check, by
`scripts/tools/audit_recipes.py`; results go to `docs/audit-reports/` and
`docs/recipe-audit-log.tsv`. Formatting rules are in
`docs/recipe-ingredient-format.md`.

## Scope
- Recipes in the **Low Fodmap** folder tree must be low FODMAP (rule 5). Other
  folders ("Regular (with Fodmaps)": the user's pre-diet recipes for guests;
  "Alison's"; none) are scored for information only.

## Checks
1. **Source and portions.** Servings match the website (JSON-LD yield, or the
   site's "1/N of the recipe" / "serves N"). Every ingredient's per-serving
   amount matches the website's, within a third, unless a notes line records a
   deliberate change. Catches wrong servings, unscaled "(optional)" lines,
   mis-scaled lines and size words changed by the old rescale ("1 large
   (260 g)" → "½ medium"). Yield ranges use the lower number.
2. **Website low FODMAP notes and variants.** "Can be made low FODMAP",
   "to make it low FODMAP", "for low FODMAP use/omit", footnotes such as
   "low FODMAP up to 45 g per serving": the variant is transcribed and recorded
   as "Low FODMAP: per the author's notes, …".
3. **Website disclaimers.** "Not suitable for the elimination phase", "not low
   FODMAP", "retested in 2022", "if you tolerate …": copied into the notes; the
   user chooses trash, adapt (with a note) or keep with the warning.
4. **Format (rules 1–2).** Amount first, qualifiers in brackets after; one
   amount per line (leading unit kept, bracketed second amount removed; pack /
   tin / whole-item weights become the amount); typos ("2oo g", unbalanced
   brackets).
5. **Duplicates (rule 3).** No ingredient listed twice. Copies removed; the
   same ingredient for different jobs labelled "(for the …)" from the website's
   sections. Not duplicates: the Waitrose pairs, "Waitrose X" (the bought
   ready meal) and "Low FODMAP X" (the from-scratch version), which both cite
   the Waitrose product page. That page is never the reference for the
   scratch copy's portions.
6. **Deliberate changes (rule 4).** Every difference from the website is in the
   notes ("Low FODMAP:" / "Changed:"), or it is an error to fix.
7. **Green parts.** Leek and spring onion lines say "green tops only" where the
   method or website uses only the green part (different FODMAP limits).
8. **Garlic and onion.** Cloves eaten in the dish are flagged; garlic only used
   to infuse oil and removed is fine. The user's olive oil substitutions stay.
9. **Unread amounts.** Lines the scorer cannot read ("187 1/2 gr", "¼ can",
   "1 small roma tomato", "1/2 ½", "1/4th", "1 slices") are flagged when the
   ingredient has a FODMAP limit, never silently skipped.
10. **Prose in the ingredient list.** Sentences ("In the original recipe…",
    "If you do not eat low FODMAP…") move to the notes.
11. **Lost qualifiers.** "optional, for serving", "for garnish" restored.
12. **Rescore.** FODMAP and saturated fat with the current scorer: per-serving
    tags "(N g per serving)", colours 🟢 ≤50% · 🟡 ≤75% · 🟠 ≤100% · 🔴 over
    the limit; title marker updated.
    Saturated fat uses two scales. The title marker scores the whole serve:
    ♡ up to 5 g · ❣ up to 10 g · 💔 over 10 g, out of a 20 g day. An
    ingredient line's tag scores that line alone, on a stricter scale:
    💚 up to 2 g · ❣ up to 4 g · 💔 over 4 g. So a 💔 line inside a ♡ recipe is
    normal: the line tag shows where the fat comes from, and the title says
    whether the serve is fine. One example is 20 g cheddar (4.2 g, 💔) in ♡
    Oatcakes with Cheese. Plan from the title marker; use line tags to choose
    what to trim.
    Dairy is scored by its fat level, never as one "cream" or "cheese": double
    (33 g/100 g), whipping (24), single or lactose-free cream (12), soured cream
    (11.6), light soured cream (5.5), crème fraîche (19), half-fat crème fraîche
    (9.6), plant-based oat or soy cream (1.5), cream cheese (20), light cream
    cheese (7.4), full-fat hard cheese (21), reduced-fat cheese (12), 0% or
    low-fat yoghurt (0.3). Keep the product's fat word in the line ("light",
    "half-fat", "0% fat", "reduced-fat") so it scores right. Where a line offers
    a choice ("sour cream or coconut cream"), the first option is scored. If a
    new dairy product scores as the generic entry, add it to
    data/cholesterol_seed.json and rescore (the user, 21 Oct 2026).
13. **Recipe links.** A `[recipe:Name]` link (for example the Bay's Kitchen
    recipes linking their home-made sauce) must match the linked recipe's exact
    current name, markers included. When a rescore changes a linked recipe's
    title markers, update every link to it, in the ingredients, directions and
    notes alike (the combined Bay's Kitchen sauce recipes link their single
    sauces from the method and notes; rerunning
    scripts/tools/create_bays_kitchen_combos.py refreshes those). Linked lines are not scored in the
    linking recipe; the linked recipe carries its own score.

## Process
Dry run → report per recipe (issues, proposed fixes, items for the user's
call) → the user approves rows → apply from the approved TSV (backup, sync
API, notify, read back) → log the recipe at this checklist version. Recipes
already logged at the current version are skipped.

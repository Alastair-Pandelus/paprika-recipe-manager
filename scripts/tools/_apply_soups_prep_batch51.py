"""Apply Prep (bowls) restyle to soups queue indices 51–end."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

from lib import PAPRIKA_API, paprika_credentials  # noqa: E402
from normalize_ingredient_units import RateLimiter, api_json, safe_print  # noqa: E402

LOCAL_DB = Path(
    r"C:\Users\Pandelus\AppData\Local\Paprika Recipe Manager 3\Database\Paprika.sqlite"
)
QUEUE = Path(__file__).resolve().parent / ".soups_prep_queue.json"

# uid -> (ingredients, directions)
REWRITES: dict[str, tuple[str, str]] = {}


def _reg(uid: str, ingredients: str, directions: str) -> None:
    REWRITES[uid] = (ingredients.strip() + "\n", directions.strip() + "\n")


# --- 51 ---
_reg(
    "47308319-1209-410F-9493-9B68B06D4690",
    """100 g pumpkin, in cubes*
1 medium roma tomatoes*
1/4 tsp ground paprika
1/4 black pepper
1/4 salt
1/2 tbsp tomato paste  🟡 Fructose 29%
0.25 l low FODMAP stock (I use FODY’s vegetable soup base)
Lactose-free cream or a plant-based cream for serving
Fresh basil for serving""",
    """**Prep (bowls):**

**Large bowl — veg (fry / soften):**
- Peel the tomatoes if you prefer (cross the base, soak briefly in hot water, then slip the skins). Cube the tomatoes and pumpkin.

**Small bowl — spice mix:**
- Mix the ground paprika, black pepper and salt.
- Have the tomato paste ready beside this bowl.

**By the hob (no bowl):** low FODMAP stock, oil for frying.

**Medium bowl — finish:**
- Set out the lactose-free or plant-based cream and fresh basil.

**1. Soften veg:** Heat oil in a large pan. Add the veg bowl and tomato paste; fry about 2 minutes.

**2. Simmer:** Add the stock and spice mix. Bring to a boil, then simmer 20–25 minutes until the pumpkin is completely soft.

**3. Blend:** Puree with an immersion blender. Taste and adjust salt or pepper.

**4. Finish:** Serve with cream and basil from the finish bowl.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.""",
)

# --- 52 ---
_reg(
    "56C43E8E-9443-4E49-81D4-DB2267415E31",
    """1/4 kabocha or Japanese pumpkin*
1/4 tsp smoked paprika powder
1/8 tsp cayenne pepper
1/8 tsp salt
1/2 stock cubes (check the ingredient list to make sure they are low FODMAP)
0.25 l water
Optional: some coconut milk for serving
Optional: some fresh parsley for serving""",
    """**Prep (bowls):**

**Large bowl — pumpkin (roast):**
- Wash the pumpkin, cut in half, and scoop out the seeds and threads.

**Small bowl — seasoning:**
- Mix the smoked paprika, cayenne and salt.

**By the hob (no bowl):** water, stock cubes, olive oil for brushing.

**Medium bowl — finish:**
- Set out optional coconut milk and fresh parsley.

**1. Roast pumpkin:** Heat the oven to 200°C. Brush the cut faces with olive oil; place cut-side down on a lined tray. Bake about 30 minutes until soft when pierced with a fork. Cool slightly, then scoop out the flesh.

**2. Simmer:** In a large pan, dissolve the stock cubes in the water. Add the roasted pumpkin flesh and simmer about 10 minutes.

**3. Blend:** Puree until smooth with a hand blender. Season with the seasoning bowl; taste and adjust salt.

**4. Finish:** Serve with a dash of coconut milk and parsley from the finish bowl if using.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.""",
)

# --- 53 ---
_reg(
    "B8387030-D759-451D-9C51-71B3A0077192",
    """181 g beef bones
1/3 sprigs fresh rosemary
1/3 quarts water
Salt and pepper""",
    """**Prep (bowls):**

**Large bowl — bones:**
- Have the beef bones ready.

**Small bowl — aromatics:**
- Have the rosemary ready.

**By the hob (no bowl):** water, salt, pepper.

**1. Slow cook:** Place the bones and rosemary in a slow cooker. Cover with water. Cook on low 8–12 hours.

**2. Strain:** Remove bones and large rosemary stems. Strain the broth (line with cheesecloth for clearer stock). Season with salt and pepper.

Cool, portion, refrigerate up to 3–4 days or freeze.""",
)

# --- 54 ---
_reg(
    "1D4909C4-8FF0-4EF3-94E5-24BAF52BD4DF",
    """113 g . chicken breast, sliced
1/4 stalk fresh lemongrass
3/4 kaffir lime leaves
5/8 smashed bird’s eye (or serrano) chilies
1 slices galangal
1/2 cup chopped bok choy
2 cup low FODMAP chicken broth
1/2 tbsp . fish sauce
1/4 lime, juice of
1/8 tsp . sugar, optional
Coriander, chopped
Chives, chopped
Red pepper flakes
Cooked rice
Cilantro, chopped""",
    """**Prep (bowls):**

**Medium bowl — chicken:**
- Slice the chicken breast into even pieces.

**Small bowl — aromatics (simmer, then remove):**
- Prepare the lemongrass, kaffir lime leaves, smashed chillies and galangal.

**Medium bowl — greens:**
- Chop the bok choy.

**By the hob (no bowl):** low FODMAP chicken broth, fish sauce, lime juice, optional sugar.

**Medium bowl — finish:**
- Chop coriander (cilantro) and chives; set out red pepper flakes and cooked rice.

**1. Simmer aromatics:** Heat the broth with the aromatics bowl and fish sauce over medium-high heat.

**2. Cook chicken:** When hot, add the chicken and greens bowls. Bring to a boil, reduce heat and cook 8–10 minutes until the chicken is done.

**3. Finish:** Stir in the lime juice and optional sugar. Remove and discard the lemongrass, lime leaves, chillies and galangal. Serve warm with rice; top with the finish bowl.

Cool, portion, refrigerate up to 3 days; reheat until piping hot.""",
)

# --- 55 ---
_reg(
    "7ECF3B1B-82D7-44CD-97AD-85B9788408CD",
    """9.8 g dark green leek tops, sliced  🟡 Fructans 35%
1 1/4 carrots, peeled and diced
3.8 g fresh basil leaves
1/8 tsp dried oregano
Freshly ground black pepper
Freshly cracked black pepper
12 ml [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)]
3/4 tsp garlic-infused olive oil
1/8 × 400 g cans whole peeled tomatoes, including juice
0.06 l water
1/8 tsp balsamic vinegar
1½ tsp salt, plus more to taste
1/8 tsp kosher salt + more to taste
15 ml full-fat coconut milk  🟢 Sorbitol 25%""",
    """**Prep (bowls):**

**Large bowl — veg (fry / soften):**
- Slice the dark green leek tops. Peel and dice the carrots.

**Medium bowl — finish:**
- Have the fresh basil ready. Set out the dried oregano and black pepper.
- Have the coconut milk ready.

**By the hob (no bowl):** garlic-infused olive oil, whole peeled tomatoes with juice, water, balsamic vinegar, salt.

**1. Soften veg:** In a large lidded soup pot, heat garlic-infused oil over medium heat. Soften the veg bowl 5–6 minutes.

**2. Simmer:** Add the tomatoes with juice, water, balsamic vinegar and salt. Break the tomatoes into large pieces. Bring to a simmer, cover, and cook 25–30 minutes until the carrots are very soft.

**3. Finish & blend:** Off the heat, stir in the coconut milk, basil and oregano from the finish bowl. Blend until smooth (immersion blender, or jug blender in batches — do not overfill; hold the lid with a towel). Return to the pot and simmer about 5 minutes. Taste salt and pepper; loosen with a splash of water if needed.

Cool, portion, refrigerate up to 4 days or freeze; reheat until piping hot.""",
)

# --- 56 ---
_reg(
    "FF615E45-9F26-478C-8D2F-FA459D7C1D5A",
    """1/8 can whole tomatoes with liquid
3/4 tsp . tomato paste  🟢 Fructose 15%
3/4 tsp . [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)]
3/4 tsp . garlic-infused olive oil
1/4 cup low FODMAP chicken broth
3/4 tsp . balsamic vinegar
Salt and pepper, to taste""",
    """**Prep (bowls):**

**Medium bowl — tomatoes (blend):**
- Blend the whole tomatoes with their liquid until smooth; keep ready.

**Small bowl — paste:**
- Have the tomato paste ready.

**By the hob (no bowl):** garlic-infused olive oil, low FODMAP chicken broth, balsamic vinegar, salt, pepper.

**1. Fry paste:** Heat oil in a saucepan over medium-high heat. Add the tomato paste and sauté 2–3 minutes.

**2. Simmer:** Whisk in the blended tomatoes and chicken broth. Bring to a boil, then reduce and simmer 10 minutes.

**3. Finish:** Stir in the balsamic vinegar; season with salt and pepper. Serve warm.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.""",
)

# --- 57 ---
_reg(
    "734011E1-E52D-44F9-A11C-DD67B8F535FB",
    """3/4 vine tomatoes
16 g carrot
1/4 stalks green onion, green tops only
38 g ground beef or mixed beef/pork
3/4 tsp gluten-free breadcrumbs
1/8 tsp paprika
1/8 tsp ground cumin
1/8 tsp black pepper
1/8 tsp salt
1/8 tsp dried oregano
1/8 tsp dried basil
1/8 tsp black pepper
1/8 tsp salt
8.8 g tomato paste  🟡 Fructose 31%
0.12 l low FODMAP broth
Fresh basil, for serving""",
    """**Prep (bowls):**

**Large bowl — veg (fry / soften):**
- Cross the tomatoes, soak briefly in hot water, peel, then chop. Chop the carrot and the green onion tops.

**Medium bowl — meatballs:**
- Mix the ground meat, breadcrumbs, paprika, cumin, black pepper and salt; form small meatballs.

**Small bowl — soup spices:**
- Mix the dried oregano, dried basil, black pepper and salt.
- Have the tomato paste ready beside this bowl.

**By the hob (no bowl):** oil for frying, low FODMAP broth.

**Medium bowl — finish:**
- Set out fresh basil (and optional lactose-free cream if using).

**1. Soften veg:** Heat oil in a large pot. Soften the veg bowl a few minutes.

**2. Simmer soup:** Add the tomato paste, broth and soup spices. Bring to a boil, then simmer covered 20 minutes.

**3. Blend & cook meatballs:** Puree the soup smooth with an immersion blender. Add the meatballs and simmer about 15 minutes until cooked through.

**4. Finish:** Serve with fresh basil from the finish bowl.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.""",
)

# --- 58 ---
_reg(
    "D8D9B135-0E5D-4948-B2A5-5DE04348E674",
    """167 g Kent pumpkin, peeled and cut into 1 cm pieces
105 g carrots, peeled and cut into 1 cm pieces
Kent pumpkin, peeled and cut into 1cm pieces
Carrots, peeled and cut into 1cm pieces
1/2 tsp mustard seeds
1/3 tsp ground coriander
1/6 tsp ground cumin
1/6 tsp ground turmeric
1/8 tsp ground cardamom
1/8 tsp chilli powder (optional)
tbsp mustard seeds
1 tsp olive oil
1 tsp [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)]
tbsp olive oil
tbsp garlic infused olive oil
0.17 l Low FODMAP stock (chicken or vegetable)
83 ml water""",
    """**Prep (bowls):**

**Large bowl — roast veg:**
- Peel and cut the pumpkin and carrots into even pieces.

**Small bowl — spice mix (fry / bloom):**
- Mix the mustard seeds, ground coriander, cumin, turmeric, cardamom and optional chilli powder.

**By the hob (no bowl):** olive oil, garlic-infused olive oil, low FODMAP stock, water.

**1. Roast:** Heat the oven to 210°C. Toss the roast veg with some of the garlic-infused oil and olive oil on a lined tray. Roast 30–35 minutes.

**2. Bloom spices:** Meanwhile, heat the remaining oils in a pot over medium heat. Add the spice mix; cook, stirring, a couple of minutes until aromatic and the mustard seeds pop.

**3. Simmer:** Add the roasted veg, stock and water. Cover, bring to a boil, then cook on low covered 15 minutes. Uncover and cool slightly about 15 minutes.

**4. Blend:** Blitz with a stick mixer until smooth. Serve with natural yoghurt (lactose-free if needed) and low FODMAP bread if liked.

Cool, portion, refrigerate or freeze; reheat until piping hot.""",
)

# --- 59 ---
_reg(
    "F935A593-D14B-4BD9-92E4-4F3FFD85701F",
    """113 g boneless, skinless chicken breasts
1/4 cup uncooked wild rice, rinsed and drained  🟡 Fructans 32%
1/4 cup peeled and diced carrots
1/4 tsp dried thyme // note 1
1/4 tsp dried rosemary
1/8 tsp celery seed  🟢 Mannitol 6%
1/2 bay leaves
3/4 tsp [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)]
3/4 tsp garlic-infused olive oil
1 1/2 cup low FODMAP chicken broth or low FODMAP vegetable broth // note 2
1/2 tbsp cornstarch mixed into ¼ cup cold water (optional)
1/8 cup lactose-free milk or unsweetened almond milk
1/2 tsp fresh lemon juice
Kosher salt and freshly cracked black pepper
Chopped fresh parsley, optional garnish""",
    """**Prep (bowls):**

**Large bowl — chicken:**
- Have the chicken breasts ready.

**Large bowl — grains (slow cook):**
- Rinse and drain the wild rice.

**Medium bowl — veg:**
- Peel and dice the carrots.

**Small bowl — herb mix (optional bloom):**
- Mix the dried thyme, rosemary and celery seed.

**By the hob (no bowl):** garlic-infused olive oil, low FODMAP broth, bay leaves, optional cornstarch slurry, milk, lemon juice, salt, pepper.

**Medium bowl — finish:**
- Chop fresh parsley if using.

**1. Optional bloom:** Warm garlic-infused oil in a small skillet; sauté the herb mix 1–2 minutes until fragrant (or skip and add herbs with the oil later).

**2. Slow cook:** Place the chicken, grains, veg, bay leaves and broth in the slow cooker. Add the bloomed herbs (or oil plus herb mix). Cover; cook on low about 6 hours until the chicken is cooked and the rice is tender.

**3. Shred:** Discard the bay leaves. Shred or dice the chicken; set aside briefly.

**4. Finish:** Stir in milk (and optional slurry if thickening). Return the chicken; cook until warmed through (about 15 minutes if using slurry). Stir in lemon juice; adjust salt and pepper. Serve with parsley from the finish bowl.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.""",
)

# --- 60 ---
_reg(
    "2E438472-D349-46C9-9DDD-41BC29D60D0D",
    """181 g bones — chicken carcasses and wings, beef marrow and knuckle bones, or a mix of the two
1/5 medium carrots, roughly chopped
1/8 stalk celery, roughly chopped
Green tops of 1 large leek, rinsed and roughly chopped
Green tops of 4 to 5 spring onions, roughly chopped
Green tops of 1 large leek, rinsed and roughly chopped (about 1 cup)
Green tops of 4 to 5 scallions, roughly chopped
3/5 sprigs fresh parsley
2/5 sprigs fresh thyme
1/5 bay leaves
1/8 tsp whole black peppercorns
1 2/3 cup cold filtered water, enough to cover the bones by 1 to 2 inches
3/5 tsp apple cider vinegar  🟢 Fructose 15%
Salt, to taste
Salt, to taste (added after straining)""",
    """**Prep (bowls):**

**Large bowl — bones:**
- Have the bones ready (for beef, optional: roast first).

**Large bowl — veg aromatics:**
- Roughly chop the carrots, celery, leek greens and spring onion greens.

**Small bowl — herbs & peppercorns:**
- Have the parsley, thyme, bay leaves and peppercorns ready.

**By the hob (no bowl):** cold filtered water, apple cider vinegar, salt (after straining).

**1. Optional roast (beef):** Heat the oven to 220°C. Roast beef bones on a rimmed sheet 30–40 minutes until deeply browned. Scrape bones and browned bits into a stockpot or slow cooker.

**2. Build the pot:** Add the veg aromatics and herbs & peppercorns. Cover with cold water by 1–2 inches. Stir in the vinegar; sit off heat 20–30 minutes. Do not salt yet.

**3. Simmer:** Bring to a gentle boil, then hold at a bare simmer 12–24 hours (12–16 for chicken; full 24 for beef), skimming foam early. Top up with hot water if needed. Cover most of the cook; uncover the last hour for a more concentrated broth if liked.

**4. Strain & cool:** Rest 15 minutes. Strain (cheesecloth for clearer broth). Salt to taste. Cool quickly; refrigerate within 2 hours. Next day, skim the fat cap if wanted, or stir it back in.

Cool, portion, refrigerate a few days or freeze.""",
)

# --- 61 ---
_reg(
    "3A1D3D15-3448-4867-9E3B-D483BFB7AC44",
    """57 g butternut squash, peeled, seeded, and cut into 1-inch cubes
1/5 medium carrots, peeled and diced
Green tops of 4 spring onions, thinly sliced
Green tops of 4 scallions, thinly sliced (about 1/2 cup)
1/3 tsp fresh ginger, peeled and grated
1/8 ground cinnamon
1/8 ground nutmeg
1/8 dried thyme, or 2 sprigs fresh
1/8 salt, plus more to taste
1/8 black pepper
3/5 tsp [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)]
3/5 tsp garlic-infused olive oil
2/5 cup low-FODMAP chicken broth or low-FODMAP vegetable broth
1/8 light canned coconut milk, or 1 heavy cream
1/8 tsp fresh lemon juice
Fresh sage leaves, fried in plain oil, for serving
Fresh sage leaves, fried in plain oil, for serving (optional)""",
    """**Prep (bowls):**

**Large bowl — veg (fry / soften):**
- Cube the butternut squash. Dice the carrots. Thinly slice the spring onion greens. Grate the ginger.

**Small bowl — spice mix:**
- Mix the cinnamon, nutmeg, dried thyme (or have fresh sprigs ready), salt and pepper.

**By the hob (no bowl):** garlic-infused olive oil, low-FODMAP broth, coconut milk or cream, lemon juice.

**Medium bowl — finish:**
- Set out fried sage leaves if using (and extra oil to drizzle).

**1. Soften aromatics:** Warm garlic-infused oil in a heavy pot over medium heat. Soften the carrot and spring onion greens from the veg bowl 4–5 minutes. Stir in the ginger 30 seconds until fragrant.

**2. Simmer:** Add the squash; coat in oil. Pour in the broth and add the spice mix. Simmer partially covered 25–30 minutes until the squash falls apart.

**3. Blend & finish:** Blend until smooth. Stir in coconut milk or cream and lemon juice; warm 2 minutes on low (do not boil if using dairy cream). Taste salt. Serve with a drizzle of oil and fried sage from the finish bowl.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.""",
)

# --- 62 ---
_reg(
    "D7F86957-9150-49BC-9FA5-91AA974CE487",
    """900 g carrots, peeled and sliced into 1/2-inch coins
Green tops of 4 spring onions, thinly sliced
Green tops of 4 scallions, thinly sliced (about 1/2 cup)
1 tsp fresh ginger, peeled and grated
1/8 tsp ground turmeric
1/8 tsp ground cumin
1/8 tsp salt, plus more to taste
1/8 tsp black pepper
1 tsp [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)]
1 tsp garlic-infused olive oil
2/3 cup low-FODMAP chicken broth or low-FODMAP vegetable broth
1/8 cup canned light coconut milk  🟡 Sorbitol 50%
1/3 tsp fresh lemon juice
Extra garlic-infused oil and toasted pepitas, for serving (optional)
Extra garlic-infused oil and toasted pepitas (up to 1 tbsp per bowl), for serving (optional)""",
    """**Prep (bowls):**

**Large bowl — veg (fry / soften):**
- Peel and slice the carrots into coins. Thinly slice the spring onion greens. Grate the ginger.

**Small bowl — spice mix (bloom):**
- Mix the turmeric, cumin, salt and pepper.

**By the hob (no bowl):** garlic-infused olive oil, low-FODMAP broth, coconut milk, lemon juice.

**Medium bowl — finish:**
- Set out optional extra oil and toasted pepitas.

**1. Soften & bloom:** Warm garlic-infused oil in a heavy pot over medium heat. Soften the spring onion greens 2 minutes. Stir in the ginger and spice mix; cook 30 seconds until fragrant.

**2. Simmer:** Add the carrots; coat in oil and spices. Pour in the broth; add salt and pepper if not already in the mix. Simmer partially covered 20–25 minutes until the carrots are tender.

**3. Blend & finish:** Blend until smooth. Stir in coconut milk and lemon juice; warm 2 minutes on low. Taste salt. Serve with a drizzle of oil and pepitas from the finish bowl.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.""",
)

# --- 63 ---
_reg(
    "EAFF387F-B982-4068-866F-D293F5244AB4",
    """151 g chicken — a whole chicken, a carcass plus wings, or bone-in thighs and drumsticks
1/5 medium carrots, roughly chopped
1/6 stalks celery, roughly chopped
Green tops of 1 large leek, rinsed and roughly chopped
Green tops of 4 to 5 spring onions, roughly chopped
Green tops of 1 large leek, rinsed and roughly chopped (about 1 cup)
Green tops of 4 to 5 scallions, roughly chopped
3/5 sprigs fresh parsley
2/5 sprigs fresh thyme
1/8 bay leaf
1/8 tsp whole black peppercorns
1 1/5 cup cold filtered water
Salt, to taste (added after straining)""",
    """**Prep (bowls):**

**Large bowl — chicken:**
- Have the chicken ready (do not rinse raw chicken).

**Large bowl — veg aromatics:**
- Roughly chop the carrots, celery, leek greens and spring onion greens.

**Small bowl — herbs & peppercorns:**
- Have the parsley, thyme, bay leaf and peppercorns ready.

**By the hob (no bowl):** cold filtered water, salt (after straining).

**1. Build the pot:** Place the chicken in a large stockpot or slow cooker. Add the veg aromatics and herbs & peppercorns. Cover with cold water by an inch or two. Do not salt yet.

**2. Simmer:** Bring to a gentle boil, skim foam, then hold at a bare simmer 3–4 hours on the stove (or 8–10 hours on low in a slow cooker). Top up with hot water if needed.

**3. Strain & cool:** Rest 10 minutes. Strain; discard vegetables and aromatics; pull usable meat for another use. Strain again through cheesecloth if clearer broth is wanted. Salt to taste. Cool quickly; refrigerate within 2 hours. Skim fat next day if preferred.

Cool, portion, refrigerate a few days or freeze.""",
)

# --- 64 ---
_reg(
    "442A66E5-B40E-4178-95A2-44C5C778C808",
    """3/4 slices plain bacon (no added garlic or onion powder), chopped
Green tops of 2 leeks, or the green parts of 4 spring onions, thinly sliced (use the GREEN tops only)
Green tops of 2 leeks, or the green parts of 4 scallions, thinly sliced (use the GREEN tops only)
1/2 medium celery stalks, finely diced
3/4 medium waxy potatoes, peeled and cut into 1/2-inch cubes
1/2 cans chopped clams, drained, juice reserved
3/4 tbsp gluten-free 1:1 flour blend  🟢 Fructans 22%
1/4 bay leaf
1/4 fresh thyme leaves
1/2 tbsp [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)]
1/2 tbsp garlic-infused olive oil
1/4 cup bottled clam juice
1/4 1 low-FODMAP chicken broth or water
1/8 cup lactose-free whole milk
1/4 lactose-free cream (or more lactose-free milk)
Salt and black pepper, to taste
3/4 tsp chopped fresh parsley, to serve""",
    """**Prep (bowls):**

**Medium bowl — bacon:**
- Chop the bacon.

**Large bowl — aromatics (fry / soften):**
- Thinly slice the leek or spring onion greens. Finely dice the celery.

**Large bowl — potatoes:**
- Peel and cube the potatoes.

**Medium bowl — clams:**
- Drain the clams; reserve the juice.

**Small bowl — thickener & herbs:**
- Have the gluten-free flour ready. Set out the bay leaf and thyme.

**By the hob (no bowl):** garlic-infused olive oil, bottled clam juice, reserved clam juice, low-FODMAP broth or water, lactose-free milk, lactose-free cream, salt, pepper.

**Medium bowl — finish:**
- Chop the parsley.

**1. Crisp bacon:** Cook the bacon in a heavy pot over medium heat until crisp, 5–7 minutes. Transfer to a plate; leave about a tablespoon of fat in the pot.

**2. Soften veg:** Add garlic-infused oil and the aromatics bowl; soften about 4 minutes without much colour.

**3. Build base:** Sprinkle over the flour; stir 1 minute. Slowly whisk in bottled clam juice, reserved clam juice and broth. Add the potatoes, bay leaf and thyme. Simmer uncovered until potatoes are tender, 15–18 minutes, stirring so nothing sticks.

**4. Finish:** On low, stir in milk and cream; heat 3–4 minutes without boiling. Add the clams; warm 2–3 minutes only. Discard the bay leaf. Season. Serve topped with reserved bacon and parsley from the finish bowl.

Cool, portion, refrigerate up to 2 days; reheat gently until piping hot.""",
)

# --- 65 ---
_reg(
    "B65BB7C0-5504-4EB8-AB0C-A865F6F4196D",
    """76 g lean ground beef (or plain ground pork; see tips before using sausage)
Green tops of 4 spring onions (spring onions), sliced (green part only)
Green tops of 4 scallions (spring onions), sliced (green part only)
1/6 tsp dried oregano
1/6 tsp dried basil
1/8 tsp red pepper flakes (optional)  🟢 Fructose 1%
1 tsp tomato paste  🟢 Fructose 20%
28 g gluten-free lasagna noodles, broken into 2-inch pieces (rice or corn based)  🟢 Fructans 19%
1/2 tsp [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)]
1/2 tsp garlic-infused olive oil
1/6 can crushed or diced tomatoes
1 cup low FODMAP beef broth or low-FODMAP chicken broth
1/8 tsp salt, plus more to taste
1/8 tsp black pepper
1 1/2 tbsp lactose-free ricotta
1/8 cup shredded mozzarella
1 1/2 tbsp grated parmesan
1 tsp chopped fresh basil (optional)""",
    """**Prep (bowls):**

**Medium bowl — meat (brown):**
- Have the ground beef (or pork) ready.

**Medium bowl — aromatics:**
- Slice the spring onion green tops.

**Small bowl — spice mix (toast):**
- Mix the dried oregano, dried basil and optional red pepper flakes.
- Have the tomato paste ready beside this bowl.

**Large bowl — noodles (add later):**
- Break the gluten-free lasagna noodles into pieces.

**By the hob (no bowl):** garlic-infused olive oil, crushed or diced tomatoes, low-FODMAP broth, salt, pepper.

**Medium bowl — finish:**
- Stir the ricotta with half the parmesan. Set out mozzarella, remaining parmesan and optional fresh basil.

**1. Brown:** Warm garlic-infused oil in a large pot. Soften the aromatics 1 minute. Add the meat; brown 6–8 minutes until cooked through (about 70°C if checking).

**2. Toast spices:** Stir in the tomato paste and spice mix; cook 1 minute.

**3. Simmer:** Add the tomatoes and broth; scrape the pot. Season with salt and pepper. Simmer uncovered 15 minutes.

**4. Noodles & finish:** Add the noodles bowl; cook 8–10 minutes until tender, stirring so they do not stick. Taste salt. Ladle into bowls; top with the ricotta mixture, mozzarella, remaining parmesan and fresh basil from the finish bowl.

Cool, portion, refrigerate up to 3 days; reheat until piping hot (noodles soften further on standing).""",
)

# --- 66 ---
_reg(
    "F6CAA96C-3F23-41B9-9F87-E2BFDB908778",
    """Green tops of 3 spring onions (spring onions), green part only, thinly sliced
Green tops of 3 scallions (spring onions), green part only, thinly sliced
225 g kabocha or Kent (Jap) pumpkin, peeled, seeded, cut into 2 cm cubes
1/2 medium carrots, peeled and chopped
3/4 tsp grated fresh ginger (optional)
1/5 tsp salt, plus more to taste
Black pepper, to taste
Pinch of ground cumin or nutmeg (optional)
1/2 tbsp [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)]
1/2 tbsp garlic-infused olive oil
1 cup low-FODMAP chicken broth or low-FODMAP vegetable broth
1/8 cup lactose-free cream, plus a little extra to swirl
1/4 tsp maple syrup (optional)
1/2 tbsp pepitas (pumpkin seeds), for garnish
3/4 tsp chopped chives or extra spring onion greens, for garnish  🟡 Fructans 38%""",
    """**Prep (bowls):**

**Large bowl — veg (fry / soften):**
- Thinly slice the spring onion greens. Cube the pumpkin; chop the carrot. Grate the ginger if using.

**Small bowl — seasoning:**
- Have salt, pepper and optional cumin or nutmeg ready.

**By the hob (no bowl):** garlic-infused olive oil, low-FODMAP broth, lactose-free cream, optional maple syrup.

**Medium bowl — finish:**
- Set out pepitas, chives (or extra spring onion greens), and a little extra cream for swirling.

**1. Soften veg:** Warm garlic-infused oil in a large pot. Soften the spring onion greens 1–2 minutes. Add pumpkin, carrot and ginger; cook 4–5 minutes, stirring.

**2. Simmer:** Pour in the broth and add salt. Boil, then simmer partly covered 18–22 minutes until very tender.

**3. Blend & finish:** Blend until smooth. Stir in cream, optional maple syrup, seasoning spices and pepper; warm gently without boiling. Taste salt. Serve with a cream swirl, pepitas and chives from the finish bowl.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.""",
)

# --- 67 ---
_reg(
    "CC3AAC0C-134B-4AEF-838E-B72ED1ED1D20",
    """3/8 medium carrots, roughly chopped
1/8 stalk celery, roughly chopped
1/8 medium parsnip (about 75g), roughly chopped
Green tops of 1 large leek, rinsed and roughly chopped
Green tops of 5 to 6 spring onions, roughly chopped
Green tops of 1 large leek, rinsed and roughly chopped (about 1 cup)
Green tops of 5 to 6 scallions, roughly chopped
1/8 corn cob, broken into a few pieces (optional, for sweetness)
3/4 sprigs fresh parsley
1/2 sprigs fresh thyme
1/8 sprig fresh rosemary
1/4 bay leaves
1/8 tsp whole black peppercorns
3/8 tsp olive oil
1 1/2 cup cold filtered water
Salt, to taste (added after straining)""",
    """**Prep (bowls):**

**Large bowl — veg (optional sauté):**
- Roughly chop the carrots, celery, parsnip, leek greens and spring onion greens. Break the optional corn cob into pieces.

**Small bowl — herbs & peppercorns:**
- Have the parsley, thyme, rosemary, bay leaves and peppercorns ready.

**By the hob (no bowl):** olive oil, cold filtered water, salt (after straining).

**1. Optional sauté:** Warm olive oil in a large stockpot. Soften the veg bowl 5–7 minutes until edges start to colour.

**2. Build & simmer:** Add the herbs & peppercorns (and corn if using). Cover with cold water by an inch or two. Do not salt yet. Bring to a gentle boil, then bare-simmer 1.5–2 hours with the lid cracked (avoid a hard boil). Top up with hot water if needed.

**3. Strain & cool:** Rest 10 minutes. Strain without pressing hard on the solids. Cheesecloth again if clearer broth is wanted. Salt to taste. Cool quickly; refrigerate within 2 hours.

Cool, portion, refrigerate a few days or freeze.""",
)


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj: dict) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


async def post_recipe(session, headers, recipe: dict, limiter: RateLimiter) -> bool:
    await limiter.wait_turn()
    form = aiohttp.FormData()
    form.add_field(
        "data",
        gzip_obj(recipe),
        content_type="application/octet-stream",
        filename="data",
    )
    async with session.post(
        f"{PAPRIKA_API}/v2/sync/recipe/{recipe['uid']}/",
        headers=headers,
        data=form,
    ) as r:
        return '"result":true' in (await r.text()).replace(" ", "")


def update_local(uid: str, ingredients: str, directions: str) -> None:
    con = sqlite3.connect(LOCAL_DB)
    con.execute(
        """
        UPDATE recipes
        SET ingredients=?, directions=?, status=?
        WHERE uid=?
        """,
        (ingredients, directions, "modified", uid),
    )
    con.commit()
    con.close()


async def main() -> None:
    queue = json.loads(QUEUE.read_text(encoding="utf-8"))
    batch = queue[51:]
    limiter = RateLimiter(0.35)
    user, pw = paprika_credentials()
    results = []

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
        st, body = await api_json(
            session,
            limiter,
            "POST",
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": pw},
        )
        if st != 200:
            raise SystemExit(f"login failed {st}")
        headers = {"Authorization": f"Bearer {body['result']['token']}"}

        for item in batch:
            uid = item["uid"]
            name = item["name"]
            if uid not in REWRITES:
                results.append({"uid": uid, "name": name, "status": "skipped", "why": "no rewrite"})
                safe_print(f"SKIP no rewrite {name}")
                continue

            ingredients, directions = REWRITES[uid]

            st, body = await api_json(
                session,
                limiter,
                "GET",
                f"{PAPRIKA_API}/v2/sync/recipe/{uid}/",
                headers=headers,
            )
            rec = (body or {}).get("result") or {}
            if not rec.get("uid"):
                results.append({"uid": uid, "name": name, "status": "failed", "why": "missing cloud"})
                safe_print(f"FAIL missing {name}")
                continue

            existing = rec.get("directions") or ""
            if "**Prep (bowls):**" in existing or "Prep (bowls):" in existing:
                # Still refresh if our rewrite differs? Skip if already prepped.
                results.append({"uid": uid, "name": name, "status": "skipped", "why": "already Prep bowls"})
                safe_print(f"SKIP already prepped {name}")
                continue

            rec["ingredients"] = ingredients
            rec["directions"] = directions
            rec["hash"] = calc_hash(rec)
            ok = await post_recipe(session, headers, rec, limiter)
            if ok:
                update_local(uid, ingredients, directions)
                results.append({"uid": uid, "name": name, "status": "ok"})
                safe_print(f"OK {name}")
            else:
                results.append({"uid": uid, "name": name, "status": "failed", "why": "POST"})
                safe_print(f"FAIL post {name}")

        await limiter.wait_turn()
        async with session.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            safe_print(f"notify: {r.status}")

    out = Path(__file__).resolve().parent / ".soups_prep_batch51_results.json"
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    safe_print(f"wrote {out}")
    ok_n = sum(1 for r in results if r["status"] == "ok")
    safe_print(f"done ok={ok_n}/{len(results)}")


if __name__ == "__main__":
    asyncio.run(main())

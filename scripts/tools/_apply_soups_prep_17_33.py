"""Apply Prep (bowls) restyle for soups queue indices 17–33."""
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


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj: dict) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


# uid -> (ingredients, directions)
UPDATES: dict[str, tuple[str, str]] = {
    # 17 Spicy tomato soup
    "273D62C5-5200-4FCE-82FC-D62731E5DDA2": (
        """17 g rice vermicelli  🟢 Fructans 9%
17 g bean sprouts  🟢 Galacto-oligosaccharides 24%
1/6 tsp ground ginger
1 tsp sugar
1/6 tsp sambal or more to taste (hot chili pepper paste)
1 tsp cornstarch
Optional: one stalk spring onion (the green part) for garnish
0.17 l water
1/3 low FODMAP stock cubes
67 g canned peeled tomatoes  🔴 Fructose 89%
33 g canned diced tomatoes  🟡 Fructose 44%
1/6 large tin of tomato paste
1 tsp soy sauce
1/2 tbsp ginger syrup
""",
        """**Prep (bowls):**

**Small bowl — flavour mix (add later):**
- Mix the ground ginger, sugar and sambal.

**Small bowl — slurry:**
- Mix the cornstarch with a little cold water until smooth.

**Medium bowl — noodles & sprouts (add later):**
- Have the rice vermicelli and bean sprouts ready.
- Optional: slice spring onion greens for garnish.

**By the hob (no bowl):** water, stock cubes, peeled tomatoes, diced tomatoes, tomato paste, soy sauce, ginger syrup.

**1. Stock:** Bring the water with the stock cubes to a boil.

**2. Tomatoes:** Add the peeled tomatoes, diced tomatoes and tomato paste. Bring back to a boil.

**3. Season:** Stir in the flavour mix, soy sauce and ginger syrup. Taste and add salt or a little more sambal if needed.

**4. Blend:** Blitz with a hand blender until smooth.

**5. Thicken:** Stir in the slurry and simmer briefly until lightly thickened.

**6. Finish:** Add the noodles & sprouts bowl. Simmer about 10 minutes. Serve with optional spring onion greens.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
    ),
    # 18 stuffed pepper soup
    "0BFD689E-B4F2-4392-897A-24502BE031C8": (
        """100 g lean minced meat
1/3 green (or red, see notes below recipe) bell peppers*  🔴 Fructose 124%
1/4 jalapeño pepper, seeds removed
50 g brown rice  🟡 Fructans 28%
3/4 tsp Italian herbs
1/4 tsp ground cumin
1/8 tsp ground paprika
1/8 tsp cayenne pepper
Salt and pepper
100 &#8211; 500 ml stock made with a low FODMAP stock cube / powder
1/4 can diced tomatoes
Olive oil
Optional: grated cheese
Optional: fresh coriander or parsley
Optional: extra jalapeño pepper for topping
Optional: fresh cilantro or parsley
""",
        """**Prep (bowls):**

**Large bowl — meat (fry / brown):**
- Have the minced meat ready; season with salt and pepper when cooking.

**Large bowl — veg (fry / soften):**
- Dice the bell pepper.
- Slice the jalapeño into rings (seeds removed).

**Large bowl — rice (boil later):**
- Rinse the brown rice if needed.

**Small bowl — spice mix (fry):**
- Mix the Italian herbs, ground cumin, ground paprika and cayenne.

**Medium bowl — finish:**
- Set out optional grated cheese, chopped coriander or parsley, and extra jalapeño for topping.

**By the hob (no bowl):** stock, diced tomatoes, olive oil, salt and pepper.

**1. Brown meat:** In a pan, brown the meat bowl; season with salt and pepper.

**2. Soften veg:** Add the veg bowl; fry 3–4 minutes until softened.

**3. Simmer:** Heat oil in a soup pan. Tip in the meat and veg. Add the stock, diced tomatoes and spice mix. Stir and simmer 15–20 minutes.

**4. Rice:** Boil the rice bowl in a separate pan until tender; drain and stir into the soup. Taste salt and pepper.

**5. Finish:** Serve topped with items from the finish bowl.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
    ),
    # 19 wild rice soup
    "F2AA4F78-FADC-4E6A-9982-23EC6AA72350": (
        """56 g oyster mushrooms, cleaned and chopped  🔴 Mannitol 119%
50 g carrot, grated and chopped
38 g wild rice  🟢 Fructans 20%
1/4 tsp dried sage
1/4 tsp fresh thyme, leaves stripped from the stems
1 green onions, only the green part
1/2 handfuls of fresh spinach
3/4 tsp butter
0.38 l low FODMAP broth
1/8 tsp salt
1/8 tsp pepper
56 g lactose-free cream
""",
        """**Prep (bowls):**

**Large bowl — veg (fry / soften):**
- Chop the oyster mushrooms.
- Grate and chop the carrot.

**Large bowl — rice (simmer later):**
- Rinse the wild rice.

**Small bowl — herbs & aromatics:**
- Strip the thyme leaves.
- Slice the spring onion greens.
- Have the dried sage ready.

**Medium bowl — finish:**
- Wash the spinach.

**By the hob (no bowl):** butter, broth, salt, pepper, lactose-free cream.

**CrockPot Express**

**1. Soften veg:** Sauté butter, then the veg bowl, about 4 minutes until slightly soft.

**2. Pressure cook:** Add the rice bowl, broth, salt, pepper and the herbs bowl. Seal and cook 35 minutes; natural release 10 minutes.

**3. Finish:** Stir in the spinach from the finish bowl and the cream. Taste salt and pepper.

**Stovetop**

**1. Soften veg:** Melt butter in a soup pot; sauté the veg bowl about 5 minutes until slightly soft.

**2. Simmer:** Add the rice bowl, broth, salt, pepper and herbs bowl. Bring to a boil, then simmer 30–40 minutes until the wild rice is tender with bite; stir occasionally.

**3. Finish:** Stir in the cream and spinach from the finish bowl. Taste salt and pepper.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
    ),
    # 20 Beef Stew
    "8C1F74BD-6CB3-4F9C-B843-BD538E869D1C": (
        """181 g beef chuck, trimmed and cut into 1.5-inch cubes
1/4 tsp kosher salt, plus more to taste
1/8 tsp black pepper
1/2 tsp gluten-free all-purpose flour without inulin or chicory root  🟢 Fructans 5%
2/3 tbsp spring onion greens, sliced thin  🔴 Fructans 100%
1 tsp tomato paste  🟢 Fructose 20%
39 g Yukon Gold or red potatoes, cut into 1-inch chunks
1/8 carrots, cut into 1-inch coins
1/6 medium parsnip, peeled and cut into 1-inch coins
1/6 small turnip, peeled and cut into 1-inch chunks (optional)
1/3 bay leaves
1/2 sprigs fresh thyme
1/6 sprig fresh rosemary
1 tsp chopped fresh parsley, to finish
Extra spring onion greens, to finish
Extra scallion greens, to finish
1/2 tbsp [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)], divided
1/8 cup dry red wine (Cabernet, Merlot, or Pinot Noir)
1 cup low-FODMAP chicken broth
""",
        """**Prep (bowls):**

**Large bowl — beef (sear):**
- Pat the beef dry. Toss with salt, pepper and flour until coated (skip flour if using a cornstarch slurry later).

**Medium bowl — aromatics (soften):**
- Slice the spring onion greens.
- Have the tomato paste ready beside this bowl.

**Large bowl — root veg (braise later):**
- Chunk the potatoes; coin the carrots and parsnip; chunk the optional turnip.

**Small bowl — herbs:**
- Set out bay leaves, thyme and rosemary sprigs.

**Medium bowl — finish:**
- Chop the parsley; set out extra spring onion / scallion greens.

**By the hob (no bowl):** garlic-infused oil, red wine, chicken broth; optional cornstarch and cold water for slurry.

**1. Sear beef:** Heat oil in a Dutch oven over medium-high. Brown the beef bowl in batches 3–4 minutes per side; transfer aside. Reduce heat if the flour darkens too fast.

**2. Soften aromatics:** Add remaining oil and the aromatics bowl; cook about 1 minute. Stir in the tomato paste 2 minutes until darkened and sweet.

**3. Deglaze:** Pour in the wine; scrape the pot and simmer 2 minutes. Return the beef with juices. Add the broth and herbs bowl. Bring to a gentle simmer.

**4. Braise:** Cover and transfer to a 165°C oven for 75 minutes.

**5. Add veg:** Stir in the root veg bowl; cover and braise 40–50 minutes until beef shreds and vegetables are tender.

**6. Finish:** Remove herb stems. Taste salt and pepper. Thicken on the hob if needed, or stir in a cornstarch slurry and cook 2 minutes. Serve topped from the finish bowl.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
    ),
    # 21 Broccoli Cheddar Soup
    "4FD031ED-AF8E-46F0-80CC-900065B9C662": (
        """1/2 tbsp spring onion greens, thinly sliced  🟠 Fructans 75%
1/16 carrots, shredded or finely diced
21 g broccoli florets only, chopped small (use florets only)  🟡 Fructans 47%
1/3 tsp gluten-free all-purpose flour  🟢 Fructans 3%
1/8 tsp mustard powder
1/8 tsp salt, plus more to taste
1/8 tsp black pepper
1/8 tsp ground nutmeg (optional)
13 g sharp aged cheddar, shredded off the block
1/6 tsp Dijon mustard (optional)
1 tsp [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)]
1 tsp butter
2/3 cup low-FODMAP chicken broth or low-FODMAP vegetable broth
1/8 cup lactose-free whole milk
1 tsp garlic-infused olive oil
""",
        """**Prep (bowls):**

**Medium bowl — aromatics (soften):**
- Slice the spring onion greens.
- Shred or finely dice the carrot.

**Medium bowl — broccoli (simmer later):**
- Chop the broccoli florets small.

**Small bowl — seasonings:**
- Mix the mustard powder, salt, pepper and optional nutmeg.
- Have the flour ready beside this bowl (skip if using a slurry later).

**Small bowl — finish:**
- Shred the cheddar.
- Have the Dijon ready if using.

**By the hob (no bowl):** garlic-infused oil, butter, broth, lactose-free milk.

**1. Soften veg:** Warm oil and butter over medium until the butter foams. Soften the aromatics bowl 4–5 minutes.

**2. Roux:** Sprinkle on the flour; cook 1–2 minutes, stirring, until the raw smell goes (or skip and slurry later).

**3. Liquids:** Whisk in the broth a little at a time, then the milk. Stir in the seasonings bowl. Bring to a gentle simmer — do not hard-boil.

**4. Broccoli:** Add the broccoli bowl. Simmer uncovered 8–12 minutes until fork-tender and lightly thickened.

**5. Cheese:** Off heat 2 minutes. Stir in cheddar from the finish bowl a little at a time until melted. Add Dijon if using. Taste salt and pepper. Optionally blend a portion for a smoother soup.

Cool, portion, refrigerate up to 3 days; reheat gently until piping hot (avoid hard boiling after cheese).
""",
    ),
    # 22 Chicken Noodle Soup
    "DF7EDB7A-E830-4B9E-9BDA-036805EC3FE3": (
        """76 g boneless skinless chicken breast or thigh, or 3 cups shredded cooked chicken
1/8 carrots, sliced into thin coins
1/3 stalks celery, thinly sliced (spread across 6 servings)
3/4 tbsp spring onion greens, sliced thin, plus more to finish  🔴 Fructans 112%
12 g gluten-free pasta (brown-rice spaghetti broken in half, small GF shells, or GF elbows)  🟢 Fructans 8%
1/6 bay leaf
1/3 sprigs fresh thyme
1 tsp chopped fresh parsley
1/2 tsp [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)] (optional)
1 1/3 cup low-FODMAP chicken broth
Salt and black pepper, to taste
Squeeze of lemon, to finish (optional)
1/2 tsp garlic-infused olive oil (optional)
""",
        """**Prep (bowls):**

**Large bowl — chicken (poach / warm):**
- Have raw chicken pieces ready, or shredded cooked chicken if using pre-cooked.

**Large bowl — veg (soften):**
- Slice the carrots into thin coins.
- Thinly slice the celery.
- Slice most of the spring onion greens (save some to finish).

**Medium bowl — noodles (add later):**
- Break spaghetti in half if using, or have shells/elbows ready.

**Small bowl — herbs:**
- Set out bay leaf and thyme sprigs.
- Chop the parsley for finish.

**By the hob (no bowl):** garlic-infused oil, chicken broth, salt, pepper, optional lemon.

**1. Soften veg:** Warm oil over medium. Soften the veg bowl 3–4 minutes until carrots start to soften.

**2. Broth:** Add the broth and herbs bowl. Bring to a gentle simmer.

**3. Chicken:** For raw chicken, poach in the broth 15–18 minutes to 75°C; lift out, rest 5 minutes, shred or chop, then return later. For cooked chicken, simmer veg and broth about 10 minutes until carrots are tender, then add chicken near the end.

**4. Noodles:** Simmer the noodles bowl per package (usually 6–9 minutes) until just tender.

**5. Finish:** Return shredded chicken if needed; warm 1–2 minutes. Remove herb stems. Season with salt and pepper. Stir in parsley and optional lemon; top with reserved spring onion greens.

Cool, portion, refrigerate up to 3 days or freeze (noodles soften on holding); reheat until piping hot.
""",
    ),
    # 23 Chicken Stew
    "714B4E3D-D834-44DD-9CAD-EEF5033F14B6": (
        """181 g bone-in, skin-on chicken thighs (about 6 thighs)
1/4 tsp kosher salt, plus more to taste
1/8 tsp black pepper
1/2 tsp gluten-free all-purpose flour without inulin or chicory root  🟢 Fructans 5%
2/3 tbsp spring onion greens, sliced thin  🔴 Fructans 100%
1 stalks celery, cut into 1/2-inch slices (one stalk per serve)
39 g Yukon Gold or red potatoes, cut into 1-inch chunks
1/8 carrots, cut into 1-inch coins
1/6 medium parsnip, peeled and cut into 1-inch coins
1/3 bay leaves
1/2 sprigs fresh thyme
1/6 sprig fresh rosemary
1 tsp chopped fresh parsley, to finish
Extra spring onion greens, to finish
Extra scallion greens, to finish
1/2 tbsp [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)], divided
1/8 cup dry white wine, optional (Sauvignon Blanc or Pinot Grigio)
1 cup low-FODMAP chicken broth
""",
        """**Prep (bowls):**

**Large bowl — chicken (sear):**
- Pat thighs dry. Season with salt and pepper; dust skin with flour (skip flour if using a slurry later).

**Medium bowl — aromatics (soften):**
- Slice the spring onion greens.
- Slice the celery.

**Large bowl — root veg (simmer later):**
- Chunk the potatoes; coin the carrots and parsnip.

**Small bowl — herbs:**
- Set out bay, thyme and rosemary.

**Medium bowl — finish:**
- Chop parsley; set out extra spring onion / scallion greens.

**By the hob (no bowl):** garlic-infused oil, optional white wine, chicken broth; optional cornstarch slurry.

**1. Sear chicken:** Heat oil in a Dutch oven over medium-high. Sear the chicken bowl skin-side down 5–6 minutes, then 3 minutes more; work in batches. Transfer aside.

**2. Soften aromatics:** Pour off all but a little fat. Add remaining oil and the aromatics bowl; cook about 3 minutes until celery softens slightly.

**3. Deglaze:** Add wine if using (or a splash of broth); scrape and simmer 2 minutes. Add remaining broth and the herbs bowl. Return chicken skin-side up; bring to a gentle simmer.

**4. Simmer:** Cover on low 25 minutes. Add the root veg bowl, keeping thighs on top; cover and simmer 20–25 minutes until veg are tender and chicken reaches 80°C.

**5. Finish:** Lift thighs; remove herb stems. Pull meat into chunks and return (discard bones/skin, or crisp skin separately). Taste salt and pepper; thicken with slurry if needed. Serve topped from the finish bowl.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
    ),
    # 24 Corn Chowder
    "E67B0E48-9E59-4CF2-BA33-0A839FA9E496": (
        """1/8 cup (about 90g) leek, GREEN tops only, thinly sliced  🔴 Fructans 107%
3/4 spring onions, GREEN tops only, sliced (reserve some for garnish)
53 g potato, peeled and diced into 1cm pieces
1/4 tsp fresh thyme leaves
38 g fresh corn kernels, cut from 2 ears (keeps each serving at or under the 38g cap)
Optional: 1 tbsp cornstarch mixed with 2 tbsp cold water, for a thicker chowder
Optional garnish: grated cheddar, extra spring onion greens, cracked pepper
Optional garnish: grated cheddar, extra scallion greens, cracked pepper
1/2 tbsp [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)]
3/4 cup (720ml) water or low-FODMAP chicken broth
1/8 cup (240ml) lactose-free whole milk
1 tbsp (120ml) lactose-free heavy cream
1/8 tsp salt, plus more to taste
Black pepper, to taste
1/2 tbsp garlic-infused olive oil
""",
        """**Prep (bowls):**

**Large bowl — aromatics (soften):**
- Thinly slice the leek greens.
- Slice the spring onion greens (reserve some for garnish).

**Large bowl — potato (simmer):**
- Peel and dice the potato.
- Strip the thyme leaves onto this bowl.

**Medium bowl — corn (add later):**
- Cut the corn kernels from the cob.

**Small bowl — slurry (optional):**
- Mix cornstarch with cold water if thickening.

**Medium bowl — finish:**
- Set out reserved spring onion greens, optional grated cheddar and cracked pepper.

**By the hob (no bowl):** garlic-infused oil, water or broth, lactose-free milk, cream, salt, pepper.

**1. Soften aromatics:** Heat oil over medium. Soften the aromatics bowl 4–5 minutes without browning.

**2. Potato:** Stir in the potato bowl 1 minute. Add water or broth; simmer covered 12–15 minutes until potato is fork-tender.

**3. Corn:** Stir in the corn bowl; simmer uncovered about 4 minutes. Optionally blend a portion and return for creaminess.

**4. Dairy:** Lower heat; stir in milk and cream. Warm 3–4 minutes without a hard boil. Add slurry if using; simmer 1–2 minutes until coats a spoon.

**5. Finish:** Season with salt and pepper. Serve topped from the finish bowl.

Cool, portion, refrigerate up to 3 days; reheat gently until piping hot.
""",
    ),
    # 25 Egg Drop Soup
    "809CFE32-84B9-4263-B5F9-1CC47D5E5C18": (
        """3/4 tsp fresh ginger, finely grated (about a 1-inch knob)
1/8 tsp ground white pepper (or black pepper)
1/8 tsp ground turmeric (optional, for color)
1/2 tbsp cornstarch
3/4 tbsp cold water
1 large eggs, beaten
3/4 tbsp spring onion greens, thinly sliced, plus more to finish  🔴 Fructans 112%
1 1/2 cup low-FODMAP chicken broth
3/8 tsp gluten-free tamari
1/8 tsp toasted sesame oil
Salt, to taste
""",
        """**Prep (bowls):**

**Small bowl — aromatics:**
- Finely grate the ginger.
- Have white pepper and optional turmeric ready.

**Small bowl — slurry:**
- Whisk cornstarch with cold water until smooth.

**Small bowl — egg:**
- Beat the eggs in a spouted jug or cup.

**Medium bowl — finish:**
- Thinly slice the spring onion greens (save a pinch for garnish).

**By the hob (no bowl):** chicken broth, tamari, sesame oil, salt.

**1. Warm broth:** Simmer broth with the aromatics bowl and tamari about 5 minutes. Taste; add salt only if needed.

**2. Thicken:** Stir in the slurry in a thin stream; simmer 1–2 minutes until lightly glossy.

**3. Drop eggs:** Lower to a bare simmer. Stir broth in a slow circle and pour the egg bowl in a thin stream; stop stirring and wait about 30 seconds for ribbons.

**4. Finish:** Off heat, stir in sesame oil and most of the finish bowl. Ladle and top with remaining greens and pepper.

Serve immediately; leftovers keep 1–2 days refrigerated — reheat gently without hard boiling.
""",
    ),
    # 26 Minestrone
    "0D378884-DFA2-4274-92C1-7A07748AA58B": (
        """3/4 tbsp spring onion greens, sliced thin  🔴 Fructans 112%
1/8 carrots, diced small
1/3 stalks celery, thinly sliced (spread across 6 servings)
1/3 medium courgette, diced into 1/2-inch pieces
1 tsp tomato paste  🟢 Fructose 20%
1/4 cup canned lentils, drained and thoroughly rinsed  🔴 Galacto-oligosaccharides 130%
1/6 tsp dried oregano
1/6 tsp dried basil
1/8 tsp dried thyme
1/6 bay leaf
7.9 g gluten-free elbows or small shells (brown-rice or corn-based, not legume)  🟢 Fructans 4%
2/3 packed cups baby spinach
1 tsp chopped fresh parsley
6.7 g grated Parmesan, to finish (optional)
1 tsp [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)]
1 1/3 cup low-FODMAP chicken broth or low-FODMAP vegetable broth
1/6 can chopped tomatoes
Salt and black pepper, to taste
1 tsp garlic-infused olive oil
""",
        """**Prep (bowls):**

**Large bowl — veg (soften):**
- Slice the spring onion greens.
- Dice the carrot; thinly slice the celery.
- Dice the courgette.
- Have the tomato paste ready beside this bowl.

**Small bowl — spice mix (fry):**
- Mix the dried oregano, dried basil and dried thyme.
- Set out the bay leaf.

**Medium bowl — lentils (add later):**
- Drain and thoroughly rinse the lentils.

**Medium bowl — pasta (add later):**
- Have the gluten-free pasta ready.

**Medium bowl — finish:**
- Wash the spinach.
- Chop the parsley.
- Grate optional Parmesan.

**By the hob (no bowl):** garlic-infused oil, broth, chopped tomatoes, salt, pepper.

**1. Soften veg:** Warm oil over medium. Soften carrot, celery and spring onion from the veg bowl 4–5 minutes. Stir in tomato paste 1 minute, then the courgette.

**2. Simmer:** Add chopped tomatoes, broth, spice mix and bay. Simmer uncovered about 15 minutes until veg are tender but hold shape. Stir in the lentils bowl; simmer 5 minutes.

**3. Pasta:** Add the pasta bowl; cook per package (usually 7–9 minutes) until just tender.

**4. Finish:** Wilt spinach from the finish bowl in handfuls (about 2 minutes). Remove bay. Season with salt and pepper; stir in parsley. Serve with Parmesan and a drizzle of oil.

Cool, portion, refrigerate up to 3 days or freeze (pasta softens on holding); reheat until piping hot.
""",
    ),
    # 27 Pho Chicken
    "D57C7C6A-ABD6-4860-B3F4-693A23A195B0": (
        """3/4 -inch piece fresh ginger, halved lengthwise
3/4 whole star anise
1/4 cinnamon stick
1 whole cloves
3/4 green cardamom pods, lightly crushed
3/4 tsp coriander seeds
113 g boneless skinless chicken breast
23 g flat rice noodles (pho-style, banh pho)  🟢 Fructans 15%
3/4 tbsp spring onion greens, thinly sliced  🔴 Fructans 112%
1/8 cup fresh Thai basil leaves
1/8 cup fresh coriander leaves
1/4 cup mung bean sprouts  🔴 Galacto-oligosaccharides 83%
1/4 lime, cut into wedges
1/4 red Thai chili or jalapeño, thinly sliced (optional)
2 cup low-FODMAP chicken broth
1/2 tsp Red Boat fish sauce (garlic-free)  🟢 Fructans 4%
3/4 tsp gluten-free tamari
1/4 tsp sugar
Salt, to taste
""",
        """**Prep (bowls):**

**Small bowl — ginger (char):**
- Halve the ginger lengthwise for charring.

**Small bowl — spice mix (toast):**
- Set out star anise, cinnamon stick, cloves, crushed cardamom and coriander seeds.

**Large bowl — chicken (poach):**
- Have the chicken breast ready for simmering.

**Medium bowl — noodles (boil later):**
- Have the rice noodles ready.

**Medium bowl — finish / table:**
- Slice spring onion greens.
- Set out Thai basil, coriander, bean sprouts, lime wedges and optional chili.

**By the hob (no bowl):** chicken broth, fish sauce, tamari, sugar, salt.

**1. Char ginger:** Char the ginger bowl over a flame or in a dry cast-iron pan 3–4 minutes per side until blackened in spots.

**2. Toast spices:** Dry-toast the spice mix 2–3 minutes until fragrant; bag or leave loose to strain later.

**3. Simmer broth:** Combine broth, charred ginger and spices. Add the chicken bowl; simmer 18–22 minutes to 75°C. Lift chicken, rest 5 minutes, slice thin. Keep broth simmering about 15 minutes more. Stir in fish sauce, tamari and sugar; salt to taste. Strain; keep hot.

**4. Noodles:** Boil the noodles bowl per package (usually 4–6 minutes); drain and rinse briefly.

**5. Assemble:** Divide noodles into bowls; top with sliced chicken. Ladle hot broth over. Serve with the finish bowl at the table.

Best eaten fresh; refrigerate leftover broth and components separately up to 3 days; reheat broth until piping hot.
""",
    ),
    # 28 Potato Leek Soup
    "3E783717-8032-438C-B165-7FBE06DEEA4D": (
        """1/8 cup leek green tops only, thinly sliced  🔴 Fructans 107%
0.052973 kg Yukon Gold potatoes, peeled and cut into 1/2-inch cubes
1/6 bay leaf
1/3 sprigs fresh thyme
1 tsp fresh chives, thinly sliced, for garnish
1 tsp [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)]
1 tsp butter
2/3 cup low-FODMAP chicken broth or low-FODMAP vegetable broth
1/6 1 lactose-free whole milk
1/2 salt, plus more to taste
1/6 black pepper
1 tsp garlic-infused olive oil
""",
        """**Prep (bowls):**

**Large bowl — leek greens (soften):**
- Use green tops only (discard white). Slice into thin half-moons. Wash well in cold water; drain.

**Large bowl — potato (simmer):**
- Peel and cube the potatoes.

**Small bowl — herbs:**
- Set out bay leaf and thyme sprigs.

**Small bowl — finish:**
- Thinly slice the chives.

**By the hob (no bowl):** garlic-infused oil, butter, broth, lactose-free milk, salt, pepper.

**1. Soften leeks:** Warm oil and butter over medium-low until butter foams. Soften the leek bowl with a pinch of salt 8–10 minutes without browning. Stir in the herbs bowl 30 seconds.

**2. Potatoes:** Add the potato bowl, broth, salt and pepper (liquid should just cover; splash of water if needed). Simmer uncovered 18–22 minutes until potatoes fall apart. Remove herb stems.

**3. Blend:** Blitz mostly smooth, leaving a little texture (avoid over-blending).

**4. Finish:** On low heat, stir in the milk; warm 2–3 minutes without boiling. Taste salt and pepper. Serve topped from the finish bowl.

Cool, portion, refrigerate up to 3 days or freeze; reheat gently until piping hot.
""",
    ),
    # 29 Tom Kha
    "FA5B38C3-BB3E-4ADB-BFD3-CD05E43C11FE": (
        """1/2 stalks lemongrass, tough outer layers removed, bruised and cut into 2-inch pieces
1/4 thumb-sized piece fresh galangal, or fresh ginger
1 makrut (kaffir) lime leaves, torn (optional, but authentic)
100 g boneless skinless chicken thighs or breast, thinly sliced
50 g oyster mushrooms, torn into strips (the only mushroom tested low-FODMAP)  🔴 Mannitol 106%
Green tops of 2 spring onions, sliced (save the white bulbs for something else, they are high-FODMAP)
1/4 red chili, sliced (optional)
Small handful fresh coriander, chopped, to garnish
Green tops of 2 scallions, sliced (save the white bulbs for something else, they are high-FODMAP)
Small handful fresh cilantro, chopped, to garnish
1/2 tbsp [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)]
1 cup low-FODMAP chicken broth
1/3 cup canned coconut milk  🔴 Sorbitol 133%
5/8 tbsp fish sauce (check the label for added onion or garlic)
5/8 tbsp fresh lime juice
3/8 tsp cane sugar or maple syrup
1/2 tbsp garlic-infused olive oil
""",
        """**Prep (bowls):**

**Small bowl — aromatics (infuse):**
- Bruise and cut the lemongrass.
- Slice the galangal or ginger.
- Tear the lime leaves if using.

**Large bowl — chicken (poach):**
- Thinly slice the chicken.

**Medium bowl — mushrooms (simmer later):**
- Tear the oyster mushrooms into strips.

**Medium bowl — finish:**
- Slice spring onion / scallion greens.
- Chop coriander / cilantro.
- Slice optional chili.

**By the hob (no bowl):** garlic-infused oil, chicken broth, coconut milk, fish sauce, lime juice, sugar or maple syrup.

**1. Aromatics:** Warm oil over medium. Stir the aromatics bowl about 1 minute until fragrant. Add broth; gentle simmer 10 minutes to infuse.

**2. Coconut & chicken:** Lower heat; stir in coconut milk (bare simmer — avoid hard boil). Add the chicken bowl; cook 5–7 minutes to 75°C. Add the mushrooms bowl; simmer 3–4 minutes until tender.

**3. Season:** Off heat, stir in fish sauce, lime juice and sugar. Taste for sour–salty–sweet balance. Lift out aromatics if preferred. Serve topped from the finish bowl.

Cool, portion, refrigerate up to 3 days; reheat gently until piping hot.
""",
    ),
    # 30 Carrot Courgette Soup
    "6AF1CFD7-44E0-4E6F-AB3A-45BD9B146892": (
        """60 g courgette (1 medium courgette)  🔴 Fructans 91%
1/2 carrots
1/4 red bell pepper  🔴 Fructose 93%
1/8 tsp ground paprika
Optional: 1/2 tsp turmeric
Pepper and salt
Fresh parsley
125 ml boiled water
1/4 low FODMAP stock cube
55 ml coconut milk  🔴 Sorbitol 92%
""",
        """**Prep (bowls):**

**Large bowl — veg (fry / soften):**
- Dice the courgette, carrot and red bell pepper.

**Small bowl — spice mix (finish / season):**
- Mix the ground paprika and optional turmeric.
- Have salt and pepper ready.

**Medium bowl — finish:**
- Chop the fresh parsley.
- Set aside a little coconut milk for swirling if desired.

**By the hob (no bowl):** oil for frying, boiled water, stock cube, coconut milk.

**1. Soften veg:** Heat oil over medium. Fry the veg bowl about 7 minutes until cooked but not browned.

**2. Simmer:** Add water, stock cube and coconut milk. Simmer low about 20 minutes until vegetables are soft.

**3. Blend:** Blitz smooth. Season with the spice mix, salt and pepper.

**4. Finish:** Serve with a swirl of coconut milk and parsley from the finish bowl.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
    ),
    # 31 LF Chicken Noodle Soup
    "FBE9CD9C-B349-4678-9B34-F6CFCC29E215": (
        """3/5 medium carrots, peeled and sliced
1/5 medium stalk celery, diced
2/5 cup brown rice pasta (I use elbow macaroni or fusilli)  🟠 Fructans 64%
2/5 cup diced or shredded cooked chicken
1/5 tsp fresh thyme
Optional garnish: Chopped fresh parsley
1 1/5 tsp [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)]
1 3/5 cup low FODMAP chicken broth (see note)
1/8 cup fresh lemon juice (about 1 lemon)
Salt and black pepper
1 1/5 tsp garlic-infused olive oil
""",
        """**Prep (bowls):**

**Large bowl — veg (soften):**
- Peel and slice the carrots.
- Dice the celery.

**Medium bowl — pasta (simmer later):**
- Have the brown rice pasta ready.

**Medium bowl — chicken (warm later):**
- Dice or shred the cooked chicken.

**Small bowl — herbs:**
- Have the fresh thyme ready.
- Chop optional parsley for garnish.

**By the hob (no bowl):** garlic-infused oil, chicken broth, lemon juice, salt, pepper.

**1. Soften veg:** Heat oil in a Dutch oven over medium to medium-high. Sauté the veg bowl about 5 minutes, stirring occasionally.

**2. Pasta:** Stir in broth, thyme and the pasta bowl. Cover, bring to a boil, then simmer about 10 minutes until pasta is cooked (timing varies by shape).

**3. Chicken:** Add the chicken bowl and lemon juice; warm through. Season with salt and pepper.

**4. Finish:** Serve warm topped with parsley from the herbs bowl if using.

Cool, portion, refrigerate up to 3 days; reheat until piping hot.
""",
    ),
    # 32 minestrone
    "7BB9ECFA-4F1A-4102-981C-4D1CAE4840B1": (
        """15 g carrot, in slices
19 g courgette*  🟡 Fructans 29%
1/16 leek, only the green part  🟡 Fructans 36%
3/4 stalks of spring onion, only the green part
1 medium roma tomatoes*
18 g tomato paste  🟠 Fructose 64%
1/4 bay leaf
1/4 tsp Italian herb mix (make sure it is low FODMAP)
1/8 tsp pepper
1/8 tsp salt
1 sprigs of thyme
1/4 handful of fresh basil
9.7 g gluten-free pasta  🟢 Fructans 6%
19 g zucchini*  🟡 Fructans 29%
A handful of fresh basil
0.38 l water
3/4 low FODMAP stock cubes
""",
        """**Prep (bowls):**

**Large bowl — veg (fry / soften):**
- Peel and slice the carrot.
- Cube the courgette / zucchini and roma tomato.
- Slice leek greens and spring onion greens into rings.

**Small bowl — spice mix (add later):**
- Mix the Italian herb mix, pepper and salt.
- Set out bay leaf; strip thyme leaves.
- Keep basil stems for the pot and leaves for garnish.

**Medium bowl — pasta (add later):**
- Have the gluten-free pasta ready.

**Medium bowl — finish:**
- Tear or chop remaining fresh basil leaves.

**By the hob (no bowl):** olive oil, water, stock cubes, tomato paste.

**1. Soften veg:** Heat oil in a soup pan. Fry the veg bowl a few minutes on low heat.

**2. Simmer:** Add tomato paste, water, stock cubes, bay, Italian herbs, thyme leaves and basil stems. Bring to a boil, stirring. Cover and simmer about 10 minutes until vegetables soften.

**3. Pasta:** Add the pasta bowl; boil about 10 minutes more. Taste salt and pepper.

**4. Finish:** Serve with basil from the finish bowl (and low FODMAP bread if liked).

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
    ),
    # 33 potato soup
    "82A48D47-3D1C-4061-A17B-06453DD0DF8F": (
        """70 g potatoes
14 g carrots
1/8 leek, the green part only  🟠 Fructans 71%
3.5 g gluten-free flour  🟢 Fructans 7%
4.9 g grated cheddar cheese
1/2 stalks of spring onion, the green part only
Optional: lactose-free sour cream or crème fraîche for garnish
6.2 g butter
188 ml low FODMAP stock
51 ml lactose-free milk
""",
        """**Prep (bowls):**

**Large bowl — veg (simmer):**
- Peel and cube the potatoes and carrots.
- Slice the leek greens into rings.

**Small bowl — flour (roux):**
- Have the gluten-free flour ready.

**Medium bowl — finish:**
- Grate the cheddar.
- Slice spring onion greens into rings.
- Set out optional lactose-free sour cream or crème fraîche.

**By the hob (no bowl):** butter, stock, lactose-free milk, salt, pepper.

**1. Roux:** Melt butter in a soup pan. Stir in the flour bowl; cook about 1 minute.

**2. Liquids & veg:** Whisk in the stock, then the milk. Add the veg bowl. Bring close to a boil, then simmer medium-low about 15 minutes, stirring now and then.

**3. Finish:** Season with salt and pepper. Optionally stir in cheddar from the finish bowl. Serve with crème fraîche and spring onion greens from the finish bowl.

Cool, portion, refrigerate up to 3 days; reheat gently until piping hot.
""",
    ),
}


async def apply_all() -> None:
    user, pw = paprika_credentials()
    limiter = RateLimiter(0.35)
    results = []

    async with aiohttp.ClientSession() as s:
        st, body = await api_json(
            s,
            limiter,
            "POST",
            f"{PAPRIKA_API}/v1/account/login",
            data={"email": user, "password": pw},
        )
        if st != 200:
            raise SystemExit(f"login failed: {st}")
        H = {"Authorization": f"Bearer {body['result']['token']}"}

        con = sqlite3.connect(LOCAL_DB)
        for uid, (ings, dirs) in UPDATES.items():
            name_row = con.execute(
                "SELECT name FROM recipes WHERE uid=?", (uid,)
            ).fetchone()
            name = name_row[0] if name_row else uid

            if "Prep (bowls):" in (
                con.execute(
                    "SELECT directions FROM recipes WHERE uid=?", (uid,)
                ).fetchone()
                or [""]
            )[0]:
                # still re-apply if already partially done — overwrite with our version
                pass

            st, body = await api_json(
                s, limiter, "GET", f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=H
            )
            cloud = (body or {}).get("result") or {}
            if not cloud.get("uid"):
                results.append((uid, name, "skipped", "cloud missing"))
                safe_print(f"SKIP {name}: cloud missing")
                continue

            # skip only if already has our prep style AND ingredients match? Always apply.
            cloud["ingredients"] = ings.strip() + "\n"
            cloud["directions"] = dirs.strip() + "\n"
            cloud["hash"] = calc_hash(cloud)

            await limiter.wait_turn()
            form = aiohttp.FormData()
            form.add_field(
                "data",
                gzip_obj(cloud),
                content_type="application/octet-stream",
                filename="data",
            )
            async with s.post(
                f"{PAPRIKA_API}/v2/sync/recipe/{uid}/", headers=H, data=form
            ) as r:
                text = await r.text()
                ok = '"result":true' in text.replace(" ", "")

            if not ok:
                results.append((uid, name, "failed", f"post {r.status}"))
                safe_print(f"FAIL {name}: post {r.status}")
                continue

            con.execute(
                "UPDATE recipes SET ingredients=?, directions=?, status=? WHERE uid=?",
                (ings.strip() + "\n", dirs.strip() + "\n", "modified", uid),
            )
            con.commit()
            results.append((uid, name, "OK", ""))
            safe_print(f"OK {name}")

        await limiter.wait_turn()
        async with s.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=H) as r:
            await r.text()
            safe_print(f"notify: {r.status}")

        con.close()

    out = Path(__file__).resolve().parent / ".soups_prep_17_33_results.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    safe_print(f"wrote {out}")


if __name__ == "__main__":
    asyncio.run(apply_all())

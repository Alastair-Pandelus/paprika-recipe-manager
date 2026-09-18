"""Apply Prep (bowls) restyle to soups queue indices 0-16."""
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


def calc_hash(obj: dict) -> str:
    data = {k: v for k, v in obj.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def gzip_obj(obj: dict) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode())


def update_local(uid: str, directions: str, ingredients: str) -> None:
    con = sqlite3.connect(LOCAL_DB)
    con.execute(
        "UPDATE recipes SET directions=?, ingredients=?, status=? WHERE uid=?",
        (directions, ingredients, "modified", uid),
    )
    con.commit()
    con.close()


# uid -> (ingredients, directions)
REWRITES: dict[str, tuple[str, str]] = {}

# ---------------------------------------------------------------------------
# 0 Light & Fresh Vegetable Detox Soup
# ---------------------------------------------------------------------------
REWRITES["505AA24F-8968-43D9-B85A-48D4E74A6BC4"] = (
    """1/3 yellow or orange bell peppers (finely diced)  🔴 Fructose 124%
1/16 carrots (finely diced)
1/16 spring onions or spring onions  🟠 Fructans 69%
1/2 medium courgette
1/2 medium summer squash
1/2 tsp sweet paprika
1/2 tsp ground turmeric
1/2 tbsp olive or coconut oil  🟢 Fructose 25%
77 g can unsweetened coconut cream (optional, see note)  🔴 Lactose 192%
153 g can unsweetened coconut cream (optional, see note)  🔴 Lactose 382%
1/4 quart vegetable or chicken stock
1/2 tsp sea salt
1/2 tbsp lime juice
1/8 cup roughly chopped fresh coriander
""",
    """**Prep (bowls):**

**Large bowl — veg (fry / soften):**
- Finely dice the bell peppers and carrots.
- Slice the spring onions.
- Dice the courgette and summer squash.

**Small bowl — spice mix (fry):**
- Mix the sweet paprika and ground turmeric.

**Medium bowl — finish:**
- Roughly chop the fresh coriander.

**By the hob (no bowl):** olive or coconut oil, coconut cream, stock, sea salt, lime juice.

**1. Soften veg:** Heat the oil in a large Dutch oven or saucepan over medium-high heat. Add the bell pepper, carrot and spring onion from the veg bowl. Sauté 7–10 minutes until starting to brown. Stir in the courgette, summer squash and the spice mix. Cook about 4 minutes more until fragrant and softening.

**2. Simmer:** Pour in the coconut cream (if using), stock and salt, scraping up any brown bits. Simmer rapidly over medium-high heat about 5 minutes until the squash is fork tender but not mushy. Remove from the heat and stir in the lime juice and half the coriander from the finish bowl.

**3. Blend & serve:** With an immersion blender or stand blender, puree about half the soup so it stays chowder-like with plenty of chunks. Garnish with the remaining coriander from the finish bowl.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
)

# ---------------------------------------------------------------------------
# 1 Vegan Lasagna Soup
# ---------------------------------------------------------------------------
REWRITES["0E085F39-B2F3-43E8-8EC5-79AF2651C3F9"] = (
    """1/4 package extra firm tofu
3/4 tbsp tomato paste  🟡 Fructose 44%
1/8 cup tamari or soy sauce (divided, GF if needed)
1 green onions (dark green tops only, or sub garlic and onions)
57 g sliced mushrooms (NOT low FODMAP!)  🔴 Mannitol 121%
1/2 tsp dried thyme
1/4 tsp dried oregano
1/4 tsp dried basil
1/4 tsp paprika
1/8 tsp ground cumin
1/2 tsp cane sugar (don&#39;t skip it!)
1/8 tsp salt (plus more for seasoning)
1/8 tsp dried chile flakes (optional, for some heat!)
1 1/4 dried lasagna noodles
3/4 tbsp extra virgin olive oil
1 cup water
100 g can diced tomatoes  🔴 Fructose 133%
1/2 tbsp nutritional yeast
1 handfuls baby spinach
1/8 cup cashew cream (or store-bought vegan cream)  🟠 Lactose 75%
vegan parmesan (homemade or store-bought)
fresh basil
fresh basil (thinly sliced)
""",
    """**Prep (bowls):**

**Large bowl — tofu (fry / brown):**
- Blot the tofu with paper towel and crumble.
- Mix in the tomato paste and part of the tamari with your hands.

**Medium bowl — aromatics (fry):**
- Slice the green onion tops.
- Slice the mushrooms if using.

**Small bowl — spice mix (fry):**
- Mix the dried thyme, oregano, basil, paprika, ground cumin, cane sugar, salt and optional chile flakes.

**Large bowl — noodles (add later):**
- Break the lasagna noodles into soup-friendly pieces if needed.

**Medium bowl — finish:**
- Have the baby spinach ready.
- Set out the nutritional yeast, cashew cream, vegan parmesan and fresh basil.

**By the hob (no bowl):** olive oil, water, diced tomatoes, remaining tamari.

**1. Brown tofu:** Heat the oil in a soup pot over medium heat. If using onions or mushrooms, cook the aromatics bowl until glossy and browned, 7–10 minutes; season, scrape into a bowl and set aside. For the written recipe, add the tofu bowl, spread evenly, sprinkle the green onions on top and cook without stirring about 7 minutes until crisp and browned in spots.

**2. Build soup:** Add the water, diced tomatoes, remaining tamari and the spice mix. Scrape the pan well, raise the heat and bring to a boil.

**3. Cook noodles:** Add the noodles bowl and simmer on medium until al dente, about 15 minutes. If the soup reduces too much, add a splash of water. Stir in the nutritional yeast, wilt the spinach from the finish bowl, and add any reserved aromatics. Serve with cashew cream, vegan parmesan and basil from the finish bowl.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
)

# ---------------------------------------------------------------------------
# 2 Creamy Mustard Soup
# ---------------------------------------------------------------------------
REWRITES["0D88FB79-2915-4E2F-B2C3-F2CBA1B8E207"] = (
    """25 g bacon (check the ingredients to make sure they are low FODMAP)
12 g butter
15 g gluten-free flour  🟡 Fructans 30%
0.34 l stock
50 ml rice cream (I use the rice cream from Alpro Soya)  🔴 Lactose 125%
5/8 tbsp mustard
Pepper and salt
""",
    """**Prep (bowls):**

**Medium bowl — bacon (fry):**
- Have the bacon ready to fry (cut into pieces if needed).

**By the hob (no bowl):** butter, gluten-free flour, stock, rice cream, mustard, pepper and salt.

**1. Make roux:** Melt the butter in a pan. Stir in the flour to make a roux. Cook on low heat about 3 minutes, stirring now and then so it does not burn.

**2. Simmer soup:** Carefully pour in the stock. Whisk to remove lumps. Lower the heat and simmer about 10 minutes.

**3. Crisp bacon:** Meanwhile, fry the bacon bowl until crisp.

**4. Finish:** Stir the mustard into the soup; taste and adjust mustard, pepper and salt. Add the rice cream and heat through. Serve topped with the bacon.

Cool, portion, refrigerate up to 3 days; reheat until piping hot.
""",
)

# ---------------------------------------------------------------------------
# 3 Broccoli cheddar soup
# ---------------------------------------------------------------------------
REWRITES["F7A50AE7-961F-49AD-854D-70153090EB9D"] = (
    """49 g broccoli florets  🔴 Fructans 109%
16 g carrot julienne
1/4 tsp mustard
1/8 tsp pepper
1/8 tsp salt
1 tbsp butter
4.9 g gluten-free flour  🟢 Fructans 10%
47 ml lactose-free milk (cow’s milk or almond milk)
125 ml low FODMAP stock (see here for an overview of low FODMAP stock cubes)
9.3 g grated cheddar cheese
""",
    """**Prep (bowls):**

**Large bowl — veg (simmer):**
- Have the broccoli florets ready.
- Have the carrot julienne ready.

**Small bowl — seasoning:**
- Mix the mustard, pepper and salt (or keep mustard beside the bowl).

**Medium bowl — finish:**
- Have the grated cheddar ready.

**By the hob (no bowl):** butter, gluten-free flour, lactose-free milk, stock.

**1. Make roux:** Melt the butter in a soup pan. Whisk in the flour for 1–2 minutes.

**2. Build base:** Pour in the milk little by little while whisking so no lumps form. Stir in the stock.

**3. Simmer veg:** Add the veg bowl and the seasoning. Cook 15–20 minutes until the broccoli has softened. Optionally puree now if you prefer a smooth soup.

**4. Finish:** Stir in the cheddar from the finish bowl until melted. Taste and adjust salt or pepper. Serve.

Cool, portion, refrigerate up to 3 days; reheat until piping hot.
""",
)

# ---------------------------------------------------------------------------
# 4 Broccoli soup
# ---------------------------------------------------------------------------
REWRITES["8820B856-9A1E-44F4-8C85-1C491D2088E3"] = (
    """63 g broccoli heads  🔴 Fructans 140%
1/8 red chili pepper
1/2 stalks of spring onion, the green part
Optional: fried bacon for serving
0.25 l stock (use a low FODMAP stock cube or stock powder)
A splash of coconut milk for cooking (or lactose-free crème fraîche, the latter is not vegan)
Salt and pepper to taste
""",
    """**Prep (bowls):**

**Large bowl — broccoli (simmer):**
- Cut the heads off the broccoli; use only the florets, not the stalks.

**Small bowl — chilli (fry):**
- Finely chop the red chilli and remove the seeds.

**Medium bowl — finish:**
- Slice the green spring onion into rings.
- Have optional fried bacon ready for topping.

**By the hob (no bowl):** oil for frying, stock, water as needed, coconut milk or lactose-free crème fraîche, salt and pepper.

**1. Fry chilli:** Heat a little oil in a soup pan and fry the chilli bowl 2–3 minutes on low heat.

**2. Simmer:** Add water and the broccoli bowl with the stock. Bring to a boil, then simmer 10–15 minutes.

**3. Blend:** Blitz smooth with an immersion blender. Season with salt and pepper.

**4. Serve:** Ladle into bowls. Add a splash of coconut milk (or crème fraîche). Garnish with spring onion from the finish bowl and optional bacon.

Cool, portion, refrigerate up to 3 days; reheat until piping hot.
""",
)

# ---------------------------------------------------------------------------
# 5 Carrot and Tomato Soup
# ---------------------------------------------------------------------------
REWRITES["2232DE0B-AA2A-4442-A68F-0871217ADA27"] = (
    """1 medium carrots, peeled and cut into 1-inch chunks
3/4 tsp . ground turmeric
1/4 tsp . ground coriander
1/4 tsp . ground cumin
1/4 can diced tomatoes
1/4 cup canned full-fat coconut milk  🔴 Sorbitol 100%
1/8 cup maple syrup, divided
1 tsp . coconut milk, divided  🟢 Sorbitol 8%
1 tsp . sesame seeds, divided
Fresh coriander, chopped
Fresh cilantro, chopped
""",
    """**Prep (bowls):**

**Large bowl — carrots (slow cook):**
- Peel the carrots and cut into chunks.

**Small bowl — spice mix:**
- Mix the ground turmeric, ground coriander and ground cumin.

**Medium bowl — finish:**
- Chop the fresh coriander / cilantro.
- Set out the sesame seeds and reserved maple syrup and coconut milk for serving.

**By the hob (no bowl):** diced tomatoes, full-fat coconut milk, maple syrup (for the pot).

**1. Slow cook:** Place the carrots bowl, diced tomatoes, coconut milk and spice mix in a slow cooker. Cover and cook on low about 4 hours until the carrots are tender.

**2. Blend & serve:** Blend smooth with an immersion blender (or carefully in batches). Serve warm topped with maple syrup, coconut milk, sesame seeds and herbs from the finish bowl.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
)

# ---------------------------------------------------------------------------
# 6 Carrot soup with coconut milk
# ---------------------------------------------------------------------------
REWRITES["B48DC715-6239-4803-B37B-EA4E09FA0662"] = (
    """0.105 kg carrots
1/4 piece of fresh ginger
A piece of fresh ginger (about 5 cm)
0.25 l stock (choose a low FODMAP stock cube)
80 ml canned coconut milk*  🔴 Sorbitol 133%
Pepper and salt
""",
    """**Prep (bowls):**

**Large bowl — veg (fry / soften):**
- Peel the carrots and cut into small pieces.
- Peel and finely chop or grate the ginger.

**By the hob (no bowl):** oil for frying, stock, coconut milk, pepper and salt.

**1. Soften veg:** Heat a little oil in a soup pan. Add the veg bowl with a pinch of salt and fry about 5 minutes, stirring now and then.

**2. Simmer:** Add the stock, bring to a boil and cook on medium heat about 30 minutes.

**3. Finish:** Stir in most of the coconut milk, reserving a little for garnish. Simmer about 5 minutes more. Season with pepper and salt. Puree smooth with a stick blender, simmer briefly, and serve garnished with the reserved coconut milk.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
)

# ---------------------------------------------------------------------------
# 7 Chicken Enchilada Soup
# ---------------------------------------------------------------------------
REWRITES["A24D7142-167D-4C93-997D-066B55E0294D"] = (
    """113 g boneless, skinless chicken breasts
1/8 cup chopped leek leaves (green parts only)  🔴 Fructans 107%
75 g drained, canned tomatillos
1/5 jalapeño, halved and seeds removed (optional)
1/4 tsp ground cumin
1/8 cup finely chopped fresh coriander (optional)
1/2 tbsp [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)]
1/2 tbsp garlic-infused olive oil
1 cup low FODMAP chicken broth
1 1/8 tsp lime juice
Salt and pepper
""",
    """**Prep (bowls):**

**Large bowl — chicken (simmer / shred):**
- Have the chicken breasts ready.

**Medium bowl — aromatics (sauté / blend):**
- Chop the green leek leaves.
- Have the drained tomatillos ready.
- Halve and deseed the optional jalapeño.

**Small bowl — spice:**
- Have the ground cumin ready.

**Medium bowl — finish:**
- Finely chop the optional fresh coriander.

**By the hob (no bowl):** garlic-infused olive oil, chicken broth, lime juice, salt and pepper, splash of water for blending.

**Slow cooker**

**1. Blend base:** Heat the oil over medium heat and sauté the leek from the aromatics bowl until bright green, fragrant and soft. Blend with the tomatillos, optional jalapeño, cumin and a splash of water until smooth.

**2. Slow cook:** Pour the mixture into a slow cooker with the chicken broth. Add the chicken bowl. Cover and cook 6–8 hours on low or 3–4 hours on high.

**3. Finish:** Remove the chicken with a slotted spoon, shred, and return to the pot. Stir in the lime juice and optional coriander from the finish bowl. Season with salt and pepper. Serve warm with optional garnishes.

**Instant Pot**

**1. Sauté & pressure cook:** On Sauté, heat the oil and cook the leek until soft. Cancel Sauté. Add tomatillos, optional jalapeño, cumin, broth and the chicken bowl. Seal, Soup setting, 30 minutes High Pressure; natural release 20 minutes, then vent.

**2. Finish:** Remove and shred the chicken. Blend the remaining liquid smooth with an immersion blender. Return the chicken, add lime juice and optional coriander from the finish bowl, and season. Serve warm.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
)

# ---------------------------------------------------------------------------
# 8 Chicken noodle soup
# ---------------------------------------------------------------------------
REWRITES["1167B5C0-7F5F-4AE4-A9E1-4077C327D1C0"] = (
    """67 g chicken thigh fillet
1/3 medium carrots
The green part of 6 stalks of spring onions
1/6 red pepper  🟠 Fructose 62%
1/2 tsp fresh ginger
67 g mix of oyster mushrooms and mushrooms from a jar (or one of both)  🔴 Mannitol 143%
25 g brown rice noodles  🟢 Fructans 14%
Optional: 3 boiled eggs, halved
0.42 l stock (use low FODMAP stock cubes)
1 tsp soy sauce*
A slow cooker
A slow cooker (I use the Crock Pot Express 6 Quart)
""",
    """**Prep (bowls):**

**Large bowl — chicken (slow cook):**
- Have the chicken thigh fillet ready.

**Large bowl — veg (slow cook):**
- Slice the carrots.
- Cut the red pepper and most of the spring onion greens into rings; reserve a handful of spring onion for garnish.
- Finely chop or grate the fresh ginger.

**Medium bowl — mushrooms (add later):**
- Clean and cut the oyster mushrooms.
- Rinse jarred mushrooms well and drain.

**Large bowl — noodles (add later):**
- Have the brown rice noodles ready.

**Medium bowl — finish:**
- Set aside the reserved spring onion rings.
- Have optional boiled eggs ready, halved.

**By the hob (no bowl):** stock, soy sauce.

**1. Load slow cooker:** Place the chicken bowl in the slow cooker. Add the veg bowl, soy sauce and stock; stir.

**2. Cook:** Cook on high for 2 hours, then on low for another 4 hours.

**3. Shred chicken:** Turn off the slow cooker. Pull the chicken apart with two forks and return it to the pot. Taste and season with salt, pepper or a little extra soy sauce.

**4. Add mushrooms & noodles:** Stir in the mushrooms bowl and the noodles bowl. Cover and cook on low about 30 minutes more.

**5. Serve:** Ladle into bowls. Top with reserved spring onion from the finish bowl and optional boiled egg.

Cool, portion, refrigerate up to 3 days; reheat until piping hot.
""",
)

# ---------------------------------------------------------------------------
# 9 Chicken soup
# ---------------------------------------------------------------------------
REWRITES["3E930156-11AE-451E-BF92-69AE7D1A1EF4"] = (
    """188 g chicken thigh with bones
33 g carrot
50 g canned mushrooms  🔴 Mannitol 106%
1/8 leek, the green part  🟠 Fructans 71%
1/8 bush of spring onion, the green part
3/4 tsp fresh parsley
3/4 tsp fresh thyme
1/4 pepper
1/4 piece of mace
1/4 bay leaf
Optional: 50 g rice vermicelli noodles
Optional: 50 g rice vermicelli noodles
0.5 l water + low FODMAP stock powder
3/4 tsp lemon juice
1/4 salt
""",
    """**Prep (bowls):**

**Large bowl — chicken (simmer):**
- Have the chicken thighs with bones ready.

**Large bowl — veg (simmer):**
- Peel and dice the carrot.
- Drain and rinse the canned mushrooms.
- Slice the green leek and spring onion into rings.
- Finely chop the parsley and strip the thyme leaves.

**Small bowl — aromatics:**
- Have the pepper, mace and bay leaf ready.

**Large bowl — noodles (optional, add later):**
- Have the rice vermicelli ready if using.

**By the hob (no bowl):** water, low FODMAP stock powder, lemon juice, salt.

**1. Build pot:** Place the chicken bowl in a large soup pot. Add the veg bowl and the aromatics bowl. Cover with water and add the stock powder.

**2. Simmer:** Bring to a boil, reduce to low, lid ajar, and simmer about 2 hours.

**3. Strain & shred:** Remove the chicken. Strain the broth into another pan; discard the bay leaf and mace, and return the vegetables to the broth. Strip the meat from the bones, discard bones and skin, and shred the meat.

**4. Finish:** Return the chicken to the broth and reheat. If using noodles, add the noodles bowl and cook briefly. Season with lemon juice, salt and a little extra pepper if needed.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
)

# ---------------------------------------------------------------------------
# 10 Creamy mushroom soup
# ---------------------------------------------------------------------------
REWRITES["F3B76AA1-A309-4589-96CA-787F6135A52B"] = (
    """39 g oyster mushrooms  🔴 Mannitol 83%
26 g canned mushrooms  🟠 Mannitol 55%
3/4 stalks of spring onion, the green part only
Fresh oregano
Fresh parsley
12 g lactose-free margarine
12 g gluten-free flour  🟢 Fructans 24%
1/2 low FODMAP stock cubes
0.25 l water
38 ml vegan cooking cream (I used Oatly oat cuisine)  🔴 Lactose 95%
1/8 tsp pepper
1/8 tsp salt
""",
    """**Prep (bowls):**

**Large bowl — mushrooms (fry):**
- Clean and cut the oyster mushrooms.
- Drain, rinse and drain the canned mushrooms well.

**Medium bowl — finish:**
- Slice the green spring onion.
- Chop the fresh oregano and parsley if using as garnish.

**By the hob (no bowl):** lactose-free margarine, gluten-free flour, stock cubes, water, vegan cooking cream, pepper and salt.

**1. Fry mushrooms:** Heat a quarter of the margarine in a soup pan and fry the mushrooms bowl about 5 minutes.

**2. Roux:** Add the remaining margarine and melt. Stir in the flour and fry 1 minute.

**3. Simmer:** Dissolve the stock cubes in hot water, then add to the pan, whisking to avoid lumps. Stir in the vegan cooking cream and simmer on low about 10 minutes.

**4. Season & serve:** Taste and season with salt and pepper. Serve garnished with spring onion and herbs from the finish bowl.

Cool, portion, refrigerate up to 3 days; reheat until piping hot.
""",
)

# ---------------------------------------------------------------------------
# 11 Goulash soup
# ---------------------------------------------------------------------------
REWRITES["720CF87D-3D99-4A02-A29B-FBBBE1E70BFC"] = (
    """38 g bacon strips
200 g beef stew meat
1/4 green bell pepper*  🔴 Fructose 93%
50 g green beans, in pieces
1/4 tbsp tomato paste  🟢 Fructose 15%
1/4 tsp ground paprika
1/8 tsp ground cumin
100 ml red wine
0.5 l stock (make sure to use low FODMAP stock cubes)
Pepper and salt
Optional: 2 tbsp lactose-free cream cheese or sour cream
""",
    """**Prep (bowls):**

**Medium bowl — bacon (fry):**
- Have the bacon strips ready.

**Large bowl — beef (fry / brown):**
- Cut the beef stew meat into cubes.

**Medium bowl — veg (fry):**
- Dice the green bell pepper.

**Medium bowl — green beans (add later):**
- Have the green beans cut into pieces.

**Small bowl — spice mix (fry):**
- Mix the tomato paste, ground paprika and ground cumin (keep tomato paste beside the spices if preferred).

**By the hob (no bowl):** red wine, stock, pepper and salt, optional lactose-free cream cheese or sour cream.

**1. Brown meat:** Fry the bacon bowl a few minutes in a soup pan. Add the beef bowl and brown a few minutes more.

**2. Soften & bloom:** Add the veg bowl and the spice mix (tomato paste, paprika, cumin). Fry about 2 minutes.

**3. Braise:** Add the stock and red wine. Bring to a boil, cover and simmer about 2 hours.

**4. Finish:** Add the green beans bowl and cook about 20 minutes more. Season with pepper and salt. Serve with a scoop of optional cream cheese or sour cream.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
)

# ---------------------------------------------------------------------------
# 12 Miso soup
# ---------------------------------------------------------------------------
REWRITES["D59EC691-4C4C-462B-AEC1-C07936FE1C66"] = (
    """50 g tofu  🟡 Galacto-oligosaccharides 29%
66 g oyster mushrooms  🔴 Mannitol 140%
1/2 stalks of spring onion (only the green part)
20 g shiro miso paste
0.25 l water
""",
    """**Prep (bowls):**

**Medium bowl — tofu (simmer):**
- Drain the tofu well and cut into cubes.

**Medium bowl — mushrooms & spring onion (simmer):**
- Clean and cut the oyster mushrooms.
- Slice the green spring onion into rings.

**Small bowl — miso slurry:**
- Put the shiro miso in a small bowl; loosen with a little hot water when the soup is ready, until smooth.

**By the hob (no bowl):** water, pepper and salt if needed.

**1. Boil base:** Bring the water to a boil in a pan.

**2. Simmer add-ins:** Add the mushrooms and spring onion bowl and the tofu bowl. Boil about 10 minutes.

**3. Finish:** Stir in the miso slurry. Taste and season with pepper and salt if needed. Simmer about 5 minutes more and serve warm.

Cool, portion, refrigerate up to 2 days; reheat gently without a hard boil.
""",
)

# ---------------------------------------------------------------------------
# 13 Mushroom soup
# ---------------------------------------------------------------------------
REWRITES["4DC97D07-86E6-46A4-A291-1E32D90FA86B"] = (
    """39 g oyster mushrooms  🔴 Mannitol 83%
26 g canned mushrooms, sliced  🟠 Mannitol 55%
1/4 parsley
1/2 stalks spring onion (only the green part)
3/4 tsp truffle-infused olive oil
0.38 l water
3/4 low FODMAP stock cubes
A pinch of pepper and salt
""",
    """**Prep (bowls):**

**Large bowl — mushrooms (fry):**
- Clean and cut the oyster mushrooms.
- Drain and rinse the canned mushrooms well.

**Medium bowl — finish:**
- Chop the parsley.
- Slice the green spring onion into rings.

**By the hob (no bowl):** truffle-infused olive oil, water, stock cubes, pepper and salt.

**1. Fry mushrooms:** Heat the truffle oil in a pan. Add the mushrooms bowl and fry a few minutes.

**2. Simmer:** Add hot water and the stock cubes; stir until dissolved. Bring to a boil and cook about 10 minutes. Season with salt, pepper and some parsley from the finish bowl.

**3. Serve:** Ladle into bowls. Garnish with spring onion and extra parsley from the finish bowl.

Cool, portion, refrigerate up to 3 days; reheat until piping hot.
""",
)

# ---------------------------------------------------------------------------
# 14 Pumpkin noodle soup
# ---------------------------------------------------------------------------
REWRITES["DCDCC25B-CE12-4929-B88A-075AE4CEDA14"] = (
    """133 g (14.1) oz pumpkin in cubes
1/2 tsp fresh ginger
One stalk of spring onion
A small handful of fresh coriander
A small handful of fresh cilantro
2/3 tbsp tandoori spices (make sure no low FODMAP ingredients have been added. The mix that I used contained: paprika, coriander, salt, cumin, pepper, ginger, chilli, cinnamon and laurel)
1/6 tsp turmeric
1/6 tsp ground cloves
1/8 tsp cayenne pepper
50 g oyster mushrooms  🔴 Mannitol 106%
50 g gluten-free noodles  🟡 Fructans 33%
Fresh basil
Unsalted peanuts
A splash of lemon juice
67 ml coconut milk  🔴 Sorbitol 112%
250 ml stock (use a low FODMAP stock cube)
1/3 tsp fish sauce (leave this out to make the recipe vegan)
1 tsp brown sugar
""",
    """**Prep (bowls):**

**Large bowl — pumpkin (boil / puree):**
- Have the pumpkin cubes ready.

**Small bowl — spice paste (fry):**
- Finely chop the ginger, coriander and spring onion.
- Mix with the tandoori spices, turmeric, ground cloves, cayenne and a splash of lemon juice; blitz to a paste with a hand blender.

**Medium bowl — mushrooms (simmer):**
- Scrub the oyster mushrooms clean and cut into pieces.

**Large bowl — noodles (cook separately):**
- Have the gluten-free noodles ready.

**Medium bowl — finish:**
- Tear fresh basil and chop unsalted peanuts for serving.

**By the hob (no bowl):** oil for frying, stock, fish sauce (optional), brown sugar, coconut milk, salt.

**1. Pumpkin puree:** Boil the pumpkin bowl in water about 10 minutes. Drain well and blitz to a puree.

**2. Fry paste:** Heat a little oil in a soup pan and fry the spice paste about 2 minutes, stirring. Add the pumpkin puree, optional fish sauce and part of the stock.

**3. Simmer mushrooms:** Add the mushrooms bowl with the brown sugar and a pinch of salt. Boil about 10 minutes.

**4. Cook noodles:** Meanwhile, boil the noodles per packet instructions; drain and rinse with cold water so they do not stick.

**5. Finish soup:** After about 10 minutes, add the remaining stock and the coconut milk; simmer about 5 minutes more. Stir in some basil from the finish bowl and the noodles. Rest off the heat, covered, about 5 minutes. Serve with extra basil and chopped peanuts from the finish bowl.

Cool, portion, refrigerate up to 3 days (store noodles separately if possible); reheat until piping hot.
""",
)

# ---------------------------------------------------------------------------
# 15 Roasted bell pepper soup
# ---------------------------------------------------------------------------
REWRITES["57CA0785-7312-4EB8-9C32-B79B1BB28D70"] = (
    """1/4 red bell peppers  🔴 Fructose 93%
1/2 tomatoes
1/4 tsp Italian herbs
7.3 g tomato paste  🟡 Fructose 26%
175 ml stock (make sure to use a low FODMAP stock cube / stock powder)
A splash of olive oil
Salt and pepper to taste
Optional: oat fraiche or another vegan cream as a topping
""",
    """**Prep (bowls):**

**Large bowl — peppers (roast):**
- Slice the red bell peppers and remove the seeds.

**Medium bowl — tomatoes (fry):**
- Have the tomatoes ready to cut after the peppers are roasted.

**Small bowl — herbs:**
- Have the Italian herbs ready.

**Medium bowl — finish:**
- Have optional oat fraîche or vegan cream ready for topping.

**By the hob (no bowl):** olive oil, stock, tomato paste, salt and pepper.

**1. Roast peppers:** Preheat the oven to 180°C. Spread the peppers bowl on a parchment-lined baking sheet. Roast 20–30 minutes until the skin has darkened. Cool briefly and peel.

**2. Soften veg:** Cut the roasted peppers and the tomatoes into pieces. Heat a little olive oil in a soup pan and fry them 2–3 minutes.

**3. Simmer:** Add the stock, tomato paste and Italian herbs. Bring to a boil and simmer about 20 minutes. Blend smooth with an immersion blender. Season with salt and pepper.

**4. Serve:** Top with optional cream from the finish bowl.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
)

# ---------------------------------------------------------------------------
# 16 Roasted Tomato Soup
# ---------------------------------------------------------------------------
REWRITES["C41C8249-488A-4277-AC0C-00A665967538"] = (
    """105 g canned whole tomatoes  🔴 Fructose 140%
105 g carrots
1 garlic cloves (crushed)
1/4 medium shallot (roughly chopped)
1/16 tsp red pepper flakes  🟢 Fructose 1%
1/8 cup basil leaves
[recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)]
Olive oil
Sea salt
1/2 cup low FODMAP vegetable or chicken stock
""",
    """**Prep (bowls):**

**Large bowl — roast veg:**
- Drain the canned tomatoes in a sieve over a bowl; reserve the juices for the blender.
- Have the carrots ready (cut if needed).

**Small bowl — oil aromatics (infuse):**
- Crush the garlic clove.
- Roughly chop the shallot.
- Have the red pepper flakes ready.

**Medium bowl — finish:**
- Tear the basil leaves for garnish.

**By the hob (no bowl):** garlic-infused olive oil, olive oil, sea salt, reserved tomato juices, stock.

**1. Roast:** Preheat the oven to 220°C. Line a sheet pan with parchment. Place drained tomatoes on one side and carrots on the other. Season with salt, drizzle generously with garlic-infused oil, toss each side separately, and roast 30–40 minutes until tomatoes are charred and carrots are caramelized and tender.

**2. Infuse oil:** Meanwhile, gently heat garlic-infused oil with the aromatics bowl over medium until sizzling, then low about 5 minutes until the shallot is translucent. Strain, press the solids, discard solids and keep the oil.

**3. Blend:** Add reserved tomato juices to a blender. Add the roasted tomatoes and carrots, stock, salt and some of the infused oil. Puree until smooth, adding more stock if needed.

**4. Serve:** Taste and adjust salt. Ladle into bowls and garnish with a drizzle of the oil, extra red pepper if desired, and basil from the finish bowl.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
)


async def main() -> None:
    queue = json.loads(QUEUE.read_text(encoding="utf-8"))[:17]
    user, pw = paprika_credentials()
    limiter = RateLimiter(0.35)
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

        for i, item in enumerate(queue):
            uid = item["uid"]
            name = item["name"]
            if uid not in REWRITES:
                results.append({"uid": uid, "name": name, "status": "skipped", "why": "no rewrite"})
                safe_print(f"{i:2d} SKIP no rewrite | {name}")
                continue

            new_ings, new_dirs = REWRITES[uid]
            st, body = await api_json(
                session,
                limiter,
                "GET",
                f"{PAPRIKA_API}/v2/sync/recipe/{uid}/",
                headers=headers,
            )
            rec = (body or {}).get("result") or {}
            if not rec.get("uid"):
                results.append({"uid": uid, "name": name, "status": "failed", "why": "cloud missing"})
                safe_print(f"{i:2d} FAIL missing | {name}")
                continue

            if "Prep (bowls)" in (rec.get("directions") or ""):
                results.append({"uid": uid, "name": rec.get("name") or name, "status": "skipped", "why": "already has Prep (bowls)"})
                safe_print(f"{i:2d} SKIP already prep | {rec.get('name') or name}")
                continue

            rec["directions"] = new_dirs
            rec["ingredients"] = new_ings
            rec["hash"] = calc_hash(rec)

            await limiter.wait_turn()
            form = aiohttp.FormData()
            form.add_field(
                "data",
                gzip_obj(rec),
                content_type="application/octet-stream",
                filename="data",
            )
            async with session.post(
                f"{PAPRIKA_API}/v2/sync/recipe/{uid}/",
                headers=headers,
                data=form,
            ) as r:
                ok = '"result":true' in (await r.text()).replace(" ", "")

            if ok:
                update_local(uid, new_dirs, new_ings)
                results.append({"uid": uid, "name": rec.get("name") or name, "status": "ok"})
                safe_print(f"{i:2d} OK | {rec.get('name') or name}")
            else:
                results.append({"uid": uid, "name": rec.get("name") or name, "status": "failed", "why": "POST rejected"})
                safe_print(f"{i:2d} FAIL post | {rec.get('name') or name}")

        await limiter.wait_turn()
        async with session.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            await r.text()

    out = Path(__file__).resolve().parent / ".soups_prep_batch0_results.json"
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    ok_n = sum(1 for r in results if r["status"] == "ok")
    safe_print(f"Done: {ok_n}/{len(results)} applied")


if __name__ == "__main__":
    asyncio.run(main())

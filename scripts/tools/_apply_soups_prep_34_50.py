"""Apply Prep (bowls) restyle to soups queue indices 34–50."""
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

TRANSFORMS: dict[str, dict[str, str]] = {
    "A5530A26-A8D8-4F9C-ADAA-B6E0AE0E182F": {
        "ingredients": """38 g tomato, in cubes  🟠 Fructose 51%
1/4 medium potato
38 g carrots, sliced
1/4 tsp paprika powder
1/8 tsp chili powder
1/8 tsp salt
3/4 tsp tomato puree  🟢 Fructose 15%
3/4 tsp Turkish pepper puree
45 g canned lentils, drained and rinsed  🔴 Galacto-oligosaccharides 98%
3/4 tsp [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)]
3/4 tsp garlic-infused olive oil
0.38 l stock (made with low FODMAP stock cubes)
Optional: fresh mint to serve
Optional: chili flakes (pul biber) to serve
Optional: lemon wedges to serve
""",
        "directions": """**Prep (bowls):**

**Large bowl — veg (fry / soften):**
- Blanch and peel the tomatoes, then cut into pieces.
- Peel and cube the potato and carrots.

**Small bowl — spice mix (fry):**
- Mix the paprika powder, chili powder and salt.
- Have the tomato puree and Turkish pepper puree ready beside this bowl.

**Medium bowl — lentils (simmer):**
- Drain and rinse the canned lentils.

**Medium bowl — finish:**
- Have the fresh mint, chili flakes and lemon wedges ready.

By the hob (no bowl): garlic-infused olive oil, stock.

**1. Soften veg:** Warm garlic-infused olive oil in a soup pan. Soften the veg bowl with the spice mix for a few minutes on low heat.

**2. Lentils:** Stir in the lentils bowl.

**3. Simmer:** Add the stock, tomato puree and Turkish pepper puree. Bring to the boil, lid ajar, and simmer on low heat for 40 minutes.

**4. Blend:** Puree with a stick blender. Taste salt and pepper; simmer 5 minutes more, then turn off the heat.

**5. Serve:** Serve with mint, chili flakes and lemon from the finish bowl.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
    },
    "6566090F-8185-4DEE-A2E3-7AEE39B2B881": {
        "ingredients": """50 g minced meat (I used half pork, half beef)
1/4 tsp ground paprika
Pepper and salt
40 g red bell pepper  🔴 Fructose 93%
1/4 large carrot
The green part of one leek
Optional: 150 g canned mushrooms
A few stalks spring onion, only the green part
Fresh parsley
0.25 l water
1/2 stock cubes
""",
        "directions": """**Prep (bowls):**

**Large bowl — meatballs (simmer later):**
- Mix the minced meat with the ground paprika, pepper and salt.
- Roll into very small balls; set aside.

**Large bowl — veg (fry / soften):**
- Dice the red bell pepper and carrot.
- Chop the green part of the leek.
- Optional: drain the canned mushrooms and have ready.

**Medium bowl — finish:**
- Slice the spring onion greens.
- Chop the fresh parsley.

By the hob (no bowl): oil, water, stock cubes.

**1. Soften veg:** Warm oil in a soup pan. Soften the veg bowl for a few minutes.

**2. Simmer base:** Add the water, stock cubes, and the spring onion greens and parsley from the finish bowl. Bring to a boil and boil at least 15 minutes.

**3. Meatballs:** Add the meatballs bowl and boil another 10 minutes until cooked through.

**4. Season:** Taste and adjust salt and pepper.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
    },
    "BB29EE89-DDD4-47BC-9E74-252A698288F2": {
        "ingredients": """1/4 large carrot
1/4 green bell pepper*  🔴 Fructose 93%
1/4 tsp cumin
1/4 tsp ground paprika
1/4 tsp turmeric
1/4 tsp oregano
1/8 tsp chilli
1/8 tsp cayenne pepper
1/8 tsp cinnamon
1/8 tsp ground black pepper
1/8 tsp salt
0.25 l vegetable broth (use a low FODMAP soup base such as this one from Fody)
1/4 can diced tomatoes
1 tbsp canned corn kernels, drained
Optional: slices of black olives, to taste
Optional: slices of jalapeno pepper, to taste
1 stalks of spring onion, the green part only
Lactose-free cream cheese or sour cream (sour cream only if you tolerate lactose)
Grated cheese to taste
Plain tortilla chips for serving
""",
        "directions": """**Prep (bowls):**

**Large bowl — veg (fry / soften):**
- Dice the carrot and green bell pepper.

**Small bowl — spice mix (fry):**
- Mix the cumin, ground paprika, turmeric, oregano, chilli, cayenne, cinnamon, black pepper and salt.

**Medium bowl — finish:**
- Drain the corn.
- Slice the olives and jalapeno if using.
- Slice the spring onion greens.
- Set out the lactose-free cream cheese or sour cream, grated cheese and tortilla chips.

By the hob (no bowl): oil, vegetable broth, diced tomatoes.

**1. Soften veg:** Warm oil in a pan. Soften the veg bowl for a few minutes.

**2. Fry spices:** Stir in the spice mix until fragrant.

**3. Simmer:** Add the vegetable broth and diced tomatoes. Bring to a boil, then simmer 15–20 minutes.

**4. Blend:** Blend smooth with an immersion blender, or only briefly if you prefer some texture. Taste salt and pepper.

**5. Serve:** Ladle into bowls. Top with corn, olives, spring onion, cream cheese or sour cream and grated cheese from the finish bowl. Serve with tortilla chips.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
    },
    "830FB55B-F0A6-413D-9858-23051A2BAEDA": {
        "ingredients": """83 g (4 1/3) Japanese pumpkin (also called kent or kabocha), cubed
50 g sweet potato, peeled and in cubes  🟠 Mannitol 71%
A pinch of cayenne pepper
1/8 tsp powdered paprika
1/8 tsp oregano
Pepper and salt
0.23 l vegetable or chicken stock (make sure you use stock that is low FODMAP)
33 ml coconut milk  🟠 Sorbitol 55%
Hand blender
""",
        "directions": """**Prep (bowls):**

**Large bowl — roast veg:**
- Cube the Japanese pumpkin.
- Peel and cube the sweet potato.

**Small bowl — spice mix:**
- Mix the cayenne, powdered paprika, oregano, pepper and salt.

By the hob (no bowl): olive oil, stock, coconut milk.

**1. Roast veg:** Heat the oven to 220°C. Spread the roast veg bowl on a parchment-lined tray, drizzle with olive oil, and bake 25 minutes.

**2. Simmer:** Warm a little oil in a soup pan. Add the roasted veg and the stock. Bring to a boil and simmer on medium heat about 20 minutes.

**3. Blend:** Puree smooth with a hand blender. Season with the spice mix. Thin with extra stock if needed.

**4. Coconut:** Stir in half the coconut milk and heat through. Serve with the remaining coconut milk as a drizzle.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
    },
    "AC5C9055-942A-4B9F-A486-FB56B871C08A": {
        "ingredients": """1 corn tortillas, cut into strips
1/2 boneless skinless chicken breasts (about 450g), or 2 cups cooked shredded chicken
Green tops of 2 spring onions (spring onions), thinly sliced, plus more to serve
Green tops of 2 scallions (spring onions), thinly sliced, plus more to serve
1/4 medium carrot (about 60g), finely diced
1/16 red bell pepper (about 45g), diced  🟢 Fructose 23%
1/4 tsp ground cumin
1/4 tsp smoked paprika
1/4 tsp pure chili powder or ground ancho (check the label for added onion or garlic)
1/8 tsp dried oregano
1/8 tsp ground coriander
Pinch of cayenne (optional)
2/3 tsp plain tomato paste (no added onion or garlic)  🟢 Fructose 13%
1/8 cup (about 160g) canned black beans, rinsed and drained  🟠 Galacto-oligosaccharides 75%
1/5 cup (about 115g) canned or frozen corn kernels, drained
1/2 tbsp [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)]
1/2 tbsp garlic-infused oil, for the tortilla strips
3/4 tsp garlic-infused oil, for the tortilla strips
1/8 cup (about 240g) canned diced tomatoes, with juice  🟡 Fructose 40%
1 cup low-FODMAP chicken broth, plus extra to thin
3/4 tsp fresh lime juice
Salt and black pepper to taste
Avocado, about 2 tbsp diced per bowl (roughly 30g)
Avocado, about 2 tablespoons diced per bowl (roughly 30g)
Grated cheddar or lactose-free cheese
Fresh coriander, sliced spring onion greens, and lime wedges
Fresh cilantro, sliced scallion greens, and lime wedges
Sliced fresh jalapeno
""",
        "directions": """**Prep (bowls):**

**Medium bowl — tortilla strips (bake):**
- Cut the corn tortillas into strips.

**Large bowl — chicken (poach / shred):**
- Have the chicken breasts ready (or cooked shredded chicken).

**Large bowl — veg (fry / soften):**
- Thinly slice the spring onion greens (reserve some for serving).
- Finely dice the carrot.
- Dice the red bell pepper.

**Small bowl — spice mix (fry):**
- Mix the cumin, smoked paprika, chili powder, oregano, coriander and optional cayenne.
- Have the tomato paste ready beside this bowl.

**Medium bowl — beans & corn (simmer later):**
- Rinse and drain the black beans.
- Drain the corn.

**Medium bowl — finish:**
- Dice the avocado.
- Set out the grated cheese, fresh coriander, jalapeno and lime wedges.
- Reserve extra spring onion greens for topping.

By the hob (no bowl): garlic-infused olive oil, diced tomatoes, chicken broth, lime juice, salt, black pepper.

**1. Crisp tortillas:** Heat the oven to 205°C. Toss the tortilla strips with garlic-infused oil and a pinch of salt. Bake 8–10 minutes, tossing once, until golden and crisp; cool (they firm as they sit).

**2. Soften veg:** Warm garlic-infused oil in a large pot over medium heat. Soften the veg bowl about 5 minutes.

**3. Fry spices:** Stir in the spice mix; toast 1 minute. Add the tomato paste and cook 1 minute, then the diced tomatoes with juice; cook 2 minutes.

**4. Broth:** Pour in the chicken broth and bring to a gentle simmer.

**5. Chicken:** Lower the chicken into the broth. Cook 15–18 minutes until the thickest part reaches 75°C. Remove, shred, and return to the pot. (If using cooked shredded chicken, add it with the beans.)

**6. Beans & corn:** Stir in the beans & corn bowl; simmer 5 minutes. Add the lime juice; season with salt and pepper. Thin with extra broth if you like.

**7. Serve:** Ladle into bowls. Top with tortilla strips and the finish bowl (avocado, cheese, coriander, spring onion greens, jalapeno, lime).

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
    },
    "4EA62CBC-30F4-44C6-9E50-0A89EE3DF2BF": {
        "ingredients": """Green tops of 1 large leek, thinly sliced, white base discarded
Green tops of 1 large leek, thinly sliced (about 1 cup / 90g), white base discarded
1/2 medium carrots, diced (about 150g)
1/4 medium celery stalk, finely diced
1/4 medium potato, peeled and diced (about 150g)
1/4 can (400g) lentils, drained and rinsed thoroughly
1/4 tsp ground cumin
1/4 tsp ground coriander
1/8 tsp smoked paprika
1/4 bay leaf
1/8 tsp salt, plus more to taste
1/8 tsp black pepper
1/2 tbsp [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)]
1/2 tbsp garlic-infused olive oil
1/4 cup (240g) canned diced tomatoes  🔴 Fructose 80%
1 cup (1L) low-FODMAP chicken broth, or a certified low-FODMAP vegetable broth
To finish: chopped spring onion green tops, a handful of fresh parsley, and a squeeze of lemon
To finish: chopped scallion green tops, a handful of fresh parsley, and a squeeze of lemon
""",
        "directions": """**Prep (bowls):**

**Large bowl — veg (fry / soften):**
- Thinly slice the leek green tops (discard the white base).
- Dice the carrots.
- Finely dice the celery.

**Large bowl — potato & lentils (simmer):**
- Peel and dice the potato.
- Drain and rinse the lentils thoroughly.

**Small bowl — spice mix (fry):**
- Mix the cumin, ground coriander and smoked paprika.
- Have the bay leaf, salt and black pepper ready beside this bowl.

**Medium bowl — finish:**
- Chop the spring onion green tops and parsley.
- Have lemon ready to squeeze.

By the hob (no bowl): garlic-infused olive oil, diced tomatoes, broth.

**1. Soften veg:** Warm garlic-infused oil in a large pot over medium heat. Soften the veg bowl 6–8 minutes until savory.

**2. Fry spices:** Stir in the spice mix; cook 1 minute until fragrant.

**3. Simmer:** Add the potato & lentils bowl, diced tomatoes, broth and bay leaf. Scrape the pot. Bring to a gentle boil, then simmer uncovered 20–25 minutes until the potato and carrot are fork-tender and the soup thickens slightly.

**4. Finish:** Remove the bay leaf. Optionally blend about a quarter of the soup and stir back for a creamier texture. Taste with salt, pepper and lemon. Serve topped with spring onion greens and parsley from the finish bowl.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
    },
    "6D4C0052-250C-4E3D-81C1-E80B5EC68B22": {
        "ingredients": """76 g ground beef or ground turkey
1/6 red bell pepper, diced  🟠 Fructose 62%
Green tops of 5 to 6 spring onions, thinly sliced (green parts only)
Green tops of 5 to 6 scallions, thinly sliced (green parts only)
1 tsp tomato paste  🟢 Fructose 20%
1/2 tsp pure chili powder (check the label so it is chile only, with no added onion or garlic powder)
1/3 tsp ground cumin
1/6 tsp smoked paprika
1/6 tsp dried oregano
1/8 tsp cayenne, or to taste (optional)
1/6 cup canned brown or green lentils, drained and rinsed well  🔴 Galacto-oligosaccharides 87%
1/6 cup canned corn, drained and rinsed
1/6 tsp salt, plus more to taste
1/8 tsp black pepper
1/2 tsp [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)]
1/2 tsp garlic-infused olive oil
1/6 can diced tomatoes, with juice
2/3 cup low-FODMAP chicken broth or water
Optional toppings: grated cheddar, lactose-free sour cream, chopped coriander, lime wedges
Optional toppings: grated cheddar, lactose-free sour cream, chopped cilantro, lime wedges
""",
        "directions": """**Prep (bowls):**

**Large bowl — meat (fry / brown):**
- Have the ground beef or turkey ready.

**Large bowl — veg (fry / soften):**
- Dice the red bell pepper.
- Thinly slice the spring onion greens (reserve some for serving).

**Small bowl — spice mix (fry):**
- Mix the chili powder, cumin, smoked paprika, oregano and optional cayenne.
- Have the tomato paste ready beside this bowl.

**Medium bowl — lentils & corn (simmer later):**
- Drain and rinse the lentils.
- Drain and rinse the corn.

**Medium bowl — finish:**
- Set out grated cheddar, lactose-free sour cream, chopped coriander and lime wedges if using.
- Reserve remaining spring onion greens for topping.

By the hob (no bowl): garlic-infused olive oil, diced tomatoes, broth or water, salt, black pepper.

**1. Brown meat:** Warm garlic-infused oil in a large pot over medium-high heat. Brown the meat bowl, breaking it up, 6–8 minutes; drain excess fat if needed.

**2. Soften veg:** Add the bell pepper from the veg bowl; cook about 3 minutes. (Hold most spring onion greens for later.)

**3. Fry spices:** Stir in the tomato paste and spice mix; cook 1 minute until fragrant.

**4. Simmer:** Add the diced tomatoes with juice and the broth. Scrape the pot. Stir in the lentils & corn bowl, salt and pepper. Bring to a gentle boil, then simmer uncovered 25–30 minutes until flavours meld and the soup thickens slightly. Stir in most of the spring onion greens; taste salt.

**5. Slow cooker option:** After browning meat and blooming spices, transfer to a slow cooker with tomatoes, broth and lentils. Cook on low 6 hours or high 3 hours; stir in corn for the last 20 minutes.

**6. Serve:** Ladle into bowls. Top with remaining spring onion greens and the finish bowl toppings.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
    },
    "3ED2DB8D-BFAC-40F1-9207-016D0CA3D2F4": {
        "ingredients": """113 g raw shrimp, peeled and deveined, shells reserved for stock
1/2 stalks lemongrass, tough outer layer removed, bruised and cut into 2-inch lengths
1/4 thumb-size piece galangal, or fresh ginger as a swap
1 3/8 kaffir lime leaves, lightly torn
5/8 fresh red bird's eye chilies, sliced
34 g oyster mushrooms, torn into bite-size strips  🟠 Mannitol 72%
2 cherry tomatoes, halved
Green tops of 2 spring onions, thinly sliced
Green tops of 2 scallions, thinly sliced
1/4 coriander leaves, roughly torn
1/2 tbsp [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)]
1/2 tbsp garlic-infused olive oil
1 1/2 cup low-FODMAP chicken broth, or homemade shrimp stock from the reserved shells
3/4 tbsp fish sauce
7/8 tbsp fresh lime juice (about 2 limes)
1/4 tsp cane sugar or maple syrup
Optional: up to 1 canned coconut milk for a creamy version (see Tips)
Optional: up to 1/4 cup (60 ml) canned coconut milk for a creamy version (see Tips)
""",
        "directions": """**Prep (bowls):**

**Large bowl — shrimp (poach later):**
- Peel and devein the shrimp; reserve shells if making shrimp stock.

**Small bowl — aromatics (fry / infuse):**
- Bruise and cut the lemongrass.
- Slice the galangal or ginger.
- Lightly tear the kaffir lime leaves.
- Slice the bird's eye chilies (reserve some for serving).

**Medium bowl — mushrooms & tomato (simmer later):**
- Tear the oyster mushrooms into strips.
- Halve the cherry tomatoes.

**Medium bowl — finish:**
- Thinly slice the spring onion greens.
- Roughly tear the coriander.
- Have the reserved chili ready.

By the hob (no bowl): garlic-infused olive oil, chicken broth (or water for shell stock), fish sauce, lime juice, sugar or maple syrup, optional coconut milk.

**1. Broth:** If using shells, simmer them in the broth or water about 10 minutes, then strain. Otherwise start with the chicken broth.

**2. Aromatics:** Warm garlic-infused oil over medium heat. Stir the aromatics bowl (use half the chili) about 1 minute until fragrant. Pour in the broth; simmer uncovered 12–15 minutes to infuse.

**3. Mushrooms:** Add the mushrooms & tomato bowl; simmer 3–4 minutes until the mushrooms soften.

**4. Shrimp:** Add the shrimp bowl; cook 2–3 minutes until pink and opaque (do not boil hard).

**5. Season:** Off the heat, stir in fish sauce, lime juice and sugar. Taste and balance salty, sour and sweet. Optional: stir in coconut milk for a creamier version.

**6. Serve:** Ladle into bowls (leave lemongrass and galangal behind if preferred). Top with the finish bowl.

Cool, portion, refrigerate up to 3 days; reheat gently until piping hot (do not overcook the shrimp).
""",
    },
    "06A6F9D8-E73D-46BF-B41C-913075219688": {
        "ingredients": """1/4 medium carrot, peeled and diced
Green tops of 4 spring onions, thinly sliced
Green tops of 4 scallions, thinly sliced (about 1/2 cup)
1/2 tbsp tomato paste  🟡 Fructose 29%
1/2 tbsp fresh basil, torn, plus more for serving
1/2 tbsp [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)]
1/2 tbsp garlic-infused olive oil
1/4 can whole or crushed tomatoes, no onion or garlic added (Muir Glen or Cento)  🟡 Fructose 37%
3/8 cup low-FODMAP chicken broth or low-FODMAP vegetable broth
1/8 cup heavy cream  🟠 Lactose 75%
1/4 tsp sugar
1/8 tsp salt, plus more to taste
1/8 tsp black pepper
""",
        "directions": """**Prep (bowls):**

**Large bowl — veg (fry / soften):**
- Peel and dice the carrot.
- Thinly slice the spring onion greens.

**Small bowl — paste & herbs:**
- Have the tomato paste ready.
- Tear the basil (reserve some for serving).

By the hob (no bowl): garlic-infused olive oil, canned tomatoes, broth, heavy cream, sugar, salt, black pepper.

**1. Soften veg:** Warm garlic-infused oil in a heavy pot over medium heat. Soften the veg bowl 4–5 minutes until the carrot softens slightly.

**2. Fry paste:** Stir in the tomato paste; cook 1 minute until it darkens and smells sweet.

**3. Simmer:** Add the canned tomatoes with juice (break up whole tomatoes if needed), broth, sugar, salt and pepper. Simmer uncovered 20–25 minutes until slightly thickened and the carrot is tender.

**4. Blend:** Off the heat, blend until smooth. Return to the pot if needed. Stir in the cream and basil from the paste & herbs bowl; warm on low 2 minutes without boiling.

**5. Serve:** Taste salt. Serve with a drizzle of garlic-infused oil and extra basil.

Cool, portion, refrigerate up to 3 days or freeze (cream may separate; reheat gently until piping hot).
""",
    },
    "2DE19236-B6A5-4DDE-B91D-35B491E88ECC": {
        "ingredients": """1/2 cm (0.8 inch) fresh galanga or ginger
1/4 red chili pepper
1/4 stalk lemon grass (sereh)
1/2 stalks spring onion, the green part only
38 g oyster mushrooms  🔴 Mannitol 81%
Optional: fresh coriander
Optional: fresh cilantro
0.25 l low FODMAP stock
1/4 can coconut milk
3/4 tbsp soy sauce
3/4 tsp brown sugar
1/2 tbsp lime juice
""",
        "directions": """**Prep (bowls):**

**Small bowl — aromatics (simmer / infuse):**
- Cut the lemongrass into pieces.
- Peel and slice the galangal or ginger.
- Deseed and slice the chili into rings.

**Medium bowl — mushrooms & greens (simmer later):**
- Clean and cut the oyster mushrooms.
- Slice the spring onion greens into rings.

**Medium bowl — finish:**
- Have fresh coriander ready.

By the hob (no bowl): stock, coconut milk, soy sauce, brown sugar, lime juice.

**1. Infuse:** Bring the stock and coconut milk to a boil. Add the aromatics bowl and boil a few minutes.

**2. Mushrooms:** Add the mushrooms & greens bowl; boil covered 5 minutes.

**3. Season:** Stir in soy sauce, lime juice and brown sugar; boil 2 minutes more. Taste and balance with extra sugar, soy sauce, water or coconut milk as needed.

**4. Finish:** Turn off the heat; remove lemongrass and ginger pieces. Serve with coriander from the finish bowl. Optional: add cooked rice noodles for a meal soup.

Cool, portion, refrigerate up to 3 days; reheat until piping hot.
""",
    },
    "79646147-866E-4F6B-979E-CFAF5EAFE9AD": {
        "ingredients": """49 g carrots (skin-on, halved)
1/16 leeks (white and light green parts only, rinsed, halved and thinly sliced 1/4-inch thick)  🟡 Fructans 36%
1/2 tbsp chopped fresh ginger
1/4 tsp ground turmeric
25 g russet potato (1 medium, peeled and cut into 1-inch cubes)
Olive oil
Sea salt
1/2 tbsp ghee or coconut oil
15 ml cans full-fat coconut milk  🟢 Sorbitol 25%
""",
        "directions": """**Prep (bowls):**

**Large bowl — roast carrots:**
- Halve the carrots (skin-on).

**Large bowl — leeks (split roast / soften):**
- Rinse, halve and thinly slice the leeks. Divide: half for roasting crisp, half for the pot.

**Small bowl — aromatics:**
- Chop the fresh ginger.
- Have the turmeric ready.
- Peel and cube the potato.

By the hob (no bowl): olive oil, sea salt, ghee or coconut oil, coconut milk, water.

**1. Roast carrots:** Heat the oven to 220°C. Toss the roast carrots bowl with olive oil and sea salt on a parchment tray. Roast in the bottom half of the oven 35–40 minutes until caramelized and fork-tender.

**2. Crisp leeks:** On a second tray, toss half the leeks with olive oil and a little salt. Roast in the top half about 15 minutes until lightly browned and crisp; set aside.

**3. Soften & simmer:** Warm ghee or coconut oil in a pot. Soften the remaining leeks about 5 minutes. Stir in the aromatics bowl (ginger, turmeric, potato); sauté about 3 minutes until fragrant. Pour in the coconut milk, a can of water, and salt. Boil, then simmer until the potatoes are fork-tender, about 10 minutes.

**4. Blend:** Blend the roasted carrots with the pot mixture until smooth. Serve topped with the crisp leeks.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
    },
    "68AD419E-B93C-4332-8425-7043E7EAF2B6": {
        "ingredients": """50 g bacon bits (I use turkey bacon strips because they’re a bit leaner)*
100 g potato, peeled
100 g canned corn
1/8 tsp salt
1/8 tsp black pepper
1/8 tsp paprika
1/8 tsp dried thyme
1/2 tsp cornstarch
Optional: fresh chives
0.25 l broth (use a low FODMAP soup base)
55 g lactose-free heavy cream
""",
        "directions": """**Prep (bowls):**

**Large bowl — bacon (fry / crisp):**
- Have the bacon ready (cut into bits if needed).

**Large bowl — potato & corn (simmer):**
- Peel and chunk the potatoes.
- Drain and rinse the corn.

**Small bowl — seasoning:**
- Mix the salt, black pepper, paprika and dried thyme.

**Small bowl — slurry:**
- Mix the cornstarch with a splash of cold water until smooth.

**Medium bowl — finish:**
- Chop the chives if using.
- Reserve a little bacon for garnish after frying.

By the hob (no bowl): broth (or soup base and water), lactose-free heavy cream.

**1. Bacon:** Cook the bacon bowl in a soup pot until crispy. Remove and set aside (keep a little for garnish).

**2. Simmer:** Add the broth, potato & corn bowl, and seasoning. Bring to a boil, then simmer gently 15–20 minutes until the potatoes are soft.

**3. Blend:** Blend fully or only briefly for a chunkier texture.

**4. Thicken & cream:** Whisk in the slurry to thicken slightly. Stir in the cream and most of the bacon; heat through.

**5. Serve:** Top with reserved bacon and chives from the finish bowl.

Cool, portion, refrigerate up to 3 days; reheat until piping hot.
""",
    },
    "C31A1057-F1E8-4DBD-AF8F-4187F1EC232C": {
        "ingredients": """1/3 cm (0.8 inch) ginger, grated
1/6 courgette
1/6 zucchini
83 g chicken breasts
17 g brown rice  🟢 Fructans 9%
1/6 tsp turmeric
17 g fresh spinach  🟢 Fructans 11%
1/3 stalks of spring onion, the green part
Optional: fresh mint
Optional: fresh parsley
Olive oil for frying
0.25 l chicken stock (I use Fody Foods low FODMAP chicken soup base)
Juice of 3 limes
A dash of soy sauce*
Salt or pepper to taste
""",
        "directions": """**Prep (bowls):**

**Small bowl — aromatics (fry):**
- Grate the ginger.

**Large bowl — veg (fry / soften):**
- Dice the courgette.

**Large bowl — chicken (poach / shred):**
- Have the chicken breasts ready.

**Large bowl — rice (simmer later):**
- Rinse the brown rice if needed.

**Small bowl — spice:**
- Have the turmeric ready.

**Medium bowl — finish:**
- Wash the spinach.
- Slice the spring onion greens.
- Have mint and parsley ready if using.

By the hob (no bowl): olive oil, chicken stock, lime juice, soy sauce, salt, pepper.

**1. Aromatics:** Warm olive oil in a soup pan. Fry the aromatics bowl a few minutes.

**2. Soften veg:** Add the veg bowl; fry about 3 minutes.

**3. Stock & chicken:** Add the stock and turmeric. Add the chicken bowl; cook 15–20 minutes until done. Remove, shred with two forks, and return to the pan.

**4. Rice:** Add the rice bowl; cook about 10 minutes more.

**5. Finish:** Stir in the lime juice, soy sauce and spinach from the finish bowl; simmer a few minutes. Taste salt and pepper. Serve garnished with mint, parsley and spring onion greens.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
    },
    "927067A6-7CF1-45B4-9946-A7342E50668A": {
        "ingredients": """Bones of 1 a cooked chicken
Bones of 1 (907 g) a cooked chicken
1/3 carrots, washed and chopped
1/6 celery stalk, washed and chopped
1/6 -inch piece fresh ginger, sliced into coins
5/6 sprigs fresh thyme
1/2 sprigs fresh rosemary
5/6 sprigs fresh parsley
1/3 bay leaves
1 1/3 whole peppercorns
1/3 quarts water
Salt, to taste (optional)
""",
        "directions": """**Prep (bowls):**

**Large bowl — bones:**
- Have the cooked chicken bones ready.

**Large bowl — veg & aromatics (slow cook):**
- Wash and chop the carrots and celery.
- Slice the ginger into coins.
- Have the thyme, rosemary, parsley, bay leaves and peppercorns ready.

By the hob (no bowl): water, optional salt.

**1. Load cooker:** Place the bones bowl and veg & aromatics bowl in a slow cooker. Cover with water.

**2. Cook:** Cook on low 8–12+ hours. Turn off and cool slightly.

**3. Strain:** Remove large bones and veg with tongs. Strain the broth (line with cheesecloth for clearer broth). Season with optional salt.

**4. Store:** Cool completely in containers. Refrigerate up to 3–4 days or freeze for longer use.
""",
    },
    "26951C68-2A1F-4ACD-9320-2CAFE66E14F3": {
        "ingredients": """1/4 cup arborio rice  🟡 Fructans 32%
1/2 cup leftover shredded chicken
1 1/2 large egg yolks
2 cup low FODMAP chicken broth
1/8 cup fresh lemon juice (about 2 lemons)
Salt and pepper
""",
        "directions": """**Prep (bowls):**

**Large bowl — rice (simmer):**
- Have the arborio rice ready.

**Medium bowl — chicken (warm later):**
- Have the shredded chicken ready.

**Medium bowl — egg temper:**
- Lightly beat the egg yolks.

By the hob (no bowl): chicken broth, lemon juice, salt, pepper.

**1. Rice:** Add the broth and rice bowl to a soup pot. Bring to a boil, then simmer on medium until the rice is cooked, about 15–20 minutes.

**2. Temper eggs:** Whisking constantly, slowly pour a ladle of hot broth into the egg temper bowl, then slowly whisk the mixture back into the pot.

**3. Finish:** Stir in the chicken bowl and lemon juice. Warm about 5 minutes. Season with salt and pepper. Serve warm.

Cool, portion, refrigerate up to 3 days; reheat gently until piping hot (do not boil hard after adding eggs).
""",
    },
    "0D644AD2-9544-476C-A6DE-D3FE9D007AC5": {
        "ingredients": """113 g ground pork // note 1
1½ tsp rubbed sage
1/8 tsp ground black pepper
1/8 tsp fennel seeds // note 2  🟢 Fructans 1%
1/8 tsp dried thyme
1½ teaspoons rubbed sage
680 g baby potatoes, diced into bite-sized pieces
1 cup chopped kale, loosely packed // note 4
1/8 tsp red pepper flakes, optional or adjust amount to preference  🟢 Fructose 1%
3/4 tsp [recipe:Garlic-Infused Olive Oil (For Low FODMAP Cooking)]
3/4 tsp garlic-infused olive oil
1 1/2 cup low FODMAP vegetable or chicken broth // note 3
Salt and black pepper
""",
        "directions": """**Prep (bowls):**

**Large bowl — sausage (fry / brown):**
- Have the ground pork ready.
- Mix the rubbed sage, black pepper, fennel seeds and dried thyme beside the meat (or season as it browns).

**Large bowl — potatoes (simmer):**
- Dice the baby potatoes into bite-sized pieces.

**Medium bowl — finish:**
- Chop the kale.
- Have optional red pepper flakes ready.

By the hob (no bowl): garlic-infused olive oil, broth, salt, black pepper.

**1. Brown sausage:** Warm a dutch oven over medium-high heat. Add garlic-infused oil and the sausage bowl with its spices. Break into crumbs and cook until browned, about 8 minutes.

**2. Potatoes:** Add the broth and potatoes bowl. Bring to a boil, then simmer until the potatoes are fork-tender, about 15 minutes.

**3. Kale:** Off the heat, stir in the kale from the finish bowl until lightly wilted. Adjust with red pepper flakes, salt and black pepper. Serve warm.

Cool, portion, refrigerate up to 3 days or freeze; reheat until piping hot.
""",
    },
    "ABEFF3EC-1E5F-44F8-B8FE-5A42DB7AFA35": {
        "ingredients": """151 g parsnips, peeled and cut into 1-inch rounds
1/6 tsp dried sage
76 g ground pork
1/6 package fresh oyster mushrooms, sliced
1 cup low FODMAP broth (I use prepared Fody Foods Low FODMAP Vegetable Soup Base)
1/2 tsp shallot-infused olive oil (or melted butter) + extra for garnish  🟢 Fructans 22%
Salt and pepper
""",
        "directions": """**Prep (bowls):**

**Large bowl — parsnips (boil / blend):**
- Peel and cut the parsnips into rounds.
- Have the dried sage ready beside this bowl.

**Large bowl — pork (fry / crisp):**
- Have the ground pork ready.

**Medium bowl — mushrooms (fry later):**
- Slice the oyster mushrooms.

By the hob (no bowl): broth, shallot-infused olive oil or butter, salt, pepper.

**1. Parsnip soup:** Cover the parsnips bowl with broth. Bring to a boil and cook about 15 minutes until fork-tender. Blend smooth. Stir in the sage; season with salt and pepper.

**2. Topping:** Meanwhile, cook the pork bowl in a skillet over medium-high heat until browned and crispy; drain excess fat. Add oil and the mushrooms bowl; cook until tender. Season with salt and pepper.

**3. Serve:** Ladle the soup; drizzle with a little oil if you like and top with the pork and mushrooms.

Cool, portion, refrigerate up to 3 days or freeze the soup base separately from the topping; reheat until piping hot.
""",
    },
}


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


def update_local(uid: str, directions: str, ingredients: str) -> None:
    con = sqlite3.connect(LOCAL_DB)
    con.execute(
        """
        UPDATE recipes
        SET directions=?, ingredients=?, status=?
        WHERE uid=?
        """,
        (directions, ingredients, "modified", uid),
    )
    con.commit()
    con.close()


async def main() -> None:
    queue = json.loads(
        Path(__file__).with_name(".soups_prep_queue.json").read_text(encoding="utf-8")
    )
    batch = queue[34:51]
    limiter = RateLimiter(0.28)
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
            t = TRANSFORMS.get(uid)
            if not t:
                results.append({"uid": uid, "name": name, "status": "skipped", "why": "no transform"})
                safe_print(f"SKIP no transform {name}")
                continue

            directions = t["directions"].strip() + "\n"
            ingredients = t["ingredients"].strip() + "\n"

            con = sqlite3.connect(LOCAL_DB)
            row = con.execute(
                "SELECT directions, ingredients FROM recipes WHERE uid=?", (uid,)
            ).fetchone()
            con.close()
            if row and row[0] == directions and row[1] == ingredients:
                results.append({"uid": uid, "name": name, "status": "skipped", "why": "already applied"})
                safe_print(f"SKIP already {name}")
                continue

            st, body = await api_json(
                session,
                limiter,
                "GET",
                f"{PAPRIKA_API}/v2/sync/recipe/{uid}/",
                headers=headers,
            )
            rec = (body or {}).get("result") or {}
            if not rec.get("uid"):
                results.append({"uid": uid, "name": name, "status": "failed", "why": "missing cloud recipe"})
                safe_print(f"FAIL missing {name}")
                continue

            if "Prep (bowls)" in (rec.get("directions") or "") and rec.get("directions") == directions:
                update_local(uid, directions, ingredients)
                results.append({"uid": uid, "name": name, "status": "skipped", "why": "cloud already matched"})
                safe_print(f"SKIP cloud match {name}")
                continue

            rec["directions"] = directions
            rec["ingredients"] = ingredients
            rec["hash"] = calc_hash(rec)
            ok = await post_recipe(session, headers, rec, limiter)
            if ok:
                update_local(uid, directions, ingredients)
                results.append({"uid": uid, "name": name, "status": "ok"})
                safe_print(f"OK {name}")
            else:
                results.append({"uid": uid, "name": name, "status": "failed", "why": "post rejected"})
                safe_print(f"FAIL save {name}")

        await limiter.wait_turn()
        async with session.post(f"{PAPRIKA_API}/v2/sync/notify/", headers=headers) as r:
            await r.text()

    out = Path(__file__).with_name(".soups_prep_34_50_results.json")
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    ok_n = sum(1 for r in results if r["status"] == "ok")
    safe_print(f"Done: {ok_n}/{len(results)} applied")


if __name__ == "__main__":
    asyncio.run(main())

"""Bowl plan for the Bay's Kitchen cook-together recipes (the user, 27 Oct 2026).

Each sauce's ingredients are weighed into named bowls once; its method then refers to the
bowls by name, so no ingredient list is repeated. Same steps, timings and order as the
single-sauce recipes. A key with ":stalks" / ":leaves" takes that part of the coriander.
"oil" is each sauce's measured oil; its water goes in with the bowl it was added with,
and the small splashes (for a paste, or if spices catch) come from the tap (the user, 27 Oct 2026).
"""
from __future__ import annotations

# sauce -> (bowls [(bowl name, [ingredient keys])], method steps using **bowl names**)
BOWLS: dict[str, tuple[list[tuple[str, list[str]]], list[str]]] = {
    "Thai Green": (
        [("Green oil", ["oil"]), ("Green paste", ["coriander_fresh:stalks", "lemongrass", "green_chilli", "ginger"]),
         ("Green spice", ["cumin"]),
         ("Green liquid", ["water", "coconut", "lime_leaf", "tamari", "sugar", "salt"]),
         ("Green veg", ["bamboo", "green_pepper", "red_pepper"]),
         ("Green thickener", ["tapioca"]),
         ("Green finish", ["lime", "coriander_fresh:leaves"])],
        ["Blitz or pound the **Green paste** with a splash of water to a paste (no oil).",
         "Warm the **Green oil** over a medium-low heat and fry the paste with the **Green spice** for 2 minutes until fragrant.",
         "Add the **Green liquid** and bring to a gentle simmer.",
         "Add the **Green veg** and simmer for 4-5 minutes. Mix the **Green thickener** with a splash of cold water, stir it in "
         "and simmer for 1 minute.",
         "Stir in the **Green finish**; remove the lime leaf."]),
    "Thai Red": (
        [("Red oil", ["oil"]), ("Red paste", ["red_chilli", "lemongrass", "ginger"]),
         ("Red spice", ["cumin", "cayenne"]),
         ("Red liquid", ["water", "coconut", "lime_leaf", "tamari", "sugar", "salt"]),
         ("Red veg", ["red_pepper"]),
         ("Red thickener", ["tapioca"]),
         ("Red finish", ["lime", "coriander_fresh"])],
        ["Blitz or pound the **Red paste** with a splash of water to a paste (no oil).",
         "Warm the **Red oil** over a medium-low heat and fry the paste with the **Red spice** for 2 minutes until fragrant.",
         "Add the **Red liquid** and bring to a gentle simmer.",
         "Add the **Red veg** and simmer for 4-5 minutes. Mix the **Red thickener** with a splash of cold water, stir it in and "
         "simmer for 1 minute.",
         "Stir in the **Red finish**; remove the lime leaves."]),
    "Massaman": (
        [("Massaman oil", ["oil"]), ("Massaman paste", ["lemongrass", "ginger", "coriander_fresh:stalks"]),
         ("Massaman spice", ["cumin", "coriander_seed", "cayenne", "turmeric"]),
         ("Massaman infusion", ["water", "cinnamon", "cardamom", "lime_leaf", "salt", "sugar"]),
         ("Massaman cream", ["coconut", "tapioca"]),
         ("Massaman finish", ["lime", "coriander_fresh:leaves"])],
        ["Blitz or pound the **Massaman paste** with a splash of water to a paste (no oil).",
         "Warm the **Massaman oil** over a medium-low heat and fry the paste for 2 minutes, then add the **Massaman spice** for "
         "30 seconds.",
         "Add the **Massaman infusion** (these infuse in the liquid). Simmer for "
         "10 minutes; remove the cardamom pods and lime leaves.",
         "Whisk the **Massaman cream** (the tapioca starch stirred into the coconut milk), stir it in and simmer gently for "
         "2 minutes.",
         "Stir in the **Massaman finish**."]),
    "Korma": (
        [("Korma oil", ["oil"]), ("Korma ginger", ["ginger"]),
         ("Korma spice", ["cumin", "coriander_seed", "turmeric"]),
         ("Korma simmer", ["water", "tomatoes", "sugar", "salt", "cinnamon", "cloves", "cardamom"]),
         ("Korma cream", ["coconut", "tapioca"]),
         ("Korma finish", ["lemon", "pepper"])],
        ["Warm the **Korma oil** over a medium-low heat. Add the **Korma ginger** for 1 minute, then the **Korma spice** for "
         "30 seconds, adding a splash of water if they catch.",
         "Add the **Korma simmer** (these infuse in the liquid, no frying needed). Simmer "
         "for 10 minutes, then fish out the cloves and cardamom pods.",
         "Whisk the **Korma cream** (the tapioca starch stirred into the coconut milk), stir it in and simmer gently for "
         "2 minutes until creamy. Do not boil hard.",
         "Stir in the **Korma finish**."]),
    "Katsu": (
        [("Katsu oil", ["oil"]), ("Katsu ginger", ["ginger"]),
         ("Katsu spice", ["cumin", "coriander_seed", "fenugreek", "fennel", "mustard", "turmeric", "chilli_powder"]),
         ("Katsu simmer", ["water", "tamari", "sugar"]),
         ("Katsu cream", ["coconut", "tapioca"]),
         ("Katsu finish", ["lemon", "pepper"])],
        ["Warm the **Katsu oil** over a medium-low heat. Add the **Katsu ginger** for 30 seconds, then the **Katsu spice** for "
         "30 seconds, adding a splash of water if they catch.",
         "Add the **Katsu simmer** and simmer for 5 minutes.",
         "Whisk the **Katsu cream** (the tapioca starch stirred into the coconut milk), stir it in and simmer for 2 minutes "
         "until thick and glossy. For the classic smooth katsu sauce, blend.",
         "Stir in the **Katsu finish**."]),
    "Tikka": (
        [("Tikka oil", ["oil"]), ("Tikka ginger", ["ginger"]),
         ("Tikka spice", ["cumin", "coriander_seed", "paprika", "turmeric", "chilli_flakes"]),
         ("Tikka simmer", ["water", "tomatoes", "tomato_puree", "sugar", "salt"]),
         ("Tikka cream", ["coconut", "tapioca"]),
         ("Tikka finish", ["lemon", "coriander_fresh", "pepper"])],
        ["Warm the **Tikka oil** over a medium-low heat. Add the **Tikka ginger** for 30 seconds, then the **Tikka spice** and "
         "stir for 30-45 seconds, adding a splash of water if they catch.",
         "Add the **Tikka simmer**. Simmer for 15 minutes.",
         "Whisk the **Tikka cream** (the tapioca starch stirred into the coconut milk), stir it in and simmer for 1-2 minutes "
         "until thickened.",
         "Stir in the **Tikka finish**."]),
    "Tom & Basil": (
        [("Basil oil", ["oil"]), ("Basil simmer", ["water", "tomatoes", "tomato_puree", "balsamic", "oregano", "sugar", "salt"]),
         ("Basil thickener", ["tapioca"]),
         ("Basil finish", ["chives", "basil", "lemon", "pepper"])],
        ["Warm the **Basil oil** in a small pan over a medium-low heat.",
         "Add the **Basil simmer**. Simmer for 15-20 minutes, stirring now and then, until slightly "
         "thickened.",
         "For a thicker sauce, mix the **Basil thickener** with a splash of cold water, stir it in and simmer for 1 minute "
         "(or simmer 10 minutes longer instead).",
         "Off the heat, stir in the **Basil finish**. Taste for salt."]),
    "Arrabbiata": (
        [("Arrabbiata oil", ["oil"]), ("Arrabbiata chilli", ["cayenne", "chilli_flakes"]),
         ("Arrabbiata simmer", ["water", "tomatoes", "tomato_puree", "sundried", "sugar", "salt"]),
         ("Arrabbiata thickener", ["tapioca"]),
         ("Arrabbiata finish", ["lemon", "pepper"])],
        ["Warm the **Arrabbiata oil** over a medium-low heat, add the **Arrabbiata chilli** and stir for 20-30 seconds (chilli "
         "heat is fat-soluble, so this is the step that needs the oil).",
         "Add the **Arrabbiata simmer**. Simmer for 15-20 minutes until slightly thickened.",
         "For a thicker sauce, mix the **Arrabbiata thickener** with a splash of cold water, stir it in and simmer for "
         "1 minute.",
         "Stir in the **Arrabbiata finish**."]),
}

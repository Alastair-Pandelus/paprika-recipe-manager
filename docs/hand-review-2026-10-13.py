"""Hand-decided fixes for the last repeats (docs/repeats-still-left.tsv).

Each op matches a line exactly as it is in the cloud today. Dry-run unless
--apply. Uses the repo's helpers; run from the Paprika Recipe Manager folder.
"""
import asyncio, gzip, json, sqlite3, sys
sys.path.insert(0, 'scripts/tools'); sys.path.insert(0, 'scripts')
import aiohttp
from lib import PAPRIKA_API, paprika_credentials
from normalize_ingredient_units import RateLimiter, api_json
from cleanup_library import TAG_RE, calc_hash, LOCAL_DB
from rationalise_repeats import scale_tags

APPLY = '--apply' in sys.argv
SUB = 'LF; replaces garlic-infused oil'


def lab(line, label):
    body = TAG_RE.sub('', line)
    return f'{body.rstrip()} ({label}){line[len(body):]}'


def amt(line, old_amt, new_amt, ratio):
    """Swap the leading amount and scale the tags by ratio."""
    body = TAG_RE.sub('', line)
    tag = line[len(body):].strip()
    assert body.startswith(old_amt), (line, old_amt)
    out = new_amt + body[len(old_amt):].rstrip()
    return out + (f'  {scale_tags(tag, ratio)}' if tag else '')


L = lab  # shorthand
OPS = {}  # name fragment -> list of (op, line, new)


def R(name, old, new):
    OPS.setdefault(name, []).append(('replace', old, new))


def D(name, old):
    OPS.setdefault(name, []).append(('delete', old, None))


def I(name, anchor, new):
    OPS.setdefault(name, []).append(('insert_after', anchor, new))


# --- olive oil in place of the website's garlic-infused oil ----------------
n = 'Side - Greek Salad'
x = '1/8 cup olive oil 💔 Sat fat 4.2g (21%)'
R(n, x, amt(x, '1/8 cup', '1 tbsp', 0.5))  # website 1/4 cup for 4

n = 'Lower FODMAP Crostini with Roasted Grapes'
x = '2/3 tsp olive oil ♥ Sat fat 0.4g (2%)'
R(n, x, L(x, SUB))

n = 'Meal - Chicken Caesar Salad'
x = '3/4 tsp olive oil ♥ Sat fat 0.5g (2%)'
R(n, x, L(x, SUB))  # website 1 tbsp garlic oil in the dressing, for 4
x = '1/8 cup olive oil 💔 Sat fat 4.2g (21%)'
R(n, x, amt(x, '1/8 cup', '1 tbsp', 0.5))  # website 1/4 cup for 4
D(n, '3/4 tsp olive oil, divided (for chicken and croutons) ♥ Sat fat 0.5g (2%)')

n = 'Side - Cucumber Tomato Salad'
x = '3/4 tsp olive oil ♥ Sat fat 0.5g (2%)'
R(n, x, L(x, SUB))

n = 'Low FODMAP Italian Pasta Salad'
x = '1/8 cup olive oil 💔 Sat fat 4.2g (21%)'
R(n, x, amt(x, '1/8 cup', '1 tbsp', 0.5))  # website 1/2 cup for 8
x = '3/4 tsp. olive oil ♥ Sat fat 0.5g (2%)'
R(n, x, L(x, SUB))

n = 'Low FODMAP Greek sheet pan chicken'
x = '1/4 tsp olive oil ♥ Sat fat 0.2g (1%)'
R(n, x, L(x, SUB))

n = 'Low FODMAP Sesame Chicken'
x = '3/4 tsp olive oil ♥ Sat fat 0.5g (2%)'
R(n, x, L(x, SUB))
x = '3/4 tsp sesame oil ♥ Sat fat 0.4g (2%)'
R(n, x, amt(x, '3/4 tsp', '1/4 tsp', 1 / 3))  # website 1 tsp for 4
x = '1 1/8 tsp avocado oil (or vegetable oil) 🟢 Sorbitol 9%'
R(n, x, x.replace('1 1/8 tsp', '3/4 to 1 1/2 tsp'))  # website 1-2 tbsp for 4
D(n, '1/4 tsp avocado oil (or vegetable oil) 🟢 Sorbitol 2%')

# --- copies and wrong amounts --------------------------------------------
n = 'Light & Fresh Vegetable Detox Soup'
x = '77 g can unsweetened coconut cream (optional, see note) 🔴 Lactose 192% 💔 Sat fat 22.3g (112%)'
R(n, x, amt(x, '77 g can', '38 g', 38 / 77))  # website one 153 g can for 4
D(n, '153 g can unsweetened coconut cream (optional, see note) 🔴 Lactose 382% 💔 Sat fat 44.4g (222%)')

# --- the same ingredient in two parts of the recipe: label each -----------
n = 'Gluten-free New York Cheesecake'
R(n, '2 g icing sugar', '2 g icing sugar (for the crust)')
R(n, '13 g icing sugar', '13 g icing sugar (for the filling)')

n = 'Low FODMAP vegan brownies with salted'
R(n, '1/12 tsp vanilla extract', '1/12 tsp vanilla extract (for the brownie)')
R(n, '1/6 tsp vanilla extract', '1/6 tsp vanilla extract (for the salted caramel sauce)')

n = 'Healthy pumpkin cake'
x = '8.3 g lactose-free yoghurt  ♥ Sat fat 0.3g (2%)'
R(n, x, L(x, 'for the topping'))
R(n, '0.4 g maple syrup', '0.4 g maple syrup (for the topping)')
R(n, '1 pinch pumpkin spice', '1 pinch pumpkin spice (for the topping)')

n = 'Sweet - Cheesecake'
R(n, '2 tablespoons granulated sugar', '2 tablespoons granulated sugar (for the crust)')
I(n, '32 oz lactose-free cream cheese, softened (Green Valley Creamery) 💔 Sat fat 11.8g (59%)',
  '1 cup granulated sugar (for the filling)')  # on the website, missing here

n = 'Snickers cheesecake'
x = '5 g butter, melted ❣ Sat fat 2.5g (13%)'
R(n, x, L(x, 'for the bottom'))
x = '7.5 g butter ❣ Sat fat 3.8g (19%)'
R(n, x, L(x, 'for the topping'))

n = 'Peanut Butter Cookie Dough'
R(n, '19 g granulated sugar', '19 g granulated sugar (for the brownies)')
R(n, '6.2 g granulated sugar', '6.2 g granulated sugar (for the cookie dough layer)')

n = 'Low FODMAP chocolate donuts'
x = '20 ml lactose-free milk ♥ Sat fat 0.2g (1%)'
R(n, x, L(x, 'for the donuts'))
x = '1/2 tbsp lactose-free milk ♥ Sat fat 0.1g (0%)'
R(n, x, L(x, 'for the chocolate glaze'))

n = 'Breakfast - Cinnamon Rolls'
x = '3 tbsp butter, melted (butter is naturally very low in lactose, so lactose-free is optional) ❣ Sat fat 2.5g (13%)'
R(n, x, L(x, 'for the dough'))
x = '3 tbsp butter, softened ❣ Sat fat 2.5g (13%)'
R(n, x, L(x, 'for the filling'))
I(n, '1/2 cup cream cheese, softened (use lactose-free if you have it). Spread across 9 rolls this keeps each serving 💔 Sat fat 4.5g (22%)',
  '2 tbsp butter, softened (for the cream cheese glaze)  ' + scale_tags('❣ Sat fat 2.5g (13%)', 2 / 3))

n = 'Low FODMAP naked cake with lemon'
x = '28 g soft butter  💔 Sat fat 14.4g (72%)'
R(n, x, L(x, 'for the cake'))
x = '22 g soft butter  💔 Sat fat 11g (55%)'
R(n, x, L(x, 'for the lemon buttercream'))

n = 'Low FODMAP apple crumble bars'
x = '9 g unsalted butter, melted 💔 Sat fat 4.6g (23%)'
R(n, x, L(x, 'for the crust'))
x = '4 g unsalted butter, cold and cubed ❣ Sat fat 2g (10%)'
R(n, x, L(x, 'for the crumble topping'))
R(n, '12 g gluten-free flour blend 🟢 Fructans 24%', '12 g gluten-free flour blend (for the crust) 🟢 Fructans 24%')
R(n, '1/4 tsp gluten-free flour blend 🟢 Fructans 2%', '1/4 tsp gluten-free flour blend (for the apple filling) 🟢 Fructans 2%')
R(n, '4 g gluten-free flour blend 🟢 Fructans 8%', '4 g gluten-free flour blend (for the crumble topping) 🟢 Fructans 8%')

n = 'Gluten-free lemon cheesecake bars'
R(n, '6.2 g granulated sugar', '6.2 g granulated sugar (for the bottom)')
I(n, '28 g lactose-free cream cheese, at room temperature 💔 Sat fat 5.6g (28%)',
  '6.2 g granulated sugar (for the cheesecake layer)')  # on the website, missing here
R(n, '9.4 g granulated sugar', '9.4 g granulated sugar (for the topping)')

n = 'Gluten-free carrot cake cheesecake'
x = '1/8 tsp gluten-free flour 🟢 Fructans 1%'
R(n, x, '1/12 tsp gluten-free flour (for the cheesecake layer) 🟢 Fructans 1%')
x = '13 g gluten-free flour 🟡 Fructans 26%'
R(n, x, L(x, 'for the carrot cake layer'))

n = 'Gluten-free lemon meringue tartlets'
R(n, '16 g sugar', '16 g sugar (for the lemon filling)')
R(n, '25 g sugar', '25 g sugar (for the meringue)')

n = 'Low FODMAP Greek Meatballs with Creamy'
R(n, '5/16 tsp chopped fresh dill', '5/16 tsp chopped fresh dill (for the meatballs)')
R(n, '1/12 tsp chopped fresh dill', '1/12 tsp chopped fresh dill (for the sauce)')

n = 'Gluten-free Potato Croquettes'
x = '2 eggs ❣ Sat fat 3.2g (16%)'
R(n, x, L(x, 'for the croquette mixture'))

n = 'Low FODMAP meatballs in tomato sauce'
R(n, '1 tsp cumin', '1 tsp cumin (for the meatballs)')
R(n, '½ tsp cumin', '½ tsp cumin (for the tomato sauce)')

n = 'Field Doctor Pumpkin Pie Bars'
R(n, '60g light brown sugar, plus extra for sprinkling', '60g light brown sugar, plus extra for sprinkling (for the crust)')
R(n, '125g light brown sugar', '125g light brown sugar (for the filling)')
R(n, '¼ teaspoon fine salt', '¼ teaspoon fine salt (for the crust)')
R(n, '½ teaspoon salt', '½ teaspoon salt (for the filling)')


async def main():
    con = sqlite3.connect(f'file:{LOCAL_DB.as_posix()}?mode=ro', uri=True)
    pending = con.execute("select count(*) from recipes where coalesce(status,'')!='unmodified' or coalesce(is_synced,1)=0").fetchone()[0]
    rows = con.execute("select uid, name from recipes where coalesce(in_trash,0)=0").fetchall()
    con.close()
    print('pending', pending)
    if pending and APPLY:
        raise SystemExit('not applied')
    u, p = paprika_credentials(); lim = RateLimiter(0.6)
    problems, written = [], 0
    async with aiohttp.ClientSession() as s:
        st, b = await api_json(s, lim, 'POST', f'{PAPRIKA_API}/v1/account/login', data={'email': u, 'password': p})
        h = {'Authorization': f"Bearer {b['result']['token']}"}
        for frag, ops in OPS.items():
            uids = [uid for uid, nm in rows if frag in nm]
            if len(uids) != 1:
                problems.append(f'{frag}: {len(uids)} recipes match'); continue
            url = f'{PAPRIKA_API}/v2/sync/recipe/{uids[0]}/'
            st, b = await api_json(s, lim, 'GET', url, headers=h); r = b['result']
            lines = r['ingredients'].split('\n'); new = list(lines); ok = True
            for op, old, val in ops:
                if old not in new:
                    problems.append(f'{frag}: line not found: {old}'); ok = False; break
                i = new.index(old)
                if op == 'replace': new[i] = val
                elif op == 'delete': del new[i]
                else: new.insert(i + 1, val)
            if not ok: continue
            print('=====', r['name'])
            for op, old, val in ops: print(f'  {op}: {old[:60]}' + (f'\n      -> {val}' if val else ''))
            if not APPLY or new == lines: continue
            open('docs/hand-review-backup.jsonl', 'a', encoding='utf-8').write(json.dumps({'uid': uids[0], 'name': r['name'], 'servings': r.get('servings'), 'ingredients': r['ingredients']}, ensure_ascii=False) + '\n')
            upd = dict(r, ingredients='\n'.join(new)); upd['hash'] = calc_hash(upd)
            form = aiohttp.FormData(); form.add_field('data', gzip.compress(json.dumps(upd, separators=(',', ':')).encode()), content_type='application/octet-stream', filename='data')
            await lim.wait_turn()
            async with s.post(url, headers=h, data=form) as resp:
                assert '"result":true' in (await resp.text()).replace(' ', ''), r['name']
            st, b = await api_json(s, lim, 'GET', url, headers=h)
            assert b['result']['ingredients'] == '\n'.join(new), 'read-back mismatch ' + r['name']
            written += 1
        if APPLY and written:
            await lim.wait_turn()
            async with s.post(f'{PAPRIKA_API}/v2/sync/notify/', headers=h) as resp: print('notify', resp.status)
    print('recipes', len(OPS), 'written', written)
    print('PROBLEMS', *problems, sep='\n  ')

asyncio.run(main())

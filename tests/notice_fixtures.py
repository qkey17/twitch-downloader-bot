"""Owner-supplied phrases, not claims about structured Twitch event metadata."""
import copy

PHRASES = [
    "whatwantt subscribed at Tier 1. They've subscribed for 17 months, currently on a 3 month streak! Ку",
    "Honn3sy subscribed at Tier 1. They've subscribed for 43 months!",
    "ljoshalata watched 240 consecutive streams and sparked a watch streak! я новый 240",
    "Honn3sy watched 185 consecutive streams and sparked a watch streak!",
    "Yazhe_zhal is gifting 1 Tier 1 Subs to Jeens's community! They've gifted a total of 1 in the channel!",
    "Yazhe_zhal gifted a Tier 1 sub to ega1lan_bs!",
    "Prylko subscribed at Tier 2. They've subscribed for 51 months! Привіт, Толік! Бажаю гарного стріма!",
    "qwertys0505 subscribed with Prime. They've subscribed for 18 months! реклама на 3 минуты",
    "wettal21 subscribed at Tier 1.",
    "xSTVOLx subscribed at Tier 1. They've subscribed for 9 months!",
    "nikitasergeevna watched 90 consecutive streams and sparked a watch streak! jeensEgg",
    "ObjectPrototype__proto__ watched 5 consecutive streams and sparked a watch streak! 日本 🎮 <script>window.fixtureXSS=1</script>",
    "sp0nkey subscribed at Tier 1. They've subscribed for 7 months! Два предложения. 🎮 https://example.invalid/a?b=1&c=2",
    "m1scliq watched 5 consecutive streams and sparked a watch streak! Привет! Kappa",
    "Alex__Kom watched 3 consecutive streams and sparked a watch streak! Здравствуйте.",
    "pulseyka subscribed at Tier 1. They've subscribed for 5 months! " + "Длинный комментарий 🎮 & 日本. " * 24,
]


def notice_rows():
    rows = []
    for i, text in enumerate(PHRASES):
        row = dict(t=f'00:00:{i:02}', user=text.split(' ')[0], text=text,
                   color='#bf94ff', badges=[], fragments=[dict(text=text)])
        if i == 9:
            row['user'] = 'STVOLove'
        if i in (4, 5):
            row['t'] = '01:14:48'  # Independent events, same timestamp.
        if i in (10, 13):
            token = 'jeensEgg' if i == 10 else 'Kappa'
            row['fragments'] = [dict(text=text[:-len(token)]), dict(text=token, emoteID='25')]
        rows.append(row)
    return rows


def mixed_rows(count, ordinary):
    """Small illustrative prefix plus notices at virtual-block boundaries."""
    examples = notice_rows()
    for i, row in enumerate(ordinary(count)):
        if i < len(examples) or i % 97 == 0:
            sample = copy.deepcopy(examples[i % len(examples)])
            if i >= len(examples):
                sample['t'] = row['t']
            yield sample
        else:
            yield row

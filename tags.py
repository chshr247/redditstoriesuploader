"""Hashtags picked from the text of the video itself, without an LLM call.

The pool used to be ten tags shuffled per upload, which meant a story about a
neighbour's flood and a fact about octopuses got the same #драма #отношения.
Matching them to the video is worth doing; paying a model to do it is not - it
would be one more call per upload, on a request that has to be right every time
and cannot be checked afterwards. Stems are enough: the topics a Reddit story
turns on are a short list, and each one says its own words out loud - a mother
in law story cannot avoid saying свекровь.

So: match stems against topic buckets, take the best few, fill the rest from
the generic pool. Deterministic where it should be (the topic) and random where
it should be (which of the topic's tags, and the filler), so two videos about
money still do not carry identical text.
"""
import random
import re

from config import YT_HASHTAGS

# Kept in the same shape as everything else here: a stem, not a word. Russian
# inflects every one of these, and "\bсосед" covers сосед, соседка, соседями,
# соседского. The cost of a stem too short is a false match, so anything that
# collides with an unrelated word is written out longer: "матер" would fire on
# "материал", hence "\bматер[иь]".
#
# The buckets themselves are ranked by how many times they hit, so their order
# here means nothing. The order INSIDE a bucket does: the first two are what
# the topic is always about, the rest are narrower and only come out for the
# topic that dominated the text. #наследство is a fine tag to own and a bad one
# to put on a story that merely said the word деньги once.
TOPICS = [
    # Two buckets, not one: свекровь and тёща are different people, and a story
    # about the husband's mother tagged #тёща is wrong to everyone it reaches.
    # The stems are written ё-less because pick() folds ё to е before matching.
    (r"свекров|свекр|золовк|деверь|сноха|невестк",
     ["#свекровь", "#родня", "#семейныеконфликты"]),
    (r"тещ|тесть|\bзят[ья]",
     ["#тёща", "#родня", "#семейныеконфликты"]),
    (r"\bмуж\b|\bмужа\b|\bмужу\b|\bжена\b|\bжену\b|\bжене\b|\bпарн[ея]|"
     r"девушк|бывш|расстал|отношени|встречал",
     ["#отношения", "#личнаяжизнь"]),
    (r"влюб|\bлюб[ил]|\bлюблю\b|свидан|роман\b|признал[ася]+ в",
     ["#любовь", "#свидание", "#отношения"]),
    # Split off the relationship bucket rather than left as its narrow tags:
    # a divorce story that only says "жена" would otherwise come out tagged
    # #измена, which is a different video and a viewer who feels lied to.
    (r"развод|разошл|алимент|\bподал[аи]? в суд|раздел имуществ",
     ["#развод", "#алименты", "#отношения"]),
    (r"измен[ыяуе]|изменил|изменял|любовниц|любовник|\bналево\b",
     ["#измена", "#предательство", "#отношения"]),
    (r"свадьб|невест|жених|торжеств|тамада|фат[ае]",
     ["#свадьба", "#торжество", "#отношения"]),
    (r"\bмат[ьи]\b|\bматер[иь]|\bмам|\bотец\b|\bотца\b|\bпап|родител|"
     r"бабушк|дедушк|\bбрат|\bсестр|\bсын|\bдоч|племянн",
     ["#семья", "#родители", "#родственники"]),
    (r"\bдет[иейя]|ребён|ребен|малыш|подрост|коляск|садик|детсад",
     ["#дети", "#родители", "#воспитание"]),
    (r"деньг|рубл|\bдолг|кредит|зарплат|\bсчёт|\bсчет|ипотек|копил|"
     r"накоплен|тысяч|потрат|занял|верн[иу] деньги",
     ["#деньги", "#долги", "#жадность", "#жмот"]),
    (r"наследств|завещан|нотариус|наследник|\bдол[юя] в квартир",
     ["#наследство", "#завещание", "#родственники"]),
    (r"начальник|\bработ|офис|коллег|увол|собеседован|\bсмен[ауы]|"
     r"директор|подчинён|подчинен",
     ["#работа", "#начальник", "#коллеги", "#офис"]),
    # "квартир" is deliberately NOT here. It is said in every second family
    # story - the flat is where they happen - and it was putting #соседи on a
    # story whose only neighbour was the mother-in-law. A topic has to be named
    # by a word that belongs to it and nowhere else.
    (r"сосед|подъезд|\bжкх\b|управляющ|общежит|этажом|затопил|потоп",
     ["#соседи", "#жизньвдоме", "#квартира"]),
    (r"аренд|съём|съем квартир|квартирант|наймодат|хозяйк[аи] квартир",
     ["#аренда", "#квартира", "#жизньвдоме"]),
    (r"отомст|\bмест[ьию]\b|мстил|проучил|наказал|справедлив|поплатил|"
     r"пожалел",
     ["#месть", "#справедливость", "#проучил"]),
    (r"школ|учител|\bкласс|однокласс|\bуро[кв]|родительск[оа]м собран",
     ["#школа", "#учёба", "#уроки"]),
    (r"универ|студент|\bсесси|препод|общаг|экзамен|диплом",
     ["#студенты", "#универ", "#учёба"]),
    (r"собак|\bкот[аеуы]?\b|кошк|щенк|котён|котен|питом|ветеринар",
     ["#животные", "#питомцы", "#собаки"]),
    (r"врач|больниц|диагноз|операци|скорая|аптек|поликлин",
     ["#здоровье", "#врачи", "#больница"]),
    (r"машин|\bавто|гаи|дтп|парков|водител|штраф|гибдд",
     ["#авто", "#дорога", "#парковка"]),
    (r"кафе|ресторан|официант|заказ|доставк|магазин|касс|продавец|курьер",
     ["#сервис", "#клиенты", "#магазин"]),
]
TOPICS = [(re.compile(p), t) for p, t in TOPICS]
# Every tag a topic can earn. Nothing in here is ever handed out as filler:
# YT_HASHTAGS still carries #семья, #работа and #отношения from when the pool
# was one flat list, and drawing those at random is precisely the mismatch this
# module exists to remove - #отношения under a story about a boss.
TOPIC_TAGS = {t for _, tags in TOPICS for t in tags}

# What a video gets when nothing matched, and what fills the rest of the slots.
# Split by kind on purpose: #драма under a fact about octopuses is the exact
# mismatch this module exists to stop. YT_HASHTAGS is folded into the story
# side so the repo variable still means something - it is the same kind of tag.
GENERIC = {
    "story": ["#истории", "#реддит", "#жизненно", "#рекомендации", "#сторитайм",
              "#ситуация", "#драма", "#реальнаяистория", "#изжизни",
              "#историиизжизни", "#чтобывыделали", "#люди"],
    "fact": ["#факты", "#интересныефакты", "#этоинтересно", "#познавательно",
             "#интересное", "#рекомендации", "#фактдня", "#узналсегодня",
             "#полезнознать", "#наука"],
}

# Three matched plus two generic. All five matched reads as a tag wall for one
# topic and loses the broad feeds; all five generic is where we started.
MATCHED = 3
HEAD = 2        # how many tags at the front of a bucket count as its centre


def pick(title: str, body: str = "", kind: str = "story", n: int = 5) -> list[str]:
    """Up to `n` hashtags for one video, best-matching topics first."""
    # ё is written out everywhere in the narration on purpose (see script.py),
    # so the stems above would miss half their matches without this.
    text = f"{title} {body}".lower().replace("ё", "е")
    hits = sorted(((len(p.findall(text)), t) for p, t in TOPICS),
                  key=lambda h: h[0], reverse=True)

    out = []
    for i, (count, topic) in enumerate(hits):
        if count == 0 or len(out) >= MATCHED:
            break
        # The first tag is the topic's own name and the one worth carrying, so
        # it is drawn twice as often as its alternate. Straight rotation gave
        # #родня to half the mother-in-law stories, which is a weaker tag on
        # the video that had the exact word for it.
        head = topic[:HEAD]
        out.append(random.choice(head + head[:1]))
        # The topic that dominated the text - and only it - gets a second,
        # narrower tag. Every other topic contributes one: two subjects named
        # beats one subject said three ways.
        if i == 0 and len(topic) > HEAD:
            out.append(random.choice(topic[HEAD:]))

    filler = [t for t in dict.fromkeys(
        GENERIC.get(kind, GENERIC["story"])
        + (list(YT_HASHTAGS) if kind != "fact" else []))
        if t not in TOPIC_TAGS]
    random.shuffle(filler)
    out = list(dict.fromkeys(out))
    for t in filler:
        if len(out) >= n:
            break
        if t not in out:
            out.append(t)
    return out[:n]


if __name__ == "__main__":
    # Which tag of a bucket comes out is random by design, so the tests ask
    # whether the right BUCKET was reached, never for a particular tag.
    def bucket(stem: str) -> list[str]:
        return next(t for p, t in TOPICS if p.search(stem))

    def hit(tags_: list[str], stem: str) -> bool:
        return bool(set(tags_) & set(bucket(stem)))

    money = pick("Мать сняла с моей карты 40000 на футбол брата",
                 "Я копила на квартиру. Мама забрала деньги без спроса.")
    assert hit(money, "деньги") and hit(money, "мама"), money

    law = pick("Свекровь въехала в нашу квартиру, пока мы были в отпуске",
               "Свекровь сказала, что квартира всё равно её сына.")
    assert hit(law, "свекровь"), law

    # ё in the text must not hide the stem behind it
    kid = pick("Ребёнок соседки разбил окно", "Соседка не платила.")
    assert hit(kid, "ребенок") and hit(kid, "сосед"), f"ё broke the matching: {kid}"

    # the topic that dominated gets two tags, the runner-up one
    assert len(set(pick("Сосед затопил соседей", "Сосед, сосед, соседка."))
               & set(bucket("сосед"))) == 2

    # a fact never gets the story pool, matched or not
    fact = pick("У осьминога три сердца и голубая кровь",
                "Кровь синеет из-за меди.", kind="fact")
    assert all(t in GENERIC["fact"] for t in fact), fact
    assert "#драма" not in fact and "#истории" not in fact, fact

    # nothing matched is still five usable tags, not an empty line
    blank = pick("Заголовок без темы", "Текст ни о чём")
    assert len(blank) == 5, blank
    # ...and not one of them is a topic tag nobody earned
    assert not (set(blank) & TOPIC_TAGS), blank
    boss = pick("Начальник заставил меня выйти в выходной", "Директор давил.")
    assert not (set(boss) & set(bucket("отношения"))), f"unearned topic tag: {boss}"

    # and two uploads of the same video must not carry identical text
    same = {" ".join(pick("Сосед прислал счёт на 80000", "Соседка затопила нас."))
            for _ in range(30)}
    assert len(same) > 3, "tags are not rotating"

    for t, b, k in [("Мать сняла с моей карты 40000 на футбол брата", "", "story"),
                    ("Начальник заставил меня выйти в выходной", "", "story"),
                    ("Сосед перекрыл нам воду на три дня", "", "story"),
                    ("У осьминога три сердца", "", "fact")]:
        print(f"{k:5} {t[:45]:47} {' '.join(pick(t, b, k))}")

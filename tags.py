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

from config import OUTPUT_LANG, YT_HASHTAGS

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
TOPICS_RU = [
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

# The same idea in English, and not a translation of the list above: the topics
# a Russian Reddit retelling turns on and the ones an English one turns on are
# not the same set. Two differences worth knowing:
#   - свекровь and тёща are two buckets in Russian because they are two people;
#     English has one word for both, so it is one bucket.
#   - "entitled" has no Russian bucket at all. It is a whole genre here and the
#     posts say the word out loud, which is exactly what a bucket needs.
# Stems again, not words: English inflects less but still enough - "\bneighbo"
# covers neighbor, neighbour, neighbors, neighbourhood.
TOPICS_EN = [
    (r"mother.in.law|father.in.law|sister.in.law|brother.in.law|in.laws?\b"
     r"|\bmil\b|\bfil\b|\bsil\b",
     ["#inlaws", "#motherinlaw", "#familydrama"]),
    (r"\bhusband|\bwife\b|\bwives\b|girlfriend|boyfriend|\bfianc|\bex\b"
     r"|broke\s+up|relationship|\bdating\b",
     ["#relationships", "#couples"]),
    (r"\bin\s+love\b|first\s+date|\bdates?\b|romantic|proposed|proposal",
     ["#dating", "#love", "#relationships"]),
    (r"divorc|separat|alimony|custody|child\s+support|split\s+the\s+house",
     ["#divorce", "#custody", "#relationships"]),
    (r"cheat|affair|mistress|unfaithful|another\s+man|another\s+woman",
     ["#cheating", "#betrayal", "#relationships"]),
    (r"wedding|bride|groom|bridesmaid|maid\s+of\s+honor|reception|registry",
     ["#wedding", "#bridezilla", "#relationships"]),
    (r"\bmom\b|\bmum\b|mother\b|\bdad\b|father\b|parents?\b|grandma|grandmother"
     r"|grandpa|grandfather|\bbrother|\bsister|\bson\b|daughter|\bnephew|\bniece"
     r"|\bcousin",
     ["#family", "#parents", "#relatives"]),
    (r"\bkids?\b|\bchild|toddler|baby\b|babies|teenager|stroller|daycare"
     r"|babysit",
     ["#kids", "#parenting", "#raisingkids"]),
    (r"\bmoney\b|\bdebt|\bloan|credit\s+card|paycheck|salary|\bbill\b|\bbills\b"
     r"|mortgage|savings|\bowe[ds]?\b|borrow|\brent\s+money",
     ["#money", "#debt", "#greed", "#cheapskate"]),
    (r"inherit|\bwill\b|estate|notary|heir|life\s+insurance",
     ["#inheritance", "#will", "#relatives"]),
    (r"\bboss\b|manager|\bwork\b|office|coworker|colleague|\bfired\b|quit\s+my"
     r"|interview|\bshift\b|\bhr\b",
     ["#work", "#boss", "#coworkers", "#officelife"]),
    # "apartment" is deliberately NOT here, for the same reason "квартир" is
    # not in the Russian list: it is where every second family story happens.
    (r"neighbo|\bhoa\b|apartment\s+building|upstairs|next\s+door|\bflood(ed)?\b"
     r"|\bfence\b|\bdriveway",
     ["#neighbors", "#hoa", "#apartmentliving"]),
    (r"landlord|\blease\b|\btenant|\brenting\b|\bdeposit\b|eviction|evict",
     ["#landlord", "#renting", "#apartmentliving"]),
    (r"revenge|got\s+even|karma|\btaught\s+\w+\s+a\s+lesson|payback|regret\s+it"
     r"|malicious\s+compliance",
     ["#revenge", "#karma", "#pettyrevenge"]),
    (r"school|teacher|classroom|classmate|principal|\bhomework|parent.teacher",
     ["#school", "#teachers", "#students"]),
    (r"college|university|dorm|roommate|professor|\bexam|semester|tuition",
     ["#college", "#roommates", "#students"]),
    (r"\bdog\b|\bdogs\b|\bcat\b|\bcats\b|puppy|kitten|\bpets?\b|\bvet\b",
     ["#pets", "#animals", "#dogs"]),
    (r"doctor|hospital|diagnos|surgery|ambulance|pharmac|\bnurse|\ber\b",
     ["#health", "#doctors", "#hospital"]),
    (r"\bcar\b|\bcars\b|driving|parking|\btow(ed)?\b|traffic|accident|\bticket\b"
     r"|insurance\s+claim",
     ["#cars", "#driving", "#parking"]),
    (r"restaurant|\bcafe\b|waitress|waiter|\bserver\b|\border\b|delivery"
     r"|\bstore\b|cashier|\btip\b|\btipping\b",
     ["#customerservice", "#serverlife", "#retail"]),
    (r"entitled|\bkaren\b|demanded|manager\s+now|refused\s+to\s+pay"
     r"|\bthe\s+audacity\b",
     ["#entitled", "#karen", "#audacity"]),
]

TOPICS = {"ru": [(re.compile(p), t) for p, t in TOPICS_RU],
          "en": [(re.compile(p, re.I), t) for p, t in TOPICS_EN]}
# Every tag a topic can earn, per language. Nothing in here is ever handed out
# as filler: YT_HASHTAGS still carries #семья, #работа and #отношения from when
# the pool was one flat list, and drawing those at random is precisely the
# mismatch this module exists to remove - #отношения under a story about a boss.
TOPIC_TAGS = {lang: {t for _, tags in buckets for t in tags}
              for lang, buckets in TOPICS.items()}

# What a video gets when nothing matched, and what fills the rest of the slots.
# YT_HASHTAGS is folded in, so the repo variable still means something - it is
# the same kind of tag.
GENERIC = {
    "ru": ["#истории", "#реддит", "#жизненно", "#рекомендации", "#сторитайм",
           "#ситуация", "#драма", "#реальнаяистория", "#изжизни",
           "#историиизжизни", "#чтобывыделали", "#люди", "#ктоправ",
           "#ктовиноват", "#рассудите"],
    "en": ["#redditstories", "#storytime", "#reddit", "#aita", "#truestory",
           "#drama", "#reallife", "#relatable", "#whatwouldyoudo",
           "#storytelling", "#people", "#fyp", "#amithewrongone", "#whosright"],
}

# Three matched plus two generic. All five matched reads as a tag wall for one
# topic and loses the broad feeds; all five generic is where we started.
MATCHED = 3
HEAD = 2        # how many tags at the front of a bucket count as its centre


def pick(title: str, body: str = "", n: int = 5, lang: str = "") -> list[str]:
    """Up to `n` hashtags for one video, best-matching topics first."""
    lang = lang or OUTPUT_LANG
    # ё is written out everywhere in the narration on purpose (see script.py),
    # so the stems above would miss half their matches without this. Harmless
    # on English text, which has no ё to fold.
    text = f"{title} {body}".lower().replace("ё", "е")
    hits = sorted(((len(p.findall(text)), t) for p, t in TOPICS[lang]),
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
        GENERIC[lang]
        # YT_HASHTAGS belongs to the channel this process is, so it is only
        # folded in when the caller asked for that channel's language
        + (list(YT_HASHTAGS) if lang == OUTPUT_LANG else []))
        if t not in TOPIC_TAGS[lang]]
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
    # whether the right BUCKET was reached, never for a particular tag. Both
    # languages run whichever channel this process is: a bucket set is only
    # ever exercised by its own channel otherwise, and a broken one there shows
    # up as tags that are merely bland rather than as an error.
    def bucket(stem: str, lang: str = "ru") -> list[str]:
        return next(t for p, t in TOPICS[lang] if p.search(stem))

    def hit(tags_: list[str], stem: str, lang: str = "ru") -> bool:
        return bool(set(tags_) & set(bucket(stem, lang)))

    money = pick("Мать сняла с моей карты 40000 на футбол брата",
                 "Я копила на квартиру. Мама забрала деньги без спроса.", lang="ru")
    assert hit(money, "деньги") and hit(money, "мама"), money

    law = pick("Свекровь въехала в нашу квартиру, пока мы были в отпуске",
               "Свекровь сказала, что квартира всё равно её сына.", lang="ru")
    assert hit(law, "свекровь"), law

    # ё in the text must not hide the stem behind it
    kid = pick("Ребёнок соседки разбил окно", "Соседка не платила.", lang="ru")
    assert hit(kid, "ребенок") and hit(kid, "сосед"), f"ё broke the matching: {kid}"

    # the topic that dominated gets two tags, the runner-up one
    assert len(set(pick("Сосед затопил соседей", "Сосед, сосед, соседка.", lang="ru"))
               & set(bucket("сосед"))) == 2

    # nothing matched is still five usable tags, not an empty line
    blank = pick("Заголовок без темы", "Текст ни о чём", lang="ru")
    assert len(blank) == 5, blank
    # ...and not one of them is a topic tag nobody earned
    assert not (set(blank) & TOPIC_TAGS["ru"]), blank
    boss = pick("Начальник заставил меня выйти в выходной", "Директор давил.", lang="ru")
    assert not (set(boss) & set(bucket("отношения"))), f"unearned topic tag: {boss}"

    # and two uploads of the same video must not carry identical text
    same = {" ".join(pick("Сосед прислал счёт на 80000", "Соседка затопила нас.",
                          lang="ru")) for _ in range(30)}
    assert len(same) > 3, "tags are not rotating"

    # The same battery in English. Same order as above, so a bucket that only
    # one language has is visible as a gap rather than as a missing line.
    money_en = pick("Mom took 40000 off my card for my brother",
                    "I was saving for a place. She took the money.", lang="en")
    assert hit(money_en, "money", "en") and hit(money_en, "mom", "en"), money_en

    law_en = pick("My mother-in-law moved in while we were on vacation",
                  "She said the apartment was her son's anyway.", lang="en")
    assert hit(law_en, "mother-in-law", "en"), law_en

    kid_en = pick("The neighbor's kid broke our window",
                  "The neighbor never paid for it.", lang="en")
    assert hit(kid_en, "kids", "en") and hit(kid_en, "neighbor", "en"), kid_en

    assert len(set(pick("Neighbor flooded the neighbors below",
                        "The neighbor, the neighbor, that neighbor.", lang="en"))
               & set(bucket("neighbor", "en"))) == 2

    blank_en = pick("A title about nothing", "Text about nothing", lang="en")
    assert len(blank_en) == 5, blank_en
    assert not (set(blank_en) & TOPIC_TAGS["en"]), blank_en
    boss_en = pick("My boss made me come in on my day off",
                   "The manager kept pushing.", lang="en")
    assert not (set(boss_en) & set(bucket("relationship", "en"))), boss_en

    # the two bucket sets must not share tags: a Russian tag on an English
    # video reaches nobody who can read it, and the other way round
    assert not (TOPIC_TAGS["ru"] & TOPIC_TAGS["en"]), "the tag pools overlap"

    for lang, rows in [
        ("ru", [("Мать сняла с моей карты 40000 на футбол брата", ""),
                ("Начальник заставил меня выйти в выходной", ""),
                ("Сосед перекрыл нам воду на три дня", "")]),
        ("en", [("Mom took 40000 off my card for my brother", ""),
                ("My boss made me come in on my day off", ""),
                ("The neighbor shut off our water for three days", "")])]:
        for t, b in rows:
            print(f"{lang} {t[:45]:47} {' '.join(pick(t, b, lang=lang))}")

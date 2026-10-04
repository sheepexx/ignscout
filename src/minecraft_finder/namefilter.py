"""Local filter for names that Mojang's username filter is very likely to refuse.

Mojang rejects offensive names (profanity, sexual terms, slurs, hate symbols) with
``NOT_ALLOWED``, but that answer only comes from the token-backed availability check.
Without a token such names come back as "no profile found" and would look available,
and because they are short real words they would even rank near the top.

This module flags them locally. The word list is conservative and certainly
incomplete (Mojang's real filter is not public). Flagged names are stored as
BLOCKED with confidence "unverified", so they never appear as available.

Matching:

* the name (and the source word it was built from) is compared after removing common
  English endings, so "boners", "bitchy" and "raping" match "boner", "bitch" and "rape";
* a short list of unambiguous fragments is also matched anywhere inside a name
  ("xxfuckxx"), but ambiguous ones are not, so bass, therapist and scrape stay allowed.
"""

from __future__ import annotations

from .models import CheckResult, Confidence, Status

# Matched as whole words, also with common endings (s, es, ed, er, ing, y, ly, ...).
_WORDS = frozenset(
    """
    anal anus arse arsehole ass asshole autoerotic autoeroticism autoerotism bastard bdsm bellend bestiality bitch blowjob bollock bollocks
    boner boob boobie booby bugger bukkake butthole buttplug camgirl chink clit clitoris cock cocksucker
    coital coition coitus coon creampie dago dike cum cumming cumshot cunnilingus cunt deepthroat dick dildo douche douchebag
    dumbass dyke ejaculate ejaculation erection erotic erotica fag faggot fap fellatio fisting fuck
    gangbang genital genitals gook grope handjob hentai heil hitler hoer homo honky horny hussy hymen incest jackass jap jizz kike
    kkk labia lesbo lube masturbate masturbation milf molest molester nazi necrophilia negro nigga nigger
    nipple nooky nookie nude orgasm orgy paedo paki pedo pedophile penis piss porn porno pube pubes pussy queef rape
    pubic rapist retard rimjob scrotum semen sex sexy shit slut smut sodom sodomite sodomize sodomy spastic spaz sperm spic
    swastika testicle threesome tit titty tranny twat vagina vibrator vulva wank wanker wetback whore
    xxx yid zoophile
    """.split()
)

# Matched anywhere inside a name. Only fragments that (almost) never occur in innocent words.
_FRAGMENTS = (
    "fuck", "shit", "cunt", "nigger", "nigga", "faggot", "whore", "jizz", "porn", "dildo", "penis",
    "vagina", "pussy", "bitch", "slut", "hitler", "blowjob", "handjob", "rimjob", "cumshot", "xxx",
)  # fmt: skip

# Innocent words that the ending rules would otherwise catch.
_ALLOWED = frozenset(
    {
        "spicy", "spicier", "spiciest", "spicily", "titer", "titers", "dicker", "dickered", "dickering", "dickers",
        "japes", "japed", "japer", "japers", "japery", "japing", "yiddish",
    }
)  # fmt: skip

_ENDINGS = ("ings", "iest", "ing", "ers", "ier", "ies", "ish", "est", "ity", "es", "ed", "er", "ly", "ie", "s", "y")

PROBABLY_BLOCKED_DETAIL = (
    "Contains a word that Mojang's name filter almost certainly refuses (local word list), so it "
    "cannot be claimed even though no profile uses it."
)
SKIPPED_DETAIL = (
    "Not looked up: contains a word that Mojang's name filter almost certainly refuses (local word list)."
)


def _stems(word: str) -> set[str]:
    stems = {word}
    for ending in _ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 2:
            base = word[: -len(ending)]
            stems.add(base)
            stems.add(base + "e")  # raping -> rape
            if ending == "ies":
                stems.add(base + "y")  # titties -> titty
            if len(base) >= 3 and base[-1] == base[-2]:
                stems.add(base[:-1])  # cummed -> cum
    return stems


def _offensive_word(word: str) -> bool:
    word = word.lower()
    if not word or word in _ALLOWED:
        return False
    letters = "".join(ch for ch in word if ch.isalpha())
    if any(fragment in letters for fragment in _FRAGMENTS):
        return True
    return any(stem in _WORDS for stem in _stems(letters))


def is_offensive(name: str, source_word: str | None = None) -> bool:
    """True if the name, or the word it was built from, is very likely refused by Mojang."""
    return _offensive_word(name) or (source_word is not None and _offensive_word(source_word))


def flag_if_offensive(result: CheckResult, source_word: str | None = None) -> CheckResult:
    """Turn an AVAILABLE/SOON result for an offensive name into a probable BLOCKED."""
    if result.status in (Status.AVAILABLE, Status.SOON) and is_offensive(result.username, source_word or result.source_word):
        return result.evolve(
            status=Status.BLOCKED, confidence=Confidence.UNVERIFIED, available_at=None, detail=PROBABLY_BLOCKED_DETAIL
        )
    return result


def skipped_result(username: str, provider: str, source_word: str | None) -> CheckResult:
    """Result for an offensive name that is not worth a request."""
    return CheckResult(
        username=username,
        status=Status.BLOCKED,
        provider=provider,
        confidence=Confidence.UNVERIFIED,
        detail=SKIPPED_DETAIL,
        source_word=source_word,
    )


def reclassify_existing(database: object) -> int:
    """Mark stored AVAILABLE/SOON results of offensive names as probably blocked. Returns the count."""
    names = [username for username, source in database.discovery_names() if is_offensive(username, source)]  # type: ignore[attr-defined]
    return database.mark_probably_blocked(names, PROBABLY_BLOCKED_DETAIL) if names else 0  # type: ignore[attr-defined]

"""relay.textutil (foundation): tags, stripping, sentences, word normalisation, TTS-word → sentence alignment.

Written by a different builder than the module, on purpose: these tests try to break it. The bugs they found are
fixed; their tests stay as regressions.
"""

from __future__ import annotations

import random

import pytest

from relay.textutil import (POSITIONAL_PROSODY, TAG_VALUES, Tag, abbreviations, acronyms, align_to_sentences,
                            ends_sentence, find_tags, has_digits, leading_tags, mid_sentence_breaks, norm_word,
                            sentence_words, split_sentences, strip_tags, word_count)

# --- tag vocabulary -------------------------------------------------------------------------------------------------

def test_tag_vocabulary_matches_the_tts_docs():
    # doc/higgs-tts.md, "Inline control tags"
    assert {kind: len(values) for kind, values in TAG_VALUES.items()} == {"emotion": 21, "style": 3, "sfx": 9,
                                                                         "prosody": 10}
    assert POSITIONAL_PROSODY == {"pause", "long_pause"} <= TAG_VALUES["prosody"]
    assert {"enthusiasm", "contemplation", "helplessness"} <= TAG_VALUES["emotion"]
    assert all(value == value.lower() for values in TAG_VALUES.values() for value in values)


@pytest.mark.parametrize("kind,value", sorted((kind, value) for kind, values in TAG_VALUES.items() for value in values))
def test_every_documented_tag_is_known(kind, value):
    [tag] = find_tags(f"<|{kind}:{value}|>Hi.")
    assert (tag.kind, tag.value, tag.known) == (kind, value, True)
    assert tag.positional == (kind == "sfx" or value in ("pause", "long_pause"))


# --- find_tags / leading_tags ---------------------------------------------------------------------------------------

def test_find_tags_positions_and_raw_text():
    text = "<|emotion:enthusiasm|>Hi <|prosody:pause|> there."
    first, second = find_tags(text)
    assert first == Tag("emotion", "enthusiasm", 0, 22, "<|emotion:enthusiasm|>")
    assert second == Tag("prosody", "pause", 25, 42, "<|prosody:pause|>")
    assert text[second.start:second.end] == second.raw


@pytest.mark.parametrize("raw,kind,value", [
    ("<|Emotion:Enthusiasm|>", "emotion", "enthusiasm"),  # case-insensitive, raw kept
    ("<| emotion : enthusiasm |>", "emotion", "enthusiasm"),  # inner spaces
    ("<|emotion:\nenthusiasm|>", "emotion", "enthusiasm"),  # inner newline
    ("<|prosody:long_pause|>", "prosody", "long_pause"),
])
def test_find_tags_tolerant_spellings(raw, kind, value):
    [tag] = find_tags(raw + "Hi.")
    assert (tag.kind, tag.value, tag.raw, tag.known) == (kind, value, raw, True)


@pytest.mark.parametrize("raw", ["<|emotion|>", "<|emotion:2|>", "<||>", "<|a:b:c|>", "<|emo tion:awe|>",
                                 "<|:awe|>", "<|emotion:|>", "<|emotion:a|b|>"])
def test_malformed_tags_come_back_with_an_empty_kind(raw):
    [tag] = find_tags(f"x {raw} y.")
    assert (tag.kind, tag.value, tag.raw, tag.known, tag.positional) == ("", "", raw, False, False)


@pytest.mark.parametrize("raw,kind,value", [("<|emotion:joy|>", "emotion", "joy"), ("<|mood:happy|>", "mood", "happy"),
                                            ("<|sfx:pause|>", "sfx", "pause"), ("<|style:enthusiasm|>", "style",
                                                                                "enthusiasm")])
def test_unknown_tags_are_parsed_but_not_known(raw, kind, value):
    [tag] = find_tags(raw)
    assert (tag.kind, tag.value, tag.known) == (kind, value, False)


def test_tags_never_span_each_other():
    assert [tag.raw for tag in find_tags("<|a:b|> and <|c:d|>")] == ["<|a:b|>", "<|c:d|>"]


@pytest.mark.parametrize("text", ["<|emotion:enthusiasm Here is how.", "Here is how. emotion:enthusiasm|>",
                                  "a <|> b.", "no tags here.", ""])
def test_unclosed_or_absent_tags_are_not_found(text):
    assert find_tags(text) == []


@pytest.mark.parametrize("kind,value,positional", [("prosody", "pause", True), ("prosody", "long_pause", True),
                                                   ("sfx", "laughter", True), ("sfx", "anything", True),
                                                   ("prosody", "speed_fast", False), ("emotion", "awe", False),
                                                   ("style", "whispering", False), ("", "", False)])
def test_positional(kind, value, positional):
    assert Tag(kind, value, 0, 0, "").positional is positional


@pytest.mark.parametrize("text,count", [
    ("<|emotion:enthusiasm|>Hi.", 1),
    ("<|emotion:enthusiasm|> <|prosody:speed_slow|>Hi.", 2),
    ("  \n<|emotion:enthusiasm|>Hi.", 1),
    ("<|prosody:pause|><|emotion:awe|>Wow.", 2),  # a positional tag at the start is still leading
    ("<|emotion:enthusiasm|>Hi <|style:whispering|>there.", 1),
    ("Hi <|emotion:enthusiasm|>there.", 0),
    ("<|emotion|><|emotion:awe|>Hi.", 2),  # malformed ones count too: the validator reports them separately
    ("", 0),
    ("<|emotion:awe|>", 1),
])
def test_leading_tags(text, count):
    lead = leading_tags(text)
    assert len(lead) == count
    assert lead == find_tags(text)[:count]


# --- strip_tags -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("<|emotion:enthusiasm|>Here is how.", "Here is how."),
    ("Wait <|prosody:pause|>, what?", "Wait, what?"),
    ("Wait<|prosody:pause|>.", "Wait."),
    ("a<|prosody:pause|>b", "a b"),  # a tag between words leaves one space
    ("Really ?", "Really?"),
    ("Hello , world", "Hello, world"),
    ("  many   spaces\n\nnewline  ", "many spaces newline"),
    ("tab\there.", "tab here."),
    ("a b.", "a b."),
    ("<|prosody:pause|>", ""),
    ("<|emotion|>Hi.", "Hi."),  # malformed tags are stripped too
    ("<|emotion:enthusiasm Hi.", "<|emotion:enthusiasm Hi."),  # unclosed: left alone (and spoken!)
    ("That is it. <|prosody:pause|> I would love a question.", "That is it. I would love a question."),
    ("A — B.", "A — B."),
    ("", ""),
])
def test_strip_tags(text, expected):
    assert strip_tags(text) == expected


def test_strip_tags_is_idempotent():
    for text in ("Wait <|prosody:pause|>, what?", "a <|sfx:laughter|>Haha b.", "x  y ."):
        assert strip_tags(strip_tags(text)) == strip_tags(text)


def test_strip_tags_keeps_dot_prefixed_words_apart():  # regression: 'the .env' used to become 'the.env'
    assert strip_tags("Use the .env file.") == "Use the .env file."


# --- sentences ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("One. Two! Three? Four…", ["One.", "Two!", "Three?", "Four…"]),
    ('He said "stop." Then he left.', ['He said "stop."', "Then he left."]),
    ("She said “go.” And went.", ["She said “go.”", "And went."]),
    ("(Like this.) Next one.", ["(Like this.)", "Next one."]),
    ("“Go.” “Now!” (Really?) [Yes.]", ["“Go.”", "“Now!”", "(Really?)", "[Yes.]"]),
    ("Wait... what happened?", ["Wait...", "what happened?"]),
    ("Really?! Yes.", ["Really?!", "Yes."]),
    ("Wait!!! What??", ["Wait!!!", "What??"]),
    ("No end here", ["No end here"]),
    ("One. Two", ["One.", "Two"]),
    ("example.com is fine.", ["example.com is fine."]),
    ("Hello.World.", ["Hello.World."]),
    ("One.\nTwo.", ["One.", "Two."]),
    ("<|emotion:enthusiasm|>One. <|prosody:pause|> Two.", ["One.", "Two."]),
    ("Hello world. — Bye.", ["Hello world.", "— Bye."]),
    ("", []),
    ("   ", []),
    ("<|prosody:pause|>", []),
])
def test_split_sentences(text, expected):
    assert split_sentences(text) == expected


def test_split_sentences_round_trips_the_stripped_text():
    text = "<|emotion:awe|>Wow. That is “big.” (Really!) Is it… real? Yes"
    assert " ".join(split_sentences(text)) == strip_tags(text)


@pytest.mark.parametrize("text,expected", [
    ("Hello.", True), ("Hello", False), ('Hello."', True), ("Hello.)", True), ("Done.]", True), ("Done!’", True),
    ("Hello…", True), ("Hello...", True), ("Why?!", True), ("Hello:)", False), ("Hello,", False), ("Hello —", False),
    ("", False), ("<|prosody:pause|>", False), ("Hello. <|prosody:pause|>", True), ("Hello.<|emotion:awe|>", True),
    ("   ", False), ("(Hello)", False),
])
def test_ends_sentence(text, expected):
    assert ends_sentence(text) is expected


@pytest.mark.parametrize("word,expected", [
    ("You've,", "youve"), ("you’ve", "youve"), ("Hello!", "hello"), ("“Quoted.”", "quoted"), ("—", ""), ("...", ""),
    ("COVID-19", "covid19"), ("e-mail", "email"), ("A.I.", "ai"), ("3rd", "3rd"), ("Ünïcödé", "ncd"), ("", ""),
    ("real-time", "realtime"),
])
def test_norm_word(word, expected):
    assert norm_word(word) == expected


def test_sentence_words():
    assert sentence_words("I said: “Hello there.” Then — left!") == [
        ("I", 0), ("said:", 0), ("“Hello", 0), ("there.”", 0), ("Then", 1), ("left!", 1)]
    assert sentence_words("<|emotion:awe|>Wow. <|prosody:pause|> Big.") == [("Wow.", 0), ("Big.", 1)]
    assert sentence_words("") == [] and sentence_words("— …") == []


# --- align_to_sentences ---------------------------------------------------------------------------------------------

TEXT = "Here is how this works. Every word has a timestamp. Then we resume."
SENTENCES = [0] * 5 + [1] * 5 + [2] * 3


def test_align_exact_words():
    assert align_to_sentences([word for word, _ in sentence_words(TEXT)], TEXT) == SENTENCES


def test_align_ignores_case_and_punctuation():
    words = ["here", "IS", "how", "this", "works", "every", "word", "has", "a", "timestamp", "then", "we", "resume"]
    assert align_to_sentences(words, TEXT) == SENTENCES


def test_align_tts_merges_and_splits_words():
    text = "Relay replies in real-time. That is why real time matters. Done."
    split = ["Relay", "replies", "in", "real", "time.", "That", "is", "why", "realtime", "matters.", "Done."]
    assert align_to_sentences(split, text) == [0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 2]


def test_align_split_word_that_recurs_later_in_the_chunk():
    # regression: word-level alignment gave the split pieces of 'real-time' the later sentence's 'real time'
    text = "It runs in real-time. Then real time ends."
    assert align_to_sentences(["It", "runs", "in", "real", "time.", "Then", "realtime", "ends."], text) == \
        [0, 0, 0, 0, 0, 1, 1, 1]


@pytest.mark.parametrize("words,expected", [
    (["um", "Here", "is"], [0, 0, 0]),  # extra word before anything matches
    (["works", "uh", "uh", "Every"], [0, 0, 0, 1]),  # extra words in the middle stay with the left neighbour
    (["resume", "extra", "words"], [2, 2, 2]),  # extra words at the end
    (["Here", "Then", "we"], [0, 2, 2]),  # a whole sentence missing
    (["timestamp"], [1]),
    (["zzz"], [0]),
])
def test_align_extra_and_missing_words(words, expected):
    assert align_to_sentences(words, TEXT) == expected


@pytest.mark.parametrize("words", [["zzz", "yyy", "xxx"], ["", "", ""], ["—", "um", "…"], ["qqq"] * 20])
def test_align_garbage_stays_monotonic_and_in_range(words):
    # nothing (or almost nothing) matches: any non-decreasing answer within the sentences is acceptable
    result = align_to_sentences(words, TEXT)
    assert len(result) == len(words) and result == sorted(result) and all(0 <= s <= 2 for s in result)


def test_align_repeated_sentences():
    assert align_to_sentences(["Here", "is", "how", "Here", "is", "how"], "Here is how. Here is how.") == \
        [0, 0, 0, 1, 1, 1]


def test_align_empty_inputs():
    assert align_to_sentences([], TEXT) == []
    assert align_to_sentences(["a", "b"], "") == [0, 0]
    assert align_to_sentences(["a", "b"], "<|prosody:pause|>") == [0, 0]


def test_align_is_monotonic_and_in_range_under_random_damage():
    rng = random.Random(1234)
    ref = [word for word, _ in sentence_words(TEXT)]
    vocabulary = ref + ["um", "uh", "realtime", "", "—", "x"]
    for _ in range(300):
        words = list(ref)
        for _ in range(rng.randint(0, 6)):
            op = rng.choice(["drop", "dup", "insert", "merge", "split"])
            k = rng.randrange(len(words)) if words else 0
            if op == "drop" and words:
                words.pop(k)
            elif op == "dup" and words:
                words.insert(k, words[k])
            elif op == "insert":
                words.insert(k, rng.choice(vocabulary))
            elif op == "merge" and len(words) > k + 1:
                words[k:k + 2] = [words[k] + words[k + 1]]
            elif op == "split" and words and len(words[k]) > 2:
                words[k:k + 1] = [words[k][:2], words[k][2:]]
        result = align_to_sentences(words, TEXT)
        assert len(result) == len(words)
        assert all(0 <= s <= 2 for s in result)
        assert result == sorted(result), (words, result)


# --- digits, acronyms, counts ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Room 101.", True), ("Four score.", False), ("<|x:2|> hi", False), ("<|emotion:awe|>Agent 7.", True),
    ("Room ٣.", True), ("", False),
])
def test_has_digits(text, expected):
    assert has_digits(text) is expected


@pytest.mark.parametrize("text,expected", [
    ("We use AI and TTS with APIs, OK? I am. AI.", ["AI", "APIs", "OK", "TTS"]),
    ("NASA's API and USB-C, HTTPS!", ["API", "HTTPS", "NASA", "USB"]),
    ("I A a", []),
    ("eBPF and iOS and McDonald", []),
    ("<|emotion:enthusiasm|>Hi.", []),
    ("<|EMOTION:AWE|>Hi.", []),  # tag text is never an acronym
])
def test_acronyms(text, expected):
    assert acronyms(text) == expected


@pytest.mark.parametrize("text,count", [
    ("Hello — world ... <|prosody:pause|> again!", 3), ("", 0), ("<|emotion:awe|>", 0), ("One. Two three.", 3),
    ("real-time", 1),
])
def test_word_count(text, count):
    assert word_count(text) == count


# --- false sentence ends (the cut/resume logic splits at every . ! ? … before a space) -------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Ask Dr. Relay.", ["Dr."]),
    ("e.g. this, i.e., that, in the U.S. at two a.m. or p.m.", ["e.g.", "i.e.", "U.S.", "a.m.", "p.m."]),
    ("Mr. and Mrs. Jones vs. Ms. Smith, etc.", ["Mr.", "Mrs.", "vs.", "Ms.", "etc."]),
    ("(e.g. this) “Dr.” said so.", ["e.g.", "Dr."]),
    ("VS. ETC. Prof. St. Jr. approx. A.I.", ["VS.", "ETC.", "Prof.", "St.", "Jr.", "approx.", "A.I."]),
    ("e.g e.g. and e.g. again", ["e.g", "e.g."]),  # once each, in order
    ("Plan A. Then plan B. No. I. OK. Dr", []),  # single letters, real words, no period
    ("Use the .env file on boson.ai or x.com.", []),
    ("<|emotion:awe|>Wow. <|prosody:pause|> Big.", []),
    ("", []),
])
def test_abbreviations(text, expected):
    assert abbreviations(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("So clear… then it came out.", [("clear…", "then")]),
    ("Wait... what? Yes. no!", [("Wait...", "what?"), ("Yes.", "no!")]),
    ('He said "stop." then left.', [('"stop."', "then")]),
    ("(Like this.) “and this.”", [("this.)", "“and")]),
    ("<|emotion:awe|>Wow. <|prosody:pause|> big.", [("Wow.", "big.")]),
    ("Ask Dr. Relay. Two a.m. comes.", []),  # breaks after an abbreviation are `abbreviations`' to report
    ("One. Two! Three? Four…", []),
    ("Meet the team. iOS support is next. — Bye.", []),
    ("No sentence end", []),
    ("", []),
])
def test_mid_sentence_breaks(text, expected):
    assert mid_sentence_breaks(text) == expected

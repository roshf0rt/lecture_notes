from notes_bot.topics import suggest


def test_subjects_whose_words_occur_in_the_note_come_first():
    ordered, matched = suggest(["Химия", "Физика", "Маркетинг"], "Законы физике: сила и масса.")
    assert ordered == ["Физика", "Химия", "Маркетинг"] and matched == 1


def test_word_forms_match_by_their_first_letters():
    assert suggest(["Маркетинг"], "Событийный маркетинг")[1] == 1
    assert suggest(["Маркетинг"], "маркетингу посвящён урок")[1] == 1


def test_nothing_matches_and_the_order_is_kept():
    assert suggest(["Физика", "Химия"], "Про бренды и стиль") == (["Физика", "Химия"], 0)


def test_short_subjects_are_not_guessed_from_random_letters():
    assert suggest(["ИИ"], "ИИ в медицине")[1] == 0

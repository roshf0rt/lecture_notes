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


SUBJECTS = ["Основы стилистики и литературного редактирования", "Рекламные тексты", "Репутационный менеджмент",
            "Интегрированные маркетинговые коммуникации"]
REPUTATION_NOTE = """Сущность репутации и целевые аудитории
Главная идея: репутация складывается из мнений аудиторий и влияет на бизнес.

1. Основные понятия
Репутация — общественное мнение. Положительная репутация помогает в кризис.
Реклама не заменяет репутацию: доверие строится годами.
2. Мониторинг
Мониторинг репутации в сети, работа с отзывами и поисковой выдачей."""


def test_only_the_subject_the_note_is_about_is_ticked_not_every_passing_word():
    ordered, matched = suggest(SUBJECTS, REPUTATION_NOTE)
    assert ordered[0] == "Репутационный менеджмент" and matched == 1


def test_generic_words_like_osnovy_do_not_match():
    assert suggest(["Основы стилистики"], "Основные понятия\nОсновные идеи курса, основы и ещё раз основное.")[1] == 0


def test_no_clear_leader_means_no_tick():
    note = "Реклама и репутация\nРеклама влияет на репутацию, репутация на рекламу."
    assert suggest(["Рекламные тексты", "Репутационный менеджмент"], note)[1] == 0


def test_the_same_subject_in_two_groups_is_ticked_in_both():
    assert suggest(["Физика", "Химия", "Физика"], "Физика движения\nФизика изучает силы.")[1] == 2

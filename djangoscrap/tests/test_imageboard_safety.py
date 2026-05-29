from djangoscrap.imageboard_ingestion import safety


def test_clean_text_has_no_high_risk_labels():
    labels = safety.classify("Did SL5x5 today, hit a 225 squat PR. Felt good.")
    assert not safety.is_high_risk(labels)
    assert labels["contains_external_link"] is False


def test_url_detection():
    labels = safety.classify("source: https://example.com/labs")
    assert labels["contains_external_link"] is True


def test_steroid_sourcing_flagged():
    labels = safety.classify("anyone got a source for tren in EU")
    assert labels["likely_steroid_sourcing"] is True
    assert safety.is_high_risk(labels)


def test_eating_disorder_flagged():
    labels = safety.classify("my goal is sub-1000 kcal until summer, thinspo only")
    assert labels["likely_eating_disorder"] is True


def test_doxxing_flagged():
    labels = safety.classify("his real name is John Smith, his address is 123 Main Street")
    assert labels["likely_doxxing"] is True


def test_make_model_safe_blanks_high_risk():
    risky = "where to buy gear cheap"
    labels = safety.classify(risky)
    assert safety.make_model_safe(risky, labels) == ""


def test_make_model_safe_strips_urls_otherwise():
    text = "I lift at https://gym.example every morning"
    labels = safety.classify(text)
    out = safety.make_model_safe(text, labels)
    assert "http" not in out
    assert "I lift" in out
